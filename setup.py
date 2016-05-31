from setuptools import setup

setup(
    name='python-fcm',
    version='0.4',
    packages=['fcm'],
    license='The MIT License (MIT)',
    author='Nam Ngo',
    author_email='nam@kogan.com.au',
    url='http://blog.namis.me/python-fcm/',
    description='Python client for Google Cloud Messaging for Android (FCM)',
    long_description=open('README.rst').read(),
    keywords='android fcm push notification google cloud messaging',
    install_requires=['requests'],
    tests_require=['mock'],
    test_suite='fcm.test',
)
