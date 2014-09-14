import logging

from six import string_types
import constants


class BucketFull(Exception):
    """Raised when the bucket is full."""


class KBucket(object):
    """FILLME"""

    def __init__(self, rangeMin, rangeMax, market_id=1):
        """
        @param rangeMin: The lower boundary for the range in the 160-bit ID
                         space covered by this k-bucket
        @param rangeMax: The upper boundary for the range in the ID space
                         covered by this k-bucket
        @param market_id: FILLME
        """

        self.lastAccessed = 0
        self.rangeMin = rangeMin
        self.rangeMax = rangeMax
        self.contacts = []

        self.log = logging.getLogger(
            '[%s] %s' % (market_id, self.__class__.__name__)
        )

    def addContact(self, contact):
        """
        Add contact to _contact list in the right order. This will move the
        contact to the end of the k-bucket if it is already present.

        @raise node.kbucket.BucketFull: Raised when the bucket is full and
                                        the contact isn't already in the bucket

        @param contact: The contact to add
        @type contact: p2p.PeerConnection
        """
        try:
            # Assume contact exists. Attempt to remove the old one...
            self.contacts.remove(contact)
            # ... and add the new one at the end of the list.
            self.log.info("adding contact %s %s %s" % (contact.uri, contact.name, contact.guid))
            self.contacts.append(contact)

            # The code above works as follows:
            # Assume C1 is the existing contact and C2 is the new contact.
            # Iff C1 is equal to C2, it will be removed from the list.
            # Since Contact.__eq__ compares only GUIDs, contact C1 will
            # be replaced even if it's not exactly the same as C2.
            # This is the intended behaviour; the fresh contact may have
            # updated add-on data (e.g. optimization-specific stuff).
        except ValueError:
            # The contact wasn't there after all, so add it.
            if len(self.contacts) < constants.k:
                self.contacts.append(contact)
            else:
                self.log.warn("No space in bucket to insert contact ")
                raise BucketFull('No space in bucket to insert contact')

    def getContact(self, contactID):
        """Get the contact with the specified node ID."""
        self.log.debug('[getContact] %s' % contactID)
        #self.log.debug('contacts %s' % self.contacts)
        for contact in self.contacts:
            if contact.guid == contactID:
                self.log.debug('[getContact] Found %s' % contact)
                return contact
        self.log.debug('[getContact] Not Found: %s' % contactID)

    def getContacts(self, count=-1, excludeContact=None):
        """
        Returns a list containing up to the first count number of contacts

        @param count: The amount of contacts to return (if 0 or less, return
                      all contacts)
        @type count: int
        @param excludeContact: A contact to exclude; if this contact is in
                               the list of returned values, it will be
                               discarded before returning. If a C{str} is
                               passed as this argument, it must be the
                               contact's ID.
        @type excludeContact: node.contact.Contact or str (GUID)

        @raise IndexError: If the number of requested contacts is too large

        @return: Return up to the first count number of contacts in a list.
                 If no contacts are present, return empty list.
        @rtype: list
        """

        currentLen = len(self)
        if not currentLen:
            return []

        if count <= 0:
            count = currentLen
        else:
            count = min(count, currentLen)

        # Return no more contacts than bucket size.
        count = min(count, constants.k)

        contactList = self.contacts[0:count]
        if excludeContact is not None:
            try:
                contactList.remove(excludeContact)
            except ValueError:
                self.log.debug(
                    '[kbucket.getContacts() warning] '
                    'tried to exclude non-existing contact '
                    '(%s)' % excludeContact
                )
        return contactList

    def removeContact(self, contact):
        """
        Remove given contact from list

        @param contact: The contact to remove, or a string containing the
                        contact's node ID
        @type contact: node.contact.Contact or str (GUID)

        @raise ValueError: The specified contact is not in this bucket
        """
        self.log.info("removing contact %s" % contact)
        #self.log.debug('Contacts %s %s' % (contact, self.contacts))
        try:
            self.contacts.remove(contact)
        except ValueError:
            raise ValueError('Contact was not found in this bucket')
        #self.log.debug('Contacts %s %s' % (contact, self.contacts))

    def keyInRange(self, key):
        """
        Tests whether the specified key (i.e. node ID) is in the range
        of the 160-bit ID space covered by this k-bucket (in other words, it
        returns whether or not the specified key should be placed in this
        k-bucket)

        @param key: The key to test
        @type key: str or int

        @return: C{True} if key is in this k-bucket's range,
                 C{False} otherwise.
        @rtype: bool
        """
        self.log.debug("keyInRange: %s" % key)
        if isinstance(key, string_types):
            key = long(key, 16)
            self.log.debug("keyInRange: now %s" % key)
        return self.rangeMin <= key < self.rangeMax

    def __len__(self):
        return len(self.contacts)
