#!/bin/bash
for pid in `ps aux | grep "python.*openbazaar_daemon.py" | grep -v grep | awk '{print $2}'`; do
  echo "Sending SIGTERM to ${pid}"
  kill ${pid}
  TRIES=50
  while [ 1 ]; do
    if ps -p $pid > /dev/null; then
      let TRIES-=1
      { usleep 100000 || sleep 0.1; } &>/dev/null
    else
      break
    fi
    if [ $TRIES -eq 0 ]; then
      echo "Still running, sending SIGKILL to ${pid}"
      kill -9 ${pid}
      break
    fi
  done
done
