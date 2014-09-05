# This library is free software, distributed under the terms of
# the GNU Lesser General Public License Version 3, or any later version.
# See the COPYING file included in this archive
#
# The docstrings in this module contain epytext markup; API documentation
# may be created by processing this file with epydoc: http://epydoc.sf.net

import time
import random
import logging
import constants
import kbucket


class RoutingTable(object):
    """ Interface for RPC message translators/formatters

    Classes inheriting from this should provide a suitable routing table for
    a parent Node object (i.e. the local entity in the Kademlia network)
    """

    def __init__(self, parentNodeID, market_id=1):
        """
        @param parentNodeID: The 160-bit node ID of the node to which this
                             routing table belongs
        @type parentNodeID: str
        """
        self.market_id = market_id

        self.log = logging.getLogger(
            '[%s] %s' % (market_id, self.__class__.__name__)
        )

    def addContact(self, contact):
        """ Add the given contact to the correct k-bucket; if it already
        exists, its status will be updated

        @param contact: The contact to add to this node's k-buckets
        @type contact: connection.PeerConnection
        """

    @staticmethod
    def distance(keyOne, keyTwo):
        """ Calculate the XOR result between two string variables

        @return: XOR result of two long variables
        @rtype: long
        """
        valKeyOne = long(keyOne.encode('hex'), 16)
        valKeyTwo = long(keyTwo.encode('hex'), 16)
        return valKeyOne ^ valKeyTwo

    def findCloseNodes(self, key, count, _rpcNodeID=None):
        """ Finds a number of known nodes closest to the node/value with the
        specified key.

        @param key: the 160-bit key (i.e. the node or value ID) to search for
        @type key: str
        @param count: the amount of contacts to return
        @type count: int
        @param _rpcNodeID: Used during RPC, this is be the sender's Node ID
                           Whatever ID is passed in the paramater will get
                           excluded from the list of returned contacts.
        @type _rpcNodeID: str

        @return: A list of node contacts
                 (C{kademlia.contact.Contact instances})
                 closest to the specified key.
                 This method will return C{k} (or C{count}, if specified)
                 contacts if at all possible; it will only return fewer if the
                 node is returning all of the contacts that it knows of.
        @rtype: list
        """

    def getContact(self, contactID):
        """ Returns the (known) contact with the specified node ID

        @raise ValueError: No contact with the specified contact ID is known
                           by this node
        """

    def getRefreshList(self, startIndex=0, force=False):
        """ Finds all k-buckets that need refreshing, starting at the
        k-bucket with the specified index, and returns IDs to be searched for
        in order to refresh those k-buckets

        @param startIndex: The index of the bucket to start refreshing at;
                           this bucket and those further away from it will
                           be refreshed. For example, when joining the
                           network, this node will set this to the index of
                           the bucket after the one containing it's closest
                           neighbour.
        @type startIndex: index
        @param force: If this is C{True}, all buckets (in the specified range)
                      will be refreshed, regardless of the time they were last
                      accessed.
        @type force: bool

        @return: A list of node ID's that the parent node should search for
                 in order to refresh the routing Table
        @rtype: list
        """

    def removeContact(self, contactID):
        """ Remove the contact with the specified node ID from the routing
        table

        @param contactID: The node ID of the contact to remove
        @type contactID: str
        """

    def touchKBucket(self, key):
        """ Update the "last accessed" timestamp of the k-bucket which covers
        the range containing the specified key in the key/ID space

        @param key: A key in the range of the target k-bucket
        @type key: str
        """


class TreeRoutingTable(RoutingTable):
    """ This class implements a routing table used by a Node class.

    The Kademlia routing table is a binary tree whose leaves are k-buckets,
    where each k-bucket contains nodes with some common prefix of their IDs.
    This prefix is the k-bucket's position in the binary tree; it therefore
    covers some range of ID values, and together all of the k-buckets cover
    the entire 160-bit ID (or key) space (with no overlap).

    @note: In this implementation, nodes in the tree (the k-buckets) are
    added dynamically, as needed; this technique is described in the 13-page
    version of the Kademlia paper, in section 2.4. It does, however, use the
    C{PING} RPC-based k-bucket eviction algorithm described in section 2.2 of
    that paper.
    """

    def __init__(self, parentNodeID, market_id):
        """
            @param parentNodeID: The 160-bit node ID of the node to which this
                                 routing table belongs
            @type parentNodeID: str
            """
        super(TreeRoutingTable, self).__init__(parentNodeID)
        self.log = logging.getLogger(
            '[%s] %s' % (market_id, self.__class__.__name__)
        )
        self.buckets = [
            kbucket.KBucket(rangeMin=0, rangeMax=2 ** 200, market_id=market_id)
        ]
        self.parentNodeID = parentNodeID

    def addContact(self, contact):
        """ Add the given contact to the correct k-bucket; if it already
        exists, its status will be updated

        @param contact: The contact to add to this node's k-buckets
        @type contact: connection.PeerConnection
        """

        # If contact is itself return
        if contact.guid == self.parentNodeID:
            return

        bucketIndex = self.kbucketIndex(contact.guid)

        # If already added then update
        if not self.buckets[bucketIndex].getContact(contact):

            try:
                self.buckets[bucketIndex].addContact(contact)
            except kbucket.BucketFull:
                self.log.error("BucketFull1")
                # The bucket is full; see if it can be split (by checking if
                # its range includes the host node's id)
                if self.buckets[bucketIndex].keyInRange(self.parentNodeID):
                    self.splitBucket(bucketIndex)
                    # Retry the insertion attempt
                    self.addContact(contact)
                else:
                    # We can't split the k-bucket
                    # NOTE:
                    # In section 2.4 of the 13-page version of the Kademlia
                    # paper, it is specified that in this case, the new
                    # contact should simply be dropped. However, in
                    # section 2.2, it states that the head contact in the
                    # k-bucket (i.e. the least-recently seen node) should be
                    # pinged - if it does not reply, it should be dropped, and
                    # the new contact added to the tail of the k-bucket. This
                    # implementation follows section 2.2 regarding this point.

                    def replaceContact(failure):
                        """Callback for the deferred PING RPC to see if the
                           head node in the k-bucket is still responding

                           @type failure: twisted.python.failure.Failure"""
                        failure.trap(TimeoutError)
                        print '==replacing contact=='
                        # Remove the old contact...
                        deadContactID = failure.getErrorMessage()
                        try:
                            bucket = self.buckets[bucketIndex]
                            bucket.removeContact(deadContactID)
                        except ValueError:
                            # The contact has already been removed (probably
                            # due to a timeout)
                            pass
                        # ...and add the new one at the tail of the bucket
                        self.addContact(contact)

                    # Ping the least-recently seen contact in this k-bucket
                    headContact = self.buckets[bucketIndex].contacts[0]

                    # headContact.send({
                    #     "type": "ping",
                    #     "guid": self.guid,
                    #     "uri": self._uri,
                    #     "findValue": peer['findValue']
                    # })

                    df = headContact.ping()
                    # If there's an error (i.e. timeout), remove the head
                    # contact, and append the new one
                    df.addErrback(replaceContact)

    def findCloseNodes(self, key, count, nodeID=None):
        """ Finds a number of known nodes closest to the node/value with the
        specified key.

        @param key: the 160-bit key (i.e. the node or value ID) to search for
        @type key: str
        @param count: the amount of contacts to return
        @type count: int
        @param nodeID: Used during RPC, this is be the sender's Node ID
                           Whatever ID is passed in the paramater will get
                           excluded from the list of returned contacts.
        @type nodeID: str

        @return: A list of node contacts
                 (C{kademlia.contact.Contact instances})
                 closest to the specified key.
                 This method will return C{k} (or C{count}, if specified)
                 contacts if at all possible; it will only return fewer if the
                 node is returning all of the contacts that it knows of.
        @rtype: list
        """
        bucketIndex = self.kbucketIndex(key)
        bucket = self.buckets[bucketIndex]
        closestNodes = bucket.getContacts(constants.k, nodeID)

        # This method must return k contacts (even if we have the node with
        # the specified key as node ID), unless there is less than k remote
        # nodes in the routing table
        i = 1
        canGoLower = bucketIndex - i >= 0
        canGoHigher = bucketIndex + i < len(self.buckets)
        # Fill up the node list to k nodes, starting with the closest
        # neighbouring nodes known
        while len(closestNodes) < constants.k and (canGoLower or canGoHigher):
            # TODO: this may need to be optimized
            if canGoLower:
                bucket = self.buckets[bucketIndex - i]
                closestNodes.extend(
                    bucket.getContacts(
                        constants.k - len(closestNodes), nodeID
                    )
                )
                canGoLower = bucketIndex - (i + 1) >= 0
            if canGoHigher:
                bucket = self.buckets[bucketIndex + i]
                closestNodes.extend(
                    bucket.getContacts(
                        constants.k - len(closestNodes), nodeID
                    )
                )
                canGoHigher = bucketIndex + (i + 1) < len(self.buckets)
            i += 1

        self.log.debug('Closest Nodes: %s' % closestNodes)
        return closestNodes

    def getContact(self, contactID):
        """ Returns the (known) contact with the specified node ID

        @raise ValueError: No contact with the specified contact ID is known
                           by this node
        """

        bucketIndex = self.kbucketIndex(contactID)

        try:
            contact = self.buckets[bucketIndex].getContact(contactID)
        except ValueError:
            raise
        else:
            return contact

    def getRefreshList(self, startIndex=0, force=False):
        """ Finds all k-buckets that need refreshing, starting at the
        k-bucket with the specified index, and returns IDs to be searched for
        in order to refresh those k-buckets

        @param startIndex: The index of the bucket to start refreshing at;
                           this bucket and those further away from it will
                           be refreshed. For example, when joining the
                           network, this node will set this to the index of
                           the bucket after the one containing it's closest
                           neighbour.
        @type startIndex: index
        @param force: If this is C{True}, all buckets (in the specified range)
                      will be refreshed, regardless of the time they were last
                      accessed.
        @type force: bool

        @return: A list of node ID's that the parent node should search for
                 in order to refresh the routing Table
        @rtype: list
        """
        bucketIndex = startIndex
        refreshIDs = []
        for bucket in self.buckets[startIndex:]:
            if force or \
               int(time.time()) - bucket.lastAccessed >= constants.refreshTimeout:
                searchID = self._randomIDInBucketRange(bucketIndex).encode('hex')
                refreshIDs.append(searchID)
            bucketIndex += 1
        return refreshIDs

    def removeContact(self, contactID):
        """ Remove the contact with the specified node ID from the routing
        table

        @param contactID: The node ID of the contact to remove
        @type contactID: str
        """
        bucketIndex = self.kbucketIndex(contactID)
        try:
            self.buckets[bucketIndex].removeContact(contactID)
        except ValueError:
            # print 'removeContact(): Contact not in routing table'
            return

    def touchKBucket(self, key):
        """ Update the "last accessed" timestamp of the k-bucket which covers
        the range containing the specified key in the key/ID space

        @param key: A key in the range of the target k-bucket
        @type key: str
        """
        bucketIndex = self.kbucketIndex(key)
        self.buckets[bucketIndex].lastAccessed = int(time.time())

    def kbucketIndex(self, key):
        """ Calculate the index of the k-bucket which is responsible for the
        specified key (or ID)

        @param key: The key for which to find the appropriate k-bucket index
        @type key: str

        @return: The index of the k-bucket responsible for the specified key
        @rtype: int
        """
        # print key
        # print obelisk.DecodeBase58Check(key)
        valKey = long(key, 16)

        i = 0
        for bucket in self.buckets:
            if bucket.keyInRange(valKey):
                self.log.debug("inRange")
                return i
            else:
                self.log.debug("not inRange")
                i += 1
        return i

    def _randomIDInBucketRange(self, bucketIndex):
        """ Returns a random ID in the specified k-bucket's range

        @param bucketIndex: The index of the k-bucket to use
        @type bucketIndex: int
        """
        idValue = random.randrange(
            self.buckets[bucketIndex].rangeMin,
            self.buckets[bucketIndex].rangeMax
        )
        randomID = hex(idValue)[2:]
        if randomID[-1] == 'L':
            randomID = randomID[:-1]
        if len(randomID) % 2 != 0:
            randomID = '0' + randomID
        randomID = randomID.decode('hex')
        randomID = (20 - len(randomID)) * '\x00' + randomID
        return randomID

    def splitBucket(self, oldBucketIndex):
        """ Splits the specified k-bucket into two new buckets which together
        cover the same range in the key/ID space

        @param oldBucketIndex: The index of k-bucket to split (in this table's
                               list of k-buckets)
        @type oldBucketIndex: int
        """
        self.log.warn("splitBucket")
        # Resize the range of the current (old) k-bucket
        oldBucket = self.buckets[oldBucketIndex]
        splitPoint = oldBucket.rangeMax - (oldBucket.rangeMax - oldBucket.rangeMin) / 2
        # Create a new k-bucket to cover the range split off from the old
        # bucket
        newBucket = kbucket.KBucket(
            splitPoint, oldBucket.rangeMax, self.market_id
        )
        oldBucket.rangeMax = splitPoint
        # Now, add the new bucket into the routing table tree
        self.buckets.insert(oldBucketIndex + 1, newBucket)
        # Finally, copy all nodes that belong to the new k-bucket into it...
        self.log.debug("copying nodes to new bucket")
        for contact in oldBucket.contacts:
            if newBucket.keyInRange(contact.guid):
                newBucket.addContact(contact)
        # ...and remove them from the old bucket
        for contact in newBucket.contacts:
            oldBucket.removeContact(contact)


class OptimizedTreeRoutingTable(TreeRoutingTable):
    """ A version of the "tree"-type routing table specified by Kademlia,
    along with contact accounting optimizations specified in section 4.1 of
    of the 13-page version of the Kademlia paper.
    """

    def __init__(self, parentNodeID, market_id):
        TreeRoutingTable.__init__(self, parentNodeID, market_id)
        # Cache containing nodes eligible to replace stale k-bucket entries
        self.replacementCache = {}

    def addContact(self, contact):
        """ Add the given contact to the correct k-bucket; if it already
        exists, its status will be updated

        @param contact: The contact to add to this node's k-buckets
        @type contact: kademlia.contact.Contact
        """

        if not contact.guid:
            self.log.error('No guid specified')
            return

        if contact.guid == self.parentNodeID:
            self.log.info('Trying to add yourself. Leaving.')
            return

        # Initialize/reset the "successively failed RPC" counter
        contact.failedRPCs = 0

        bucketIndex = self.kbucketIndex(contact.guid)

        self.log.info("numbuckets: %s" % len(self.buckets))
        self.log.info("selecting bucketIndex: %s" % bucketIndex)

        old_contact = self.buckets[bucketIndex].getContact(contact)

        if not old_contact:

            try:
                self.buckets[bucketIndex].addContact(contact)
                self.log.info("node added; bucket %s now has %s nodes" % (bucketIndex, len(self.buckets[bucketIndex])))
            except kbucket.BucketFull:
                self.log.error("BucketFull2")
                # The bucket is full; see if it can be split (by checking if
                # its range includes the host node's id)
                if self.buckets[bucketIndex].keyInRange(self.parentNodeID):
                    self.splitBucket(bucketIndex)
                    # Retry the insertion attempt
                    self.addContact(contact)
                else:
                    # We can't split the k-bucket
                    # NOTE: This implementation follows section 4.1 of the 13
                    # page version of the Kademlia paper (optimized contact
                    # accounting without PINGs - results in much less network
                    # traffic, at the expense of some memory)

                    # Put the new contact in our replacement cache for the
                    # corresponding k-bucket (or update it's position if it
                    # exists already)
                    if bucketIndex not in self.replacementCache:
                        self.log.warn("bfrtk")
                        self.replacementCache[bucketIndex] = []
                    if contact in self.replacementCache[bucketIndex]:
                        self.log.warn("bfrtk in replacement cache; removing to re-add")
                        self.replacementCache[bucketIndex].remove(contact)
                    # TODO: Using k to limit the size of the contact
                    # replacement cache - maybe define a separate value for
                    # this in constants.py?
                    elif len(self.replacementCache) >= constants.k:
                        self.log.warn("bfrtk replacement cache over size limit")
                        self.replacementCache.pop(0)
                    self.log.warn("bfrtk adding to replacement cache")
                    self.replacementCache[bucketIndex].append(contact)

        else:
            if old_contact.address != contact.address:

                self.log.info('Remove contact')
                self.removeContact(contact.guid)

                try:
                    self.buckets[bucketIndex].addContact(contact)
                except kbucket.BucketFull:
                    self.log.error("BucketFull3")

                    # The bucket is full; see if it can be split (by checking
                    # if its range includes the host node's id)
                    if self.buckets[bucketIndex].keyInRange(self.parentNodeID):
                        self.splitBucket(bucketIndex)
                        # Retry the insertion attempt
                        self.addContact(contact)
                    else:
                        # We can't split the k-bucket
                        # NOTE: This implementation follows section 4.1 of the
                        # 13 page version of the Kademlia paper (optimized
                        # contact accounting without PINGs - results in much
                        # less network traffic, at the expense of some memory)

                        # Put the new contact in our replacement cache for the
                        # corresponding k-bucket (or update it's position if
                        # it exists already)
                        if bucketIndex not in self.replacementCache:
                            self.replacementCache[bucketIndex] = []
                        if contact in self.replacementCache[bucketIndex]:
                            self.replacementCache[bucketIndex].remove(contact)
                        # TODO: Using k to limit the size of the contact
                        # replacement cache - maybe define a separate value
                        # for this in constants.py?
                        elif len(self.replacementCache) >= constants.k:
                            self.replacementCache.pop(0)
                        self.replacementCache[bucketIndex].append(contact)

    def removeContact(self, contactID):
        """ Remove the contact with the specified node ID from the routing
        table

        @param contactID: The node ID of the contact to remove
        @type contactID: str
        """
        bucketIndex = self.kbucketIndex(contactID)
        try:
            contact = self.buckets[bucketIndex].getContact(contactID)
        except ValueError:
            # print 'removeContact(): Contact not in routing table'
            return

        if contact:
            self.buckets[bucketIndex].removeContact(contact)
            self.log.info("node removed; bucket %s now has %s nodes" % (bucketIndex, len(self.buckets[bucketIndex])))
            # Replace this stale contact with one from our replacement
            # cache, if we have any
            if bucketIndex in self.replacementCache:
                if len(self.replacementCache[bucketIndex]) > 0:
                    self.buckets[bucketIndex].addContact(
                        self.replacementCache[bucketIndex].pop()
                    )

            self.log.debug(
                'Contacts: %s' % self.buckets[bucketIndex].contacts
            )


class TimeoutError(Exception):
    """ Raised when a RPC times out """
