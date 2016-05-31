python-fcm
======================

.. image:: https://badges.gitter.im/Join%20Chat.svg
:alt: Join the chat at https://gitter.im/geeknam/python-fcm
   :target: https://gitter.im/geeknam/python-fcm?utm_source=badge&utm_medium=badge&utm_campaign=pr-badge&utm_content=badge
.. image:: https://img.shields.io/pypi/v/python-fcm.svg
:target: https://pypi.python.org/pypi/python-fcm
.. image:: https://img.shields.io/pypi/dm/python-fcm.svg
:target: https://pypi.python.org/pypi/python-fcm
.. image:: https://secure.travis-ci.org/geeknam/python-fcm.png?branch=master
:alt: Build Status
   :target: http://travis-ci.org/geeknam/python-fcm
.. image:: https://landscape.io/github/geeknam/python-fcm/master/landscape.png
:target: https://landscape.io/github/geeknam/python-fcm/master
   :alt: Code Health
.. image:: https://coveralls.io/repos/geeknam/python-fcm/badge.svg?branch=master
:target: https://coveralls.io/r/geeknam/python-fcm
.. image:: https://img.shields.io/gratipay/geeknam.svg
:target: https://gratipay.com/geeknam/

Python client for Google Cloud Messaging for Android (FCM)

Installation
-------------

.. code-block:: bash

   pip install git+https://github.com/lostdragon/python-fcm.git

Features
------------

* Supports multicast message
* Resend messages using exponential back-off
* Proxy support
* Easily handle errors
* Uses `requests` from version > 0.2
* `Topic Messaging  <https://firebase.google.com/docs/cloud-messaging/topic-messaging>`__
* TCP connection pooling and Keep-Alive when passing an explict requests.Session object to the used FCM request call
* Topic Manager

Usage
------------

Read about `Google Firebase Cloud Messaging <https://firebase.google.com/docs/cloud-messaging/>`__

.. code-block:: python

   from fcm import FCM

   fcm = FCM(API_KEY)
   data = {'param1': 'value1', 'param2': 'value2'}

   # Downstream message using JSON request
   reg_ids = ['token1', 'token2', 'token3']
   response = fcm.json_request(registration_ids=reg_ids, data=data)

   # Downstream message using JSON request with extra arguments
   res = fcm.json_request(
       registration_ids=reg_ids, data=data,
       collapse_key='uptoyou', delay_while_idle=True, time_to_live=3600
   )

   # Topic Messaging
   topic = 'topic name'
   fcm.send_topic_message(topic=topic, data=data)

See `examples <examples>`_  directory for more usage details, including error handling.

Contributing
==========
See `CONTRIBUTING.md <CONTRIBUTING.md>`_

Licensing
=======
See `LICENSE <LICENSE>`_
