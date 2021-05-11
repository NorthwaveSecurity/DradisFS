#!/usr/bin/env python
from __future__ import print_function, absolute_import, division

import logging

from collections import defaultdict
from errno import ENOENT
from stat import S_IFDIR, S_IFLNK, S_IFREG
from sys import argv, exit
from time import time
from functools import cache
import re

from fuse import FUSE, FuseOSError, Operations, LoggingMixIn
from dradis import Dradis

import configparser

config = configparser.ConfigParser()
config.read('config.ini')

api_token = config['DEFAULT']['api_token']
url = config['DEFAULT']['url']


def create_filename(label):
    return re.sub(r'[^\w\-_\. ]', '_', label)


class DradisCached(Dradis):

    @cache
    def get_all_projects(self):
        return super().get_all_projects()

    @cache
    def get_all_issues(self, project_id):
        return super().get_all_issues(project_id)

    @cache
    def get_all_evidence(self, project_id, node_id):
        return super().get_all_evidence(project_id, node_id)

    @cache
    def get_all_nodes(self, project_id):
        return super().get_all_nodes(project_id)


class DradisFS(LoggingMixIn, Operations):
    'Interaction with dradis api via filesystem'

    def __init__(self, api_token, url):
        self.api = DradisCached(api_token, url)
        self.files = {}
        self.files['/'] = {
            'stats': self.get_stats(),
            'type': 'root',
        }
        self.projects = {}
        self.data = {}
        self.fd = 0
        self.update_projects()

    def get_stats(self, dir=True):
        now = time()
        mode = 0o644
        if dir:
            return dict(st_mode=(S_IFDIR | mode), st_ctime=now,
                        st_mtime=now, st_atime=now, st_nlink=2)
        else:
            return dict(st_mode=(S_IFREG | mode), st_nlink=1,
                                 st_size=0, st_ctime=time(), st_mtime=time(),
                                 st_atime=time())

    def create(self, path, mode):
        "create new evidence"
        pass

    def mkdir(self, path, mode):
        "create new issue"
        pass

    def open(self, path, flags):
        "open fd"
        f = self.files[path]
        if f['type'] == 'evidence':
            evidence = self.api.get_evidence(f['project_id'], f['node_id'], f['id'])
            contents = self.encode_contents(evidence['content'])
            self.data[path] = contents
            self.files[path]['stats']['st_size'] = len(contents)
            self.fd += 1
            return self.fd
        elif f['type'] == 'issue_content':
            self.fd += 1
            return self.fd
        else:
            return FuseOSError("Failed to open file")

    def read(self, path, size, offset, fh):
        return self.data[path][offset:offset + size]

    def getxattr(self, path, name, position=0):
        attrs = self.files[path].get('attrs', {})

        try:
            return attrs[name]
        except KeyError:
            return ''       # Should return ENOATTR

    def getattr(self, path, fh=None):
        if path not in self.files:
            raise FuseOSError(ENOENT)
        return self.files[path]['stats']

    def update_projects(self):
        for p in self.api.get_all_projects():
            filename = create_filename('{}_{}'.format(p['id'], p['name']))
            p['filename'] = filename
            path = '/' + filename
            self.projects[p['id']] = p
            self.files[path] = {
                'type': 'project',
                'stats': self.get_stats(),
                'id': p['id'],
            }

    def get_issues(self, project_path):
        project_id = self.files[project_path]['id']
        result = []
        for i in self.api.get_all_issues(project_id):
            filename = create_filename("{}_{}".format(i['id'], i['title']))
            path = "{}/{}".format(project_path, filename)
            self.files[path] = {
                'type': 'issue',
                'stats': self.get_stats(),
                'id': i['id'],
                'project_id': project_id,
            }
            issue_content_path = path + "/issue"
            self.files[issue_content_path] = {
                'type': 'issue_content',
                'stats': self.get_stats(False),
                'id': i['id'],
                'project_id': project_id,
            }
            contents = self.encode_contents(i['text'])
            self.files[issue_content_path]['stats']['st_size'] = len(contents)
            self.data[issue_content_path] = contents
            result.append(filename)
        return result

    def get_nodes(self, issue_path):
        f = self.files[issue_path]
        result = []
        for node in self.api.get_all_nodes(f['project_id']):
            has_evidence = False
            node_filename = create_filename(node['label'])
            node_path = "{}/{}".format(issue_path, node_filename)
            evidences = list(filter(lambda e: e['issue']['id'] == f['id'], node['evidence']))
            for e in evidences:
                if e['content'] != '':
                    has_evidence = True
                    break
            if has_evidence:
                self.files[node_path] = {
                    'type': 'node',
                    'stats': self.get_stats(),
                    'id': node['id'],
                    'project_id': f['project_id'],
                    'issue_id': f['id'],
                }
                result.append(node_filename)
        return result

    def encode_contents(self, contents):
        return contents.encode('utf-8')

    def get_evidence(self, node_path):
        f = self.files[node_path]
        result = []
        i = 0
        for e in self.api.get_all_evidence(f['project_id'], f['id']):
            if e['issue']['id'] != f['issue_id']:
                continue
            filename = str(i)
            i += 1
            path = "{}/{}".format(node_path, filename)
            stats = self.get_stats(dir=False)
            stats['st_size'] = len(self.encode_contents(e['content']))
            self.files[path] = {
                'type': 'evidence',
                'stats': stats,
                'node_id': f['id'],
                'issue_id': f['issue_id'],
                'project_id': f['project_id'],
                'id': e['id'],
            }
            result.append(filename)
        return result

    def readdir(self, path, fh=None):
        if path == '/':
            self.update_projects()
            return ['.', '..'] + [p['filename'] for p in self.projects.values()]
        if path not in self.files:
            return FuseOSError(ENOENT)
        f = self.files[path]
        type = f['type']
        if type == 'project':
            return ['.', '..'] + self.get_issues(path)
        if type == 'issue':
            return ['.', '..', 'issue'] + self.get_nodes(path)
        if type == 'node':
            return ['.', '..'] + self.get_evidence(path)
        return ['.', '..']

    def rename(self, old, new):
        "Rename issue or evidence"
        pass
        #self.files[new] = self.files.pop(old)

    def rmdir(self, path):
        "Remove issue and evidences"
        pass

    def unlink(self, path):
        "Remote evidence"
        pass

    def write(self, path, data, offset, fh):
        "Update issue"
        pass
        # self.data[path] = self.data[path][:offset] + data
        # self.files[path]['st_size'] = len(self.data[path])
        # return len(data)

    def utimens(self, path, times=None):
        now = time()
        atime, mtime = times if times else (now, now)
        self.files[path]['stats']['st_atime'] = atime
        self.files[path]['stats']['st_mtime'] = mtime


if __name__ == '__main__':
    if len(argv) != 2:
        print('usage: %s <mountpoint>' % argv[0])
        exit(1)

    logging.basicConfig(level=logging.DEBUG)
    fuse = FUSE(DradisFS(api_token, url), argv[1], foreground=True)

