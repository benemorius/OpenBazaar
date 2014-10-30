"""
This module manages all market related activities
"""
import ast
from base64 import b64decode, b64encode
import gnupg
import hashlib
import json
import logging
from PIL import Image, ImageOps
import random
from StringIO import StringIO
from threading import Thread
import traceback

from bitcoin.main import privkey_to_pubkey
import tornado

import constants
from crypto_util import Cryptor
from data_uri import DataURI
from orders import Orders
from protocol import proto_page, query_page


class Market(object):
    """This class manages the active market for the application"""

    def __init__(self, transport, db):
        """Class constructor defines the basic attributes and callbacks

        Attributes:
          transport (CryptoTransportLayer): Transport layer
              for messaging between nodes.
          dht (DHT): For storage across the network (distributed hash table).
          market_id (int): Indicates which local market we're working with.
          peers: Active peers/nodes on the P2P network
          db: Database ORM handler
          orders: Orders for goods from database
          pages:
          mypage:
          signature: Digitial signature
          nickname: Short name of the node - a synonym for GUID
          log: Log handler
          settings: Local settings
          gpg: Public PGP key class
        """

        # Current
        self.transport = transport
        self.dht = transport.dht
        self.market_id = transport.market_id
        self.peers = self.dht.getActivePeers()
        self.db = db
        self.orders = Orders(transport, self.market_id, db)
        self.pages = {}
        self.mypage = None
        self.signature = None
        self.nickname = ""
        self.log = logging.getLogger(
            "[%s] %s" % (self.market_id, self.__class__.__name__))
        self.settings = self.transport.settings
        self.gpg = gnupg.GPG()

        self.all_messages = (
            'query_myorders',
            'peer',
            'query_page',
            'query_listings',
            'negotiate_pubkey',
            'response_pubkey'
        )

        # Register callbacks for incoming events
        self.transport.add_callbacks([
            (
                msg,
                {
                    'cb': getattr(self, 'on_%s' % msg),
                    'validator_cb': getattr(self, 'validate_on_%s' % msg)
                }
            )
            for msg in self.all_messages
        ])

        self.nickname = self.settings.get('nickname', '')

        # Periodically refresh buckets
        loop = tornado.ioloop.IOLoop.instance()
        refreshCB = tornado.ioloop.PeriodicCallback(self.dht._refreshNode,
                                                    constants.refreshTimeout,
                                                    io_loop=loop)
        refreshCB.start()

    def disable_welcome_screen(self):
        """This just flags the welcome screen to not show on startup"""
        self.db.updateEntries(
            "settings",
            {"welcome": "disable"},
            {'market_id': self.transport.market_id}
        )

    def private_key(self):
        """Returns private key for local node"""
        return self.settings['secret']

    def on_listing_results(self, results):
        """Add incoming information to log"""
        self.log.debug("Listings %s", results)

    @staticmethod
    def process_contract_image(image):
        """Get image from web client for use on server side"""
        uri = DataURI(image)
        imageData = uri.data
        # mime_type = uri.mimetype
        charset = uri.charset

        image = Image.open(StringIO(imageData))
        croppedImage = ImageOps.fit(image, (200, 200), centering=(0.5, 0.5))
        data = StringIO()
        croppedImage.save(data, format='PNG')
        new_uri = DataURI.make(
            'image/png',
            charset=charset,
            base64=True,
            data=data.getvalue())
        data.close()

        return new_uri

    @staticmethod
    def get_contract_id():
        """Choice of number of new contract to prevent guessing the sequence of contract' id.
           Other members not to be able to extract order volume from peers by viewing the latest order id.

        """
        return random.randint(0, 1000000)

    @staticmethod
    def linebreak_signing_data(data):
        """For signing with gpg, the width of the text is formatted 52 characters long"""
        json_string = json.dumps(data, indent=0)
        seg_len = 52
        out_text = "\n".join(
            json_string[x:x + seg_len]
            for x in range(0, len(json_string), seg_len)
        )
        return out_text

    @staticmethod
    def generate_contract_key(signed_contract):
        """Generate digest of digital signature or digest key"""
        contract_hash = hashlib.sha1(str(signed_contract)).hexdigest()
        hash_value = hashlib.new('ripemd160')
        hash_value.update(contract_hash)
        return hash_value.hexdigest()

    def save_contract_to_db(self, contract_id, body, signed_body, key):
        """Insert contract to database"""
        self.db.insertEntry(
            "contracts",
            {
                "id": contract_id,
                "market_id": self.transport.market_id,
                "contract_body": json.dumps(body),
                "signed_contract_body": str(signed_body),
                "state": "seed",
                "deleted": 0,
                "key": key
            }
        )

    def update_keywords_on_network(self, key, keywords):
        """Update keyword for sharing it with nodes"""
        for keyword in keywords:
            keyword = keyword.upper()
            hash_value = hashlib.new('ripemd160')
            keyword_key = 'keyword-%s' % keyword
            hash_value.update(keyword_key.encode('utf-8'))
            keyword_key = hash_value.hexdigest()

            self.log.debug("Sending keyword to network: %s", keyword_key)

            self.transport.store(
                keyword_key,
                json.dumps({
                    'keyword_index_add': {
                        "guid": self.transport.guid,
                        "key": key
                    }
                }),
                self.transport.guid
            )

    def save_contract(self, msg):
        """Sign, store contract in the database and update the keyword in the
        network
        """
        contract_id = self.get_contract_id()

        # Refresh market settings
        self.settings = self.get_settings()

        seller = msg['Seller']
        seller['seller_PGP'] = self.gpg.export_keys(self.settings['PGPPubkeyFingerprint'])
        seller['seller_BTC_uncompressed_pubkey'] = self.settings['btc_pubkey']
        seller['seller_GUID'] = self.settings['guid']
        seller['seller_Bitmessage'] = self.settings['bitmessage']

        # Process and crop thumbs for images
        if 'item_images' in msg['Contract']:
            if 'image1' in msg['Contract']['item_images']:
                img = msg['Contract']['item_images']['image1']
                new_uri = self.process_contract_image(img)
                msg['Contract']['item_images'] = new_uri
        else:
            self.log.debug('No image for contract')

        # Line break the signing data
        out_text = self.linebreak_signing_data(msg)

        # Sign the contract
        signed_data = self.gpg.sign(
            out_text,
            passphrase='P@ssw0rd',
            keyid=self.settings.get('PGPPubkeyFingerprint'))

        # Save contract to DHT
        contract_key = self.generate_contract_key(signed_data)

        # Store contract in database
        self.save_contract_to_db(contract_id, msg, signed_data, contract_key)

        # Store listing
        self.transport.store(
            contract_key,
            str(signed_data),
            self.transport.guid)

        self.update_listings_index()

        # If keywords are present
        keywords = msg['Contract']['item_keywords']
        self.update_keywords_on_network(contract_key, keywords)

    def shipping_address(self):
        """Get shipping address"""
        settings = self.get_settings()
        shipping_address = {
            'recipient_name': settings.get('recipient_name'),
            'street1': settings.get('street1'),
            'street2': settings.get('street2'),
            'city': settings.get('city'),
            'stateRegion': settings.get('stateRegion'),
            'stateProvinceRegion': settings.get('stateProvinceRegion'),
            'zip': settings.get('zip'),
            'country': settings.get('country'),
            'countryCode': settings.get('countryCode'),
        }
        return shipping_address

    def add_trusted_notary(self, guid, nickname=""):
        """Add selected trusted notary to the local list"""
        self.log.debug("%s %s", guid, nickname)
        notaries = self.settings.get('notaries')

        self.log.debug("notaries: %s", notaries)
        if notaries == "" or notaries == []:
            notaries = []
        else:
            notaries = json.loads(notaries)

        for notary in notaries:
            self.log.info(notary)
            if notary.get('guid') == guid:
                if notary.get('nickname') != nickname:
                    notary['nickname'] = nickname
                    notary['idx'] = notary
                    self.settings['notaries'] = notaries
                return

        notaries.append({"guid": guid, "nickname": nickname})
        self.settings['notaries'] = json.dumps(notaries)

        if 'btc_pubkey' in self.settings:
            del self.settings['btc_pubkey']

        self.db.updateEntries(
            "settings",
            self.settings,
            {'market_id': self.transport.market_id}
        )

    def _decode_list(self, data):
        """Receives data and decode to list"""
        rv = []
        for item in data:
            if isinstance(item, unicode):
                item = item.encode('utf-8')
            elif isinstance(item, list):
                item = self._decode_list(item)
            elif isinstance(item, dict):
                item = self._decode_dict(item)
            rv.append(item)
        return rv

    def _decode_dict(self, data):
        """so this receives a dictionary:

        iterates over the key,value pairs...
        if the key is unicode, it re-encodes the key in utf.
        if the value is unicode, it re-encodes it utf-8
        else if the value is a list, it calls a decode_list method
        if the value is a dict, it does a recursive call
        then updates the dictionary as it iterates

        """
        rv = {}
        for key, value in data.iteritems():
            if isinstance(key, unicode):
                key = key.encode('utf-8')
            if isinstance(value, unicode):
                value = value.encode('utf-8')
            elif isinstance(value, list):
                value = self._decode_list(value)
            elif isinstance(value, dict):
                value = self._decode_dict(value)
            rv[key] = value
        return rv

    def remove_trusted_notary(self, guid):
        """Not trusted to selected notary. Dlete notary from the local list"""
        notaries = self.settings.get('notaries')
        notaries = ast.literal_eval(notaries)

        for idx, notary in enumerate(notaries):

            if notary.get('guid') == guid:
                del notaries[idx]

        self.settings['notaries'] = json.dumps(notaries)

        self.db.updateEntries(
            "settings",
            self.settings,
            {'market_id': self.transport.market_id}
        )

    def republish_contracts(self):
        """Update information about contracts in the network"""
        listings = self.db.selectEntries("contracts", {"deleted": 0})
        for listing in listings:
            self.transport.store(
                listing['key'],
                listing.get('signed_contract_body'),
                self.transport.guid
            )

            # Push keyword index out again
            contract = listing.get('Contract')
            keywords = contract.get('item_keywords') if contract is not None else []

            t3 = Thread(target=self.update_keywords_on_network, args=(listing.get('key'), keywords,))
            t3.start()

        # Updating the DHT index of your store's listings
        self.update_listings_index()

    def get_notaries(self, online_only=False):
        """Getting notaries and exchange contact in network"""
        self.log.debug('Getting notaries')
        notaries = []
        settings = self.get_settings()

        # Untested code
        if online_only:
            for n in settings['notaries']:
                peer = self.dht.routingTable.getContact(n.guid)
                if peer is not None:
                    peer.start_handshake()
                    notaries.append(n)
            return notaries
        # End of untested code

        return settings['notaries']

    @staticmethod
    def valid_guid(guid):
        """Checking guid - global user ID secure hash of the public key"""
        return len(guid) == 40 and int(guid, 16)

    def republish_listing(self, msg):
        """Update information about products in the network"""
        listing_id = msg.get('productID')
        listing = self.db.selectEntries("products", {"id": listing_id})

        if listing:
            listing = listing[0]
        else:
            return

        listing_key = listing['key']

        self.transport.store(
            listing_key,
            listing.get('signed_contract_body'),
            self.transport.guid
        )
        # Updating the DHT index of your store's listings
        self.update_listings_index()

    def update_listings_index(self):
        """This method is responsible for updating the DHT index of your
           store's listings. There is a dictionary in the DHT that has an
           array of your listing IDs. This updates that listing index in
           the DHT, simply put.

        """
        # Store to marketplace listing index
        contract_index_key = hashlib.sha1('contracts-%s' %
                                          self.transport.guid).hexdigest()
        hashvalue = hashlib.new('ripemd160')
        hashvalue.update(contract_index_key)
        contract_index_key = hashvalue.hexdigest()

        # Calculate index of contracts
        contract_ids = self.db.selectEntries(
            "contracts",
            {"market_id": self.transport.market_id, "deleted": 0}
        )
        my_contracts = []
        for contract_id in contract_ids:
            my_contracts.append(contract_id['key'])

        self.log.debug("My Contracts: %s", my_contracts)

        # Sign listing index for validation and tamper resistance
        data_string = str({'guid': self.transport.guid,
                           'contracts': my_contracts})
        cryptor = Cryptor(privkey_hex=self.transport.settings['secret'])
        signature = cryptor.sign(data_string)

        value = {
            'signature': signature.encode('hex'),
            'data': {
                'guid': self.transport.guid,
                'contracts': my_contracts
            }
        }

        # Pass off to thread to keep GUI snappy
        t = Thread(
            target=self.transport.store,
            args=(
                contract_index_key,
                value,
                self.transport.guid,
                )
            )
        t.start()

    def remove_contract(self, msg):
        """Remove contract and update own list of contracts keywords"""
        self.log.info("Removing contract: %s", msg)

        # Remove from DHT keyword indices
        self.remove_from_keyword_indexes(msg['contract_id'])

        self.db.updateEntries(
            "contracts",
            {"deleted": 1},
            {"id": msg["contract_id"]}
        )
        # Updating the DHT index of your store's listings
        self.update_listings_index()

    def remove_from_keyword_indexes(self, contract_id):
        """Remove from DHT keyword indices"""
        contract = self.db.selectEntries("contracts", {"id": contract_id})[0]
        contract_key = contract['key']

        contract = json.loads(contract['contract_body'])
        contract_keywords = contract['Contract']['item_keywords']

        for keyword in contract_keywords:
            # Remove keyword from index
            hash_value = hashlib.new('ripemd160')
            keyword_key = 'keyword-%s' % keyword
            hash_value.update(keyword_key.encode('utf-8'))
            keyword_key = hash_value.hexdigest()

            self.transport.store(
                keyword_key,
                json.dumps({
                    'keyword_index_remove': {
                        "guid": self.transport.guid,
                        "key": contract_key
                    }
                }),
                self.transport.guid
            )

    def get_messages(self):
        """Get messages listing for market"""
        self.log.info(
            "Listing messages for market: %s", self.transport.market_id)
        settings = self.get_settings()
        try:
            # Request all messages for our address
            inboxmsgs = json.loads(
                self.transport.bitmessage_api.getInboxMessagesByReceiver(
                    settings['bitmessage']))
            for m in inboxmsgs['inboxMessages']:
                # Base64 decode subject and content
                m['subject'] = b64decode(m['subject'])
                m['message'] = b64decode(m['message'])
                # TODO: Augment with market, if available

            return {"messages": inboxmsgs}
        except Exception as e:
            self.log.error("Failed to get inbox messages: {}".format(e))
            self.log.error(traceback.format_exc())
            return {}

    def send_message(self, msg):
        """Send message for market by bitmessage"""
        self.log.info(
            "Sending message for market: %s", self.transport.market_id)
        settings = self.get_settings()
        try:
            # Base64 decode subject and content
            self.log.info("Encoding message: {}".format(msg))
            subject = b64encode(msg['subject'])
            body = b64encode(msg['body'])
            result = self.transport.bitmessage_api.sendMessage(
                msg['to'], settings['bitmessage'], subject, body
            )
            self.log.info("Send message result: {}".format(result))
            return {}
        except Exception as e:
            self.log.error("Failed to send message: %s", e)
            self.log.error(traceback.format_exc())
            return {}

    def get_contracts(self, page=0):
        """Select contracts for market from database"""
        self.log.info(
            "Getting contracts for market: %s", self.transport.market_id)
        contracts = self.db.selectEntries(
            "contracts",
            {"market_id": self.transport.market_id, "deleted": 0},
            limit=10,
            limit_offset=(page * 10)
        )
        my_contracts = []
        for contract in contracts:
            try:
                contract_body = json.loads(u"%s" % contract['contract_body'])
            except (KeyError, ValueError) as e:
                self.log.error('Problem loading the contract body JSON: %s',
                               e.message)
                continue
            try:
                contract_field = contract_body['Contract']
            except KeyError:
                self.log.error('Contract field not found in contract_body')
                continue
            except TypeError:
                self.log.error('Malformed contract_body: %s',
                               str(contract_body))
                continue
            item_price = contract_field.get('item_price')
            if item_price is None or item_price < 0:
                item_price = 0
            try:
                item_delivery = contract_field['item_delivery']
            except KeyError:
                self.log.error('item_delivery not found in Contract field')
                continue
            except TypeError:
                self.log.error('Malformed Contract field: %s',
                               str(contract_field))
                continue
            shipping_price = item_delivery.get('shipping_price')
            if shipping_price is None or shipping_price < 0:
                shipping_price = 0

            my_contracts.append({
                'key': contract.get('key', ''),
                'id': contract.get('id', ''),
                'item_images': contract_field.get('item_images'),
                'signed_contract_body': contract.get('signed_contract_body', ''),
                'contract_body': contract_body,
                'unit_price': item_price,
                'deleted': contract.get('deleted'),
                'shipping_price': shipping_price,
                'item_title': contract_field.get('item_title'),
                'item_desc': contract_field.get('item_desc'),
                'item_condition': contract_field.get('item_condition'),
                'item_quantity_available': contract_field.get('item_quantity'),
            })

        return {
            "contracts": my_contracts, "page": page,
            "total_contracts": len(
                self.db.selectEntries("contracts", {"deleted": "0"}))}

    def undo_remove_contract(self, contract_id):
        """Restore removed contract"""
        self.log.info("Undo remove contract: %s", contract_id)
        self.db.updateEntries(
            "contracts",
            {"deleted": "0"},
            {"market_id": self.transport.market_id.replace("'", "''"), "id": contract_id}
        )

    def save_settings(self, msg):
        """Update local settings"""

        # Check for any updates to arbiter or notary status to push to the DHT
        if 'notary' in msg:
            # Generate notary index key
            hash_value = hashlib.new('ripemd160')
            hash_value.update('notary-index')
            key = hash_value.hexdigest()

            if msg['notary'] is True:
                self.log.info('Letting the network know you are now a notary')
                data = json.dumps({'notary_index_add': self.transport.guid})
                self.transport.store(key, data, self.transport.guid)
            else:
                self.log.info('Letting the network know you are not a notary')
                data = json.dumps({'notary_index_remove': self.transport.guid})
                self.transport.store(key, data, self.transport.guid)

        # Update nickname
        self.transport.nickname = msg['nickname']

        if 'burnAmount' in msg:
            del msg['burnAmount']
        if 'burnAddr' in msg:
            del msg['burnAddr']

        # Update local settings
        self.db.updateEntries(
            "settings",
            msg,
            {'market_id': self.transport.market_id}
        )

    def get_settings(self):
        """Get local settings"""

        self.log.debug(
            "Getting settings info for Market %s", self.transport.market_id)
        settings = self.db.getOrCreate(
            "settings",
            {"market_id": self.transport.market_id})

        if settings['arbiter'] == 1:
            settings['arbiter'] = True
        if settings['notary'] == 1:
            settings['notary'] = True

        for key in ('notaries', 'trustedArbiters'):
            # Fix key not found, None and empty string
            value = settings.get(key) or '[]'
            settings[key] = ast.literal_eval(value)

        if 'secret' not in settings:
            settings['privkey'] = ''

        settings['btc_pubkey'] = privkey_to_pubkey(settings.get('privkey'))
        settings['secret'] = settings.get('secret')

        if settings:
            return settings
        else:
            return {}

    def query_page(self, find_guid, callback=lambda msg: None):
        """Query network for node"""
        self.log.info("Searching network for node: %s", find_guid)
        msg = query_page(find_guid)
        msg['uri'] = self.transport.uri
        msg['senderGUID'] = self.transport.guid
        msg['sin'] = self.transport.sin
        msg['pubkey'] = self.transport.pubkey

        self.transport.send(msg, find_guid, callback)

    def validate_on_query_page(self, *data):
        self.log.debug('Validating on query page message.')
        keys = ("senderGUID", "uri", "pubkey", "senderNick")
        return all(k in data[0] for k in keys)

    def on_query_page(self, peer):
        """Return your page info if someone requests it on the network"""
        self.log.info("Someone is querying for your page")
        settings = self.get_settings()

        new_peer = self.transport.get_crypto_peer(
            peer['senderGUID'],
            peer['uri'],
            pubkey=peer['pubkey'],
            nickname=peer['senderNick']
        )

        def send_page_query():
            """Send a request for the local identity page"""
            new_peer.start_handshake()

            new_peer.send(proto_page(
                self.transport.uri,
                self.transport.pubkey,
                self.transport.guid,
                settings['storeDescription'],
                self.signature,
                settings['nickname'],
                settings.get('PGPPubKey', ''),
                settings.get('email', ''),
                settings.get('bitmessage', ''),
                settings.get('arbiter', ''),
                settings.get('notary', ''),
                settings.get('arbiterDescription', ''),
                self.transport.sin))

        send_page_query()

    def validate_on_query_myorders(self, *data):
        self.log.debug('Validating on query myorders message.')
        return True

    def on_query_myorders(self, peer):
        """Run if someone is querying for your page"""
        self.log.debug("Someone is querying for your page: %s", peer)

    def validate_on_query_listings(self, *data):
        self.log.debug('Validating on query listings message.')
        return "senderGUID" in data[0]

    def on_query_listings(self, peer, page=0):
        """Run if someone is querying your listings"""
        self.log.info("Someone is querying your listings: %s", peer)
        contracts = self.get_contracts(page)

        if len(contracts['contracts']) == 0:
            self.transport.send(
                {"type": "no_listing_result"},
                peer['senderGUID'])
            return
        else:
            for contract in contracts['contracts']:
                contract = contract
                contract['type'] = "listing_result"
                self.transport.send(contract, peer['senderGUID'])

    def validate_on_peer(self, *data):
        self.log.debug('Validating on peer message.')
        return True

    def on_peer(self, peer):
        pass

    def validate_on_negotiate_pubkey(self, *data):
        self.log.debug('Validating on negotiate pubkey message.')
        keys = ("nickname", "ident_pubkey")
        return all(k in data[0] for k in keys)

    def on_negotiate_pubkey(self, ident_pubkey):
        """Run if someone is asking for your real pubKey"""
        self.log.info("Someone is asking for your real pubKey")
        nickname = ident_pubkey['nickname']
        ident_pubkey = ident_pubkey['ident_pubkey'].decode("hex")
        self.transport.respond_pubkey_if_mine(nickname, ident_pubkey)

    def validate_on_response_pubkey(self, *data):
        self.log.debug('Validating on response pubkey message.')
        keys = ("pubkey", "nickname", "signature")
        return all(k in data[0] for k in keys)

    def on_response_pubkey(self, response):
        """Deprecated. This is a DarkMarket holdover.
           Run to verify signature if someone send you the pubKey.
        """
        pubkey = response["pubkey"].decode("hex")
        # signature = response["signature"].decode("hex")
        nickname = response["nickname"]
        # Cache mapping for later.
        if nickname not in self.transport.nick_mapping:
            self.transport.nick_mapping[nickname] = [None, pubkey]
        # Verify signature here...
        # Add to our dict.
        self.transport.nick_mapping[nickname][1] = pubkey
        self.log.info("[market] mappings: ###############")
        for key, value in self.transport.nick_mapping.iteritems():
            self.log.info(
                "'%s' -> '%s' (%s)",
                key, value[1].encode("hex") if value[1] is not None else value[1],
                value[0].encode("hex") if value[0] is not None else value[0])
        self.log.info("##################################")

    def release_funds_to_merchant(self, buyer_order_id, tx, script, signatures, guid):
        """Send TX to merchant"""
        self.log.debug("Release funds to merchant: %s %s %s %s", buyer_order_id, tx, signatures, guid)
        self.transport.send(
            {
                'type': 'release_funds_tx',
                'tx': tx,
                'script': script,
                'buyer_order_id': buyer_order_id,
                'signatures': signatures
            },
            guid
        )
        self.log.debug('TX sent to merchant')
