#!/usr/bin/env bash
# Start / stop / check the Thor lab server.
#   ./thorctl.sh start | stop | restart | status | log
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$HERE/server.pid"
LOG="$HERE/server.log"

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

case "${1:-status}" in
  start)
    if running; then echo "already running (pid $(cat "$PIDFILE"))"; exit 0; fi
    nohup /usr/bin/python3 "$HERE/server.py" --config "$HERE/config.yaml" >>"$LOG" 2>&1 &
    echo $! >"$PIDFILE"
    sleep 1
    if running; then echo "started (pid $(cat "$PIDFILE")), log: $LOG"; else echo "failed to start; see $LOG"; tail -5 "$LOG"; exit 1; fi
    ;;
  stop)
    if running; then kill "$(cat "$PIDFILE")"; sleep 1; echo "stopped"; else echo "not running"; fi
    rm -f "$PIDFILE"
    ;;
  restart) "$0" stop; "$0" start ;;
  status) if running; then echo "running (pid $(cat "$PIDFILE"))"; else echo "not running"; fi ;;
  log) tail -n 50 -f "$LOG" ;;
  *) echo "usage: $0 start|stop|restart|status|log"; exit 2 ;;
esac
