#!/usr/bin/env python

import setuptools

setuptools.setup(
    name='systemd-minecraft',
    description='A systemd service file for one or more vanilla Minecraft servers',
    author='Wurstmineberg',
    author_email='mail@wurstmineberg.de',
    py_modules=['minecraft'],
    install_requires=[
        'docopt',
        'loops',
        'mcrcon',
        'more-itertools',
        'requests'
    ],
    dependency_links=[
        'git+https://github.com/fenhl/python-loops.git#egg=loops'
    ]
)
