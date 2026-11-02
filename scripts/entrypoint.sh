#!/usr/bin/env bash
set -euo pipefail
umask 077
mkdir -p /data/mpau/cookies /data/logs /data/media
Xvfb :99 -screen 0 1440x960x24 -nolisten tcp >/data/logs/xvfb.log 2>&1 &
xvfb_pid=$!
for attempt in $(seq 1 50); do
  if xdpyinfo -display :99 >/dev/null 2>&1; then break; fi
  sleep 0.2
done
xdpyinfo -display :99 >/dev/null
openbox >/data/logs/openbox.log 2>&1 &
openbox_pid=$!
# VNC is internal only. Access is proxied by the authenticated FastAPI WebSocket.
x11vnc -display :99 -localhost -rfbport 5900 -nopw -forever -shared -noxdamage -repeat -wait 25 >/data/logs/vnc.log 2>&1 &
vnc_pid=$!
# /usr/local/bin/python is the app interpreter; MPAU has its own isolated venv.
/usr/local/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1 --no-access-log &
app_pid=$!
cleanup() {
  kill -TERM "$app_pid" "$vnc_pid" "$openbox_pid" "$xvfb_pid" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' TERM INT
wait -n "$app_pid" "$vnc_pid" "$xvfb_pid"
