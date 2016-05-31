import requests
import json
from collections import defaultdict
import time
import random
from sys import version_info
import logging
import re

if version_info.major == 2:
    from urllib2 import unquote
else:
    from urllib.parse import unquote

FCM_URL = 'https://fcm.googleapis.com/fcm/send'


class FCMException(Exception):
    pass


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
        # Parse from HTTP-Date (e.g. Retry-After: Fri, 31 Dec 1999 23:59:59 GMT)
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

    def validate_registration_ids(self, registration_ids):

        if len(registration_ids) > 1000:
            raise FCMTooManyRegIdsException("Exceded number of registration_ids")

    def validate_to(self, value):
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
            self.__dict__['registration_id'] = self.__dict__.pop(
                'registration_ids', None
            )
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
    logging_handler = None

    def __init__(self, api_key, proxy=None, timeout=None, debug=False):
        """ api_key : google api key
            url: url of fcm service.
            proxy: can be string "http://host:port" or dict {'https':'host:port'}
            timeout: timeout for every HTTP request, see 'requests' documentation for possible values.
        """
        self.api_key = api_key
        self.url = FCM_URL

        if isinstance(proxy, str):
            protocol = self.url.split(':')[0]
            self.proxy = {protocol: proxy}
        else:
            self.proxy = proxy

        self.timeout = timeout
        self.debug = debug
        self.retry_after = None

        if self.debug:
            FCM.enable_logging()

    @staticmethod
    def enable_logging(level=logging.DEBUG, handler=None):
        """
        Helper for quickly adding a StreamHandler to the logger. Useful for debugging.

        :param handler:
        :param level:
        :return: the handler after adding it
        """
        if not handler:
            # Use a singleton logging_handler instead of recreating it,
            # so we can remove-and-re-add safely without having duplicate handlers
            if FCM.logging_handler is None:
                FCM.logging_handler = logging.StreamHandler()
                FCM.logging_handler.setFormatter(logging.Formatter(
                    '[%(asctime)s - %(levelname)s - %(filename)s:%(lineno)s - %(funcName)s()] %(message)s'))
            handler = FCM.logging_handler

        FCM.logger = logging.getLogger(__name__)
        FCM.logger.removeHandler(handler)
        FCM.logger.addHandler(handler)
        FCM.logger.setLevel(level)
        FCM.log('Added a stderr logging handler to logger: {0}', __name__)

        # Enable requests logging
        requests_logger_name = 'requests.packages.urllib3'
        requests_logger = logging.getLogger(requests_logger_name)
        requests_logger.removeHandler(handler)
        requests_logger.addHandler(handler)
        requests_logger.setLevel(level)
        FCM.log('Added a stderr logging handler to logger: {0}', requests_logger_name)

    @staticmethod
    def log(message, *data):
        if FCM.logger and message:
            FCM.logger.debug(message.format(*data))

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
            if 'topic' not in kwargs and 'registration_ids' not in kwargs:
                raise FCMMissingRegistrationException("Missing registration_ids or topic")
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

    def make_request(self, data, is_json=True, session=None):
        """
        Makes a HTTP request to FCM servers with the constructed payload

        :param data: return value from construct_payload method
        :type data:
        :param is_json:
        :type is_json: bool
        :param session: requests.Session object to use for request (optional)
        :type session: requests.Sesstion
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
            headers['Content-Type'] = 'application/x-www-form-urlencoded;charset=UTF-8'

        FCM.log('Request URL: {0}', self.url)
        FCM.log('Request headers: {0}', headers)
        FCM.log('Request proxy: {0}', self.proxy)
        FCM.log('Request timeout: {0}', self.timeout)
        FCM.log('Request data: {0}', data)
        FCM.log('Request is_json: {0}', is_json)

        new_session = None
        if not session:
            session = new_session = requests.Session()

        try:
            response = session.post(
                self.url, data=data, headers=headers,
                proxies=self.proxy, timeout=self.timeout,
            )
        finally:
            if new_session:
                new_session.close()

        FCM.log('Response status: {0} {1}', response.status_code, response.reason)
        FCM.log('Response headers: {0}', response.headers)
        FCM.log('Response data: {0}', response.text)

        # 5xx or 200 + error:Unavailable
        self.retry_after = get_retry_after(response.headers)

        # Successful response
        if response.status_code == 200:
            if is_json:
                response = response.json()
            else:
                response = response.content
            return response

        # Failures
        if response.status_code == 400:
            raise FCMMalformedJsonException(
                "The request could not be parsed as JSON")
        elif response.status_code == 401:
            raise FCMAuthenticationException(
                "There was an error authenticating the sender account")
        elif response.status_code == 503:
            raise FCMUnavailableException("FCM service is unavailable")
        else:
            error = "FCM service error: %d" % response.status_code
            raise FCMUnavailableException(error)

    @staticmethod
    def raise_error(error):
        if error == 'InvalidRegistration':
            raise FCMInvalidRegistrationException("Registration ID is invalid")
        elif error == 'Unavailable':
            # Plain-text requests will never return Unavailable as the error code.
            # http://developer.android.com/guide/google/fcm/fcm.html#error_codes
            raise FCMUnavailableException(
                "Server unavailable. Resent the message")
        elif error == 'NotRegistered':
            raise FCMNotRegisteredException(
                "Registration id is not valid anymore")
        elif error == 'MismatchSenderId':
            raise FCMMismatchSenderIdException(
                "A Registration ID is tied to a certain group of senders")
        elif error == 'MessageTooBig':
            raise FCMMessageTooBigException("Message can't exceed 4096 bytes")
        elif error == 'MissingRegistration':
            raise FCMMissingRegistrationException("Missing registration")

    def handle_plaintext_response(self, response):
        if type(response) not in [bytes, str]:
            raise TypeError("Invalid type for response parameter! Expected: bytes or str. "
                            "Actual: {0}".format(type(response).__name__))

        # Split response by line
        if version_info.major == 3 and type(response) is bytes:
            response = response.decode("utf-8", "strict")

        response_lines = response.strip().split('\n')

        # Split the first line by =
        key, value = response_lines[0].split('=')

        # Error on first line
        if key == 'Error':
            self.raise_error(value)
        else:  # Canonical_id from the second line
            if len(response_lines) == 2:
                return unquote(response_lines[1].split('=')[1])
            return None  # TODO: Decide a way to return message id without breaking backwards compatibility
            # unquote(value)  # ID of the sent message (from the first line)

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
    def handle_topic_response(response):
        error = response.get('error')
        if error:
            raise FCMTopicMessageException(error)
        return response['message_id']

    @staticmethod
    def extract_unsent_reg_ids(info):
        if 'errors' in info and 'Unavailable' in info['errors']:
            return info['errors']['Unavailable']
        return []

    def plaintext_request(self, **kwargs):
        """
        Makes a plaintext request to FCM servers

        :return dict of response body from Google including multicast_id, success, failure, canonical_ids, etc
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
                response = self.make_request(payload, is_json=False, session=session)
                info = self.handle_plaintext_response(response)
                has_error = False
            except FCMUnavailableException:
                has_error = True

            if self.retry_after:
                FCM.log("[plaintext_request - Attempt #{0}] Retry-After ~> Sleeping for {1} seconds".format(attempt,
                                                                                                            self.retry_after))
                time.sleep(self.retry_after)
                self.retry_after = None
            elif has_error:
                sleep_time = backoff / 2 + random.randrange(backoff)
                nap_time = float(sleep_time) / 1000
                FCM.log(
                    "[plaintext_request - Attempt #{0}]Backoff ~> Sleeping for {1} seconds".format(attempt, nap_time))
                time.sleep(nap_time)
                if 2 * backoff < self.MAX_BACKOFF_DELAY:
                    backoff *= 2
            else:
                break

        if has_error:
            raise IOError("Could not make request after %d attempts" % retries)

        return info

    def json_request(self, **kwargs):
        """
        Makes a JSON request to FCM servers

        :param kwargs: dict mapping of key-value pairs of parameters
        :return dict of response body from Google including multicast_id, success, failure, canonical_ids, etc
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
                response = self.make_request(payload, is_json=True, session=session)
                info = self.handle_json_response(response, registration_ids)
                unsent_reg_ids = self.extract_unsent_reg_ids(info)
                has_error = False
            except FCMUnavailableException:
                unsent_reg_ids = registration_ids
                has_error = True

            if unsent_reg_ids:
                registration_ids = unsent_reg_ids

                # Make the retry request with the unsent registration ids
                args['registration_ids'] = registration_ids
                payload = self.construct_payload(**args)

                if self.retry_after:
                    FCM.log("[json_request - Attempt #{0}] Retry-After ~> Sleeping for {1}".format(attempt,
                                                                                                   self.retry_after))
                    time.sleep(self.retry_after)
                    self.retry_after = None
                else:
                    sleep_time = backoff / 2 + random.randrange(backoff)
                    nap_time = float(sleep_time) / 1000
                    FCM.log("[json_request - Attempt #{0}] Backoff ~> Sleeping for {1}".format(attempt, nap_time))
                    time.sleep(nap_time)
                    if 2 * backoff < self.MAX_BACKOFF_DELAY:
                        backoff *= 2
            else:
                break

        if has_error:
            raise IOError("Could not make request after %d attempts" % retries)

        return info

    def send_downstream_message(self, **kwargs):
        return self.json_request(**kwargs)

    def send_topic_message(self, **kwargs):
        """
        Publish Topic Messaging to FCM servers
        Ref: https://developers.google.com/cloud-messaging/topic-messaging

        :param kwargs: dict mapping of key-value pairs of parameters
        :return message_id
        :raises FCMInvalidInputException: if the topic is empty
        """

        if 'topic' not in kwargs:
            raise FCMInvalidInputException("Topic name missing!")
        elif not kwargs['topic']:
            raise FCMInvalidInputException("Topic name cannot be empty!")

        retries = kwargs.pop('retries', 5)
        session = kwargs.pop('session', None)
        payload = self.construct_payload(**kwargs)
        backoff = self.BACKOFF_INITIAL_DELAY

        for attempt in range(retries):
            try:
                response = self.make_request(payload, is_json=True, session=session)
                return self.handle_topic_response(response)
            except (FCMUnavailableException, FCMTopicMessageException):
                if self.retry_after:
                    FCM.log("[send_topic_message - Attempt #{0}] Retry-After ~> Sleeping for {1}"
                            .format(attempt, self.retry_after))

                    time.sleep(self.retry_after)
                    self.retry_after = None
                else:
                    sleep_time = backoff / 2 + random.randrange(backoff)
                    nap_time = float(sleep_time) / 1000
                    FCM.log("[send_topic_message - Attempt #{0}] Backoff ~> Sleeping for {1}".format(attempt, nap_time))
                    time.sleep(nap_time)
                    if 2 * backoff < self.MAX_BACKOFF_DELAY:
                        backoff *= 2

        raise IOError("Could not make request after %d attempts" % retries)


class TopicManager(object):
    # Timeunit is milliseconds.
    BACKOFF_INITIAL_DELAY = 1000
    MAX_BACKOFF_DELAY = 1024000
    logger = None
    logging_handler = None

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
            FCM.enable_logging()

    def make_request(self, method, url, data=None, session=None):
        """
        Makes a HTTP request to FCM servers with the constructed payload

        :param method: http method, get or post
        :type method: str
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

        FCM.log('Request method: {0}', method)
        FCM.log('Request URL: {0}', url)
        FCM.log('Request headers: {0}', headers)
        FCM.log('Request proxy: {0}', self.proxy)
        FCM.log('Request timeout: {0}', self.timeout)
        FCM.log('Request data: {0}', data)

        new_session = None
        if not session:
            session = new_session = requests.Session()

        try:
            response = session.request(method, url, data=data, headers=headers,
                                       proxies=self.proxy, timeout=self.timeout)
        finally:
            if new_session:
                new_session.close()

        FCM.log('Response status: {0} {1}', response.status_code, response.reason)
        FCM.log('Response headers: {0}', response.headers)
        FCM.log('Response data: {0}', response.text)

        # 5xx or 200 + error:Unavailable
        self.retry_after = get_retry_after(response.headers)

        # Successful response
        if response.status_code == 200:
            response = response.json()
            return response

        # Failures
        if response.status_code == 400:
            raise FCMMalformedJsonException(
                "The request could not be parsed as JSON")
        elif response.status_code == 401:
            raise FCMAuthenticationException(
                "There was an error authenticating the sender account")
        elif response.status_code == 503:
            raise FCMUnavailableException("FCM service is unavailable")
        else:
            error = "FCM service error: %d" % response.status_code
            raise FCMUnavailableException(error)

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

    def json_request(self, **kwargs):
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
                response = self.make_request(method=method, url=url, data=payload, session=session)
                if method == 'get':
                    info = response
                    break

                info = self.handle_json_response(response, registration_ids)
                unsent_reg_ids = self.extract_unsent_reg_ids(info)
                has_error = False
            except FCMUnavailableException:
                unsent_reg_ids = registration_ids
                has_error = True

            if unsent_reg_ids:
                registration_ids = unsent_reg_ids

                # Make the retry request with the unsent registration ids
                args['registration_ids'] = registration_ids
                payload = self.construct_payload(**args)

                if self.retry_after:
                    FCM.log("[json_request - Attempt #{0}] Retry-After ~> Sleeping for {1}".format(attempt,
                                                                                                   self.retry_after))
                    time.sleep(self.retry_after)
                    self.retry_after = None
                else:
                    sleep_time = backoff / 2 + random.randrange(backoff)
                    nap_time = float(sleep_time) / 1000
                    FCM.log("[json_request - Attempt #{0}] Backoff ~> Sleeping for {1}".format(attempt, nap_time))
                    time.sleep(nap_time)
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

    def batch_add(self, topic, registration_ids):
        """
        bulk addition of app instances to a GCM topic

        :param topic: topic name
        :type topic: str
        :param registration_ids: Instance IDs
        :type registration_ids: list
        :return:
        :rtype:
        """
        url = '{}/v1:batchAdd'.format(self.url)
        return self.json_request(method='post', url=url,
                                 topic=topic,
                                 registration_ids=registration_ids,
                                 )

    def batch_remove(self, topic, registration_ids):
        """
        bulk removal of app instances to a GCM topic

        :param topic: topic name
        :type topic: str
        :param registration_ids: Instance IDs
        :type registration_ids: list
        :return:
        :rtype:
        """
        url = '{}/v1:batchRemove'.format(self.url)
        return self.json_request(method='post', url=url, topic=topic,
                                 registration_ids=registration_ids,
                                 )

    def info(self, registration_id, details=True):
        """
        Get information about app instances
        :param registration_id: Instance ID
        :type registration_id: str
        :return:
        :rtype: dict

        application - package name associated with the token.
        authorizedEntity - projectId authorized to send to the token.
        applicationVersion - version of the application.
        appSigner - sha1 fingerprint for the signature applied to the package. Indicates which party signed the app; for example,Play Store.
        attestStatus - returns ROOTED, NOT_ROOTED, or UNKNOWN to indicate whether or not the device is rooted.
        platform - returns ANDROID, IOS, or CHROME to indicate the device platform to which the token belongs.

        If the details flag is set:

        connectionType - returns WIFI, MOBILE or OTHER. Returns nothing if the connection is uninitialized.
        connectDate - the date the device was last seen.
        rel - relations associated with the token. For example, a list of topic subscriptions.

        """

        url = '{}/info/{}{}'.format(self.url, registration_id, '?details=true' if details is True else '')
        return self.json_request(method='get', url=url)
