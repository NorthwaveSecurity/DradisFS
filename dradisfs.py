#!/usr/bin/env python
from __future__ import print_function, absolute_import, division

import logging

from errno import ENOENT, EPERM
from stat import S_IFDIR, S_IFREG
from time import time
from functools import cache
from templates import default_issue, default_evidence, default_content_block
import re
import os

from fuse import FUSE, FuseOSError, Operations, LoggingMixIn
from dradis import Dradis

import configparser

dir = os.path.dirname(os.path.realpath(__file__))
config = configparser.ConfigParser()
config.read(os.path.join(dir, 'config.ini'))

api_token = config['DEFAULT']['api_token']
url = config['DEFAULT']['url']

DEFAULT_MODE = 0o644


def create_filename(label):
    """Replace invalid characters in a filename

    :returns: A valid filename
    """

    return re.sub(r'[^\w\-_\. ]', '_', label)


class DradisCached(Dradis):
    '''A cache around the Dradis API to prevent having to query all documents every time'''

    @cache
    def get_all_projects(self):
        return super().get_all_projects()


class DradisFS(LoggingMixIn, Operations):
    '''Interaction with dradis api via filesystem'''

    def __init__(self, api_token, url, project_id=None):
        self.api = DradisCached(api_token, url)
        self.files = {}
        self.data = {}
        self.projects = {}
        self.fd = 0

        if project_id:
            # If a project id is provided, mount that project as the root
            project = self.api.get_project(project_id)
            self.create_project(project, '/')
        else:
            # Otherwise, mount the directory containing all projects
            self.files['/'] = {
                'stats': self.get_stats(),
                'type': 'root',
            }
            self.update_projects()

    def get_stats(self, dir=True, mode=DEFAULT_MODE):
        """Wrapper to assign base properties to a new file or directory

        :param dir: If True return stats for directory instead of file
        :param mode: Unix permissions to assign
        """

        now = time()
        if dir:
            return dict(st_mode=(S_IFDIR | mode), st_ctime=now,
                        st_mtime=now, st_atime=now, st_nlink=2)
        else:
            return dict(st_mode=(S_IFREG | mode), st_nlink=1,
                        st_size=0, st_ctime=time(), st_mtime=time(),
                        st_atime=time())

    def update_contents(self, path, contents):
        """Update the contents of a file while keeping all statistics in sync"""

        # Update contents
        self.data[path] = contents
        # Update file size
        self.files[path]['stats']['st_size'] = len(contents)
        # Update times
        self.utimens(path)

    def create(self, path, mode):
        """create new evidence, issue, content block or node

        :param path: Path to the issue
        :param mode: Unix permissions to assign
        """

        # Split path into directory and filename
        index = path.rfind("/")
        dir = path[:index]
        if dir == '':
            dir = '/'
        filename = path[index+1:]
        f = self.files[dir]

        stats = self.get_stats(path, mode=mode)
        if f['type'] == 'node':
            contents = default_evidence
            evidence = self.api.create_evidence(f['project_id'], f['id'], f['issue_id'], contents)
            self.get_evidence(dir)
        if f['type'] == 'project':
            contents = default_issue
            issue = self.api.create_issue(f['id'], contents)
            self.get_issues(dir)
        if f['type'] == 'content_blocks':
            contents = default_content_block
            content_block = self.api.create_contentblock(f['project_id'], contents)
            self.get_content_blocks(dir)
        if f['type'] == 'issue':
            label = filename
            self.api.create_node(f['project_id'], label, type_id=1)
            self.get_nodes(dir)

    def mkdir(self, path, mode):
        """Currently not used, create a file instead"""
        pass

    def open(self, path, flags):
        """open fd"""
        self.get_content(path)
        self.fd += 1
        return self.fd

    def get_content(self, path):
        """Get contents of evidence, issue or content block from Dradis and store it locally"""

        f = self.files[path]
        if f['type'] == 'evidence':
            evidence = self.api.get_evidence(f['project_id'], f['node_id'], f['id'])
            contents = evidence['content']
        elif f['type'] == 'issue_content':
            issue = self.api.get_issue(f['project_id'], f['id'])
            contents = issue['text']
        elif f['type'] == 'content_block':
            content_block = self.api.get_contentblock(f['project_id'], f['id'])
            contents = content_block['content']
        else:
            return FuseOSError("Failed to open file")
        contents = self.encode_contents(contents)
        self.update_contents(path, contents)

    def read(self, path, size, offset, fh):
        return self.data[path][offset:offset + size]

    def getxattr(self, path, name, position=0):
        attrs = self.files[path].get('attrs', {})

        try:
            return attrs[name]
        except KeyError:
            return ''

    def getattr(self, path, fh=None):
        if path not in self.files:
            raise FuseOSError(ENOENT)
        return self.files[path]['stats']

    def create_project(self, project, path=None):
        """Create a new project"""

        filename = create_filename('{}_{}'.format(project['id'], project['name']))
        project['filename'] = filename
        if not path:
            path = '/' + filename
        # Create project
        self.projects[project['id']] = project
        self.files[path] = {
            'type': 'project',
            'stats': self.get_stats(),
            'id': project['id'],
        }
        # Add path for content blocks of the project
        content_blocks_path = os.path.join(path, 'content_blocks')
        self.files[content_blocks_path] = {
            'type': 'content_blocks',
            'stats': self.get_stats(),
            'project_id': project['id'],
        }

    def update_projects(self):
        """Get the latest version of all projects"""
        for p in self.api.get_all_projects():
            self.create_project(p)

    def get_issues(self, project_path):
        """Get all issues for a project

        :returns: List of issue filenames
        """

        project_id = self.files[project_path]['id']
        result = []
        for i in self.api.get_all_issues(project_id):
            # Create the issues
            filename = create_filename("{}_{}".format(i['id'], i['title']))
            path = os.path.join(project_path, filename)
            self.files[path] = {
                'type': 'issue',
                'stats': self.get_stats(),
                'id': i['id'],
                'project_id': project_id,
            }
            # Add the /issue file containing the contents
            issue_content_path = path + "/issue"
            self.files[issue_content_path] = {
                'type': 'issue_content',
                'stats': self.get_stats(dir=False),
                'id': i['id'],
                'project_id': project_id,
            }
            contents = self.encode_contents(i['text'])
            self.update_contents(issue_content_path, contents)
            result.append(filename)
        return result

    def get_nodes(self, issue_path):
        """Get all nodes for an issue

        :returns: List of node filenames
        """

        f = self.files[issue_path]
        result = []
        for node in self.api.get_all_nodes(f['project_id']):
            if not (node['parent_id'] is None and node['type_id'] == 1):
                # Filter nodes that are not usually used
                continue
            node_filename = create_filename(node['label'])
            node_path = os.path.join(issue_path, node_filename)
            self.files[node_path] = {
                'type': 'node',
                'stats': self.get_stats(),
                'id': node['id'],
                'project_id': f['project_id'],
                'issue_id': f['id'],
            }
            result.append(node_filename)
        return result

    def get_content_blocks(self, path):
        """Get all content blocks

        :returns: List of content block filenames
        """

        f = self.files[path]
        result = []
        for block in self.api.get_all_contentblocks(f['project_id']):
            block_filename = create_filename("{}_{}".format(block['id'], block['title']))
            block_path = os.path.join(path, block_filename)
            content = self.encode_contents(block['content'])
            stats = self.get_stats(dir=False)
            stats['st_size'] = len(content)
            self.files[block_path] = {
                'type': 'content_block',
                'stats': stats,
                'id': block['id'],
                'project_id': f['project_id'],
            }
            self.data[block_path] = content
            result.append(block_filename)
        return result

    def encode_contents(self, contents):
        return contents.encode('utf-8')

    def decode_contents(self, contents):
        return contents.decode('utf-8')

    def add_evidence_to_files(self, path, evidence, node_file):
        """Add the given evidence object to the files dictionary

        :param path: Path of the evidence
        :param evidence: The evidence object from Dradis
        :param node_file: The file dictionary containing the node information
        """

        stats = self.get_stats(dir=False)
        contents = self.encode_contents(evidence['content'])
        stats['st_size'] = len(contents)
        self.files[path] = {
            'type': 'evidence',
            'stats': stats,
            'node_id': node_file['id'],
            'issue_id': node_file['issue_id'],
            'project_id': node_file['project_id'],
            'id': evidence['id'],
        }
        self.data[path] = contents

    def get_evidence(self, node_path):
        """Get all evidence for a given node

        :returns: List of evidence filenames
        """

        f = self.files[node_path]
        result = []
        # Start indexing the evidences
        i = 1
        # Sort the evidences by id to keep a fixed order
        for e in sorted(self.api.get_all_evidence(f['project_id'], f['id']), key=lambda x: x['id']):
            if e['issue']['id'] != f['issue_id']:
                # Skip evidences that do not belong to the issue of the given node_path
                continue
            filename = str(i)
            i += 1
            path = os.path.join(node_path, filename)
            self.add_evidence_to_files(path, e, f)
            result.append(filename)
        return result

    def readdir(self, path, fh=None):
        """Read contents of a directory

        :returns: List of filenames
        """

        if path not in self.files:
            return FuseOSError(ENOENT)
        f = self.files[path]
        type = f['type']
        if type == 'root':
            self.update_projects()
            return ['.', '..'] + [p['filename'] for p in self.projects.values()]
        if type == 'project':
            return ['.', '..', 'content_blocks'] + self.get_issues(path)
        if type == 'issue':
            return ['.', '..', 'issue'] + self.get_nodes(path)
        if type == 'node':
            return ['.', '..'] + self.get_evidence(path)
        if type == 'content_blocks':
            return ['.', '..'] + self.get_content_blocks(path)
        return ['.', '..']

    def rename(self, old, new):
        """Rename issue or evidence, this is executed when the unix `mv` command is executed"""

        if new not in self.files:
            # File should be created
            self.create(new, DEFAULT_MODE)
        # Refresh the content of the source
        self.get_content(old)
        # Copy the contents to the destination
        self.data[new] = self.data[old]
        # Sync to Dradis
        self.update(new)
        # Delete the source
        self.delete(old)

    def delete(self, path):
        """Delete a file or directory"""

        f = self.files[path]
        if f['type'] == 'evidence':
            self.api.delete_evidence(f['project_id'], f['node_id'], f['id'])
        elif f['type'] == 'issue' or f['type'] == 'issue_content':
            self.api.delete_issue(f['project_id'], f['id'])
        elif f['type'] == 'content_block':
            self.api.delete_contentblock(f['project_id'], f['id'])
        elif f['type'] == 'node':
            self.api.delete_node(f['project_id'], f['id'])
        else:
            raise FuseOSError(EPERM)

        # Remove from local filesystem
        del self.files[path]
        del self.data[path]

    def rmdir(self, path):
        """Remove issue or node"""
        self.delete(path)

    def unlink(self, path):
        """Remove evidence"""
        self.delete(path)

    def releasedir(self, path):
        self.rmdir(path)

    def truncate(self, path, length, fh):
        """Truncate a file"""

        self.data[path] = self.data[path][:length]
        self.files[path]['stats']['st_size'] = length
        self.utimens(path)

    def update(self, path):
        """Sync contents of the given path to Dradis"""

        contents = self.decode_contents(self.data[path])
        f = self.files[path]
        if f['type'] == 'evidence':
            self.api.update_evidence(f['project_id'], f['node_id'], f['issue_id'], f['id'], contents)
        if f['type'] == 'issue_content':
            self.api.update_issue(f['project_id'], f['id'], contents)
        if f['type'] == 'content_block':
            self.api.update_contentblock(f['project_id'], f['id'], contents)
        self.utimens(path)

    def write(self, path, data, offset, fh):
        """Update contents of file"""

        self.data[path] = self.data[path][:offset] + data
        self.files[path]['stats']['st_size'] = len(self.data[path])
        self.update(path)
        return len(data)

    def utimens(self, path, times=None):
        """Update access and modification times

        :param path: Path to update
        :param times: A tuple of the access time and modification time (atime, mtime)
        """

        now = time()
        atime, mtime = times if times else (now, now)
        self.files[path]['stats']['st_atime'] = atime
        self.files[path]['stats']['st_mtime'] = mtime

    def chmod(self, path, mode):
        pass


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("mountpoint")
    parser.add_argument("-p", "--project", help="Mount only this dradis project")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG)
    dradisfs = DradisFS(api_token, url, project_id=args.project)
    fuse = FUSE(dradisfs, args.mountpoint, foreground=True, allow_other=True)

if __name__ == '__main__':
    main()
