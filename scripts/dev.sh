#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
export APP_DATA="${APP_DATA:-$root/data}"
sd_port="${PORT:-3000}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --port) sd_port="$2"; shift 2 ;;
    --host) shift 2 ;;
    *) shift ;;
  esac
done
sd_python="${PYTHON_BIN:-python3}"
if [ -x "$root/.venv/bin/python" ]; then sd_python="$root/.venv/bin/python"; fi
exec "$sd_python" -m uvicorn app.main:app --host 0.0.0.0 --port "$sd_port" --no-access-log
