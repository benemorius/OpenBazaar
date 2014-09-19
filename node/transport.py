import connection
from dht import DHT
from protocol import hello_request
from protocol import hello_response
from protocol import goodbye
from protocol import proto_response_pubkey
from urlparse import urlparse
from zmq.eventloop import ioloop, zmqstream
from zmq.eventloop.ioloop import PeriodicCallback
from collections import defaultdict
from pprint import pformat
from pybitcointools.main import privkey_to_pubkey
from pybitcointools.main import privtopub
from pybitcointools.main import random_key
from crypto_util import pubkey_to_pyelliptic
from crypto_util import makePrivCryptor
from crypto_util import makePubCryptor
from pysqlcipher.dbapi2 import OperationalError, DatabaseError
import gnupg
import xmlrpclib
import logging
import pyelliptic as ec
import requests
import json
import socket
import traceback
from threading import Thread
import zlib
import obelisk
import network_util
import zmq
import random
import hashlib


ioloop.install()


class TransportLayer(object):
    # Transport layer manages a list of peers
    def __init__(self, market_id, my_ip, my_port, my_guid, nickname=None):

        self.peers = {}
        self.callbacks = defaultdict(list)
        self.timeouts = []
        self.port = my_port
        self.ip = my_ip
        self.guid = my_guid
        self.market_id = market_id
        self.nickname = nickname
        self.handler = None

        try:
            socket.inet_pton(socket.AF_INET6, my_ip)
            my_uri = 'tcp://[%s]:%s' % (self.ip, self.port)
        except (socket.error, ValueError):
            my_uri = 'tcp://%s:%s' % (self.ip, self.port)
        self.uri = my_uri

        self.log = logging.getLogger(
            '[%s] %s' % (market_id, self.__class__.__name__)
        )

    def add_callbacks(self, callbacks):
        for section, callback in callbacks:
            self.callbacks[section] = []
            self.add_callback(section, callback)

    def set_websocket_handler(self, handler):
        self.handler = handler

    def add_callback(self, section, callback):
        if callback not in self.callbacks[section]:
            self.callbacks[section].append(callback)

    def trigger_callbacks(self, section, *data):

        # Run all callbacks in specified section
        for cb in self.callbacks[section]:
            cb(*data)

        # Run all callbacks registered under the 'all' section. Don't duplicate
        # calls if the specified section was 'all'.
        if not section == 'all':
            for cb in self.callbacks['all']:
                cb(*data)

    def get_profile(self):
        return hello_request({'uri': self.uri})

    def listen(self, pubkey):
        self.log.info("Listening at: %s:%s" % (self.ip, self.port))
        self.ctx = zmq.Context()
        self.socket = self.ctx.socket(zmq.REP)

        if network_util.is_loopback_addr(self.ip):
            try:
                # we are in local test mode so bind that socket on the
                # specified IP
                self.socket.bind(self.uri)
            except Exception as e:
                error_message = "\n\nTransportLayer.listen() error!!!: "
                error_message += "Could not bind socket to " + self.uri
                error_message += " (" + str(e) + ")"
                import platform
                if platform.system() == 'Darwin':
                    error_message += "\n\nPerhaps you have not added a "\
                                     "loopback alias yet.\n"
                    error_message += "Try this on your terminal and restart "\
                                     "OpenBazaar in development mode again:\n"
                    error_message += "\n\t$ sudo ifconfig lo0 alias 127.0.0.2"
                    error_message += "\n\n"
                raise Exception(error_message)

        else:
            try:
                socket.inet_pton(socket.AF_INET6, self.ip)
                self.socket.ipv6 = True
                self.socket.bind('tcp://[*]:%s' % self.port)
            except (AttributeError, socket.error, ValueError):
                self.socket.bind('tcp://*:%s' % self.port)

        self.stream = zmqstream.ZMQStream(
            self.socket, io_loop=ioloop.IOLoop.current()
        )

        def handle_recv(message):
            for msg in message:
                self._on_raw_message(msg)
            self.stream.send(
                json.dumps({
                    'type': 'ok',
                    'senderGUID': self.guid,
                    'pubkey': pubkey,
                    'senderNick': self.nickname
                })
            )

        self.stream.on_recv(handle_recv)

    def closed(self, *args):
        self.log.info("client left")

    def _init_peer(self, msg):
        uri = msg['uri']

        if uri not in self.peers:
            self.peers[uri] = connection.PeerConnection(self, uri)

    def remove_peer(self, uri, guid):
        self.log.info("Removing peer %s", uri)
        ip = urlparse(uri).hostname
        port = urlparse(uri).port
        if (ip, port, guid) in self.shortlist:
            self.shortlist.remove((ip, port, guid))

        self.log.info('Removed')

        # try:
        # del self.peers[uri]
        # msg = {
        # 'type': 'peer_remove',
        # 'uri': uri
        #     }
        #     self.trigger_callbacks(msg['type'], msg)
        #
        # except KeyError:
        #     self.log.info("Peer %s was already removed", uri)

    def send(self, data, send_to=None, callback=lambda msg: None):

        self.log.info("Outgoing Data: %s %s" % (data, send_to))
        data['senderNick'] = self.nickname

        # Directed message
        if send_to is not None:
            peer = self.dht.routingTable.getContact(send_to)
            # self.log.debug(
            #     '%s %s %s' % (peer.guid, peer.address, peer.pub)
            # )
            peer.send(data, callback=callback)
            return

        else:
            # FindKey and then send

            for peer in self.dht.activePeers:
                try:
                    data['senderGUID'] = self.guid
                    data['pubkey'] = self.pubkey
                    # if peer.pub:
                    #    peer.send(data, callback)
                    # else:
                    print 'test %s' % peer

                    def cb(msg):
                        print msg
                    peer.send(data, cb)

                except:
                    self.log.info("Error sending over peer!")
                    traceback.print_exc()

    def broadcast_goodbye(self):
        self.log.info("Broadcast goodbye")
        msg = goodbye({'uri': self.uri})
        self.send(msg)

    def _on_message(self, msg):

        # here goes the application callbacks
        # we get a "clean" msg which is a dict holding whatever
        self.log.info("[On Message] Data received: %s" % msg)

        # if not self.routingTable.getContact(msg['senderGUID']):
        # Add to contacts if doesn't exist yet
        # self._addCryptoPeer(msg['uri'], msg['senderGUID'], msg['pubkey'])
        if msg['type'] != 'ok':
            self.trigger_callbacks(msg['type'], msg)

    def _on_raw_message(self, serialized):
        self.log.info("connected " + str(len(serialized)))
        try:
            msg = json.loads(serialized[0])
        except:
            self.log.info("incorrect msg! " + serialized)
            return

        msg_type = msg.get('type')
        if msg_type == 'hello_request' and msg.get('uri'):
            self._init_peer(msg)
        else:
            self._on_message(msg)

    def valid_peer_uri(self, uri):
        try:
            [self_protocol, self_addr, self_port] = \
                network_util.uri_parts(self.uri)
            [other_protocol, other_addr, other_port] = \
                network_util.uri_parts(uri)
        except RuntimeError:
            return False

        if not network_util.is_valid_protocol(other_protocol) \
                or not network_util.is_valid_port(other_port):
            return False

        if network_util.is_private_ip_address(self_addr):
            if not network_util.is_private_ip_address(other_addr):
                self.log.warning((
                    'Trying to connect to external '
                    'network with a private ip address.'
                ))
        else:
            if network_util.is_private_ip_address(other_addr):
                return False

        return True

    def shutdown(self):
        if self.ctx is not None:
            print "TransportLayer.shutdown() destroying zmq ctx sockets."
            self.ctx.destroy(linger=None)


class CryptoTransportLayer(TransportLayer):

    def __init__(self, my_ip, my_port, market_id, db, bm_user=None, bm_pass=None,
                 bm_port=None, seed_mode=0, dev_mode=False, disable_ip_update=False):

        self.log = logging.getLogger(
            '[%s] %s' % (market_id, self.__class__.__name__)
        )
        requests_log = logging.getLogger("requests")
        requests_log.setLevel(logging.WARNING)

        # Connect to database
        self.db = db

        self.bitmessage_api = None
        if (bm_user, bm_pass, bm_port) != (None, None, None):
            if not self._connect_to_bitmessage(bm_user, bm_pass, bm_port):
                self.log.info('Bitmessage not installed or started')

        try:
            socket.inet_pton(socket.AF_INET6, my_ip)
            my_uri = "tcp://[%s]:%s" % (my_ip, my_port)
        except (socket.error, ValueError):
            my_uri = "tcp://%s:%s" % (my_ip, my_port)

        self.market_id = market_id
        self.nick_mapping = {}
        self.uri = my_uri
        self.ip = my_ip
        self.nickname = ""
        self._dev_mode = dev_mode

        # Set up
        self._setup_settings()

        self.dht = DHT(self, self.market_id, self.settings, self.db)

        # self._myself = ec.ECC(pubkey=self.pubkey.decode('hex'),
        #                       privkey=self.secret.decode('hex'),
        #                       curve='secp256k1')

        TransportLayer.__init__(self, market_id, my_ip, my_port,
                                self.guid, self.nickname)

        self.setup_callbacks()
        self.listen(self.pubkey)

        if seed_mode == 0 and not dev_mode and not disable_ip_update:
            self.start_ip_address_checker()

    def setup_callbacks(self):
        self.add_callbacks([('hello', self._ping),
                            ('findNode', self._find_node),
                            ('findNodeResponse', self._find_node_response),
                            ('store', self._store_value)])

    def start_ip_address_checker(self):
        '''Checks for possible public IP change'''
        self.caller = PeriodicCallback(self._ip_updater_periodic_callback, 5000, ioloop.IOLoop.instance())
        self.caller.start()

    def _ip_updater_periodic_callback(self):
        try:
            r = requests.get('https://icanhazip.com')

            if r and hasattr(r, 'text'):
                ip = r.text
                ip = ip.strip(' \t\n\r')
                if ip != self.ip:
                    self.ip = ip
                    try:
                        socket.inet_pton(socket.AF_INET6, self.ip)
                        my_uri = 'tcp://[%s]:%s' % (self.ip, self.port)
                    except (socket.error, ValueError):
                        my_uri = 'tcp://%s:%s' % (self.ip, self.port)
                    self.uri = my_uri
                    self.stream.close()
                    self.listen(self.pubkey)

                    self.dht._iterativeFind(self.guid, [], 'findNode')
            else:
                self.log.error('Could not get IP')
        except Exception as e:
            self.log.error('[Requests] error: %s' % e)

    def save_peer_to_db(self, peer_tuple):
        uri = peer_tuple[0]
        pubkey = peer_tuple[1]
        guid = peer_tuple[2]
        nickname = peer_tuple[3]

        # Update query
        self.db.deleteEntries("peers", {"uri": uri, "guid": guid}, "OR")
        # if len(results) > 0:
        #     self.db.updateEntries("peers", {"id": results[0]['id']}, {"market_id": self.market_id, "uri": uri, "pubkey": pubkey, "guid": guid, "nickname": nickname})
        # else:
        if guid is not None:
            self.db.insertEntry("peers", {
                "uri": uri,
                "pubkey": pubkey,
                "guid": guid,
                "nickname": nickname,
                "market_id": self.market_id
            })

    def _connect_to_bitmessage(self, bm_user, bm_pass, bm_port):
        # Get bitmessage going
        # First, try to find a local instance
        result = False
        try:
            self.log.info('[_connect_to_bitmessage] Connecting to Bitmessage on port %s' % bm_port)
            self.bitmessage_api = xmlrpclib.ServerProxy("http://{}:{}@localhost:{}/".format(bm_user, bm_pass, bm_port), verbose=0)
            result = self.bitmessage_api.add(2, 3)
            self.log.info("[_connect_to_bitmessage] Bitmessage API is live".format(result))
        # If we failed, fall back to starting our own
        except Exception as e:
            self.log.info("Failed to connect to bitmessage instance: {}".format(e))
            self.bitmessage_api = None
            # self._log.info("Spawning internal bitmessage instance")
            # # Add bitmessage submodule path
            # sys.path.insert(0, os.path.join(
            #     os.path.dirname(__file__), '..', 'pybitmessage', 'src'))
            # import bitmessagemain as bitmessage
            # bitmessage.logger.setLevel(logging.WARNING)
            # bitmessage_instance = bitmessage.Main()
            # bitmessage_instance.start(daemon=True)
            # bminfo = bitmessage_instance.getApiAddress()
            # if bminfo is not None:
            #     self._log.info("Started bitmessage daemon at %s:%s".format(
            #         bminfo['address'], bminfo['port']))
            #     bitmessage_api = xmlrpclib.ServerProxy("http://{}:{}@{}:{}/".format(
            #         bm_user, bm_pass, bminfo['address'], bminfo['port']))
            # else:
            #     self._log.info("Failed to start bitmessage dameon")
            #     self._bitmessage_api = None
        return result

    def _checkok(self, msg):
        self.log.info('Check ok')

    def get_guid(self):
        return self.guid

    def get_dht(self):
        return self.dht

    def get_bitmessage_api(self):
        return self.bitmessage_api

    def get_market_id(self):
        return self.market_id

    # def get_myself(self):
    #     return self._myself

    def _ping(self, msg):

        self.log.info('Pinged %s ' % json.dumps(msg, ensure_ascii=False))
        #
        # pinger = CryptoPeerConnection(self, msg['uri'], msg['pubkey'], msg['senderGUID'])
        # pinger.send_raw(json.dumps(
        #     {"type": "hello_response",
        #      "senderGUID": self.guid,
        #      "uri": self.uri,
        #      "senderNick": self.nickname,
        #      "pubkey": self.pubkey,
        #     }))

    def _store_value(self, msg):
        self.dht._on_storeValue(msg)

    def _find_node(self, msg):
        self.dht.on_find_node(msg)

    def _find_node_response(self, msg):
        self.dht.on_findNodeResponse(self, msg)

    def _setup_settings(self):

        try:
            self.settings = self.db.selectEntries("settings", {"market_id": self.market_id})
        except (OperationalError, DatabaseError) as e:
            print e
            raise SystemExit("database file %s corrupt or empty - cannot continue" % self.db.db_path)

        if len(self.settings) == 0:
            self.settings = {"market_id": self.market_id, "welcome": "enable"}
            self.db.insertEntry("settings", self.settings)
        else:
            self.settings = self.settings[0]

        # Generate PGP key during initial setup or if previous PGP gen failed
        if not ('PGPPubKey' in self.settings and self.settings["PGPPubKey"]):
            try:
                self.log.info('Generating PGP keypair. This may take several minutes...')
                print 'Generating PGP keypair. This may take several minutes...'
                gpg = gnupg.GPG()
                input_data = gpg.gen_key_input(key_type="RSA",
                                               key_length=2048,
                                               name_email='gfy@gfy.com',
                                               name_comment="Autogenerated by Open Bazaar",
                                               passphrase="P@ssw0rd")
                assert input_data is not None
                key = gpg.gen_key(input_data)
                assert key is not None

                pubkey_text = gpg.export_keys(key.fingerprint)
                newsettings = {"PGPPubKey": pubkey_text, "PGPPubkeyFingerprint": key.fingerprint}
                self.db.updateEntries("settings", {"market_id": self.market_id}, newsettings)
                self.settings.update(newsettings)

                self.log.info('PGP keypair generated.')
            except Exception as e:
                self.log.error("Encountered a problem with GPG: %s" % e)
                raise SystemExit("Encountered a problem with GPG: %s" % e)

        if not ('pubkey' in self.settings and self.settings['pubkey']):
            # Generate Bitcoin keypair
            self._generate_new_keypair()

        if not ('nickname' in self.settings and self.settings['nickname']):
            newsettings = {'nickname': 'Default'}
            self.db.updateEntries('settings', {"market_id": self.market_id}, newsettings)
            self.settings.update(newsettings)

        self.nickname = self.settings['nickname'] if 'nickname' in self.settings else ""
        self.secret = self.settings['secret'] if 'secret' in self.settings else ""
        self.pubkey = self.settings['pubkey'] if 'pubkey' in self.settings else ""
        self.privkey = self.settings.get('privkey')
        self.btc_pubkey = privkey_to_pubkey(self.privkey)
        self.guid = self.settings['guid'] if 'guid' in self.settings else ""
        self.sin = self.settings['sin'] if 'sin' in self.settings else ""
        self.bitmessage = self.settings['bitmessage'] if 'bitmessage' in self.settings else ""

        if not ('bitmessage' in self.settings and self.settings['bitmessage']):
            # Generate Bitmessage address
            if self.bitmessage_api is not None:
                self._generate_new_bitmessage_address()

        self._myself = ec.ECC(
            pubkey=pubkey_to_pyelliptic(self.pubkey).decode('hex'),
            raw_privkey=self.secret.decode('hex'),
            curve='secp256k1'
        )

        self.log.debug('Retrieved Settings: \n%s', pformat(self.settings))

    def _generate_new_keypair(self):
        secret = str(random.randrange(2 ** 256))
        self.secret = hashlib.sha256(secret).hexdigest()
        self.pubkey = privtopub(self.secret)
        self.privkey = random_key()
        print 'PRIVATE KEY: ', self.privkey
        self.btc_pubkey = privtopub(self.privkey)
        print 'PUBLIC KEY: ', self.btc_pubkey

        # Generate SIN
        sha_hash = hashlib.sha256()
        sha_hash.update(self.pubkey)
        ripe_hash = hashlib.new('ripemd160')
        ripe_hash.update(sha_hash.digest())

        self.guid = ripe_hash.digest().encode('hex')
        self.sin = obelisk.EncodeBase58Check('\x0F\x02%s' + ripe_hash.digest())

        newsettings = {
            "secret": self.secret,
            "pubkey": self.pubkey,
            "privkey": self.privkey,
            "guid": self.guid,
            "sin": self.sin
        }
        self.db.updateEntries("settings", {"market_id": self.market_id}, newsettings)
        self.settings.update(newsettings)

    def _generate_new_bitmessage_address(self):
        # Use the guid generated previously as the key
        self.bitmessage = self.bitmessage_api.createRandomAddress(
            self.guid.encode('base64'),
            False,
            1.05,
            1.1111
        )
        newsettings = {"bitmessage": self.bitmessage}
        self.db.updateEntries("settings", {"market_id": self.market_id}, newsettings)
        self.settings.update(newsettings)

    def join_network(self, seed_peers=[], callback=lambda msg: None):

        self.log.info('Joining network')

        known_peers = []

        # Connect up through seed servers
        for idx, seed in enumerate(seed_peers):
            try:
                socket.inet_pton(socket.AF_INET6, seed)
                seed_peers[idx] = "tcp://[%s]:12345" % seed
            except (socket.error, ValueError):
                seed_peers[idx] = "tcp://%s:12345" % seed

        # Connect to persisted peers
        db_peers = self.get_past_peers()

        known_peers = list(set(seed_peers)) + list(set(db_peers))

        print 'known_peers', known_peers

        self.connect_to_peers(known_peers)

        # Populate routing table by searching for self
        if len(known_peers) > 0:
            self.search_for_my_node()

        if callback is not None:
            callback('Joined')

    def get_past_peers(self):
        peers = []
        result = self.db.selectEntries("peers", {"market_id": self.market_id})
        for peer in result:
            peers.append(peer['uri'])
        return peers

    def search_for_my_node(self):
        print 'Searching for myself'
        self.dht._iterativeFind(self.guid, self.dht.knownNodes, 'findNode')

    def connect_to_peers(self, known_peers):
        for known_peer in known_peers:
            t = Thread(target=self.dht.add_peer, args=(self, known_peer,))
            t.start()

    def get_crypto_peer(self, guid=None, uri=None, pubkey=None, nickname=None,
                        callback=None):
        if guid == self.guid:
            self.log.error('Cannot get CryptoPeerConnection for your own node')
            return

        self.log.debug('Getting CryptoPeerConnection' +
                       '\nGUID:%s\nURI:%s\nPubkey:%s\nNickname:%s' %
                       (guid, uri, pubkey, nickname))

        return connection.CryptoPeerConnection(self,
                                               uri,
                                               pubkey,
                                               guid=guid,
                                               nickname=nickname,
                                               callback=callback)

    def addCryptoPeer(self, peer_to_add):

        foundOutdatedPeer = False
        for idx, peer in enumerate(self.dht.activePeers):

            if (peer.address, peer.guid, peer.pub) == \
               (peer_to_add.address, peer_to_add.guid, peer_to_add.pub):
                self.log.info('Found existing peer, not adding.')
                return

            if peer.guid == peer_to_add.guid or \
               peer.pub == peer_to_add.pub or \
               peer.address == peer_to_add.address:

                foundOutdatedPeer = True
                self.log.info('Found an outdated peer')

                # Update existing peer
                self.activePeers[idx] = peer_to_add
                self.dht.add_peer(self,
                                  peer_to_add.address,
                                  peer_to_add.pub,
                                  peer_to_add.guid,
                                  peer_to_add.nickname)

        if not foundOutdatedPeer and peer_to_add.guid != self.guid:
            self.log.info('Adding crypto peer at %s' % peer_to_add.nickname)
            self.dht.add_peer(self,
                              peer_to_add.address,
                              peer_to_add.pub,
                              peer_to_add.guid,
                              peer_to_add.nickname)

    def get_profile(self):
        peers = {}

        self.settings = self.db.selectEntries("settings", {"market_id": self.market_id})[0]
        for uri, peer in self.peers.iteritems():
            if peer.pub:
                peers[uri] = peer.pub.encode('hex')
        return {'uri': self.uri,
                'pub': self._myself.get_pubkey().encode('hex'),
                'nickname': self.nickname,
                'peers': peers}

    def respond_pubkey_if_mine(self, nickname, ident_pubkey):

        if ident_pubkey != self.pubkey:
            self.log.info("Public key does not match your identity")
            return

        # Return signed pubkey
        pubkey = self._myself.pubkey
        ec_key = obelisk.EllipticCurveKey()
        ec_key.set_secret(self.secret)
        digest = obelisk.Hash(pubkey)
        signature = ec_key.sign(digest)

        # Send array of nickname, pubkey, signature to transport layer
        self.send(proto_response_pubkey(nickname, pubkey, signature))

    def pubkey_exists(self, pub):

        for uri, peer in self.peers.iteritems():
            self.log.info(
                'PEER: %s Pub: %s' % (
                    peer.pub.encode('hex'), pub.encode('hex')
                )
            )
            if peer.pub.encode('hex') == pub.encode('hex'):
                return True

        return False

    def create_peer(self, uri, pub, node_guid):

        if pub:
            pub = pub.decode('hex')

        # Create the peer if public key is not already in the peer list
        # if not self.pubkey_exists(pub):
        self.peers[uri] = connection.CryptoPeerConnection(self, uri, pub, node_guid)

        # Call 'peer' callbacks on listeners
        self.trigger_callbacks('peer', self.peers[uri])

        # else:
        #    print 'Pub Key is already in peer list'

    def send(self, data, send_to=None, callback=lambda msg: None):

        self.log.debug("Outgoing Data: %s %s" % (data, send_to))

        # Directed message
        if send_to is not None:

            peer = self.dht.routingTable.getContact(send_to)
            if not peer:
                for activePeer in self.dht.activePeers:
                    if activePeer.guid == send_to:
                        peer = activePeer
                        break

            # peer = CryptoPeerConnection(msg['uri'])
            if peer:
                self.log.debug('Directed Data (%s): %s' % (send_to, data))
                try:
                    peer.send(data, callback=callback)
                except Exception as e:
                    self.log.error('Not sending message directly to peer %s' % e)
            else:
                self.log.error('No peer found')

        else:
            # FindKey and then send

            for peer in self.dht.activePeers:
                try:
                    peer = self.dht.routingTable.getContact(peer.guid)
                    data['senderGUID'] = self.guid
                    data['pubkey'] = self.pubkey

                    def cb(msg):
                        self.log.debug('Message Back: \n%s' % pformat(msg))

                    peer.send(data, cb)

                except:
                    self.log.info("Error sending over peer!")
                    traceback.print_exc()

    def send_enc(self, uri, msg):
        peer = self.peers[uri]
        pub = peer.pub

        # Now send a hello message to the peer
        if pub:
            self.log.info(
                "Sending encrypted [%s] message to %s" % (
                    msg['type'], uri
                )
            )
            peer.send(msg)
        else:
            # Will send clear profile on initial if no pub
            self.log.info(
                "Sending unencrypted [%s] message to %s" % (
                    msg['type'], uri
                )
            )
            self.peers[uri].send_raw(json.dumps(msg))

    def _init_peer(self, msg):

        uri = msg['uri']
        pub = msg.get('pub')
        nickname = msg.get('nickname')
        msg_type = msg.get('type')
        guid = msg['guid']

        if not self.valid_peer_uri(uri):
            self.log.error("Invalid Peer: %s " % uri)
            return

        if uri not in self.peers:
            # Unknown peer
            self.log.info('Add New Peer: %s' % uri)
            self.create_peer(uri, pub, guid)

            if not msg_type:
                self.send_enc(uri, hello_request(self.get_profile()))
            elif msg_type == 'hello_request':
                self.send_enc(uri, hello_response(self.get_profile()))

        else:
            # Known peer
            if pub:
                # test if we have to update the pubkey
                if not self.peers[uri].pub:
                    self.log.info("Setting public key for seed node")
                    self.peers[uri].pub = pub.decode('hex')
                    self.trigger_callbacks('peer', self.peers[uri])

                if self.peers[uri].pub != pub.decode('hex'):
                    self.log.info("Updating public key for node")
                    self.peers[uri].nickname = nickname
                    self.peers[uri].pub = pub.decode('hex')

                    self.trigger_callbacks('peer', self.peers[uri])

            if msg_type == 'hello_request':
                # reply only if necessary
                self.send_enc(uri, hello_response(self.get_profile()))

    def _on_message(self, msg):

        # here goes the application callbacks
        # we get a "clean" msg which is a dict holding whatever
        # self.log.info("[On Message] Data received: %s" % msg)

        pubkey = msg.get('pubkey')
        uri = msg.get('uri')
        ip = urlparse(uri).hostname
        port = urlparse(uri).port
        guid = msg.get('senderGUID')
        nickname = msg.get('senderNick')[:120]

        self.dht.add_known_node((ip, port, guid, nickname))
        self.log.info('On Message: %s' % json.dumps(msg, ensure_ascii=False))
        self.dht.add_peer(self, uri, pubkey, guid, nickname)
        t = Thread(target=self.trigger_callbacks, args=(msg['type'], msg,))
        t.start()

    def _on_raw_message(self, serialized):
        try:

            # Decompress message
            serialized = zlib.decompress(serialized)

            msg = json.loads(serialized)
            self.log.info("Message Received [%s]" % msg.get('type', 'unknown'))

            if msg.get('type') is None:

                data = msg.get('data').decode('hex')
                sig = msg.get('sig').decode('hex')

                try:
                    cryptor = makePrivCryptor(self.secret)

                    try:
                        data = cryptor.decrypt(data)
                    except Exception as e:
                        self.log.info('Exception: %s' % e)

                    self.log.debug('Signature: %s' % sig.encode('hex'))
                    self.log.debug('Signed Data: %s' % data)

                    # Check signature
                    data_json = json.loads(data)
                    sigCryptor = makePubCryptor(data_json['pubkey'])
                    if sigCryptor.verify(sig, data):
                        self.log.info('Verified')
                    else:
                        self.log.error('Message signature could not be verified %s' % msg)
                        # return

                    msg = json.loads(data)
                    self.log.debug('Message Data %s ' % msg)
                except Exception as e:
                    self.log.error('Could not decrypt message properly %s' % e)

        except ValueError:
            try:
                # Encrypted?
                try:
                    msg = self._myself.decrypt(serialized)
                    msg = json.loads(msg)

                    self.log.info(
                        "Decrypted Message [%s]" % msg.get('type', 'unknown')
                    )
                except:
                    self.log.error("Could not decrypt message: %s" % msg)
                    return
            except:
                self.log.error('Message probably sent using incorrect pubkey')

                return

        if msg.get('type') is not None:
            self._on_message(msg)
        else:
            self.log.error('Received a message with no type')

    def shutdown(self):
        print "CryptoTransportLayer.shutdown()!"
        try:
            TransportLayer.shutdown(self)
            print "CryptoTransportLayer.shutdown(): ZMQ sockets destroyed."
        except Exception as e:
            self.log.error("Transport shutdown error: " + e.message)

        print "Notice: explicit DHT Shutdown not implemented."

        try:
            self.bitmessage_api.close()
        except Exception as e:
            # might not even be open, not much more we can do on our way out if exception thrown here.
            self.log.error("Could not shutdown bitmessage_api's ServerProxy. " + e.message)
