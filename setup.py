from setuptools import setup

setup(
    name='afcm',
    version='0.4',
    packages=['afcm'],
    license='The MIT License (MIT)',
    author='Nam Ngo',
    author_email='nam@kogan.com.au',
    url='http://blog.namis.me/python-fcm/',
    description='Python client for Google Cloud Messaging for Android (FCM)',
    long_description=open('README.rst').read(),
    keywords='android fcm push notification google cloud messaging',
    install_requires=['aiohttp'],
    tests_require=['mock'],
    test_suite='fcm.test',
)
