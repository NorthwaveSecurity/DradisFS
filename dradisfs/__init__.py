#!/usr/bin/env python
from __future__ import print_function, absolute_import, division

import logging
import traceback

from errno import ENOENT, EPERM
from stat import S_IFDIR, S_IFREG
from functools import cache
from cachetools import cached, TTLCache
from dradisfs.templates import default_issue, default_evidence, default_content_block
import re
import os
from io import BytesIO
from time import time, sleep
import threading

from fuse import FUSE, FuseOSError, Operations, LoggingMixIn, ENOTSUP
from dradis import Dradis
from pathlib import Path

import configparser
from dataclasses import dataclass

mydir = Path(__file__).parent
config = configparser.ConfigParser()
config.read(mydir.parent / 'config.ini')
config.read(os.path.expanduser('~/.config/dradisfs.ini'))

api_token = config['DEFAULT']['api_token']
url = config['DEFAULT']['url']

DEFAULT_MODE = 0o644

ISSUES_DIRNAME = 'issues'
NODES_DIRNAME = 'nodes'
ATTACHMENTS_DIRNAME = 'attachments'
CONTENT_BLOCKS_DIRNAME = 'content_blocks'


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

    @cached(TTLCache(maxsize=1024, ttl=10))
    def get_all_nodes(self, project_id):
        return super().get_all_nodes(project_id)


class FsObject:
    stats: dict = None
    project_id: int = None
    node_id: int = None
    issue_id: int = None
    evidence_id: int = None
    id: int = None
    api: DradisCached = None
    type: str = None
    parent = None

    def __init__(self, **kwargs):
        for k,v in kwargs.items():
            setattr(self, k, v)
        self.stats = self.get_stats()

    def get_stats(self, mode=DEFAULT_MODE):
        """Wrapper to assign base properties to a new file or directory

        :param dir: If True return stats for directory instead of file
        :param mode: Unix permissions to assign
        """
        raise NotImplementedError

    def delete(self):
        raise NotImplementedError

    @property
    def inode(self):
        return self.type + f"_{self.id}"


class File(FsObject):
    _contents: BytesIO = None
    dirty: bool = False

    @property
    def contents(self):
        self.pull()
        return self._contents.getvalue()

    @contents.setter
    def contents(self, contents):
        if not isinstance(contents, bytes):
            contents = contents.encode('utf-8')
        # Update contents
        self._contents = BytesIO(contents)
        # Update file size
        self.stats['st_size'] = len(contents)
        # Update times
        self.utimens()

    def read(self, size, offset):
        self.pull()
        self._contents.seek(offset)
        return self._contents.read(size)

    def write(self, data, offset):
        self.dirty = True
        self._contents.seek(offset)
        self._contents.write(data)
        self.stats['st_size'] = len(self.contents)

    def _pull(self):
        raise NotImplementedError
        
    def pull(self):
        """ Pull data from API to local """
        if self.dirty:
            return
        return self._pull()

    def _push(self):
        raise NotImplementedError

    def push(self):
        """ Push data from local to API """
        self._push()
        self.dirty = False

    def get_stats(self, mode=DEFAULT_MODE):
        now = time()
        return dict(st_mode=(S_IFREG | mode), st_nlink=1,
                    st_size=0, st_ctime=now, st_mtime=now,
                    st_atime=now)

    def utimens(self, times=None):
        now = time()
        atime, mtime = times if times else (now, now)
        self.stats['st_atime'] = atime
        self.stats['st_mtime'] = mtime

    def truncate(self, length):
        self.contents = self.read(length, 0)


class Evidence(File):
    type = "evidence"

    def _pull(self):
        evidence = self.api.get_evidence(self.project_id, self.node_id, self.id)
        self.contents = evidence['content']

    def _push(self):
        self.api.update_evidence(self.project_id, self.node_id, self.issue_id, self.id, self.contents.decode('utf-8'))

    def delete(self):
        self.api.delete_evidence(self.project_id, self.node_id, self.id)


class IssueMixin:
    def delete(self):
        self.api.delete_issue(self.project_id, self.id)


class IssueContent(IssueMixin, File):
    type = "issue_content"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.issue_id = self.id

    def _pull(self):
        issue = self.api.get_issue(self.project_id, self.id)
        self.contents = issue['text']

    def _push(self):
        self.api.update_issue(self.project_id, self.id, self.contents.decode('utf-8'))

class ContentBlock(File):
    type = "content_block"

    def _pull(self):
        content_block = self.api.get_contentblock(self.project_id, self.id)
        self.contents = content_block['content']

    def _push(self):
        self.api.update_contentblock(self.project_id, self.id, self.contents.decode('utf-8'))

    def delete(self):
        self.api.delete_contentblock(self.project_id, self.id)


class Attachment(File):
    type = "attachment"
    filename = None

    def _pull(self):
        self.contents = self.api.download_attachment(self.project_id, self.node_id, self.filename)

    def _push(self):
        self.api.delete_attachment(self.project_id, self.node_id, self.filename)
        self.api.create_attachment_bytes(self.project_id, self.node_id, (self.filename, self.contents))
        # Wait until dradis has processed the new attachment
        res = None
        while not res:
            res = self.api.download_attachment(self.project_id, self.node_id, self.filename)

    @property
    def reference_string(self):
        return f"/pro/projects/{self.project_id}/nodes/{self.node_id}/attachments/{self.filename}"

    @property
    def inode(self):
        return self.type + f"_{self.node_id}" + "_" + self.filename


class Directory(FsObject):
    children = None
    # Keep track of which children still existed since the last refresh
    refreshed_children = set()
    # Do not delete these types if the server does not return them anymore (mostly because they are never returned by the server in the first place)
    exclude_from_deletion = set()

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.children = {}

    def ls(self):
        return ['.', '..'] + list(self.children.keys())

    def start_refresh(self):
        self.refreshed_children = set()
        for inode in self.children.values():        
            for exclusion in self.exclude_from_deletion:
                if inode.startswith(exclusion):
                    # Pretend it already is refreshed
                    self.refreshed_children.add(inode)

    def end_refresh(self):
        inodes_to_delete = set(self.children.values()) - self.refreshed_children
        children_to_delete = []
        for k,v in self.children.items():
            if v in inodes_to_delete:
                children_to_delete.append(k)
        for c in children_to_delete:
            del self.children[c]
        self.refreshed_children = None
        return inodes_to_delete

    def add_child(self, filename, child):
        if self.refreshed_children is not None:
            self.refreshed_children.add(child.inode)
        if child.inode in self.children.values():
            # This inode already exists in the directory, no need to make a new file
            return
        child.parent = self
        self.children[filename] = child.inode

    def get_filename_by_inode(self, inode):
        return [filename for filename, i in self.children.items() if i == inode][0]

    def delete_child(self, inode):
        filename = self.get_filename_by_inode(inode)
        del self.children[filename]

    def rename_child(self, inode, new_filename):
        self.delete_child(inode)
        self.children[new_filename] = inode

    def get_stats(self, mode=DEFAULT_MODE):
        now = time()
        return dict(st_mode=(S_IFDIR | mode), st_ctime=now,
                    st_mtime=now, st_atime=now, st_nlink=2)


class Root(Directory):
    type = "root"

class ContentBlocks(Directory):
    type = "content_blocks"

class Issues(Directory):
    type = "issues"

class Nodes(Directory):
    type = "nodes"

class Project(Directory):
    type = "project"
    exclude_from_deletion = {"nodes", "issues", "content_blocks"}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.project_id = self.id


class Issue(IssueMixin, Directory):
    type = "issue"
    exclude_from_deletion = {"issue_content"}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.issue_id = self.id


class Attachments(Directory):
    type = "attachments"

    @property
    def inode(self):
        return self.type + f"_{self.issue_id}_{self.node_id}"


class Node(Directory):
    type = "node"
    exclude_from_deletion = {"attachments"}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.node_id = self.id

    def delete(self):
        self.api.delete_node(self.project_id, self.id)

    @property
    def inode(self):
        return super().inode + f"_{self.issue_id}"


class DradisFS(LoggingMixIn, Operations):
    '''Interaction with dradis api via filesystem'''

    def __init__(self, api_token, url, project_id=None, only_nodes_with_evidence=False, allow_delete=False):
        self.api = DradisCached(api_token, url)
        self.root = None
        self.inodes = {}
        self.fd = 0
        self.task_queue = []
        self.only_nodes_with_evidence = only_nodes_with_evidence
        self.allow_delete = allow_delete

        if project_id:
            # If a project id is provided, mount that project as the root
            project = self.api.get_project(project_id)
            self.root = self.create_project(project)
        else:
            # Otherwise, mount the directory containing all projects
            self.root = Root()
            self.update_projects()

    def add_fsobject(self, parent, filename, fsobject):
        if parent is not None:
            parent.add_child(filename, fsobject)
            fsobject.parent = parent
        if fsobject.inode in self.inodes:
            old_fsobject = self.inodes[fsobject.inode]
            if isinstance(old_fsobject, File) and old_fsobject.dirty:
                # File is dirty, do not overwrite
                return
            if isinstance(old_fsobject, Directory):
                # Do not overwrite directories
                return
        self.inodes[fsobject.inode] = fsobject

    def get_fsobject(self, path):
        fsobject = self.root
        if fsobject is None:
            raise FuseOSError(ENOENT)
        if path == '/':
            return fsobject
        inode = None
        split = path.split('/')[1:]
        for segment in split:
            try:
                inode = fsobject.children[segment]
            except KeyError:
                raise FuseOSError(ENOENT)
            fsobject = self.inodes[inode]
        return fsobject

    def delete_inodes(self, inodes_to_delete):
        for inode in inodes_to_delete:
            del self.inodes[inode]

    def add_task(self, task):
        if task in self.task_queue:
            return
        self.task_queue.append(task)

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
        f = self.get_fsobject(dir)

        if isinstance(f, Node):
            contents = default_evidence
            evidence = self.api.create_evidence(f.project_id, f.id, f.issue_id, contents)
            fsobject = Evidence(
                id = evidence['id'],
                project_id = f.project_id,
                issue_id = f.issue_id,
                node_id = f.node_id,
                api=self.api
            )
            fsobject.contents = contents
        elif isinstance(f, ContentBlocks):
            contents = default_content_block
            content_block = self.api.create_contentblock(f.project_id, contents)
            fsobject = ContentBlock(
                id=content_block['id'],
                project_id = f.project_id,
                api=self.api
            )
            fsobject.contents = contents
        elif isinstance(f, Attachments):
            fsobject = Attachment(
                filename=filename,
                project_id = f.project_id,
                node_id=f.node_id,
                api=self.api,
                dirty=True
            )
            fsobject.contents = b""
        else:
            raise FuseOSError(ENOTSUP)

        self.add_fsobject(f, filename, fsobject)

        fd = self.fd
        self.fd += 1
        return fd

    def mkdir(self, path, mode):
        """Currently not used, create a file instead"""
        index = path.rfind("/")
        parent_dir = path[:index]
        filename = path[index+1:]
        if parent_dir == '':
            parent_dir = '/'
        f = self.get_fsobject(parent_dir)

        if isinstance(f, Issues):
            contents = default_issue
            issue = self.api.create_issue(f.project_id, contents)
            fsobject = Issue(
                id=issue['id'],
                project_id = f.project_id,
                api=self.api
            )
            self.add_fsobject(f, filename, fsobject)
            issue_contents = IssueContent(
                id=issue['id'],
                project_id = f.project_id,
                api=self.api
            )
            issue_contents.contents = contents
            self.add_fsobject(fsobject, 'issue', issue_contents)
        elif isinstance(f, Issue):
            label = filename
            node = self.api.create_node(f.project_id, label, type_id=1)
            self.add_node_to_files(f, filename, node)
        else:
            raise FuseOSError(ENOTSUP)


    def open(self, path, flags):
        """open fd"""
        self.fd += 1
        return self.fd

    def get_content(self, path):
        """Get contents of evidence, issue or content block from Dradis and store it locally"""

        f = self.get_fsobject(path)
        f.pull()

    def read(self, path, size, offset, fh):
        return self.get_fsobject(path).read(size, offset)

    def getxattr(self, path, name, position=0):
        f = self.get_fsobject(path)
        try:
            return str(getattr(f, name)).encode()
        except AttributeError:
            raise FuseOSError(ENOENT)

    def listxattr(self, path):
        f = self.get_fsobject(path)
        blacklist = ['api', 'contents', 'parent', 'stats', 'children', 'exclude_from_deletion']
        res = []
        for attr in dir(f):
            if attr.startswith("_"):
                # Skip private attributes
                continue
            if attr in blacklist:
                # skip blacklisted
                continue
            value = getattr(f, attr)
            if callable(value):
                # Skip functions
                continue
            if value is None:
                # Skip none values
                continue
            res.append(attr)
        return res

    def getattr(self, path, fh=None):
        return self.get_fsobject(path).stats

    def create_project(self, project, parent=None):
        """Create a new project"""
        filename = create_filename('{}_{}'.format(project['id'], project['name']))
        project_dir = Project(
            id= project['id']
        )
        # Create project
        self.add_fsobject(parent, filename, project_dir)
        content_blocks = ContentBlocks(
            project_id= project['id'],
            api=self.api
        )
        self.add_fsobject(project_dir, CONTENT_BLOCKS_DIRNAME, content_blocks)
        issues = Issues(
            project_id= project['id'],
            api=self.api
        )
        self.add_fsobject(project_dir, ISSUES_DIRNAME, issues)
        nodes = Nodes(
            project_id= project['id'],
            api=self.api
        )
        self.add_fsobject(project_dir, NODES_DIRNAME, nodes)

        self.add_fsobject(parent, filename, project_dir)
        return project_dir

    def update_projects(self):
        """Get the latest version of all projects"""
        for p in self.api.get_all_projects():
            self.create_project(p, parent=self.root)

    def get_issues(self, parent):
        """Get all issues for a project

        :returns: List of issue filenames
        """

        result = []
        for i in self.api.get_all_issues(parent.project_id):
            # Create the issues
            filename = create_filename("{}_{}".format(i['title'], i['id']))
            issue =  Issue(
                id= i['id'],
                project_id= parent.project_id,
                api=self.api
            )
            self.add_fsobject(parent, filename, issue)
            # Add the /issue file containing the contents
            issue_content = IssueContent(
                id= i['id'],
                project_id= parent.project_id,
                api=self.api
            )
            issue_content.contents = i['text']
            self.add_fsobject(issue, 'issue', issue_content)
            result.append(filename)
        return result

    def add_node_to_files(self, parent, filename, node):
        fsobject = Node(
            id= node['id'],
            project_id= parent.project_id,
            issue_id= parent.issue_id,
            api=self.api
        )
        self.add_fsobject(parent, filename, fsobject)
        attachments = Attachments(
            node_id= node['id'],
            project_id= parent.project_id,
            issue_id= parent.issue_id,
            api=self.api
        )
        self.add_fsobject(fsobject, ATTACHMENTS_DIRNAME, attachments)

    def get_all_nodes(self, parent):
        result = []
        for node in self.api.get_all_nodes(parent.project_id):
            node_filename = create_filename(node['label'])
            self.add_node_to_files(parent, node_filename, node)
            result.append(node_filename)
        return result

    def get_nodes(self, parent):
        """Get all nodes for an issue

        :returns: List of node filenames
        """

        result = []
        for node in self.api.get_all_nodes(parent.project_id):
            if not node['type_id'] == 1:
                # Filter nodes that are not usually used
                continue
            evidence = list(self.get_evidence_for_issue(parent.project_id, node['id'], parent.id))
            node_filename = create_filename(node['label'])
            if evidence:
                node_filename += " *"
            elif self.only_nodes_with_evidence:
                # Skip nodes without evidence
                continue
            self.add_node_to_files(parent, node_filename, node)
            result.append(node_filename)
        return result

    def get_content_blocks(self, parent):
        """Get all content blocks

        :returns: List of content block filenames
        """

        result = []
        for block in self.api.get_all_contentblocks(parent.project_id):
            block_filename = create_filename("{}_{}".format(block['id'], block['title']))
            content_block = ContentBlock(
                id= block['id'],
                project_id= parent.project_id,
                api=self.api
            )
            content_block.contents = block['content']
            self.add_fsobject(parent, block_filename, content_block)
            result.append(block_filename)
        return result

    def add_evidence_to_files(self, parent, filename, evidence):
        """Add the given evidence object to the files dictionary

        :param path: Path of the evidence
        :param evidence: The evidence object from Dradis
        :param node_file: The file dictionary containing the node information
        """

        contents = evidence['content']
        evidence = Evidence(
            node_id= parent.id,
            issue_id= parent.issue_id,
            project_id= parent.project_id,
            id= evidence['id'],
            api=self.api
        )
        evidence.contents = contents
        self.add_fsobject(parent, filename, evidence)

    def get_evidence_for_node(self, project_id, node_id):
        yield from sorted(self.api.get_all_evidence(project_id, node_id), key=lambda x: x['id'])

    def get_evidence_for_issue(self, project_id, node_id, issue_id):
        for e in self.get_evidence_for_node(project_id, node_id):
            if e['issue']['id'] != issue_id:
                # Skip evidences that do not belong to the issue of the given node_path
                continue
            yield e

    def get_evidence(self, parent):
        """Get all evidence for a given node

        :returns: List of evidence filenames
        """

        result = []
        # Start indexing the evidences
        i = 1
        if parent.issue_id is not None:
            evidences = self.get_evidence_for_issue(parent.project_id, parent.id, parent.issue_id)
        else:
            evidences = self.get_evidence_for_node(parent.project_id, parent.id)
        for e in evidences:
            filename = str(i)
            i += 1
            self.add_evidence_to_files(parent, filename, e)
            result.append(filename)
        return result

    def get_attachments(self, parent):
        result = []
        for attachment in self.api.get_all_attachments(parent.project_id, parent.node_id):
            filename = attachment['filename']
            # TODO filter by issue if 'issue_id' is set in f
            result.append(filename)
            attachment = Attachment(
                node_id= parent.node_id,
                issue_id= parent.issue_id,
                project_id= parent.project_id,
                filename= filename,
                api=self.api
            )
            attachment.pull()
            self.add_fsobject(parent, filename, attachment)
        return result

    def readdir(self, path, fh=None):
        """Read contents of a directory

        :returns: List of filenames
        """

        f = self.get_fsobject(path)
        f.start_refresh()
        refreshed = True
        if isinstance(f, Root):
            self.update_projects()
        elif isinstance(f, Issue):
            self.get_nodes(f)
        elif isinstance(f, Node):
            self.get_evidence(f)
        elif isinstance(f, ContentBlocks):
            self.get_content_blocks(f)
        elif isinstance(f, Issues):
            self.get_issues(f)
        elif isinstance(f, Nodes):
            self.get_all_nodes(f)
        elif isinstance(f, Attachments):
            self.get_attachments(f)
        else:
            refreshed = False

        if refreshed:
            inodes_to_delete = f.end_refresh()
            self.delete_inodes(inodes_to_delete)
        return f.ls()

    def rename(self, old, new):
        """Rename issue or evidence, this is executed when the unix `mv` command is executed"""

        index = old.rfind("/")
        old_dir = old[:index]
        if old_dir == '':
            old_dir = '/'
        old_filename = old[index+1:]
        index = new.rfind("/")
        new_dir = new[:index]
        if new_dir == '':
            new_dir = '/'
        new_filename = new[index+1:]
        old_obj = self.get_fsobject(old)
        if old_dir == new_dir:
            # Simple rename, just update the filename
            old_obj.parent.rename_child(old_obj.inode, new_filename)
            return
        if isinstance(old_obj, Directory):
            # Moving directories is not supported yet
            raise FuseOSError(ENOTSUP)

        try:
            new_obj = self.get_fsobject(new)
        except FuseOSError:
            # File should be created
            self.create(new, DEFAULT_MODE)
            new_obj = self.get_fsobject(new)

        # Refresh the content of the source
        old_obj.pull()
        # Copy the contents to the destination
        new_obj.contents = old_obj.contents
        # Sync to Dradis
        new_obj.push()
        # Delete the source
        if self.allow_delete:
            self.delete(old)

    def delete(self, path):
        """Delete a file or directory"""
        if not self.allow_delete:
            raise FuseOSError(ENOTSUP)

        f = self.get_fsobject(path)
        f.delete()

        # Remove from local filesystem
        f.parent.delete_child(f.inode)
        del self.inodes[f.inode]

    def rmdir(self, path):
        """Remove issue or node"""
        if not self.allow_delete:
            raise FuseOSError(ENOTSUP)
        self.delete(path)

    def unlink(self, path):
        """Remove evidence"""
        if not self.allow_delete:
            raise FuseOSError(ENOTSUP)
        self.delete(path)

    def releasedir(self, path, *args):
        pass

    def release(self, path, *args):
        pass

    def truncate(self, path, length, fh=None):
        """Truncate a file"""

        self.get_fsobject(path).truncate(length)

    def write(self, path, data, offset, fh):
        """Update contents of file"""
        f = self.get_fsobject(path)
        f.write(data, offset)

        self.add_task({
            "file": f,
            "task": "update"
        })
        return len(data)

    def utimens(self, path, times=None):
        """Update access and modification times

        :param path: Path to update
        :param times: A tuple of the access time and modification time (atime, mtime)
        """

        self.get_fsobject(path).utimens(times)

    def chmod(self, path, mode):
        pass


def sync(dradisfs):
    while True:
        while dradisfs.task_queue:
            task = dradisfs.task_queue.pop(0)
            try:
                match task['task']:
                    case "update":
                        f = task['file']
                        f.push()
            except Exception as e:
                traceback.print_exception(e)
        sleep(1)



def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("mountpoint")
    parser.add_argument("-p", "--project", help="Mount only this dradis project", default=os.environ.get('DRADIS_PROJECT'))
    parser.add_argument("--only-nodes-with-evidence", action='store_true', help="Show only nodes with evidence, hiding nodes without evidence for issues. This can be useful if you have a lot of nodes in a project, which can clutter the view.")
    parser.add_argument("--allow-delete", action='store_true', help="Allow performing delete actions, this is disabled by default for safety reasons.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    if not args.project:
        raise argparse.ArgumentError("You must provide a project id via -p/--project or the DRADIS_PROJECT environment variable")
    dradisfs = DradisFS(api_token, url, project_id=args.project, only_nodes_with_evidence=args.only_nodes_with_evidence, allow_delete=args.allow_delete)
    sync_thread = threading.Thread(target=sync, args=(dradisfs, ))
    sync_thread.start()
    fuse = FUSE(dradisfs, args.mountpoint, foreground=True, allow_other=True)
    sync_thread.join()


if __name__ == '__main__':
    main()
