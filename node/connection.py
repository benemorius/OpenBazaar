from pprint import pformat
from urlparse import urlparse
from zmq.eventloop import ioloop, zmqstream
from crypto_util import makePrivCryptor
from crypto_util import hexToPubkey
import logging
import pyelliptic as ec
import socket
import zlib
import obelisk
import zmq
import errno
import json

ioloop.install()


class PeerConnection(object):
    def __init__(self, transport, address, nickname=""):
        # timeout in seconds
        self.timeout = 10
        self.transport = transport
        self.address = address
        self.nickname = nickname
        self.responses_received = {}
        self.log = logging.getLogger(
            '[%s] %s' % (self.transport.market_id, self.__class__.__name__)
        )
        self.ctx = zmq.Context()

    def create_zmq_socket(self):
        self.log.info('Creating Socket')

        try:
            socket = self.ctx.socket(zmq.REQ)
            socket.setsockopt(zmq.LINGER, 0)
            return socket
        except Exception as e:
            self.log.error('Cannot create socket %s' % e)
            raise
        # self._socket.setsockopt(zmq.SOCKS_PROXY, "127.0.0.1:9051");

    def cleanup_context(self):
        self.ctx.destroy()

    def send(self, data, callback):
        self.send_raw(json.dumps(data), callback)

    def send_raw(self, serialized, callback=lambda msg: None):

        compressed_data = zlib.compress(serialized, 9)

        try:
            s = self.create_zmq_socket()
            try:
                s.connect(self.address)
            except zmq.ZMQError as e:
                if e.errno != errno.EINVAL:
                    raise
                try:
                    s.ipv6 = True
                except AttributeError:
                    self.log.error("no ipv6")
                    print "no ipv6"
                    return
                s.connect(self.address)

            stream = zmqstream.ZMQStream(s, io_loop=ioloop.IOLoop.current())
            self.log.error("gonna send")
            self.log.error("stream is ]%s[" % stream)
            stream.send(compressed_data)
            self.log.error("did send")

            def cb(stream, msg):
                response = json.loads(msg[0])
                self.log.debug('[send_raw] %s' % pformat(response))

                # Update active peer info

                if 'senderNick' in response and\
                   response['senderNick'] != self.nickname:
                    self.nickname = response['senderNick']

                if callback is not None:
                    self.log.debug('%s' % msg)
                    callback(msg)
                stream.close()

            stream.on_recv_stream(cb)
        except zmq.ZMQError as e:
            self.log.error("zmq exception: %s" % e)
            raise
        except Exception as e:
            self.log.error("other exception: %s" % e)
            # Shouldn't we raise the exception here?
            # I think not doing this could cause buggy behavior on top.
            self.log.error("raising")
            raise
        except:
            self.log.error("wtf exception")
            raise


class CryptoPeerConnection(PeerConnection):

    def __init__(self, transport, address, pub=None, guid=None, nickname=None,
                 sin=None, callback=lambda msg: None):

        # self._priv = transport._myself
        self.pub = pub

        # Convert URI over
        parseResult = urlparse(address)
        self.ip = parseResult.hostname
        self.port = parseResult.port
        self.host_to_ip()

        self.nickname = nickname
        self.sin = sin
        self.peer_alive = False  # unused; might remove it later if unnecessary
        self.guid = guid
        self.address = "tcp://%s:%s" % (self.ip, self.port)

        PeerConnection.__init__(self, transport, address)

        self.log = logging.getLogger(
            '[%s] %s' % (transport.market_id, self.__class__.__name__)
        )

    def host_to_ip(self):
        addr_info = socket.getaddrinfo(str(self.ip), self.port)
        self.ip = addr_info[0][4][0]

    def start_handshake(self, handshake_cb=None):
        if self.address == self.transport.uri:
            self.log.debug("Not starting handshake with self")
            return
        if self.check_port():
            def cb(msg):
                if msg:

                    self.log.debug('ALIVE PEER %s' % msg[0])
                    msg = msg[0]
                    msg = json.loads(msg)

                    # Update Information
                    self.guid = msg['senderGUID']
                    self.sin = self.generate_sin(self.guid)
                    self.pub = msg['pubkey']
                    self.nickname = msg['senderNick']

                    self.peer_alive = True

                    # Add this peer to active peers list
                    for idx, peer in enumerate(self.transport.dht.activePeers):
                        if peer.guid == self.guid or peer.address == self.address:
                            self.transport.dht.activePeers[idx] = self
                            self.transport.dht.add_peer(
                                self.transport,
                                self.address,
                                self.pub,
                                self.guid,
                                self.nickname
                            )
                            return

                    self.transport.dht.activePeers.append(self)
                    self.transport.dht.routingTable.addContact(self)

                    if handshake_cb is not None:
                        handshake_cb()

            self.send_raw(
                json.dumps({
                    'type': 'hello',
                    'pubkey': self.transport.pubkey,
                    'uri': self.transport.uri,
                    'senderGUID': self.transport.guid,
                    'senderNick': self.transport.nickname
                }),
                cb
            )
        else:
            self.log.error("Failed to connect to new peer for handshake: %s %s %s" % (self.address, self.guid, self.nickname))

    def __repr__(self):
        return '{ guid: %s, ip: %s, port: %s, pubkey: %s }' % (
            self.guid, self.ip, self.port, self.pub
        )

    def generate_sin(self, guid):
        return obelisk.EncodeBase58Check('\x0F\x02%s' + guid.decode('hex'))

    def check_port(self):

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect((self.ip, self.port))
        except socket.error as e:
            self.log.info("IPv4 socket exception on %s:%i %s" % (self.ip, self.port,  e))
            try:
                s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                s.settimeout(1)
                s.connect((self.ip, self.port))
            except socket.error as e:
                self.log.info("IPv6 socket exception on %s:%i %s" % (self.ip, self.port, e))
                self.transport.dht.remove_active_peer(self.address)
                return False
        except TypeError:
            self.log.error("tried connecting to invalid address: %s" % self.ip)
            return False

        if s:
            self.log.info('SOCKET %s' % s)
            s.close()
        return True

    def sign(self, data):
        self.log.info('secret %s' % self.transport.settings['secret'])
        cryptor = makePrivCryptor(self.transport.settings['secret'])
        return cryptor.sign(data)

    def encrypt(self, data):
        try:
            if self.pub is not None:
                hexkey = hexToPubkey(self.pub)
                return ec.ECC.encrypt(data, hexkey)
            else:
                self.log.error('Public Key is missing')
                return False
        except Exception as e:
            self.log.error('Encryption failed. %s' % e)

    def send(self, data, callback=lambda msg: None):

        if hasattr(self, 'guid'):

            if self.check_port():

                # Include guid
                data['guid'] = self.guid
                data['senderGUID'] = self.transport.guid
                data['uri'] = self.transport.uri
                data['pubkey'] = self.transport.pubkey
                data['senderNick'] = self.transport.nickname

                self.log.info('Sending to peer: %s' % self.ip)
                self.log.debug('data before encryption: %s' % pformat(data))

                if self.pub == '':
                    self.log.info('There is no public key for encryption')
                else:
                    signature = self.sign(json.dumps(data))
                    data = self.encrypt(json.dumps(data))

                    try:
                        if data is not None:
                            self.send_raw(
                                json.dumps({
                                    'sig': signature.encode('hex'),
                                    'data': data.encode('hex')
                                }),
                                callback
                            )
                        else:
                            self.log.error('Data was empty')
                    except Exception as e:
                        self.log.error(
                            "Was not able to encode empty data: %s" % e
                        )
            else:
                self.log.error('Peer is not available for sending data')
        else:
            self.log.error('Cannot send to peer')

    def peer_to_tuple(self):
        return self.ip, self.port, self.guid

    def get_guid(self):
        return self.guid
