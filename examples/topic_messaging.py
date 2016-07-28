#!/usr/bin/env python

from gcm import FCM

# Topic Messaging

API_KEY = "your api key"

fcm = FCM(API_KEY)
data = {'param1': 'value1', 'param2': 'value2'}
topic = 'your topic name'

response = fcm.send_topic_message(topic=topic, data=data)

print(response)
