#!/usr/bin/env python

from fcm import TopicManager

# JSON request

API_KEY = "your api key"

topic_manager = TopicManager(API_KEY)

registration_ids = ["your token 1", "your token 2"]

topic = 'global'

response = topic_manager.batch_add(topic=topic, registration_ids=registration_ids)

print(response)

# Handling errors
if 'errors' in response:
    for error, reg_ids in response['errors'].items():
        # Check for errors and act accordingly
        if error in ['NotRegistered', 'InvalidRegistration']:
            # Remove reg_ids from database
            for reg_id in reg_ids:
                print("Removing reg_id: {0} from db".format(reg_id))

# Replace reg_id with canonical_id in your database
if 'canonical' in response:
    for reg_id, canonical_id in response['canonical'].items():
        print("Replacing reg_id: {0} with canonical_id: {1} in db".format(reg_id, canonical_id))

response = topic_manager.info(registration_ids[0])

print(response)

response = topic_manager.batch_remove(topic=topic, registration_ids=registration_ids)

print(response)

# Handling errors
if 'errors' in response:
    for error, reg_ids in response['errors'].items():
        # Check for errors and act accordingly
        if error in ['NotRegistered', 'InvalidRegistration']:
            # Remove reg_ids from database
            for reg_id in reg_ids:
                print("Removing reg_id: {0} from db".format(reg_id))

# Replace reg_id with canonical_id in your database
if 'canonical' in response:
    for reg_id, canonical_id in response['canonical'].items():
        print("Replacing reg_id: {0} with canonical_id: {1} in db".format(reg_id, canonical_id))

response = topic_manager.info(registration_ids[0])

print(response)
