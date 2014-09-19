#!/bin/bash
# set -x

usage()
{
cat << EOF
usage: $0 options

This script starts up the OpenBazaar client and server.

OPTIONS:
  -h                        Help information
  -o                        Seed Mode
  -i                        Server Public IP
  -p                        Server Public Port (default 12345)
  -k                        Web Interface IP (default 127.0.0.1; use 0.0.0.0 for any)
  -q                        Web Interface Port (default random)
  -l                        Log file
  -d                        Development mode
  -n                        Number of Dev nodes to start up
  -a                        Bitmessage API username
  -b                        Bitmessage API password
  -c                        Bitmessage API port
  -u                        Market ID
  -j                        Disable upnp
  -s                        List of additional seeds
  -f                        Path to database file (default db/ob.db)
  --disable-open-browser    Don't open the default web browser when OpenBazaar starts
  --disable-sqlite-crypt    Disable encryption on sqlite database
EOF
}

PYTHON="./env/bin/python"
if [ ! -x $PYTHON ]; then
  echo "No python executable found at ${PYTHON}"
  if type python2 &>/dev/null; then
    PYTHON=python2
  elif type python &>/dev/null; then
    PYTHON=python
  else
    echo "No python executable found anywhere"
    exit
  fi
fi
if [[ "$OSTYPE" == "darwin"* ]]; then
  export DYLD_LIBRARY_PATH=$(brew --prefix openssl)/lib:${DYLD_LIBRARY_PATH}
fi
if [[ $OSTYPE == linux-gnu || $OSTYPE == linux-gnueabihf ]]; then
  if [ -f /etc/fedora-release ]; then
    export LD_LIBRARY_PATH="/opt/openssl-compat-bitcoin/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  fi
fi

# Default values
SERVER_PORT=12345
LOGDIR=logs
DBDIR=db
DBFILE=ob.db
DEVELOPMENT=0
SEED_URI='seed.openbazaar.org seed2.openbazaar.org seed.openlabs.co us.seed.bizarre.company eu.seed.bizarre.company'
LOG_FILE=production.log
DISABLE_UPNP=0
DISABLE_OPEN_DEFAULT_WEBBROWSER=0

# CRITICAL   50
# ERROR      40
# WARNING    30
# INFO       20
# DEBUG      10
# NOTSET      0
LOG_LEVEL=10

NODES=3
BM_USERNAME=brian
BM_PASSWORD=P@ssw0rd
BM_PORT=8442

HTTP_IP=127.0.0.1
HTTP_PORT=-1

# Tor Information
# - If you enable Tor here you will be operating a hidden
#   service behind your Tor proxy (notional)
TOR_ENABLE=0
TOR_CONTROL_PORT=9051
TOR_SERVER_PORT=9050
TOR_COOKIE_AUTHN=1
TOR_HASHED_CONTROL_PASSWORD=
TOR_PROXY_IP=127.0.0.1
TOR_PROXY_PORT=7000

while getopts "hp:l:dn:a:b:c:u:oi:jk:q:s:-:f:" OPTION
do
     case ${OPTION} in
         h)
             usage
             exit 1
             ;;
         p)
             SERVER_PORT=$OPTARG
             ;;
         l)
             LOG_FILE=$OPTARG
             ;;
         d)
             DEVELOPMENT=1
             ;;
         n)
             NODES=$OPTARG
             ;;
         a)
             BM_USERNAME=$OPTARG
             ;;
         b)
             BM_PASSWORD=$OPTARG
             ;;
         c)
             BM_PORT=$OPTARG
             ;;
         u)
             MARKET_ID=$OPTARG
             ;;
         o)
             SEED_MODE=1
             ;;
         i)
             SERVER_IP=$OPTARG
             ;;
         j)
             DISABLE_UPNP=1
             ;;
         k)
             HTTP_IP=$OPTARG
             ;;
         q)
             HTTP_PORT=$OPTARG
             ;;
         s)
             SEED_URI_ADD=$OPTARG
             ;;
         f)
             DATABASE_FILE=$OPTARG
             ;;
         -)
         	 case "${OPTARG}" in
         	     disable-open-browser)
                     DISABLE_OPEN_DEFAULT_WEBBROWSER=1
                     ;;
                 disable-sqlite-crypt)
                     DISABLE_SQLITE_CRYPT=1
                     ;;
                 *)
                     usage
                     exit
                     ;;
             esac;;
         ?)
             usage
             exit
             ;;
     esac
done

SEED_URI+=" $SEED_URI_ADD"
HTTP_OPTS="-k $HTTP_IP -q $HTTP_PORT"

if [ -z "$SERVER_IP" ]; then
    SERVER_IP=$(wget -qO- icanhazip.com)
    DISABLE_IP_UPDATE=""
else
    DISABLE_IP_UPDATE="--disable-ip-update"
fi

if [ ! -d "$LOGDIR" ]; then
    mkdir $LOGDIR
fi

if [ -z "$DATABASE_FILE" ]; then
    if [ ! -d "$DBDIR" ]; then
        mkdir "$DBDIR"
    fi
    DATABASE_FILE="$DBDIR/$DBFILE"
fi

if [ "$DISABLE_UPNP" == 1 ]; then
    echo "Disabling upnp"
    DISABLE_UPNP="--disable_upnp"
else
    DISABLE_UPNP=""
fi

if [ "$DISABLE_OPEN_DEFAULT_WEBBROWSER" == 1 ]; then
    DISABLE_OPEN_DEFAULT_WEBBROWSER="--disable_open_browser"
else
    DISABLE_OPEN_DEFAULT_WEBBROWSER=""
fi

if [ "$DISABLE_SQLITE_CRYPT" == 1 ]; then
    DISABLE_SQLITE_CRYPT="--disable_sqlite_crypt"
else
    DISABLE_SQLITE_CRYPT=""
fi

echo "OpenBazaar is starting..."

if [ "$SEED_MODE" == 1 ]; then
    echo "Seed Mode $SERVER_IP"

    if [ ! -f "$DATABASE_FILE" ]; then
       echo "File $DATABASE_FILE does not exist. Running setup script."
       $PYTHON node/setup_db.py "$DATABASE_FILE" $DISABLE_SQLITE_CRYPT
       wait
    fi

    $PYTHON node/openbazaar_daemon.py $SERVER_IP $HTTP_OPTS -p $SERVER_PORT $DISABLE_UPNP --database "$DATABASE_FILE" -s 1 --bmuser $BM_USERNAME --bmpass $BM_PASSWORD --bmport $BM_PORT -l $LOGDIR/production.log -u 1 --log_level $LOG_LEVEL $DISABLE_OPEN_DEFAULT_WEBBROWSER $DISABLE_SQLITE_CRYPT $DISABLE_IP_UPDATE &

elif [ "$DEVELOPMENT" == 0 ]; then
    echo "Production Mode"

    if [ ! -f "$DATABASE_FILE" ]; then
       echo "File $DATABASE_FILE does not exist. Running setup script."
       export PYTHONPATH=$PYTHONPATH:`pwd`
       $PYTHON node/setup_db.py "$DATABASE_FILE" $DISABLE_SQLITE_CRYPT
       wait
    fi

	$PYTHON node/openbazaar_daemon.py $SERVER_IP $HTTP_OPTS -p $SERVER_PORT $DISABLE_UPNP --database "$DATABASE_FILE" --bmuser $BM_USERNAME --bmpass $BM_PASSWORD --bmport $BM_PORT -S $SEED_URI -l $LOGDIR/production.log -u 1 --log_level $LOG_LEVEL $DISABLE_OPEN_DEFAULT_WEBBROWSER $DISABLE_SQLITE_CRYPT $DISABLE_IP_UPDATE &

else
	# Primary Market - No SEED_URI specified
	echo "Development Mode"

	if [ ! -f $DBDIR/ob-dev.db ]; then
       echo "File db/ob-dev.db does not exist. Running setup script."
       export PYTHONPATH=$PYTHONPATH:`pwd`
       $PYTHON node/setup_db.py db/ob-dev.db $DISABLE_SQLITE_CRYPT
       wait
    fi

	$PYTHON node/openbazaar_daemon.py 127.0.0.1 $HTTP_OPTS $DISABLE_UPNP --database db/ob-dev.db -s 1 --bmuser $BM_USERNAME -d --bmpass $BM_PASSWORD --bmport $BM_PORT -l $LOGDIR/development.log -u 1 --log_level $LOG_LEVEL $DISABLE_OPEN_DEFAULT_WEBBROWSER $DISABLE_SQLITE_CRYPT $DISABLE_IP_UPDATE &
    ((NODES=NODES+1))
    i=2
    while [[ $i -le $NODES ]]
    do
        sleep 2
        if [ ! -f db/ob-dev-$i.db ]; then
           echo "File db/ob-dev-$i.db does not exist. Running setup script."
           export PYTHONPATH=$PYTHONPATH:`pwd`
           $PYTHON node/setup_db.py db/ob-dev-$i.db $DISABLE_SQLITE_CRYPT
           wait
        fi
	    $PYTHON node/openbazaar_daemon.py 127.0.0.$i $HTTP_OPTS $DISABLE_UPNP --database db/ob-dev-$i.db -d --bmuser $BM_USERNAME --bmpass $BM_PASSWORD --bmport $BM_PORT -S 127.0.0.1 -l $LOGDIR/development.log -u $i --log_level $LOG_LEVEL $DISABLE_OPEN_DEFAULT_WEBBROWSER $DISABLE_SQLITE_CRYPT $DISABLE_IP_UPDATE &
	    ((i=i+1))
    done
fi
