from collections import defaultdict
from sys import version_info
import logging
import re
import json
import random
import asyncio

import aiohttp
from urllib.parse import unquote

FCM_URL = 'https://fcm.googleapis.com/fcm/send'


class FCMException(Exception):
    def __init__(self, *args, retry_after=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class FCMMalformedJsonException(FCMException):
    pass


class FCMConnectionException(FCMException):
    pass


class FCMAuthenticationException(FCMException):
    pass


class FCMTooManyRegIdsException(FCMException):
    pass


class FCMInvalidTtlException(FCMException):
    pass


class FCMTopicMessageException(FCMException):
    pass


# Exceptions from Google responses


class FCMMissingRegistrationException(FCMException):
    pass


class FCMMismatchSenderIdException(FCMException):
    pass


class FCMNotRegisteredException(FCMException):
    pass


class FCMMessageTooBigException(FCMException):
    pass


class FCMInvalidRegistrationException(FCMException):
    pass


class FCMUnavailableException(FCMException):
    pass


class FCMInvalidInputException(FCMException):
    pass


# TODO: Refactor this to be more human-readable
# TODO: Use OrderedDict for the result type to be able to preserve the order of the messages returned by FCM server
def group_response(response, registration_ids, key):
    # Pair up results and reg_ids
    mapping = zip(registration_ids, response['results'])
    # Filter by key
    filtered = ((reg_id, res[key]) for reg_id, res in mapping if key in res)
    # Grouping of errors and mapping of ids
    if key in ['registration_id', 'message_id']:
        grouping = dict(filtered)
    else:
        grouping = defaultdict(list)
        for k, v in filtered:
            grouping[v].append(k)

    return grouping or None


def get_retry_after(response_headers):
    retry_after = response_headers.get('Retry-After')

    if retry_after:
        # Parse from seconds (e.g. Retry-After: 120)
        if type(retry_after) is int:
            return retry_after
        # Parse from HTTP-Date
        # (e.g. Retry-After: Fri, 31 Dec 1999 23:59:59 GMT)
        else:
            try:
                from email.utils import parsedate
                from calendar import timegm
                return timegm(parsedate(retry_after))
            except (TypeError, OverflowError, ValueError):
                return None

    return None


class Payload(object):
    """
    Base Payload class which prepares data for HTTP requests
    """

    # TTL in seconds
    FCM_TTL = 2419200

    topicPattern = re.compile('/topics/[a-zA-Z0-9-_.~%]+')

    def __init__(self, **kwargs):
        self.validate(kwargs)
        self.__dict__.update(**kwargs)

    def validate(self, options):
        """
        Allow adding validation on each payload key
        by defining `validate_{key_name}`
        """
        for key, value in options.items():
            validate_method = getattr(self, 'validate_%s' % key, None)
            if validate_method:
                validate_method(value)

    def validate_time_to_live(self, value):
        if not (0 <= value <= self.FCM_TTL):
            raise FCMInvalidTtlException("Invalid time to live value")

    @staticmethod
    def validate_registration_ids(registration_ids):

        if len(registration_ids) > 1000:
            raise FCMTooManyRegIdsException("Exceded number of registration_ids")

    @staticmethod
    def validate_to(value):
        if not re.match(Payload.topicPattern, value):
            raise FCMInvalidInputException(
                "Invalid topic name: {0}! Does not match the {1} pattern".format(value, Payload.topicPattern))

    @property
    def body(self):
        raise NotImplementedError


class PlaintextPayload(Payload):
    @property
    def body(self):
        # Safeguard for backwards compatibility
        if 'registration_id' not in self.__dict__:
            self.__dict__['registration_id'] = self.__dict__.pop('registration_ids', None)
        # Inline data for for plaintext request
        data = self.__dict__.pop('data')
        for key, value in data.items():
            self.__dict__['data.%s' % key] = value
        return self.__dict__


class JsonPayload(Payload):
    @property
    def body(self):
        return json.dumps(self.__dict__)


class FCM(object):
    # Timeunit is milliseconds.
    BACKOFF_INITIAL_DELAY = 1000
    MAX_BACKOFF_DELAY = 1024000
    logger = None

    def __init__(self, api_key, debug=False):
        """ api_key : google api key
            url: url of fcm service.
            proxy: can be string "http://host:port" or dict {'https':'host:port'}
            timeout: timeout for every HTTP request, see 'requests' documentation for possible values.
        """
        self.api_key = api_key
        self.url = FCM_URL

        self.debug = debug
        if self.debug:
            self.logger = logging.getLogger(__name__)

    def log(self, message, *data):
        if self.logger and message:
            self.logger.debug(message.format(*data))

    @staticmethod
    def construct_payload(**kwargs):
        """
        Construct the dictionary mapping of parameters.
        Encodes the dictionary into JSON if for json requests.

        :return constructed dict or JSON payload
        :raises FCMInvalidTtlException: if time_to_live is invalid
        """

        is_json = kwargs.pop('is_json', True)

        if is_json:
            if 'topic' not in kwargs and 'registration_ids' not in kwargs and 'condition' not in kwargs:
                raise FCMMissingRegistrationException("Missing registration_ids or topic or condition")
            elif 'topic' in kwargs and 'registration_ids' in kwargs:
                raise FCMInvalidInputException(
                    "Invalid parameters! Can't have both 'registration_ids' and 'to' as input parameters")

            if 'topic' in kwargs:
                kwargs['to'] = '/topics/{}'.format(kwargs.pop('topic'))
            elif 'registration_ids' not in kwargs:
                raise FCMMissingRegistrationException("Missing registration_ids")

            payload = JsonPayload(**kwargs).body
        else:
            payload = PlaintextPayload(**kwargs).body

        return payload

    async def make_request(self, data, is_json=True, session=None):
        """
        Makes a HTTP request to FCM servers with the constructed payload

        :param data: return value from construct_payload method
        :param is_json:
        :param session: aiohttp.ClientSession object to use for request
        :type  session: aiohttp.ClientSession | None
        :raises FCMMalformedJsonException: if malformed JSON request found
        :raises FCMAuthenticationException: if there was a problem with authentication, invalid api key
        :raises FCMConnectionException: if FCM is screwed
        """

        headers = {
            'Authorization': 'key=%s' % self.api_key,
        }

        if is_json:
            headers['Content-Type'] = 'application/json'
        else:
            headers['Content-Type'] = \
                'application/x-www-form-urlencoded;charset=UTF-8'

        self.log('Request URL: {0}', self.url)
        self.log('Request headers: {0}', headers)
        self.log('Request data: {0}', data)
        self.log('Request is_json: {0}', is_json)

        new_session = None
        if not session:
            session = new_session = aiohttp.ClientSession()

        try:
            response = await session.post(self.url, data=data, headers=headers)
        finally:
            if new_session:
                new_session.close()

        text = await response.text()
        self.log('Response status: {0} {1}', response.status, response.reason)
        self.log('Response headers: {0}', response.headers)
        self.log('Response data: {0}', text)

        # Successful response
        if response.status == 200:
            return response

        # Failures
        retry_after = get_retry_after(response.headers)

        if response.status == 400:
            raise FCMMalformedJsonException("The request could not be parsed as JSON", retry_after=retry_after)
        elif response.status == 401:
            raise FCMAuthenticationException(
                "There was an error authenticating the sender account", retry_after=retry_after)
        elif response.status == 503:
            raise FCMUnavailableException("GCM service is unavailable", retry_after=retry_after)
        else:
            error = "GCM service error: %d" % response.status_code
            raise FCMUnavailableException(error, retry_after=retry_after)

    @staticmethod
    def raise_error(error, retry_after=None):
        if error == 'InvalidRegistration':
            raise FCMInvalidRegistrationException("Registration ID is invalid")
        elif error == 'Unavailable':
            # Plain-text requests will never return Unavailable # as the error code.
            # http://developer.android.com/guide/google/fcm/fcm.html#error_codes
            raise FCMUnavailableException("Server unavailable. Resent the message", retry_after=retry_after)
        elif error == 'NotRegistered':
            raise FCMNotRegisteredException("Registration id is not valid anymore", retry_after=retry_after)
        elif error == 'MismatchSenderId':
            raise FCMMismatchSenderIdException(
                "A Registration ID is tied to a certain group of senders", retry_after=retry_after)
        elif error == 'MessageTooBig':
            raise FCMMessageTooBigException("Message can't exceed 4096 bytes", retry_after=retry_after)
        elif error == 'MissingRegistration':
            raise FCMMissingRegistrationException("Missing registration", retry_after=retry_after)

    def handle_plaintext_response(self, response, retry_after=None):
        if type(response) not in [bytes, str]:
            raise TypeError("Invalid type for response parameter! Expected: bytes or str. Actual: {0}"
                            .format(type(response).__name__))

        # Split response by line
        if version_info.major == 3 and type(response) is bytes:
            response = response.decode("utf-8", "strict")

        response_lines = response.strip().split('\n')

        # Split the first line by =
        key, value = response_lines[0].split('=')

        # Error on first line
        if key == 'Error':
            self.raise_error(value, retry_after=retry_after)
        else:  # Canonical_id from the second line
            if len(response_lines) == 2:
                return unquote(response_lines[1].split('=')[1])
            # TODO: Decide a way to return message id
            # without breaking backwards compatibility
            return None
            # unquote(value)  # ID of the sent message (from the first line)

    @staticmethod
    def handle_json_response(response, registration_ids):
        errors = group_response(response, registration_ids, 'error')
        canonical = group_response(
            response, registration_ids, 'registration_id')
        success = group_response(response, registration_ids, 'message_id')

        info = {}

        if errors:
            info.update({'errors': errors})

        if canonical:
            info.update({'canonical': canonical})

        if success:
            info.update({'success': success})

        return info

    @staticmethod
    def handle_topic_response(response, retry_after=None):
        error = response.get('error')
        if error:
            raise FCMTopicMessageException(error, retry_after=retry_after)
        return response['message_id']

    @staticmethod
    def extract_unsent_reg_ids(info):
        if 'errors' in info and 'Unavailable' in info['errors']:
            return info['errors']['Unavailable']
        return []

    async def plaintext_request(self, **kwargs):
        """
        Makes a plaintext request to FCM servers

        :return dict of response body from Google including multicast_id,
                success, failure, canonical_ids, etc
        """
        if 'registration_id' not in kwargs:
            raise FCMMissingRegistrationException("Missing registration_id")
        elif not kwargs['registration_id']:
            raise FCMMissingRegistrationException("Empty registration_id")

        kwargs['is_json'] = False
        retries = kwargs.pop('retries', 5)
        session = kwargs.pop('session', None)
        payload = self.construct_payload(**kwargs)
        backoff = self.BACKOFF_INITIAL_DELAY
        info = None
        has_error = False

        for attempt in range(retries):
            try:
                response = await self.make_request(payload, is_json=False, session=session)
                text = await response.text()
                retry_after = get_retry_after(response.headers)
                info = self.handle_plaintext_response(text, retry_after)
                has_error = False

            except FCMUnavailableException as e:
                retry_after = e.retry_after
                has_error = True

            if retry_after:
                self.log("[plaintext_request - Attempt #{0}] Retry-After ~> Sleeping for {1} seconds"
                         .format(attempt, retry_after))
                await asyncio.sleep(retry_after)

            elif has_error:
                sleep_time = backoff / 2 + random.randrange(backoff)
                nap_time = float(sleep_time) / 1000
                self.log("[plaintext_request - Attempt #{0}] Backoff ~> Sleeping for {1} seconds"
                         .format(attempt, nap_time))
                await asyncio.sleep(nap_time)
                if 2 * backoff < self.MAX_BACKOFF_DELAY:
                    backoff *= 2

            else:
                break

        if has_error:
            raise IOError("Could not make request after %d attempts" % retries)

        return info

    async def json_request(self, **kwargs):
        """
        Makes a JSON request to FCM servers

        :param kwargs: dict mapping of key-value pairs of parameters
        :return dict of response body from Google including multicast_id,
                success, failure, canonical_ids, etc
        """
        if 'registration_ids' not in kwargs:
            raise FCMMissingRegistrationException("Missing registration_ids")
        elif not kwargs['registration_ids']:
            raise FCMMissingRegistrationException("Empty registration_ids")

        args = dict(**kwargs)

        retries = args.pop('retries', 5)
        session = args.pop('session', None)
        payload = self.construct_payload(**args)
        registration_ids = args['registration_ids']
        backoff = self.BACKOFF_INITIAL_DELAY
        info = None
        has_error = False

        for attempt in range(retries):
            try:
                response = await self.make_request(payload, is_json=True, session=session)
                json_ = await response.json()
                retry_after = get_retry_after(response.headers)
                info = self.handle_json_response(json_, registration_ids)
                unsent_reg_ids = self.extract_unsent_reg_ids(info)
                has_error = False

            except FCMUnavailableException as e:
                retry_after = e.retry_after
                unsent_reg_ids = registration_ids
                has_error = True

            if unsent_reg_ids:
                registration_ids = unsent_reg_ids

                # Make the retry request with the unsent registration ids
                args['registration_ids'] = registration_ids
                payload = self.construct_payload(**args)

                if retry_after:
                    self.log("[json_request - Attempt #{0}] Retry-After ~> Sleeping for {1}"
                             .format(attempt, retry_after))
                    await asyncio.sleep(retry_after)

                else:
                    sleep_time = backoff / 2 + random.randrange(backoff)
                    nap_time = float(sleep_time) / 1000
                    self.log("[json_request - Attempt #{0}] Backoff ~> Sleeping for {1}"
                             .format(attempt, nap_time))
                    await asyncio.sleep(nap_time)
                    if 2 * backoff < self.MAX_BACKOFF_DELAY:
                        backoff *= 2

            else:
                break

        if has_error:
            raise IOError("Could not make request after %d attempts" % retries)

        return info

    async def send_downstream_message(self, **kwargs):
        response = await self.json_request(**kwargs)
        return response

    async def send_topic_message(self, **kwargs):
        """
        Publish Topic Messaging to FCM servers
        Ref: https://developers.google.com/cloud-messaging/topic-messaging

        :param kwargs: dict mapping of key-value pairs of parameters
        :return message_id
        :raises FCMInvalidInputException: if the topic is empty
        """

        if 'topic' not in kwargs and 'condition' not in kwargs:
            raise FCMInvalidInputException("Topic or condition missing!")

        elif not kwargs.get('topic') and not kwargs.get('condition'):
            raise FCMInvalidInputException("Topic or condition cannot be empty!")

        retries = kwargs.pop('retries', 5)
        session = kwargs.pop('session', None)
        payload = self.construct_payload(**kwargs)
        backoff = self.BACKOFF_INITIAL_DELAY

        for attempt in range(retries):
            try:
                response = await self.make_request(payload, is_json=True, session=session)
                json_ = await response.json()
                retry_after = get_retry_after(response.headers)
                return self.handle_topic_response(json_, retry_after)

            except (FCMUnavailableException, FCMTopicMessageException) as e:
                retry_after = e.retry_after

                if retry_after:
                    self.log("[send_topic_message - Attempt #{0}] Retry-After ~> Sleeping for {1}"
                             .format(attempt, retry_after))

                    await asyncio.sleep(retry_after)

                else:
                    sleep_time = backoff / 2 + random.randrange(backoff)
                    nap_time = float(sleep_time) / 1000
                    self.log("[send_topic_message - Attempt #{0}] Backoff ~> Sleeping for {1}"
                             .format(attempt, nap_time))
                    await asyncio.sleep(nap_time)
                    if 2 * backoff < self.MAX_BACKOFF_DELAY:
                        backoff *= 2

        raise IOError("Could not make request after %d attempts" % retries)


class TopicManager(object):
    # Timeunit is milliseconds.
    BACKOFF_INITIAL_DELAY = 1000
    MAX_BACKOFF_DELAY = 1024000
    logger = None

    def __init__(self, api_key, proxy=None, timeout=None, debug=False):
        """ api_key : google api key
            url: url of fcm service.
            proxy: can be string "http://host:port" or dict {'https':'host:port'}
            timeout: timeout for every HTTP request, see 'requests' documentation for possible values.
        """
        self.api_key = api_key
        self.url = "https://iid.googleapis.com/iid"

        if isinstance(proxy, str):
            protocol = self.url.split(':')[0]
            self.proxy = {protocol: proxy}
        else:
            self.proxy = proxy

        self.timeout = timeout
        self.debug = debug
        self.retry_after = None

        if self.debug:
            self.logger = logging.getLogger(__name__)

    def log(self, message, *data):
        if self.logger and message:
            self.logger.debug(message.format(*data))

    async def make_request(self, url, data=None, session=None):
        """
        Makes a HTTP request to FCM servers with the constructed payload

        :param url: request url
        :type url: str
        :param data: return value from construct_payload method
        :type data:
        :param session: requests.Session object to use for request (optional)
        :type session: requests.Sesstion
        :raises FCMMalformedJsonException: if malformed JSON request found
        :raises FCMAuthenticationException: if there was a problem with authentication, invalid api key
        :raises FCMConnectionException: if FCM is screwed
        """

        headers = {
            'Authorization': 'key=%s' % self.api_key,
            'Content-Type': 'application/json'
        }

        self.log('Request URL: {0}', url)
        self.log('Request headers: {0}', headers)
        self.log('Request data: {0}', data)

        new_session = None
        if not session:
            session = new_session = aiohttp.ClientSession()

        try:
            response = await session.post(url, data=data, headers=headers)
        finally:
            if new_session:
                new_session.close()

        text = await response.text()
        self.log('Response status: {0} {1}', response.status, response.reason)
        self.log('Response headers: {0}', response.headers)
        self.log('Response data: {0}', text)

        # Successful response
        if response.status == 200:
            return response

        # Failures
        retry_after = get_retry_after(response.headers)

        if response.status == 400:
            raise FCMMalformedJsonException("The request could not be parsed as JSON", retry_after=retry_after)
        elif response.status == 401:
            raise FCMAuthenticationException(
                "There was an error authenticating the sender account", retry_after=retry_after)
        elif response.status == 503:
            raise FCMUnavailableException("GCM service is unavailable", retry_after=retry_after)
        else:
            error = "GCM service error: %d" % response.status_code
            raise FCMUnavailableException(error, retry_after=retry_after)

    @staticmethod
    def handle_json_response(response, registration_ids):
        errors = group_response(response, registration_ids, 'error')
        canonical = group_response(response, registration_ids, 'registration_id')
        success = group_response(response, registration_ids, 'message_id')

        info = {}

        if errors:
            info.update({'errors': errors})

        if canonical:
            info.update({'canonical': canonical})

        if success:
            info.update({'success': success})

        return info

    @staticmethod
    def extract_unsent_reg_ids(info):
        if 'errors' in info and 'Unavailable' in info['errors']:
            return info['errors']['Unavailable']
        return []

    async def json_request(self, **kwargs):
        """
        Makes a JSON request to FCM servers

        :param kwargs: dict mapping of key-value pairs of parameters
        :return dict of response body from Google including multicast_id, success, failure, canonical_ids, etc
        """

        args = dict(**kwargs)

        retries = args.pop('retries', 5)
        session = args.pop('session', None)

        method = args.pop('method', 'get')
        url = args.pop('url')

        backoff = self.BACKOFF_INITIAL_DELAY
        info = None
        has_error = False

        if method == 'post' and 'topic' in args and 'registration_ids' in args:
            payload = self.construct_payload(**args)
            registration_ids = args['registration_ids']

        else:
            payload = None
            registration_ids = None

        for attempt in range(retries):
            try:
                response = await self.make_request(url=url, data=payload, session=session)
                if method == 'get':
                    info = await response.text()
                    break

                json_ = await response.json()
                retry_after = get_retry_after(response.headers)
                info = self.handle_json_response(json_, registration_ids)
                unsent_reg_ids = self.extract_unsent_reg_ids(info)
                has_error = False

            except FCMUnavailableException as e:
                retry_after = e.retry_after
                unsent_reg_ids = registration_ids
                has_error = True

            if unsent_reg_ids:
                registration_ids = unsent_reg_ids

                # Make the retry request with the unsent registration ids
                args['registration_ids'] = registration_ids
                payload = self.construct_payload(**args)

                if retry_after:
                    self.log("[json_request - Attempt #{0}] Retry-After ~> Sleeping for {1}"
                             .format(attempt, self.retry_after))
                    await asyncio.sleep(retry_after)

                else:
                    sleep_time = backoff / 2 + random.randrange(backoff)
                    nap_time = float(sleep_time) / 1000
                    self.log("[json_request - Attempt #{0}] Backoff ~> Sleeping for {1}".format(attempt, nap_time))
                    await asyncio.sleep(nap_time)
                    if 2 * backoff < self.MAX_BACKOFF_DELAY:
                        backoff *= 2
            else:
                break

        if has_error:
            raise IOError("Could not make request after %d attempts" % retries)

        return info

    @staticmethod
    def construct_payload(**kwargs):
        """
        Construct the dictionary mapping of parameters.
        Encodes the dictionary into JSON if for json requests.

        :return constructed dict or JSON payload
        :raises FCMInvalidTtlException: if time_to_live is invalid
        """

        if 'topic' not in kwargs or 'registration_ids' not in kwargs:
            raise FCMMissingRegistrationException("Missing registration_ids and topic")

        if 'topic' in kwargs:
            kwargs['to'] = '/topics/{}'.format(kwargs.pop('topic'))

        # replace param_name registration_ids to registration_tokens
        payload = JsonPayload(**kwargs).body.replace('registration_ids', 'registration_tokens')

        return payload

    async def batch_add(self, topic, registration_ids):
        """
        bulk addition of app instances to a GCM topic

        :param topic: topic name
        :type topic: str
        :param registration_ids: Instance IDs
        :type registration_ids: list
        :return:
        :rtype:
        """
        url = '{0}/v1:batchAdd'.format(self.url)
        response = await self.json_request(method='post', url=url, topic=topic, registration_ids=registration_ids)
        return response

    async def batch_remove(self, topic, registration_ids):
        """
        bulk removal of app instances to a GCM topic

        :param topic: topic name
        :type topic: str
        :param registration_ids: Instance IDs
        :type registration_ids: list
        :return:
        :rtype:
        """
        url = '{0}/v1:batchRemove'.format(self.url)
        response = await self.json_request(method='post', url=url, topic=topic, registration_ids=registration_ids)
        return response

    async def info(self, registration_id, details=True):
        """
        Get information about app instances
        :param details:
        :param registration_id: Instance ID
        :type registration_id: str
        :return:
        :rtype: dict

        application - package name associated with the token.
        authorizedEntity - projectId authorized to send to the token.
        applicationVersion - version of the application.
        appSigner - sha1 fingerprint for the signature applied to the package.
        Indicates which party signed the app; for example,Play Store.
        attestStatus - returns ROOTED, NOT_ROOTED, or UNKNOWN to indicate whether or not the device is rooted.
        platform - returns ANDROID, IOS, or CHROME to indicate the device platform to which the token belongs.

        If the details flag is set:

        connectionType - returns WIFI, MOBILE or OTHER. Returns nothing if the connection is uninitialized.
        connectDate - the date the device was last seen.
        rel - relations associated with the token. For example, a list of topic subscriptions.

        """

        url = '{0}/info/{1}{2}'.format(self.url, registration_id, '?details=true' if details is True else '')
        response = await self.json_request(method='get', url=url)
        return response
