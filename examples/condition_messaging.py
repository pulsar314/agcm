#!/usr/bin/env python

from fcm import FCM

# Topic Messaging

API_KEY = "your api key"

fcm = FCM(API_KEY)
data = {'param1': 'value1', 'param2': 'value2'}
condition = "'TopicA' in topics && ('TopicB' in topics || 'TopicC' in topics)"

response = fcm.send_topic_message(condition=condition, data=data)

print(response)
