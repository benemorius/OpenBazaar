from multisig import Multisig
import StringIO
import gnupg
import hashlib
import json
import logging
import qrcode
import random
import time
import urllib
from bitcoin import mk_multisig_script, privkey_to_pubkey, scriptaddr

from decimal import Decimal
import trust


class Orders(object):
    class State(object):
        """Enum inner class. Python introduces enums in Python 3.0, but this should be good enough"""
        SENT = 'Sent'
        ACCEPTED = 'accepted'
        BID = 'bid'
        BUYER_PAID = 'Buyer Paid'
        NEED_TO_PAY = 'Need to Pay'
        NEW = 'new'
        NOTARIZED = 'Notarized'
        PAID = 'Paid'
        RECEIVED = 'received'
        SHIPPED = 'Shipped'
        WAITING_FOR_PAYMENT = 'Waiting for Payment'

    def __init__(self, transport, market_id, db):
        self.transport = transport
        self.market_id = market_id
        self.log = logging.getLogger('[%s] %s' % (self.market_id, self.__class__.__name__))
        self.gpg = gnupg.GPG()
        self.db = db
        self.orders = self.get_orders()

        self.transport.add_callbacks([
            (
                'order',
                {
                    'cb': getattr(self, 'on_order'),
                    'validator_cb': getattr(self, 'validate_on_order')
                }
            )
        ])

    def validate_on_order(self, *data):
        self.log.debug('Validating on order message.')
        return True

    def on_order(self, msg):

        state = msg.get('state')

        if state == self.State.NEW:
            self.new_order(msg)

        if state == self.State.BID:
            self.handle_bid_order(msg)

        if state == self.State.NOTARIZED:
            self.log.info('You received a notarized contract')
            self.handle_notarized_order(msg)

        if state == self.State.PAID:
            self.log.info('You received a payment notification')
            self.handle_paid_order(msg)

        if state == self.State.SHIPPED:
            self.log.info('You received a shipping notification')
            self.handle_shipped_order(msg)

    @staticmethod
    def get_offer_json(raw_contract, state):

        if state == Orders.State.SENT:
            offer_data = ''.join(raw_contract.split('\n')[5:])
            sig_index = offer_data.find('- -----BEGIN PGP SIGNATURE-----', 0, len(offer_data))
            offer_data_json = offer_data[0:sig_index]
            return json.loads(offer_data_json)

        if state in [Orders.State.WAITING_FOR_PAYMENT,
                     Orders.State.NOTARIZED,
                     Orders.State.NEED_TO_PAY,
                     Orders.State.PAID,
                     Orders.State.BUYER_PAID,
                     Orders.State.SHIPPED]:
            start_line = 8
        else:
            start_line = 4

        offer_data = ''.join(raw_contract.split('\n')[start_line:])

        if state in [Orders.State.NOTARIZED,
                     Orders.State.NEED_TO_PAY,
                     Orders.State.PAID,
                     Orders.State.BUYER_PAID,
                     Orders.State.SHIPPED]:
            index_of_seller_signature = offer_data.find('- -----BEGIN PGP SIGNATURE-----', 0, len(offer_data))
        else:
            index_of_seller_signature = offer_data.find('-----BEGIN PGP SIGNATURE-----', 0, len(offer_data))

        if state in (Orders.State.NEED_TO_PAY,
                     Orders.State.NOTARIZED,
                     Orders.State.BUYER_PAID,
                     Orders.State.PAID,
                     Orders.State.SHIPPED):
            offer_data_json = offer_data[0:index_of_seller_signature - 2]
            offer_data_json = json.loads(offer_data_json)
        elif state in Orders.State.WAITING_FOR_PAYMENT:
            offer_data_json = offer_data[0:index_of_seller_signature - 4]
            offer_data_json = json.loads(str(offer_data_json))
        else:
            offer_data_json = '{"Seller": {' + offer_data[0:index_of_seller_signature - 2]
            offer_data_json = json.loads(str(offer_data_json))

        return offer_data_json

    @staticmethod
    def get_buyer_json(raw_contract, state):

        if state in [Orders.State.NOTARIZED, Orders.State.NEED_TO_PAY]:
            start_line = 8
        else:
            start_line = 6
        offer_data = ''.join(raw_contract.split('\n')[start_line:])
        index_of_seller_signature = offer_data.find('-----BEGIN PGP SIGNATURE-----', 0, len(offer_data))

        # Find Buyer Data in Contract
        bid_data_index = offer_data.find('"Buyer"', index_of_seller_signature, len(offer_data))
        if state in [Orders.State.SENT]:
            end_of_bid_index = offer_data.find('-----BEGIN PGP SIGNATURE', bid_data_index, len(offer_data))
        else:
            end_of_bid_index = offer_data.find('- -----BEGIN PGP SIGNATURE', bid_data_index, len(offer_data))

        buyer_data_json = "{" + offer_data[bid_data_index:end_of_bid_index]
        buyer_data_json = json.loads(buyer_data_json)

        return buyer_data_json

    @staticmethod
    def get_notary_json(raw_contract, state):

        if state in [Orders.State.NOTARIZED, Orders.State.NEED_TO_PAY]:
            start_line = 8
        else:
            start_line = 6
        offer_data = ''.join(raw_contract.split('\n')[start_line:])
        index_of_seller_signature = offer_data.find('-----BEGIN PGP SIGNATURE-----', 0, len(offer_data))

        # Find Buyer Data in Contract
        bid_data_index = offer_data.find('"Buyer"', index_of_seller_signature, len(offer_data))
        end_of_bid_index = offer_data.find('- -----BEGIN PGP SIGNATURE', bid_data_index, len(offer_data))

        # Find Notary Data in Contract
        notary_data_index = offer_data.find('"Notary"', end_of_bid_index, len(offer_data))
        end_of_notary_index = offer_data.find('-----BEGIN PGP SIGNATURE', notary_data_index, len(offer_data))
        notary_data_json = "{" + offer_data[notary_data_index:end_of_notary_index]

        notary_data_json = json.loads(notary_data_json)

        return notary_data_json

    @staticmethod
    def get_qr_code(item_title, address, total):
        if isinstance(item_title, unicode):
            item_title = item_title.encode('utf-8', 'ignore')
        qr_url = urllib.urlencode({"url": item_title})
        qr = qrcode.make("bitcoin:" + address + "?amount=" + str(total) + "&message=" + qr_url)
        output = StringIO.StringIO()
        qr.save(output, "PNG")
        qr = output.getvalue().encode("base64")
        output.close()
        return qr

    def get_order(self, order_id, by_buyer_id=False):

        if not by_buyer_id:
            _order = self.db.selectEntries("orders", {"order_id": order_id})[0]
        else:
            _order = self.db.selectEntries("orders", {"buyer_order_id": order_id})[0]
        total_price = 0

        offer_data_json = self.get_offer_json(_order['signed_contract_body'], _order['state'])
        buyer_data_json = self.get_buyer_json(_order['signed_contract_body'], _order['state'])

        if _order['state'] != Orders.State.SENT:
            notary_json = self.get_notary_json(_order['signed_contract_body'], _order['state'])
            notary = notary_json['Notary']['notary_GUID']
        else:
            notary = ""

        if _order['state'] in (Orders.State.NEED_TO_PAY,
                               Orders.State.NOTARIZED,
                               Orders.State.WAITING_FOR_PAYMENT,
                               Orders.State.PAID,
                               Orders.State.BUYER_PAID,
                               Orders.State.SHIPPED):

            def cb(total):
                if self.transport.handler is not None:
                    self.transport.handler.send_to_client(None, {"type": "order_payment_amount",
                                                                 "value": total})

            pubkeys = [
                offer_data_json['Seller']['seller_BTC_uncompressed_pubkey'],
                buyer_data_json['Buyer']['buyer_BTC_uncompressed_pubkey'],
                notary_json['Notary']['notary_BTC_uncompressed_pubkey']
            ]

            script = mk_multisig_script(pubkeys, 2, 3)
            payment_address = scriptaddr(script)

            trust.get_unspent(payment_address, cb)

            if 'shipping_price' in _order:
                shipping_price = _order['shipping_price'] if _order['shipping_price'] != '' else 0
            else:
                shipping_price = 0

            try:
                total_price = str((Decimal(shipping_price) + Decimal(_order['item_price']))) \
                    if 'item_price' in _order else _order['item_price']
            except Exception as e:
                self.log.error('Probably not a number %s', e)

        # Generate QR code
        qr = self.get_qr_code(offer_data_json['Contract']['item_title'], _order['address'], total_price)
        merchant_bitmessage = offer_data_json.get('Seller', '').get('seller_Bitmessage')
        buyer_bitmessage = buyer_data_json.get('Buyer', '').get('buyer_Bitmessage')

        # Get order prototype object before storing
        order = {"id": _order['id'],
                 "state": _order.get('state'),
                 "address": _order.get('address'),
                 "buyer": _order.get('buyer'),
                 "merchant": _order.get('merchant'),
                 "order_id": _order.get('order_id'),
                 "item_price": _order.get('item_price'),
                 "shipping_price": _order.get('shipping_price'),
                 "shipping_address": str(_order.get('shipping_address')),
                 "total_price": total_price,
                 "merchant_bitmessage": merchant_bitmessage,
                 "buyer_bitmessage": buyer_bitmessage,
                 "notary": notary,
                 "payment_address": _order.get('payment_address'),
                 "payment_address_amount": _order.get('payment_address_amount'),
                 "qrcode": 'data:image/png;base64,' + qr,
                 "item_title": offer_data_json['Contract']['item_title'],
                 "signed_contract_body": _order.get('signed_contract_body'),
                 "note_for_merchant": _order.get('note_for_merchant'),
                 "updated": _order.get('updated')}

        if 'item_images' in offer_data_json['Contract'] and offer_data_json['Contract']['item_images'] != {}:
            order['item_image'] = offer_data_json['Contract']['item_images']
        else:
            order['item_image'] = "img/no-photo.png"

        self.log.datadump('FULL ORDER: %s', order)

        return order

    def get_orders(self, page=0, merchant=None, notarizations=False):

        orders = []

        if merchant is None:
            if notarizations:
                self.log.info('Retrieving notarizations')
                order_ids = self.db.selectEntries(
                    "orders",
                    {"market_id": self.market_id},
                    order_field="updated",
                    order="DESC",
                    limit=10,
                    limit_offset=page * 10,
                    select_fields=['order_id']
                )
                for result in order_ids:
                    if result['merchant'] != self.transport.guid and result['buyer'] != self.transport.guid:
                        order = self.get_order(result['order_id'])
                        orders.append(order)
                all_orders = self.db.selectEntries(
                    "orders",
                    {"market_id": self.market_id}
                )
                total_orders = 0
                for order in all_orders:
                    if order['merchant'] != self.transport.guid and order['buyer'] != self.transport.guid:
                        total_orders += 1
            else:
                order_ids = self.db.selectEntries(
                    "orders",
                    {"market_id": self.market_id},
                    order_field="updated",
                    order="DESC",
                    limit=10,
                    limit_offset=page * 10,
                    select_fields=['order_id']
                )
                for result in order_ids:
                    order = self.get_order(result['order_id'])
                    orders.append(order)
                total_orders = len(self.db.selectEntries(
                    "orders",
                    {"market_id": self.market_id}
                ))
        else:
            if merchant:
                order_ids = self.db.selectEntries(
                    "orders",
                    {"market_id": self.market_id,
                     "merchant": self.transport.guid},
                    order_field="updated",
                    order="DESC",
                    limit=10,
                    limit_offset=page * 10,
                    select_fields=['order_id']
                )
                for result in order_ids:
                    order = self.get_order(result['order_id'])
                    orders.append(order)

                all_orders = self.db.selectEntries(
                    "orders",
                    {"market_id": self.market_id}
                )
                total_orders = 0
                for order in all_orders:
                    if order['merchant'] == self.transport.guid:
                        total_orders += 1
            else:
                order_ids = self.db.selectEntries(
                    "orders",
                    {"market_id": self.market_id},
                    order_field="updated",
                    order="DESC", limit=10,
                    limit_offset=page * 10
                )
                for result in order_ids:
                    if result['buyer'] == self.transport.guid:
                        order = self.get_order(result['order_id'])
                        orders.append(order)

                all_orders = self.db.selectEntries(
                    "orders",
                    {"market_id": self.market_id}
                )
                total_orders = 0
                for order in all_orders:
                    if order['buyer'] == self.transport.guid:
                        total_orders += 1

        for order in orders:

            buyer = self.db.selectEntries("peers", {"guid": order['buyer']})
            if len(buyer) > 0:
                order['buyer_nickname'] = buyer[0]['nickname']
            merchant = self.db.selectEntries("peers", {"guid": order['merchant']})
            if len(merchant) > 0:
                order['merchant_nickname'] = merchant[0]['nickname']

        return {"total": total_orders, "orders": orders}

    def ship_order(self, order, order_id, payment_address):
        self.log.info('Shipping order')

        del order['qrcode']
        del order['item_image']
        del order['total_price']
        del order['item_title']
        del order['buyer_bitmessage']
        del order['merchant_bitmessage']
        del order['payment_address_amount']

        order['state'] = Orders.State.SHIPPED
        order['payment_address'] = payment_address
        self.db.updateEntries("orders", order, {"order_id": order_id})

        order['type'] = 'order'
        order['payment_address'] = payment_address

        # Find Seller Data in Contract
        offer_data = ''.join(order['signed_contract_body'].split('\n')[8:])
        index_of_seller_signature = offer_data.find('- - -----BEGIN PGP SIGNATURE-----', 0, len(offer_data))
        offer_data_json = offer_data[0:index_of_seller_signature]
        self.log.info('Offer Data: %s', offer_data_json)
        offer_data_json = json.loads(str(offer_data_json))

        # Find Buyer Data in Contract
        self.log.info(offer_data)
        bid_data_index = offer_data.find('"Buyer"', index_of_seller_signature, len(offer_data))
        end_of_bid_index = offer_data.find('- -----BEGIN PGP SIGNATURE', bid_data_index, len(offer_data))
        bid_data_json = "{" + offer_data[bid_data_index:end_of_bid_index]
        bid_data_json = json.loads(bid_data_json)
        self.log.info('Bid Data: %s', bid_data_json)

        self.transport.send(order, bid_data_json['Buyer']['buyer_GUID'])

    def accept_order(self, new_order):

        # TODO: Need to have a check for the vendor to agree to the order

        new_order['state'] = Orders.State.ACCEPTED
        seller = new_order['seller']
        buyer = new_order['buyer']

        new_order['escrows'] = [new_order.get('escrows')[0]]
        escrow = new_order['escrows'][0]

        # Create 2 of 3 multisig address
        self._multisig = Multisig(None, 2, [seller, buyer, escrow])

        new_order['address'] = self._multisig.address

        if len(self.db.selectEntries("orders", {"order_id": new_order['id']})) > 0:
            self.db.updateEntries("orders", {new_order}, {"order_id": new_order['id']})
        else:
            self.db.insertEntry("orders", new_order)

        self.transport.send(new_order, new_order['buyer'].decode('hex'))

    def pay_order(self, new_order, order_id):  # action
        new_order['state'] = Orders.State.PAID

        self.log.debug(new_order)

        del new_order['qrcode']
        del new_order['item_image']
        del new_order['total_price']
        del new_order['item_title']
        del new_order['buyer_bitmessage']
        del new_order['merchant_bitmessage']
        del new_order['payment_address_amount']

        self.db.updateEntries("orders", new_order, {"order_id": order_id})

        new_order['type'] = 'order'

        self.transport.send(new_order, new_order['merchant'])

    def offer_json_from_seed_contract(self, seed_contract):
        self.log.debug('Seed Contract: %s', seed_contract)
        contract_data = ''.join(seed_contract.split('\n')[6:])
        index_of_signature = contract_data.find('- -----BEGIN PGP SIGNATURE-----', 0, len(contract_data))
        contract_data_json = contract_data[0:index_of_signature]
        self.log.debug('json %s', contract_data_json)
        return json.loads(contract_data_json)

    def send_order(self, order_id, contract, notary):  # action

        self.log.info('Verify Contract and Store in Orders Table')
        self.log.debug('%s', contract)
        contract_data_json = self.offer_json_from_seed_contract(contract)

        try:
            self.log.debug('%s', contract_data_json)
            seller_pgp = contract_data_json['Seller']['seller_PGP']
            self.gpg.import_keys(seller_pgp)
            v = self.gpg.verify(contract)

            if v:
                self.log.info('Verified Contract')
                self.log.info(self.get_shipping_address())
                try:
                    self.db.insertEntry(
                        "orders",
                        {
                            "order_id": order_id,
                            "state": "Sent",
                            "signed_contract_body": contract,
                            "market_id": self.market_id,
                            "shipping_address": json.dumps(self.get_shipping_address()),
                            "updated": time.time(),
                            "merchant": contract_data_json['Seller']['seller_GUID'],
                            "buyer": self.transport.guid
                        }
                    )
                except Exception as e:
                    self.log.error('Cannot update DB %s', e)

                order_to_notary = {}
                order_to_notary['type'] = 'order'
                order_to_notary['rawContract'] = contract
                order_to_notary['state'] = Orders.State.BID

                merchant = self.transport.dht.routingTable.getContact(
                    contract_data_json['Seller']['seller_GUID']
                )
                order_to_notary['merchantURI'] = merchant.address
                order_to_notary['merchantGUID'] = merchant.guid
                order_to_notary['merchantNickname'] = merchant.nickname
                order_to_notary['merchantPubkey'] = merchant.pub

                self.log.info('Sending order to %s', notary)

                # Send order to notary for approval
                self.transport.send(order_to_notary, notary)

            else:
                self.log.error('Could not verify signature of contract.')

        except Exception as e2:
            self.log.error(e2)

    def receive_order(self, new_order):  # action
        new_order['state'] = Orders.State.RECEIVED

        order_id = random.randint(0, 1000000)
        while len(self.db.selectEntries("orders", {'id': order_id})) > 0:
            order_id = random.randint(0, 1000000)

        new_order['order_id'] = order_id
        self.db.insertEntry("orders", new_order)
        self.transport.send(new_order, new_order['seller'].decode('hex'))

    def get_shipping_address(self):

        settings = self.transport.settings

        shipping_info = {
            "street1": settings.get('street1'),
            "street2": settings.get('street2'),
            "city": settings.get('city'),
            "stateRegion": settings.get('stateRegion'),
            "stateProvinceRegion": settings.get('stateProvinceRegion'),
            "zip": settings.get('zip'),
            "country": settings.get('country'),
            "countryCode": settings.get('countryCode'),
            "recipient_name": settings.get('recipient_name')
        }
        return shipping_info

    def new_order(self, msg):

        self.log.debug('New Order: %s', msg)

        # Save order locally in database
        order_id = random.randint(0, 1000000)
        while (len(self.db.selectEntries("orders", {"id": order_id}))) > 0:
            order_id = random.randint(0, 1000000)

        seller = self.transport.dht.routingTable.getContact(msg['sellerGUID'])

        buyer = {}
        buyer['Buyer'] = {}
        buyer['Buyer']['buyer_GUID'] = self.transport.guid
        buyer['Buyer']['buyer_BTC_uncompressed_pubkey'] = msg['btc_pubkey']
        buyer['Buyer']['buyer_pgp'] = self.transport.settings['PGPPubKey']
        buyer['Buyer']['buyer_Bitmessage'] = self.transport.settings['bitmessage']
        buyer['Buyer']['buyer_deliveryaddr'] = seller.encrypt(json.dumps(self.get_shipping_address())).encode(
            'hex')
        buyer['Buyer']['note_for_seller'] = msg['message']
        buyer['Buyer']['buyer_order_id'] = order_id

        # Add to contract and sign
        seed_contract = msg.get('rawContract')

        gpg = self.gpg

        # Prepare contract body
        json_string = json.dumps(buyer, indent=0)
        seg_len = 52
        out_text = "\n".join(
            json_string[x:x + seg_len]
            for x in range(0, len(json_string), seg_len)
        )

        # Append new data to contract
        out_text = "%s\n%s" % (seed_contract, out_text)

        signed_data = gpg.sign(out_text, passphrase='P@ssw0rd',
                               keyid=self.transport.settings.get('PGPPubkeyFingerprint'))

        self.log.debug('Double-signed Contract: %s', signed_data)

        # Hash the contract for storage
        contract_key = hashlib.sha1(str(signed_data)).hexdigest()
        hash_value = hashlib.new('ripemd160')
        hash_value.update(contract_key)
        contract_key = hash_value.hexdigest()

        self.db.updateEntries(
            "orders",
            {
                'market_id': self.transport.market_id,
                'contract_key': contract_key,
                'signed_contract_body': str(signed_data),
                'shipping_address': str(json.dumps(self.get_shipping_address())),
                'state': Orders.State.NEW,
                'updated': time.time(),
                'note_for_merchant': msg['message']
            },
            {
                'order_id': order_id
            }
        )

        # Send order to seller
        self.send_order(order_id, str(signed_data), msg['notary'])

    @staticmethod
    def get_seed_contract_from_doublesigned(contract):
        start_index = contract.find('- -----BEGIN PGP SIGNED MESSAGE-----', 0, len(contract))
        end_index = contract.find('- -----END PGP SIGNATURE-----', start_index, len(contract))
        contract = contract[start_index:end_index + 29]
        return contract

    def get_json_from_doublesigned_contract(self, contract):
        start_index = contract.find("{", 0, len(contract))
        end_index = contract.find('- -----BEGIN PGP SIGNATURE-----', 0, len(contract))
        self.log.info(contract[start_index:end_index])
        return json.loads("".join(contract[start_index:end_index].split('\n')))

    def handle_bid_order(self, bid):

        self.log.info('Bid Order: %s', bid)

        new_peer = self.transport.get_crypto_peer(bid.get('merchantGUID'),
                                                  bid.get('merchantURI'),
                                                  bid.get('merchantPubkey'))

        # Generate unique id for this bid
        order_id = random.randint(0, 1000000)
        while len(self.db.selectEntries("contracts", {"id": order_id})) > 0:
            order_id = random.randint(0, 1000000)

        # Add to contract and sign
        contract = bid.get('rawContract')

        contract_stripped = "".join(contract.split('\n'))

        bidder_pgp_start_index = contract_stripped.find("buyer_pgp", 0, len(contract_stripped))
        bidder_pgp_end_index = contract_stripped.find("buyer_GUID", 0, len(contract_stripped))
        bidder_pgp = contract_stripped[bidder_pgp_start_index + 13:bidder_pgp_end_index]

        self.gpg.import_keys(bidder_pgp)
        v = self.gpg.verify(contract)
        if v:
            self.log.info('Sellers contract verified')

        notary = {}
        notary['Notary'] = {
            'notary_GUID': self.transport.guid,
            'notary_BTC_uncompressed_pubkey': privkey_to_pubkey(self.transport.settings['privkey']),
            'notary_pgp': self.transport.settings['PGPPubKey'],
            'notary_fee': "1%",
            'notary_order_id': order_id
        }

        self.log.debug('Notary: %s', notary)

        gpg = self.gpg

        # Prepare contract body
        json_string = json.dumps(notary, indent=0)
        seg_len = 52
        out_text = "\n".join(
            json_string[x:x + seg_len]
            for x in range(0, len(json_string), seg_len)
        )

        # Append new data to contract
        out_text = "%s\n%s" % (contract, out_text)

        signed_data = gpg.sign(out_text, passphrase='P@ssw0rd',
                               keyid=self.transport.settings.get('PGPPubkeyFingerprint'))

        self.log.debug('Double-signed Contract: %s', signed_data)

        # Hash the contract for storage
        contract_key = hashlib.sha1(str(signed_data)).hexdigest()
        hash_value = hashlib.new('ripemd160')
        hash_value.update(contract_key)
        contract_key = hash_value.hexdigest()

        self.log.info('Order ID: %s', order_id)

        # Push buy order to DHT and node if available
        # self.transport.store(contract_key, str(signed_data), self.transport.guid)
        # self.update_listings_index()

        # Find Seller Data in Contract
        offer_data = ''.join(contract.split('\n')[8:])
        index_of_seller_signature = offer_data.find('- -----BEGIN PGP SIGNATURE-----', 0, len(offer_data))
        offer_data_json = "{\"Seller\": {" + offer_data[0:index_of_seller_signature]
        self.log.info('Offer Data: %s', offer_data_json)
        offer_data_json = json.loads(str(offer_data_json))

        # Find Buyer Data in Contract
        bid_data_index = offer_data.find('"Buyer"', index_of_seller_signature, len(offer_data))
        end_of_bid_index = offer_data.find('-----BEGIN PGP SIGNATURE', bid_data_index, len(offer_data))
        bid_data_json = "{" + offer_data[bid_data_index:end_of_bid_index]
        bid_data_json = json.loads(bid_data_json)
        self.log.info('Bid Data: %s', bid_data_json)

        buyer_order_id = "%s-%s" % (
            bid_data_json['Buyer']['buyer_GUID'],
            bid_data_json['Buyer']['buyer_order_id']
        )

        pubkeys = [
            offer_data_json['Seller']['seller_BTC_uncompressed_pubkey'],
            bid_data_json['Buyer']['buyer_BTC_uncompressed_pubkey'],
            privkey_to_pubkey(self.transport.settings['privkey'])
        ]

        script = mk_multisig_script(pubkeys, 2, 3)
        multisig_address = scriptaddr(script)

        self.db.insertEntry(
            "orders", {
                'market_id': self.transport.market_id,
                'contract_key': contract_key,
                'signed_contract_body': str(signed_data),
                'state': Orders.State.NOTARIZED,
                'buyer_order_id': buyer_order_id,
                'order_id': order_id,
                'merchant': offer_data_json['Seller']['seller_GUID'],
                'buyer': bid_data_json['Buyer']['buyer_GUID'],
                'address': multisig_address,
                'item_price': offer_data_json['Contract'].get('item_price', 0),
                'shipping_price': offer_data_json['Contract']['item_delivery'].get('shipping_price', ""),
                'note_for_merchant': bid_data_json['Buyer']['note_for_seller'],
                "updated": time.time()
            }
        )

        # Send order to seller and buyer
        self.log.info('Sending notarized contract to buyer and seller %s', bid)

        if self.transport.handler is not None:
            self.transport.handler.send_to_client(None, {"type": "order_notify",
                                                         "msg": "You just auto-notarized a contract."})

        notarized_order = {
            "type": "order",
            "state": "Notarized",
            "rawContract": str(signed_data)
        }

        if new_peer is not None:
            new_peer.send(notarized_order)
        self.transport.send(notarized_order, bid_data_json['Buyer']['buyer_GUID'])

        self.log.info('Sent notarized contract to Seller and Buyer')

    def generate_order_id(self):
        order_id = random.randint(0, 1000000)
        while self.db.contracts.find({'id': order_id}).count() > 0:
            order_id = random.randint(0, 1000000)
        return order_id

    def handle_paid_order(self, msg):
        self.log.info('Entering Paid Order handling')
        self.log.debug('Paid Order %s', msg)

        offer_data = ''.join(msg['signed_contract_body'].split('\n')[8:])
        index_of_seller_signature = offer_data.find('- - -----BEGIN PGP SIGNATURE-----', 0, len(offer_data))
        offer_data_json = offer_data[0:index_of_seller_signature]
        self.log.info('Offer Data: %s', offer_data_json)
        offer_data_json = json.loads(str(offer_data_json))

        bid_data_index = offer_data.find('"Buyer"', index_of_seller_signature, len(offer_data))
        end_of_bid_index = offer_data.find('- -----BEGIN PGP SIGNATURE', bid_data_index, len(offer_data))
        bid_data_json = "{" + offer_data[bid_data_index:end_of_bid_index]
        bid_data_json = json.loads(bid_data_json)
        self.log.info('Bid Data: %s', bid_data_json)

        buyer_order_id = "%s-%s" % (
            bid_data_json['Buyer']['buyer_GUID'],
            bid_data_json['Buyer']['buyer_order_id']
        )

        self.db.updateEntries(
            "orders",
            {'state': Orders.State.BUYER_PAID, 'shipping_address': json.dumps(msg['shipping_address']),
             "updated": time.time()},
            {'buyer_order_id': buyer_order_id}
        )
        if self.transport.handler is not None:
            self.transport.handler.send_to_client(None, {"type": "order_notify",
                                                         "msg": "A buyer just paid for an order."})

    def handle_shipped_order(self, msg):
        self.log.info('Entering Shipped Order handling')
        self.log.debug('Shipped Order %s', msg)

        offer_data = ''.join(msg['signed_contract_body'].split('\n')[8:])
        index_of_seller_signature = offer_data.find('- - -----BEGIN PGP SIGNATURE-----', 0, len(offer_data))
        offer_data_json = offer_data[0:index_of_seller_signature]
        offer_data_json = json.loads(str(offer_data_json))

        bid_data_index = offer_data.find('"Buyer"', index_of_seller_signature, len(offer_data))
        end_of_bid_index = offer_data.find('- -----BEGIN PGP SIGNATURE', bid_data_index, len(offer_data))
        bid_data_json = "{" + offer_data[bid_data_index:end_of_bid_index]
        bid_data_json = json.loads(bid_data_json)

        self.db.updateEntries(
            "orders",
            {
                'state': Orders.State.SHIPPED,
                'updated': time.time(),
                'payment_address': msg['payment_address']
            },
            {
                'order_id': bid_data_json['Buyer']['buyer_order_id']
            }
        )

        if self.transport.handler is not None:
            self.transport.handler.send_to_client(None, {"type": "order_notify",
                                                         "msg": "The seller just shipped your order."})

    def handle_notarized_order(self, msg):

        self.log.info('Handling notarized order')

        contract = msg['rawContract']

        # Find Seller Data in Contract
        offer_data = ''.join(contract.split('\n')[8:])
        index_of_seller_signature = offer_data.find('- - -----BEGIN PGP SIGNATURE-----', 0, len(offer_data))
        offer_data_json = offer_data[0:index_of_seller_signature]
        self.log.info('Offer Data: %s', offer_data_json)
        offer_data_json = json.loads(str(offer_data_json))

        # Find Buyer Data in Contract
        bid_data_index = offer_data.find('"Buyer"', index_of_seller_signature, len(offer_data))
        end_of_bid_index = offer_data.find('- -----BEGIN PGP SIGNATURE', bid_data_index, len(offer_data))
        bid_data_json = "{" + offer_data[bid_data_index:end_of_bid_index]
        bid_data_json = json.loads(bid_data_json)
        self.log.info('Bid Data: %s', bid_data_json)

        # Find Notary Data in Contract
        notary_data_index = offer_data.find('"Notary"', end_of_bid_index, len(offer_data))
        end_of_notary_index = offer_data.find('-----BEGIN PGP SIGNATURE', notary_data_index, len(offer_data))
        notary_data_json = "{" + offer_data[notary_data_index:end_of_notary_index]
        notary_data_json = json.loads(notary_data_json)
        self.log.info('Notary Data: %s', notary_data_json)

        # Generate multi-sig address
        pubkeys = [offer_data_json['Seller']['seller_BTC_uncompressed_pubkey'],
                   bid_data_json['Buyer']['buyer_BTC_uncompressed_pubkey'],
                   notary_data_json['Notary']['notary_BTC_uncompressed_pubkey']]
        script = mk_multisig_script(pubkeys, 2, 3)

        multisig_address = scriptaddr(script)

        seller_GUID = offer_data_json['Seller']['seller_GUID']

        order_id = bid_data_json['Buyer']['buyer_order_id']

        contract_key = hashlib.sha1(str(contract)).hexdigest()
        hash_value = hashlib.new('ripemd160')
        hash_value.update(contract_key)
        contract_key = hash_value.hexdigest()

        if seller_GUID == self.transport.guid:
            self.log.info('I am the seller!')
            state = 'Waiting for Payment'

            merchant_order_id = random.randint(0, 1000000)
            while len(self.db.selectEntries("orders", {"id": order_id})) > 0:
                merchant_order_id = random.randint(0, 1000000)

            buyer_id = "%s-%s" % (
                bid_data_json['Buyer']['buyer_GUID'],
                bid_data_json['Buyer']['buyer_order_id']
            )

            self.db.insertEntry(
                "orders",
                {
                    'market_id': self.transport.market_id,
                    'contract_key': contract_key,
                    'order_id': merchant_order_id,
                    'signed_contract_body': str(contract),
                    'state': state,
                    'buyer_order_id': buyer_id,
                    'merchant': offer_data_json['Seller']['seller_GUID'],
                    'buyer': bid_data_json['Buyer']['buyer_GUID'],
                    'notary': notary_data_json['Notary']['notary_GUID'],
                    'address': multisig_address,
                    'shipping_address': self.transport.cryptor.decrypt(
                        bid_data_json['Buyer']['buyer_deliveryaddr'].decode('hex')),
                    'item_price': offer_data_json['Contract'].get('item_price', 0),
                    'shipping_price': offer_data_json['Contract']['item_delivery'].get('shipping_price', 0),
                    'note_for_merchant': bid_data_json['Buyer']['note_for_seller'],
                    "updated": time.time()
                }
            )

            self.transport.handler.send_to_client(None, {"type": "order_notify",
                                                         "msg": "You just received a new order."})

        else:
            self.log.info('I am the buyer')
            state = 'Need to Pay'

            self.db.updateEntries(
                "orders",
                {
                    'order_id': order_id
                },
                {
                    'market_id': self.transport.market_id,
                    'contract_key': contract_key,
                    'signed_contract_body': str(contract),
                    'state': state,
                    'merchant': offer_data_json['Seller']['seller_GUID'],
                    'buyer': bid_data_json['Buyer']['buyer_GUID'],
                    'notary': notary_data_json['Notary']['notary_GUID'],
                    'address': multisig_address,
                    'shipping_address': json.dumps(self.get_shipping_address()),
                    'item_price': offer_data_json['Contract'].get('item_price', 0),
                    'shipping_price': offer_data_json['Contract']['item_delivery'].get('shipping_price', ''),
                    'note_for_merchant': bid_data_json['Buyer']['note_for_seller'],
                    "updated": time.time()
                }
            )

            self.transport.handler.send_to_client(None, {"type": "order_notify",
                                                         "msg": "Your order requires payment now."})
