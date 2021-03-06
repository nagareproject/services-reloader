# Encoding: utf-8

# --
# Copyright (c) 2008-2021 Net-ng.
# All rights reserved.
#
# This software is licensed under the BSD License, as described in
# the file LICENSE.txt, which you should have received as part of
# this distribution.
# --

from os import path

from setuptools import setup, find_packages


here = path.normpath(path.dirname(__file__))

with open(path.join(here, 'README.rst')) as long_description:
    LONG_DESCRIPTION = long_description.read()

install_requires = ['watchdog', 'webob', 'nagare-server']
try:
    import gevent  # noqa: F401
    install_requires += ['watchdog_gevent']
except ImportError:
    pass

setup(
    name='nagare-services-reloader',
    author='Net-ng',
    author_email='alain.poirier@net-ng.com',
    description='Reloader service',
    long_description=LONG_DESCRIPTION,
    license='BSD',
    keywords='',
    url='https://github.com/nagareproject/services-reloader',
    packages=find_packages(),
    include_package_data=True,
    package_data={'': ['nagare/static/*']},
    zip_safe=False,
    setup_requires=['setuptools_scm'],
    use_scm_version=True,
    install_requires=install_requires,
    entry_points='''
        [nagare.services]
        reloader = nagare.services.reloader:Reloader
    '''
)
