import logging
import json
import multiprocessing
import os
import signal
from threading import Lock
import time

import tornado.httpserver
import tornado.netutil
import tornado.web
from zmq.eventloop import ioloop

from db_store import Obdb
from market import Market
from transport import CryptoTransportLayer
import upnp
from util import open_default_webbrowser, is_mac
from ws import WebSocketHandler

if is_mac():
    from util import osx_check_dyld_library_path
    osx_check_dyld_library_path()

ioloop.install()


class MainHandler(tornado.web.RequestHandler):
    def get(self):
        self.redirect("/html/index.html")


class OpenBazaarStaticHandler(tornado.web.StaticFileHandler):
    def set_extra_headers(self, path):
        self.set_header("X-Frame-Options", "DENY")
        self.set_header("X-Content-Type-Options", "nosniff")


class OpenBazaarContext(object):
    """
    This Object holds all of the runtime parameters
    necessary to start an OpenBazaar instance.

    This object is convenient to pass on method interfaces,
    and reduces issues of API inconsistencies (as in the order
    in which parameters are passed, which can cause bugs)
    """

    def __init__(self,
                 nat_status,
                 server_ip,
                 server_port,
                 http_ip,
                 http_port,
                 db_path,
                 log_path,
                 log_level,
                 market_id,
                 bm_user,
                 bm_pass,
                 bm_port,
                 seeds,
                 seed_mode,
                 dev_mode,
                 dev_nodes,
                 disable_upnp,
                 disable_stun_check,
                 disable_open_browser,
                 disable_sqlite_crypt,
                 enable_ip_checker):
        self.nat_status = nat_status
        self.server_ip = server_ip
        self.server_port = server_port
        self.http_ip = http_ip
        self.http_port = http_port
        self.db_path = db_path
        self.log_path = log_path
        self.log_level = log_level
        self.market_id = market_id
        self.bm_user = bm_user
        self.bm_pass = bm_pass
        self.bm_port = bm_port
        self.seeds = seeds
        self.seed_mode = seed_mode
        self.dev_mode = dev_mode
        self.dev_nodes = dev_nodes
        self.disable_upnp = disable_upnp
        self.disable_stun_check = disable_stun_check
        self.disable_open_browser = disable_open_browser
        self.disable_sqlite_crypt = disable_sqlite_crypt
        self.enable_ip_checker = enable_ip_checker

        # to deduct up-time, and (TODO) average up-time
        # time stamp in (non-local) Coordinated Universal Time format.
        self.started_utc_timestamp = long(time.time())

    def __repr__(self):
        r = {"server_ip": self.server_ip,
             "server_port": self.server_port,
             "http_ip": self.http_ip,
             "http_port": self.http_port,
             "log_path": self.log_path,
             "market_id": self.market_id,
             "bm_user": self.bm_user,
             "bm_pass": self.bm_pass,
             "bm_port": self.bm_port,
             "seeds": self.seeds,
             "seed_mode": self.seed_mode,
             "dev_mode": self.dev_mode,
             "dev_nodes": self.dev_nodes,
             "log_level": self.log_level,
             "db_path": self.db_path,
             "disable_upnp": self.disable_upnp,
             "disable_open_browser": self.disable_open_browser,
             "disable_sqlite_crypt": self.disable_sqlite_crypt,
             "enable_ip_checker": self.enable_ip_checker,
             "started_utc_timestamp": self.started_utc_timestamp,
             "uptime_in_secs": (long(time.time()) -
                                long(self.started_utc_timestamp))}

        return json.dumps(r).replace(", ", ",\n  ")

    @staticmethod
    def get_defaults():
        return {'market_id': 1,
                'server_ip': '127.0.0.1',
                'server_port': 12345,
                'log_dir': 'logs',
                'log_file': 'production.log',
                'dev_log_file': 'development-{0}.log',
                'db_dir': 'db',
                'db_file': 'ob.db',
                'dev_db_file': 'ob-dev-{0}.db',
                'dev_mode': False,
                'dev_nodes': 3,
                'seed_mode': False,
                'seeds': [
                    'seed.openbazaar.org',
                    'seed2.openbazaar.org',
                    'seed.openlabs.co',
                    'us.seed.bizarre.company',
                    'eu.seed.bizarre.company'
                ],
                'disable_upnp': False,
                'disable_stun_check': False,
                'disable_open_browser': False,
                'disable_sqlite_crypt': False,
                'log_level': 30,
                # CRITICAL=50 ERROR=40 WARNING=30 DEBUG=10 DEBUGV=7 DATADUMP=5 NOTSET=0
                'http_ip': '127.0.0.1',
                'http_port': 0,
                'bm_user': None,
                'bm_pass': None,
                'bm_port': -1,
                'enable_ip_checker': False,
                'config_file': None}

    @staticmethod
    def create_default_instance():
        defaults = OpenBazaarContext.get_defaults()
        return OpenBazaarContext(
            None,
            server_ip=defaults['server_ip'],
            server_port=defaults['server_port'],
            http_ip=defaults['http_ip'],
            http_port=defaults['http_port'],
            db_path=os.path.join(defaults['db_dir'], defaults['db_file']),
            log_path=os.path.join(defaults['log_dir'], defaults['log_file']),
            log_level=defaults['log_level'],
            market_id=defaults['market_id'],
            bm_user=defaults['bm_user'],
            bm_pass=defaults['bm_pass'],
            bm_port=defaults['bm_port'],
            seeds=defaults['seeds'],
            seed_mode=defaults['seed_mode'],
            dev_mode=defaults['dev_mode'],
            dev_nodes=defaults['dev_nodes'],
            disable_upnp=defaults['disable_upnp'],
            disable_stun_check=defaults['disable_stun_check'],
            disable_open_browser=defaults['disable_open_browser'],
            disable_sqlite_crypt=defaults['disable_sqlite_crypt'],
            enable_ip_checker=defaults['enable_ip_checker']
        )


class MarketApplication(tornado.web.Application):
    def __init__(self, ob_ctx):
        self.shutdown_mutex = Lock()
        self.ob_ctx = ob_ctx
        db = Obdb(ob_ctx.db_path, ob_ctx.disable_sqlite_crypt)
        self.transport = CryptoTransportLayer(ob_ctx, db)
        self.market = Market(self.transport, db)
        self.upnp_mapper = None

        peers = ob_ctx.seeds if not ob_ctx.seed_mode else []
        self.transport.join_network(peers)

        handlers = [
            (r"/", MainHandler),
            (r"/main", MainHandler),
            (r"/html/(.*)", OpenBazaarStaticHandler, {'path': './html'}),
            (r"/ws", WebSocketHandler,
             dict(transport=self.transport, market_application=self, db=db))
        ]

        # TODO: Move debug settings to configuration location
        settings = dict(debug=True)
        super(MarketApplication, self).__init__(handlers, **settings)

    def start_app(self):
        # If self.ob_ctx.http_port is 0, the kernel is queried for a port.
        sockets = tornado.netutil.bind_sockets(
            self.ob_ctx.http_port,
            address=self.ob_ctx.http_ip
        )
        server = tornado.httpserver.HTTPServer(self)
        server.add_sockets(sockets)

        self.ob_ctx.http_port = sockets[0].getsockname()[1]

        if not self.ob_ctx.disable_upnp:
            self.setup_upnp_port_mappings(self.ob_ctx.server_port)
        else:
            print "MarketApplication.start_app(): Disabling upnp setup"

    def get_transport(self):
        return self.transport

    def setup_upnp_port_mappings(self, p2p_port):
        result = False

        if not self.ob_ctx.disable_upnp:
            upnp.PortMapper.DEBUG = False
            print "Setting up UPnP Port Map Entry..."
            self.upnp_mapper = upnp.PortMapper()
            self.upnp_mapper.clean_my_mappings(p2p_port)

            result_tcp_p2p_mapping = self.upnp_mapper.add_port_mapping(
                p2p_port, p2p_port
            )
            print "UPnP TCP P2P Port Map configuration done ",
            print "(%s -> %s) => %s" % (
                p2p_port, p2p_port, result_tcp_p2p_mapping
            )

            result_udp_p2p_mapping = self.upnp_mapper.add_port_mapping(
                p2p_port, p2p_port, 'UDP'
            )
            print "UPnP UDP P2P Port Map configuration done ",
            print "(%s -> %s) => %s" % (
                p2p_port, p2p_port, result_udp_p2p_mapping
            )

            result = result_tcp_p2p_mapping and result_udp_p2p_mapping
            if not result:
                print "Warning: UPnP was not setup correctly. ",
                print "Ports could not be automatically mapped."

        return result

    def cleanup_upnp_port_mapping(self):
        if not self.ob_ctx.disable_upnp:
            try:
                if self.upnp_mapper is not None:
                    print "Cleaning UPnP Port Mapping -> ", \
                        self.upnp_mapper.clean_my_mappings(self.transport.port)
            except AttributeError:
                print (
                    "[openbazaar] "
                    "MarketApplication.clean_upnp_port_mapping() failed!"
                )

    def shutdown(self, x=None, y=None):
        self.shutdown_mutex.acquire()
        print "MarketApplication.shutdown!"
        log = logging.getLogger(
            '[%s] %s' % (self.market.market_id, 'root')
        )
        log.info("Received TERMINATE, exiting...")

        self.cleanup_upnp_port_mapping()
        tornado.ioloop.IOLoop.instance().stop()

        self.transport.shutdown()
        self.shutdown_mutex.release()
        os._exit(0)


def start_io_loop():
    if not tornado.ioloop.IOLoop.instance():
        ioloop.install()

    try:
        tornado.ioloop.IOLoop.instance().start()
    except Exception as e:
        print "openbazaar::start_io_loop Exception:", e
        raise


def create_custom_log_levels():
    logging.addLevelName(5, "DATADUMP")

    def datadump(self, message, *args, **kwargs):
        if self.isEnabledFor(5):
            self._log(5, message, args, **kwargs)

    logging.Logger.datadump = datadump

    logging.addLevelName(7, "DEBUGV")

    def debugv(self, message, *args, **kws):
        if self.isEnabledFor(7):
            self._log(7, message, args, **kws)

    logging.Logger.debugv = debugv


def create_logger(ob_ctx):
    logger = None
    try:
        logger = logging.getLogger()
        logger.setLevel(int(ob_ctx.log_level))

        handler = logging.handlers.RotatingFileHandler(
            ob_ctx.log_path,
            encoding='utf-8',
            maxBytes=50000000,
            backupCount=1
        )
        logFormat = logging.Formatter(
            u'%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(logFormat)
        logger.addHandler(handler)

        create_custom_log_levels()

    except Exception as e:
        print "Could not setup logger, continuing: ", e.message
    return logger


def log_openbazaar_start(log, ob_ctx):
    log.info("Started OpenBazaar Web App at http://%s:%s" %
             (ob_ctx.http_ip, ob_ctx.http_port))
    print "Started OpenBazaar Web App at http://%s:%s" % (ob_ctx.http_ip, ob_ctx.http_port)


def attempt_browser_open(ob_ctx):
    if not ob_ctx.disable_open_browser:
        open_default_webbrowser(
            'http://%s:%s' % (ob_ctx.http_ip, ob_ctx.http_port))


def setup_signal_handlers(application):
    try:
        signal.signal(signal.SIGTERM, application.shutdown)
    except ValueError:
        pass


def node_starter(ob_ctxs):
    # This is the target for the the Process which
    # will spawn the children processes that spawn
    # the actual OpenBazaar instances.

    for ob_ctx in ob_ctxs:
        p = multiprocessing.Process(
            target=start_node, args=(ob_ctx,),
            name="Process::openbazaar_daemon::target(start_node)")
        p.daemon = False  # python has to wait for this user thread to end.
        p.start()


def start_node(ob_ctx):
    logger = create_logger(ob_ctx)
    application = MarketApplication(ob_ctx)
    setup_signal_handlers(application)
    application.start_app()
    log_openbazaar_start(logger, ob_ctx)
    attempt_browser_open(ob_ctx)
    start_io_loop()
