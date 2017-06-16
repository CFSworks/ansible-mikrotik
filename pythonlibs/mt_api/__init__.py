from __future__ import unicode_literals

import binascii
import hashlib
import logging
import socket
import ssl
import sys

from .retryloop import RetryError
from .retryloop import retryloop
from .socket_utils import set_keepalive

PY2 = sys.version_info[0] < 3
logger = logging.getLogger(__name__)


class RosAPIError(Exception):
    def __init__(self, value):
        self.value = value

    def __str__(self):
        if isinstance(self.value, dict) and self.value.get('message'):
            return self.value['message']
        elif isinstance(self.value, list):
            elements = (
                '%s: %s' %
                (element.__class__, str(element)) for element in self.value
            )
            return '[%s]' % (', '.join(element for element in elements))
        else:
            return str(self.value)


class RosAPIConnectionError(RosAPIError):
    pass


class RosAPIFatalError(RosAPIError):
    pass


class RosApiLengthUtils(object):
    def __init__(self, api):
        self.api = api

    def write_length(self, length):
        self.api.write_bytes(self.length_to_bytes(length))

    def length_to_bytes(self, length):
        if length < 0x80:
            return self.to_bytes(length)
        elif length < 0x4000:
            length |= 0x8000
            return self.to_bytes(length, 2)
        elif length < 0x200000:
            length |= 0xC00000
            return self.to_bytes(length, 3)
        elif length < 0x10000000:
            length |= 0xE0000000
            return self.to_bytes(length, 4)
        else:
            return self.to_bytes(0xF0) + self.to_bytes(length, 4)

    def read_length(self):
        b = self.api.read_bytes(1)
        i = self.from_bytes(b)
        if (i & 0x80) == 0x00:
            return i
        elif (i & 0xC0) == 0x80:
            return self._unpack(1, i & ~0xC0)
        elif (i & 0xE0) == 0xC0:
            return self._unpack(2, i & ~0xE0)
        elif (i & 0xF0) == 0xE0:
            return self._unpack(3, i & ~0xF0)
        elif (i & 0xF8) == 0xF0:
            return self.from_bytes(self.api.read_bytes(1))
        else:
            raise RosAPIFatalError('Unknown value: %x' % i)

    def _unpack(self, times, i):
        temp1 = self.to_bytes(i)
        temp2 = self.api.read_bytes(times)
        try:
            temp3 = temp2.decode('utf-8')
        except:
            try:
                temp3 = temp2.decode('windows-1252')
            except Exception:
                print("Cannot decode response properly:", temp2)
                print(Exception)
                exit(1)

        res = temp1 + temp3
        return self.from_bytes(res)

    if PY2:
        def from_bytes(self, data):
            data_values = [ord(char) for char in data]
            value = 0
            for byte_value in data_values:
                value <<= 8
                value += byte_value
            return value

        def to_bytes(self, i, size=1):
            data = []
            for _ in xrange(size):
                data.append(chr(i & 0xff))
                i >>= 8
            return ''.join(reversed(data))
    else:
        def from_bytes(self, data):
            return int.from_bytes(data, 'big')

        def to_bytes(self, i, size=1):
            return i.to_bytes(size, 'big')


class RosAPI(object):
    """Routeros api"""

    def __init__(self, socket):
        self.socket = socket
        self.length_utils = RosApiLengthUtils(self)

    def login(self, username, pwd):
        assert type(username) is type(pwd) is bytes
        for _, attrs in self.talk([b'/login']):
            token = binascii.unhexlify(attrs[b'ret'])
        hasher = hashlib.md5()
        hasher.update(b'\x00')
        hasher.update(pwd)
        hasher.update(token)
        self.talk([b'/login', b'=name=' + username,
                   b'=response=00' + hasher.hexdigest().encode('ascii')])

    def talk(self, words):
        assert all(type(word) is bytes for word in words)
        if self.write_sentence(words) == 0:
            return
        output = []
        while True:
            input_sentence = self.read_sentence()
            if not len(input_sentence):
                continue
            attrs = {}
            reply = input_sentence.pop(0)
            for line in input_sentence:
                try:
                    second_eq_pos = line.index(b'=', 1)
                except IndexError:
                    attrs[line[1:]] = b''
                else:
                    attrs[line[1:second_eq_pos]] = line[second_eq_pos + 1:]
            output.append((reply, attrs))
            if reply == b'!done':
                if output[0][0] == b'!trap':
                    raise RosAPIError(output[0][1])
                if output[0][0] == b'!fatal':
                    self.socket.close()
                    raise RosAPIFatalError(output[0][1])
                return output

    def write_sentence(self, words):
        assert all(type(word) is bytes for word in words)
        words_written = 0
        for word in words:
            self.write_word(word)
            words_written += 1
        self.write_word(b'')
        return words_written

    def read_sentence(self):
        sentence = []
        while True:
            word = self.read_word()
            if not len(word):
                return sentence
            sentence.append(word)

    def write_word(self, word):
        assert type(word) is bytes
        logger.debug('>>> %s' % word)
        self.length_utils.write_length(len(word))
        self.write_bytes(word)

    def read_word(self):
        word = self.read_bytes(self.length_utils.read_length())
        logger.debug('<<< %s' % word)
        return word

    def write_bytes(self, data):
        assert type(data) is bytes
        sent_overall = 0
        while sent_overall < len(data):
            try:
                sent = self.socket.send(data[sent_overall:])
            except socket.error as e:
                raise RosAPIConnectionError(str(e))
            if sent == 0:
                raise RosAPIConnectionError('Connection closed by remote end.')
            sent_overall += sent

    def read_bytes(self, length):
        received_overall = b''
        while len(received_overall) < length:
            try:
                received = self.socket.recv(
                    length - len(received_overall))
            except socket.error as e:
                raise RosAPIConnectionError(str(e))
            if len(received) == 0:
                raise RosAPIConnectionError('Connection closed by remote end.')
            received_overall += received
        return received_overall

class Mikrotik(object):
  def __init__(self, host, username, password, port=8728):
    self.host = host
    self.username = username
    self.password = password
    self.port = port

  def login(self):
    s = socket.create_connection((self.host, self.port))
    mt = RosAPI(s)
    mt.login(self.username.encode('ascii'),
             self.password.encode('ascii'))
    return mt

  def talk(self, talk_command):
    r = self.login()
    response = r.talk([word.encode('utf8') for word in talk_command])
    return(response)

  def api_print(self, base_path, params=None):
    command = [base_path + '/print']
    if params is not None:
      for key, value in params.items():
        item = '=' + key + '=' + str(value)
        command.append(item)

    return self.talk(command)

  def api_add(self, base_path, params):
    command = [base_path + '/add']
    for key, value in params.items():
      item = '=' + key + '=' + str(value)
      command.append(item)

    return self.talk(command)

  def api_edit(self, base_path, params):
    command = [base_path + '/set']
    for key, value in params.items():
      item = '=' + key + '=' + str(value)
      command.append(item)

    return self.talk(command)

  def api_remove(self, base_path, remove_id):
    command = [
        base_path + '/remove',
        '=.id=' + remove_id
    ]

    return self.talk(command)

  def api_command(self, base_path, params=None):
    command = [base_path]
    if params is not None:
      for key, value in params.items():
        item = '=' + key + '=' + str(value)
        command.append(item)

    return self.talk(command)
