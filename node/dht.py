from protocol import proto_store
from urlparse import urlparse
import constants
import datastore
import hashlib
import json
import logging
import os
import routingtable
import time
from threading import Thread

import network_util


class DHT(object):
    def __init__(self, transport, market_id, settings, db_connection):

        self.log = logging.getLogger(
            '[%s] %s' % (market_id, self.__class__.__name__)
        )
        self.settings = settings
        self.knownNodes = []
        self.searches = []
        self.search_keys = {}
        self.activePeers = []
        self.republishThreads = []
        self.transport = transport
        self.market_id = market_id

        # Routing table
        self.routingTable = routingtable.OptimizedTreeRoutingTable(
            self.settings['guid'], market_id)
        self.dataStore = datastore.SqliteDataStore(db_connection)

    def getActivePeers(self):
        return self.activePeers

    def start(self, seed_peer):
        """ This method executes only when the server is starting up for the
            first time and add the seed peer(s) to known node list and
            active peer list. It then executes a findNode against the network
            for itself to refresh buckets.

        :param seed_peer: (CryptoPeerConnection) for seed peer
        """
        ip = seed_peer.ip
        port = seed_peer.port
        self._add_known_node(('tcp://%s:%s' % (ip, port), seed_peer.guid, seed_peer.nickname))

        self.log.debug('Starting Seed Peer: %s', seed_peer.nickname)
        self.add_peer(self.transport,
                      seed_peer.address,
                      seed_peer.pub,
                      seed_peer.guid,
                      seed_peer.nickname)

        self._iterativeFind(self.settings['guid'], self.knownNodes,
                            'findNode')

    def find_active_peer(self, uri, pubkey=None, guid=None, nickname=None):
        found_peer = False
        for peer in self.activePeers:
            if (guid, uri, pubkey, nickname) == (peer.guid, peer.address, peer.pub, peer.nickname):
                found_peer = peer
        return found_peer

    def remove_active_peer(self, uri):
        for idx, peer in enumerate(self.activePeers):
            if uri == peer.address:
                self.activePeers[idx].close_socket()
                del self.activePeers[idx]

    def add_seed(self, transport, uri):

        new_peer = self.transport.get_crypto_peer(uri=uri)
        self.log.debug(new_peer)

        def start_handshake_cb():
            self.knownNodes.append(
                (urlparse(uri).hostname, urlparse(uri).port, new_peer.guid)
            )
            self.log.debug('Known Nodes: %s', self.knownNodes)

        Thread(target=new_peer.start_handshake, args=(start_handshake_cb,)).start()

    def add_peer(self, transport, uri, pubkey=None, guid=None, nickname=None):
        """ This takes a tuple (pubkey, URI, guid) and adds it to the active
        peers list if it doesn't already reside there.

        :param transport: (CryptoTransportLayer) so we can get a new CryptoPeer

        TODO: Refactor to just pass a peer object. evil tuples.
        """

        assert uri, 'URI is required to add a peer'

        peer_tuple = (uri, pubkey, guid, nickname)

        for idx, peer in enumerate(self.activePeers):
            active_peer_tuple = (peer.address, peer.pub, peer.guid, peer.nickname)

            if active_peer_tuple == peer_tuple:
                old_peer = self.routingTable.getContact(guid)

                if old_peer and (old_peer.address != uri or old_peer.pub != pubkey):
                    # Update routing table
                    self.routingTable.removeContact(guid)
                    self.routingTable.addContact(peer)
                return
            else:
                if peer.guid == guid or peer.address == uri:

                    # Update peer
                    peer.guid = guid
                    peer.address = uri
                    peer.pub = pubkey
                    peer.nickname = nickname
                    self.activePeers[idx] = peer

                    # Update routing table
                    self.routingTable.removeContact(guid)
                    self.routingTable.addContact(peer)

                    return

        if peer_tuple in self.knownNodes:
            self.log.debug("This peer is already known and up to date")
            return
        else:
            self._add_known_node(peer_tuple)

        self.log.info(
            'New peer seen; starting handshake - %s %s %s',
            uri, guid, nickname
        )

        new_peer = self.transport.get_crypto_peer(guid, uri, pubkey, nickname)

        def cb():
            self.log.debug('Back from handshake %s', new_peer)
            self.transport.save_peer_to_db(peer_tuple)

        t = Thread(target=new_peer.start_handshake, args=(cb,))
        t.start()

    def _add_known_node(self, node):
        """ Accept a peer tuple and add it to known nodes list
        :param node: (tuple)
        :return: N/A
        """
        self.log.debug('Adding known node: %s', node)
        if node not in self.knownNodes and node[1] is not None:
            self.knownNodes.append(node)

    def get_known_nodes(self):
        """ Get known nodes list and return it
        :return: (list)
        """
        return self.knownNodes

    def on_find_node(self, msg):
        """ When a findNode message is received it will be of several types:
        - findValue: Looking for a specific key-value
        - findNode: Looking for a node with a key

        If you find the key-value pair you send back the value in the foundKey
        field.

        If you find the node then send back the exact node and if you don't
        send back a list of k closest nodes in the foundNodes field.

        :param msg: Incoming message from other node with findNode request
        :return: N/A
        """

        self.log.debug('Received a findNode request: %s', msg)

        guid = msg['senderGUID']
        key = msg['key']
        findID = msg['findID']
        uri = msg['uri']
        pubkey = msg['pubkey']

        assert guid is not None and guid != self.transport.guid
        assert key is not None
        assert findID is not None
        assert uri is not None
        assert pubkey is not None

        new_peer = self.routingTable.getContact(guid)

        if new_peer is not None:
            response_msg = {"type": "findNodeResponse",
                            "senderGUID": self.transport.guid,
                            "uri": self.transport.uri,
                            "pubkey": self.transport.pubkey,
                            "senderNick": self.transport.nickname,
                            "findID": findID
                            }

            if msg['findValue'] is True:
                if key in self.dataStore and self.dataStore[key] is not None:
                    # Found key in local data store
                    response_msg["foundKey"] = self.dataStore[key]
                    self.log.info('Found a key: %s', key)
                else:
                    self.log.info('Did not find a key: %s', key)
                    response_msg["foundNodes"] = self.close_nodes(key, guid)
                    self.log.info('Sending found close nodes to: %s', guid)

                new_peer.send(response_msg)
            else:
                self.log.info('Sending found nodes to: %s', guid)
                response_msg["foundNodes"] = self.close_nodes(key, guid)

                new_peer.send(response_msg)

            if new_peer is not None and new_peer.address != uri:
                # update peer address in routing table.
                new_peer.address = uri
                self.routingTable.removeContact(new_peer.guid)
                self.routingTable.addContact(new_peer)

    def close_nodes(self, key, guid):
        contacts = self.routingTable.findCloseNodes(key, constants.k, guid)
        contactTriples = []
        for contact in contacts:
            contactTriples.append((contact.guid, contact.address, contact.pub, contact.nickname))

        return self.dedupe(contactTriples)

    def on_findNodeResponse(self, transport, msg):

        # Update existing peer's pubkey if active peer
        for idx, peer in enumerate(self.activePeers):
            if peer.guid == msg['senderGUID']:
                peer.nickname = msg['senderNick']
                peer.pub = msg['pubkey']
                self.activePeers[idx] = peer

        # If key was found by this node then
        if 'foundKey' in msg.keys():
            self.log.debug('Found the key-value pair. Executing callback.')

            for idx, s in enumerate(self.searches):
                if s.findID == msg['findID']:
                    s.callback(msg['foundKey'])
                    if idx in self.searches:
                        del self.searches[idx]

        else:

            if 'foundNode' in msg.keys():

                foundNode = msg['foundNode']
                self.log.debug('Found the node you were looking for: %s', foundNode)

                # Add foundNode to active peers list and routing table
                if foundNode[2] != self.transport.guid:
                    self.log.debug('Found a tuple %s', foundNode)
                    if len(foundNode) == 3:
                        foundNode.append('')
                    self.add_peer(self.transport, foundNode[1], foundNode[2], foundNode[0], foundNode[3])

                for idx, search in enumerate(self.searches):
                    if search.findID == msg['findID']:

                        # Execute callback
                        if search.callback is not None:
                            search.callback((foundNode[2], foundNode[1], foundNode[0], foundNode[3]))

                        # Clear search
                        del self.searches[idx]

            else:

                foundSearch = False
                search = ""
                findID = msg['findID']
                for s in self.searches:
                    if s.findID == findID:
                        search = s
                        foundSearch = True

                if not foundSearch:
                    self.log.info('No search found')
                    return
                else:

                    # Get current shortlist length
                    shortlist_length = len(search.shortlist)

                    nodes_to_extend = []

                    # Extends shortlist if necessary
                    for node in msg['foundNodes']:
                        self.log.info('FOUND NODE: %s', node)
                        if node[0] != self.transport.guid and node[2] != self.transport.pubkey \
                                and node[1] != self.transport.uri:
                            self.log.info('Found it %s %s', node[0], self.transport.guid)
                            nodes_to_extend.append(node)

                    self.extendShortlist(transport, msg['findID'], nodes_to_extend)

                    # Remove active probe to this node for this findID
                    search_ip = urlparse(msg['uri']).hostname
                    search_port = urlparse(msg['uri']).port
                    search_guid = msg['senderGUID']
                    search_tuple = (search_ip, search_port, search_guid)
                    for idx, probe in enumerate(search.active_probes):
                        if probe == search_tuple:
                            del search.active_probes[idx]
                    self.log.datadump(
                        'Find Node Response - Active Probes After: %s',
                        search.active_probes
                    )

                    # Add this to already contacted list
                    if search_tuple not in search.already_contacted:
                        search.already_contacted.append(search_tuple)
                    self.log.datadump(
                        'Already Contacted: %s',
                        search.already_contacted
                    )

                    # If we added more to shortlist then keep searching
                    if len(search.shortlist) > shortlist_length:
                        self.log.info('Lets keep searching')
                        self._searchIteration(search)
                    else:
                        self.log.info('Shortlist is empty')
                        if search.callback is not None:
                            search.callback(search.shortlist)

    def _refreshNode(self):
        """ Periodically called to perform k-bucket refreshes and data
        replication/republishing as necessary """
        self._refreshRoutingTable()
        self._republishData()

    def _refreshRoutingTable(self):
        self.log.info('Started Refreshing Routing Table')

        # Get Random ID from every k-bucket
        nodeIDs = self.routingTable.getRefreshList(0, False)

        def searchForNextNodeID():
            if len(nodeIDs) > 0:
                searchID = nodeIDs.pop()
                self.iterativeFindNode(searchID)
                searchForNextNodeID()
            else:
                # If this is reached, we have finished refreshing the routing table
                return

        # Start the refreshing cycle
        searchForNextNodeID()

    def _republishData(self, *args):
        self._threadedRepublishData()

    def _threadedRepublishData(self, *args):
        """ Republishes and expires any stored data (i.e. stored
        C{(key, value pairs)} that need to be republished/expired

        This method should run in a deferred thread
        """
        self.log.debug('Republishing Data')
        expiredKeys = []

        for key in self.dataStore.keys():

            # Filter internal variables stored in the data store
            if key == 'nodeState':
                continue

            now = int(time.time())
            key = key.encode('hex')
            originalPublisherID = self.dataStore.originalPublisherID(key)
            age = now - self.dataStore.originalPublishTime(key) + 500000

            if originalPublisherID == self.settings['guid']:
                # This node is the original publisher; it has to republish
                # the data before it expires (24 hours in basic Kademlia)
                if age >= constants.dataExpireTimeout:
                    self.iterativeStore(key, self.dataStore[key])

            else:
                # This node needs to replicate the data at set intervals,
                # until it expires, without changing the metadata associated with it
                # First, check if the data has expired
                if age >= constants.dataExpireTimeout:
                    # This key/value pair has expired and has not been
                    # republished by the original publishing node,
                    # so remove it.
                    expiredKeys.append(key)
                elif now - self.dataStore.lastPublished(key) >= constants.replicateInterval:
                    self.iterativeStore(key, self.dataStore[key], originalPublisherID, age)

        for key in expiredKeys:
            del self.dataStore[key]

    def extendShortlist(self, transport, findID, foundNodes):

        self.log.datadump('foundNodes: %s', foundNodes)

        foundSearch = False
        for s in self.searches:
            if s.findID == findID:
                search = s
                foundSearch = True

        if not foundSearch:
            self.log.error('There was no search found for this ID')
            return

        self.log.datadump('Short list before: %s', search.shortlist)

        for node in foundNodes:

            node_guid, node_uri, node_pubkey, node_nick = node
            node_ip = urlparse(node_uri).hostname
            node_port = urlparse(node_uri).port

            # Add to shortlist
            if (node_ip, node_port, node_guid, node_nick) not in search.shortlist:
                search.add_to_shortlist([(node_ip, node_port, node_guid, node_nick)])

            # Skip ourselves if returned
            if node_guid == self.settings['guid']:
                continue

            for peer in self.activePeers:
                if node_guid == peer.guid:
                    # Already an active peer or it's myself
                    continue

            if node_guid != self.settings['guid']:
                self.log.debug('Adding new peer to active peers list: %s', node)
                self.add_peer(self.transport, node_uri, node_pubkey, node_guid, node_nick)

        self.log.datadump('Short list after: %s', search.shortlist)

    def find_listings(self, transport, key, listingFilter=None, callback=None):
        """
        Send a get product listings call to the node in question and
        then cache those listings locally.
        TODO: Ideally we would want to send an array of listing IDs that
        we have locally and then the node would send back the missing or
        updated listings. This would save on queries for listings we
        already have.
        """

        peer = self.routingTable.getContact(key)

        if peer:
            peer.send({'type': 'query_listings', 'key': key})
            return

        # Check cache in DHT if peer not available
        listing_index_key = hashlib.sha1('contracts-%s' % key).hexdigest()
        hashvalue = hashlib.new('ripemd160')
        hashvalue.update(listing_index_key)
        listing_index_key = hashvalue.hexdigest()

        self.log.info('Finding contracts for store: %s', listing_index_key)

        self.iterativeFindValue(listing_index_key, callback)

    def find_listings_by_keyword(self, transport, keyword, listingFilter=None, callback=None):

        hashvalue = hashlib.new('ripemd160')
        keyword_key = 'keyword-%s' % keyword
        hashvalue.update(keyword_key.encode('utf-8'))
        listing_index_key = hashvalue.hexdigest()

        self.log.info('Finding contracts for keyword: %s', keyword)

        self.iterativeFindValue(listing_index_key, callback)

    def iterativeStore(self, key, value_to_store=None, originalPublisherID=None, age=0):
        """ The Kademlia store operation

        Call this to store/republish data in the DHT.

        @param key: The hashtable key of the data
        @type key: str
        @param originalPublisherID: The node ID of the node that is the
                                    B{original} publisher of the data
        @type originalPublisherID: str
        @param age: The relative age of the data (time in seconds since it was
                    originally published). Note that the original publish time
                    isn't actually given, to compensate for clock skew between
                    different nodes.
        @type age: int
        """
        if originalPublisherID is None:
            originalPublisherID = self.transport.guid

        # Find appropriate storage nodes and save key value
        if value_to_store:
            self.log.debug('Value to store: %s %s', key, value_to_store)
            self.iterativeFindNode(
                key,
                lambda msg, findKey=key, value=value_to_store, originalPublisherID=originalPublisherID, age=age:
                    self.storeKeyValue(msg, findKey, value, originalPublisherID, age)
            )

    def storeKeyValue(self, nodes, key, value, originalPublisherID, age):

        self.log.debug('Store Key Value: (%s, %s %s)', nodes, key, type(value))

        try:

            value_json = json.loads(value)

            # Add Notary GUID to index
            if 'notary_index_add' in value_json:
                existing_index = self.dataStore[key]
                if existing_index is not None:
                    if not value_json['notary_index_add'] in existing_index['notaries']:
                        existing_index['notaries'].append(value_json['notary_index_add'])
                    value = existing_index
                else:
                    value = {'notaries': [value_json['notary_index_add']]}
                self.log.info('Notaries: %s', existing_index)

            if 'notary_index_remove' in value_json:
                existing_index = self.dataStore[key]
                if existing_index is not None:
                    if value_json['notary_index_remove'] in existing_index['notaries']:
                        existing_index['notaries'].remove(value_json['notary_index_remove'])
                        value = existing_index
                    else:
                        return
                else:
                    return

            # Add listing to keyword index
            if 'keyword_index_add' in value_json:
                existing_index = self.dataStore[key]

                if existing_index is not None:
                    if not value_json['keyword_index_add'] in existing_index['listings']:
                        existing_index['listings'].append(value_json['keyword_index_add'])
                    value = existing_index
                else:
                    value = {'listings': [value_json['keyword_index_add']]}

                self.log.info('Keyword Index: %s', value)

            if 'keyword_index_remove' in value_json:

                existing_index = self.dataStore[key]

                if existing_index is not None:

                    if value_json['keyword_index_remove'] in existing_index['listings']:
                        existing_index['listings'].remove(value_json['keyword_index_remove'])
                        value = existing_index
                    else:
                        return

                else:
                    # Not in keyword index anyways
                    return

        except Exception as e:
            self.log.debug('Value is not a JSON array: %s', e)

        now = int(time.time())
        originallyPublished = now - age

        # Store it in your own node
        self.dataStore.setItem(
            key, value, now, originallyPublished, originalPublisherID, market_id=self.market_id
        )

        for node in nodes:
            self.log.debug('Sending data to store in DHT: %s', node)
            uri = network_util.get_peer_url(node[0], node[1])
            guid = node[2]
            peer = self.routingTable.getContact(guid)

            if guid == self.transport.guid:
                break

            if not peer:
                peer = self.transport.get_crypto_peer(guid, uri)
                peer.start_handshake()

            peer.send(proto_store(key, value, originalPublisherID, age))

    def _on_storeValue(self, msg):

        key = msg['key']
        value = msg['value']
        originalPublisherID = msg['originalPublisherID']
        age = msg['age']

        self.log.info('Storing key %s for %s', key, originalPublisherID)
        self.log.datadump('Value: %s', value)

        now = int(time.time())
        originallyPublished = now - age

        if value:
            self.dataStore.setItem(key, value, now, originallyPublished, originalPublisherID, self.market_id)
        else:
            self.log.error('No value to store')

    def store(self, key, value, originalPublisherID=None, age=0, **kwargs):
        """ Store the received data in this node's local hash table

        @param key: The hashtable key of the data
        @type key: str
        @param value: The actual data (the value associated with C{key})
        @type value: str
        @param originalPublisherID: The node ID of the node that is the
                                    B{original} publisher of the data
        @type originalPublisherID: str
        @param age: The relative age of the data (time in seconds since it was
                    originally published). Note that the original publish time
                    isn't actually given, to compensate for clock skew between
                    different nodes.
        @type age: int

        @rtype: str

        @todo: Since the data (value) may be large, passing it around as a buffer
               (which is the case currently) might not be a good idea... will have
               to fix this (perhaps use a stream from the Protocol class?)
        """
        # Get the sender's ID (if any)
        if '_rpcNodeID' in kwargs:
            rpcSenderID = kwargs['_rpcNodeID']
        else:
            rpcSenderID = None

        if originalPublisherID is None:
            if rpcSenderID is not None:
                originalPublisherID = rpcSenderID
            else:
                raise TypeError(
                    'No publisher specifed, and RPC caller ID not available.'
                    'Data requires an original publisher.'
                )

        now = int(time.time())
        originallyPublished = now - age
        self.dataStore.setItem(
            key, value, now, originallyPublished, originalPublisherID, market_id=self.market_id
        )
        return 'OK'

    def iterativeFindNode(self, key, callback=None):
        """ The basic Kademlia node lookup operation

        Call this to find a remote node in the P2P overlay network.

        @param key: the 160-bit key (i.e. the node or value ID) to search for
        @type key: str
        """
        self.log.info('Looking for node at: %s', key)
        self._iterativeFind(key, [], callback=callback)

    def _iterativeFind(self, key, startupShortlist=None, call='findNode', callback=None):
        """
        - Create a new DHTSearch object and add the key and call back to it
        - Add the search to our search queue (self.searches)
        - Find out if we're looking for a value or for a node
        -

        """
        # Create a new search object
        self.log.debug('Startup short list: %s', startupShortlist)
        new_search = DHTSearch(self.market_id, key, call, callback=callback)
        self.searches.append(new_search)

        # Determine if we're looking for a node or a key
        findValue = True if call != 'findNode' else False

        if startupShortlist == [] or startupShortlist is None:

            # Retrieve closest nodes and add them to the shortlist for the search
            closeNodes = self.routingTable.findCloseNodes(key, constants.alpha, self.settings['guid'])
            shortlist = []

            for closeNode in closeNodes:
                shortlist.append((closeNode.ip, closeNode.port, closeNode.guid))

            if len(shortlist) > 0:
                new_search.add_to_shortlist(shortlist)

            # Refresh the k-bucket for this key
            if key != self.settings['guid']:
                self.routingTable.touchKBucket(key)

            # Abandon the search if the shortlist has no nodes
            if len(new_search.shortlist) == 0:
                self.log.info('Search Finished')
                if callback is not None:
                    callback([])
                else:
                    return []

        else:
            new_search.shortlist = startupShortlist

        self._searchIteration(new_search, findValue=findValue)

    def _searchIteration(self, new_search, findValue=False):

        # Update slow nodes count
        new_search.slowNodeCount[0] = len(new_search.active_probes)

        # Sort shortlist from closest to farthest
        self.activePeers.sort(lambda firstNode, secondNode, targetKey=new_search.key: cmp(
            self.routingTable.distance(firstNode.guid, targetKey),
            self.routingTable.distance(secondNode.guid, targetKey)))

        # TODO: Put this in the callback
        # if new_search.key in new_search.find_value_result:
        # return new_search.find_value_result
        # elif len(new_search.shortlist) and findValue is False:
        #
        # # If you have more k amount of nodes in your shortlist then stop
        # # or ...
        # if (len(new_search.shortlist) >= constants.k) or (
        # new_search.shortlist[0] == new_search.previous_closest_node and len(
        # new_search.active_probes) ==
        # new_search.slowNodeCount[0]):
        # if new_search.callback is not None:
        # new_search.callback(new_search.shortlist)
        # return

        # Update closest node
        if len(self.activePeers):
            closestPeer = self.activePeers[0]
            closestPeer_ip = urlparse(closestPeer.address).hostname
            closestPeer_port = urlparse(closestPeer.address).port
            new_search.previous_closest_node = (closestPeer_ip, closestPeer_port, closestPeer.guid)

        # Sort short list again
        if len(new_search.shortlist) > 1:

            # Remove dupes
            new_search.shortlist = self.dedupe(new_search.shortlist)
            self.log.datadump(new_search.shortlist)

            new_search.shortlist.sort(lambda firstNode, secondNode, targetKey=new_search.key: cmp(
                self.routingTable.distance(firstNode[2], targetKey),
                self.routingTable.distance(secondNode[2], targetKey)))

            new_search.prevShortlistLength = len(new_search.shortlist)

        # See if search was cancelled
        if not self.activeSearchExists(new_search.findID):
            self.log.info('Active search does not exist')
            return

        # Send findNodes out to all nodes in the shortlist
        for node in new_search.shortlist:
            if node not in new_search.already_contacted:
                if node[2] is not None and node[2] != self.transport.guid:

                    new_search.active_probes.append(node)
                    new_search.already_contacted.append(node)

                    contact = self.routingTable.getContact(node[2])

                    if contact:

                        msg = {"type": "findNode",
                               "uri": contact.transport.uri,
                               "senderGUID": self.transport.guid,
                               "key": new_search.key,
                               "findValue": findValue,
                               "senderNick": self.transport.nickname,
                               "findID": new_search.findID,
                               "pubkey": contact.transport.pubkey}
                        self.log.debug('Sending findNode to: %s %s', contact.address, msg)

                        Thread(target=contact.send, args=(msg,)).start()
                        new_search.contactedNow += 1

                    else:
                        self.log.error('No contact was found for this guid: %s', node[2])

    def activeSearchExists(self, findID):

        activeSearchExists = False
        for search in self.searches:
            if findID == search.findID:
                return True
        if not activeSearchExists:
            return False

    def iterativeFindValue(self, key, callback=None):
        self._iterativeFind(key, call='findValue', callback=callback)

    @staticmethod
    def dedupe(lst):
        seen = set()
        result = []
        for item in lst:
            fs = frozenset(item)
            if fs not in seen:
                result.append(item)
                seen.add(fs)
        return result


class DHTSearch(object):
    def __init__(self, market_id, key, call="findNode", callback=None):
        self.key = key  # Key to search for
        self.call = call  # Either findNode or findValue depending on search
        self.callback = callback  # Callback for when search finishes
        self.shortlist = []  # List of nodes that are being searched against
        self.active_probes = []  #
        self.already_contacted = []  # Nodes are added to this list when they've been sent a findXXX action
        self.previous_closest_node = None  # This is updated to be the closest node found during search
        self.find_value_result = {}  # If a findValue search is found this is the value
        self.pendingIterationCalls = []  #
        self.slowNodeCount = [0]  #
        self.contactedNow = 0  # Counter for how many nodes have been contacted
        self.dhtCallbacks = []  # Callback list
        self.prevShortlistLength = 0

        self.log = logging.getLogger(
            '[%s] %s' % (market_id, self.__class__.__name__)
        )

        # Create a unique ID (SHA1) for this _iterativeFind request to support parallel searches
        self.findID = hashlib.sha1(os.urandom(128)).hexdigest()

    def add_to_shortlist(self, additions):

        self.log.debug('Additions: %s', additions)
        for item in additions:
            if item not in self.shortlist:
                self.shortlist.append(item)

        self.log.datadump('Updated short list: %s', self.shortlist)
