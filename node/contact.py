class Contact(object):
    """
    A remote contact

    Encapsulates information about a single remote contact.
    """
    def __init__(self, guid, uri, name='', firstComm=0):
        self.guid = guid
        self.uri = uri
        self.name = name
        # self._peerConnection = PeerConnection(uri, guid)
        self.commTime = firstComm
        self.failedRPCs = 0

    def __eq__(self, other):
        if isinstance(other, Contact):
            return self.guid == other.guid
        elif isinstance(other, str):
            return self.guid == other
        return False

    def __ne__(self, other):
        return not (self == other)

    def __repr__(self):
        return '<%s.%s object; GUID: %s, URI: %s>' % (
            self.__module__,
            self.__class__.__name__,
            self.guid,
            self.uri
        )
