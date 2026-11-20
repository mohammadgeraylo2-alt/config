#!/bin/bash
set -e
export PORT=8080
pick() { for d in "$@"; do [ -z "$d" ] && continue; if mkdir -p "$d" 2>/dev/null && touch "$d/.w" 2>/dev/null; then rm -f "$d/.w"; echo "$d"; return 0; fi; done; return 1; }
DATA_DIR="$(pick "$DATA_DIR" /app/data /tmp/panel-data)" || { echo "[start] no writable dir"; exit 1; }
export DATA_DIR
case "$DATA_DIR" in /tmp/*) echo "[start] WARNING: storage is not persistent. Mount a Volume at /app/data";; esac
export WS_PATH_FILE="$DATA_DIR/ws_path.txt"
[ -s "$WS_PATH_FILE" ] || python3 -c "import secrets;print('/'+secrets.token_hex(8))" > "$WS_PATH_FILE"
export WS_PATH="$(tr -d '\n' < "$WS_PATH_FILE")"
export NGINX_RUN_DIR=/tmp/nginx-run; mkdir -p $NGINX_RUN_DIR
envsubst '${PORT} ${NGINX_RUN_DIR} ${WS_PATH}' < /app/nginx.conf.template > $NGINX_RUN_DIR/nginx.conf
trap 'kill 0; exit 0' TERM INT
uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-server-header &
nginx -e /dev/stderr -c $NGINX_RUN_DIR/nginx.conf -g "daemon off;" &
wait -n; echo "[start] a process exited; stopping"; kill 0; exit 1
