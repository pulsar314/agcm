from setuptools import setup

setup(
    name='agcm',
    version='0.4',
    packages=['agcm'],
    license='The MIT License (MIT)',
    author='Nam Ngo',
    author_email='nam@kogan.com.au',
    url='http://blog.namis.me/python-gcm/',
    description='Python client for Google Cloud Messaging for Android (GCM)',
    long_description=open('README.rst').read(),
    keywords='android gcm push notification google cloud messaging',
    install_requires=['aiohttp'],
    tests_require=['mock'],
    test_suite='gcm.test',
)
