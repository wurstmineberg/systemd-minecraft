#!/usr/bin/env python

import setuptools

setuptools.setup(
    name='systemd-minecraft',
    description='A systemd service file for one or more vanilla Minecraft servers',
    author='Wurstmineberg',
    author_email='mail@wurstmineberg.de',
    packages=['minecraft'],
    use_scm_version={
        'write_to': 'minecraft/_version.py',
    },
    setup_requires=[
        'setuptools_scm',
    ],
    install_requires=[
        'docopt',
        'loops',
        'mcrcon',
        'more-itertools',
        'requests',
    ],
    dependency_links=[
        'git+https://github.com/fenhl/python-loops.git#egg=loops',
        'git+https://github.com/wurstmineberg/MCRcon.git#egg=mcrcon'
    ]
)
