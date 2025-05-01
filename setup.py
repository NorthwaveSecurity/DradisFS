#!/usr/bin/env python
from distutils.core import setup
from setuptools import find_packages

setup(name='DradisFS',
      version='0.1',
      description='Interact with Dradis through a FUSE filesystem.',
      author='Northwave Security',
      author_email='janjaap.korpershoek@northwave.nl',
      install_requires=[
        'dradis-api @ git+ssh://git@github.com/NorthwaveSecurity/dradis-api.git@v1.4',
        'requests',
        'fusepy',
        'cachetools',
      ],
      py_modules=['dradisfs'],
      entry_points={
        'console_scripts': ['dradisfs=dradisfs:main'],
      },
      )
