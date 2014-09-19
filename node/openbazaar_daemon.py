import argparse
import tornado.web
from zmq.eventloop import ioloop
ioloop.install()

from transport import CryptoTransportLayer
from db_store import Obdb
from market import Market
from ws import WebSocketHandler
import logging
import signal
from threading import Thread
from twisted.internet import reactor
from util import open_default_webbrowser
from network_util import get_random_free_tcp_port
import upnp
import os


class MainHandler(tornado.web.RequestHandler):
    def get(self):
        self.redirect("/html/index.html")


class OpenBazaarStaticHandler(tornado.web.StaticFileHandler):
    def set_extra_headers(self, path):
        self.set_header("X-Frame-Options", "DENY")
        self.set_header("X-Content-Type-Options", "nosniff")


class MarketApplication(tornado.web.Application):
    def __init__(self, market_ip, market_port, market_id=1,
                 bm_user=None, bm_pass=None, bm_port=None, seed_peers=None,
                 seed_mode=0, dev_mode=False, db_path='db/ob.db', disable_sqlite_crypt=False, disable_ip_update=False):
        if seed_peers is None:
            seed_peers = []

        db = Obdb(db_path, disable_sqlite_crypt)

        self.transport = CryptoTransportLayer(market_ip,
                                              market_port,
                                              market_id,
                                              db,
                                              bm_user,
                                              bm_pass,
                                              bm_port,
                                              seed_mode,
                                              dev_mode,
                                              disable_ip_update)

        self.market = Market(self.transport, db)

        def post_joined():
            self.transport.dht._refreshNode()
            self.market.republish_contracts()

        peers = seed_peers if seed_mode == 0 else []
        self.transport.join_network(peers)

        Thread(target=reactor.run, args=(False,)).start()

        handlers = [
            (r"/", MainHandler),
            (r"/main", MainHandler),
            (r"/html/(.*)", OpenBazaarStaticHandler, {'path': './html'}),
            (r"/ws", WebSocketHandler,
                dict(transport=self.transport, market_application=self, db=db))
        ]

        # TODO: Move debug settings to configuration location
        settings = dict(debug=True)
        tornado.web.Application.__init__(self, handlers, **settings)

    def get_transport(self):
        return self.transport

    def setup_upnp_port_mappings(self, http_port, p2p_port):
        upnp.PortMapper.DEBUG = False
        print "Setting up UPnP Port Map Entry..."
        # TODO: Add some setting whether or not to use UPnP
        # if Settings.get(Settings.USE_UPNP_PORT_MAPPINGS):
        self.upnp_mapper = upnp.PortMapper()
        # TODO: Add some setting whether or not to clean all previous port
        # mappings left behind by us
        # if Settings.get(Settings.CLEAN_UPNP_PORT_MAPPINGS_ON_START):
        #    upnp_mapper.cleanMyMappings()

        # for now let's always clean mappings every time.
        self.upnp_mapper.clean_my_mappings(p2p_port)
        # result_http_port_mapping = self.upnp_mapper.add_port_mapping(http_port,
        #                                                             http_port)
        # print ("UPnP HTTP Port Map configuration done (%s -> %s) => %s" %
        #        (str(http_port), str(http_port), str(result_http_port_mapping)))

        result_tcp_p2p_mapping = self.upnp_mapper.add_port_mapping(p2p_port,
                                                                   p2p_port)
        print ("UPnP TCP P2P Port Map configuration done (%s -> %s) => %s" %
               (str(p2p_port), str(p2p_port), str(result_tcp_p2p_mapping)))

        result_udp_p2p_mapping = self.upnp_mapper.add_port_mapping(p2p_port,
                                                                   p2p_port,
                                                                   'UDP')
        print ("UPnP UDP P2P Port Map configuration done (%s -> %s) => %s" %
               (str(p2p_port), str(p2p_port), str(result_udp_p2p_mapping)))

        result = result_tcp_p2p_mapping and result_udp_p2p_mapping
        if not result:
            print("Warning: UPnP was not setup correctly. Try doing a port forward on %s and start the node again with -j" % p2p_port)

        return result

    def cleanup_upnp_port_mapping(self):
        try:
            if self.upnp_mapper is not None:
                print "Cleaning UPnP Port Mapping -> ", \
                    self.upnp_mapper.clean_my_mappings(self.transport.port)
        except AttributeError:
            print "[openbazaar] MarketApplication.clean_upnp_port_mapping() failed!"

    def shutdown(self, x=None, y=None):
        print "MarketApplication.shutdown!"
        locallogger = logging.getLogger(
            '[%s] %s' % (self.market.market_id, 'root')
        )
        locallogger.info("Received TERMINATE, exiting...")

        # application.get_transport().broadcast_goodbye()
        self.cleanup_upnp_port_mapping()
        tornado.ioloop.IOLoop.instance().stop()

        self.transport.shutdown()
        os._exit(0)


def start_node(my_market_ip,
               my_market_port,
               http_ip,
               http_port,
               log_file,
               market_id,
               bm_user=None,
               bm_pass=None,
               bm_port=None,
               seed_peers=None,
               seed_mode=0,
               dev_mode=False,
               log_level=None,
               database='db/ob.db',
               disable_upnp=False,
               disable_open_browser=False,
               disable_sqlite_crypt=False,
               disable_ip_update=False):
    if seed_peers is None:
        seed_peers = []

    try:
        logging.basicConfig(
            level=int(log_level),
            format=u'%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            filename=log_file
        )
        logging._defaultFormatter = logging.Formatter(u'%(message)s')
        locallogger = logging.getLogger('[%s] %s' % (market_id, 'root'))

        handler = logging.handlers.RotatingFileHandler(
            log_file,
            encoding='utf-8',
            maxBytes=50,
            backupCount=0
        )
        locallogger.addHandler(handler)
    except Exception as e:
        print "Could not setup logger, continuing: ", e.message

    application = MarketApplication(my_market_ip,
                                    my_market_port,
                                    market_id,
                                    bm_user,
                                    bm_pass,
                                    bm_port,
                                    seed_peers,
                                    seed_mode,
                                    dev_mode,
                                    database,
                                    disable_sqlite_crypt,
                                    disable_ip_update)

    error = True
    p2p_port = my_market_port

    if http_port == -1:
        http_port = get_random_free_tcp_port(8889, 8988)

    while error:
        try:
            application.listen(http_port, http_ip)
            error = False
        except:
            http_port += 1

    if not disable_upnp:
        application.setup_upnp_port_mappings(http_port, p2p_port)
    else:
        print "Disabling upnp setup"

    locallogger.info("Started OpenBazaar Web App at http://%s:%s" %
                     (http_ip, http_port))

    print "Started OpenBazaar Web App at http://%s:%s" % (http_ip, http_port)
    print "Use ./stop.sh to stop"

    if not disable_open_browser:
        open_default_webbrowser('http://%s:%s' % (http_ip, http_port))

    try:
        signal.signal(signal.SIGTERM, application.shutdown)
    except ValueError:
        # not the main thread
        pass

    if not tornado.ioloop.IOLoop.instance():
        ioloop.install()
    else:
        try:
            tornado.ioloop.IOLoop.instance().start()
        except Exception as e:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("my_market_ip")
    parser.add_argument("-p", "--my_market_port",
                        type=int, default=12345)
    # default secure behavior is to keep HTTP port private
    parser.add_argument("-k", "--http_ip", default="127.0.0.1")
    parser.add_argument("-q", "--http_port", type=int, default=-1)
    parser.add_argument("-l", "--log_file",
                        default='logs/production.log')
    parser.add_argument("-u", "--market_id",
                        default=1)
    parser.add_argument("-S", "--seed_peers",
                        nargs='*', default=[])
    parser.add_argument("-s", "--seed_mode",
                        default=0)
    parser.add_argument("-d", "--dev_mode",
                        action='store_true')
    parser.add_argument("--database",
                        default='db/ob.db', help="Database filename")
    parser.add_argument("--bmuser",
                        default='username', help="Bitmessage instance user")
    parser.add_argument("--bmpass",
                        default='password', help="Bitmessage instance pass")
    parser.add_argument("--bmport",
                        default='8442', help="Bitmessage instance RPC port")
    parser.add_argument("--log_level",
                        default=10, help="Numeric value for logging level")
    parser.add_argument("--disable_upnp",
                        action='store_true')
    parser.add_argument("--disable_open_browser",
                        action='store_true',
                        default=False)
    parser.add_argument("--disable_sqlite_crypt",
                        action='store_true',
                        default=False)
    parser.add_argument("--disable-ip-update",
                        action='store_true',
                        default=False)

    args = parser.parse_args()
    start_node(args.my_market_ip,
               args.my_market_port,
               args.http_ip,
               args.http_port,
               args.log_file,
               args.market_id,
               args.bmuser,
               args.bmpass,
               args.bmport,
               args.seed_peers,
               args.seed_mode,
               args.dev_mode,
               args.log_level,
               args.database,
               args.disable_upnp,
               args.disable_open_browser,
               args.disable_sqlite_crypt,
               args.disable_ip_update)

# Run this if executed directly
if __name__ == "__main__":
    main()
