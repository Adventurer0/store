#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
docker compose up -d --build
echo 'ShopDock 正在启动，请打开 http://localhost:8080'
