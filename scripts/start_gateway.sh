#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT_DIR"

if [[ -f "$APP_DIR/.env.local" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$APP_DIR/.env.local"
  set +a
fi

export MAAS_PROXY_URL="${MAAS_PROXY_URL:-}"
export MAAS_CONTEXT_WINDOW="${MAAS_CONTEXT_WINDOW:-500000}"
export MAAS_MAX_TOKENS="${MAAS_MAX_TOKENS:-131072}"
export MAAS_GATEWAY_LOG="${MAAS_GATEWAY_LOG:-logs/gateway_requests.jsonl}"
export MAAS_GATEWAY_HOST="${MAAS_GATEWAY_HOST:-127.0.0.1}"
export MAAS_GATEWAY_PORT="${MAAS_GATEWAY_PORT:-18788}"
export PYTHONPATH="$APP_DIR${PYTHONPATH:+:$PYTHONPATH}"

if [[ -z "${MAAS_API_KEY:-}" ]]; then
  echo "MAAS_API_KEY is required. Put it in $APP_DIR/.env.local or export it before starting." >&2
  exit 2
fi

if [[ "${MAAS_STRICT_KEY_CHECK:-1}" != "0" ]]; then
  if [[ ${#MAAS_API_KEY} -lt 40 || "$MAAS_API_KEY" != *:* ]]; then
    cat >&2 <<EOF
MAAS_API_KEY does not look like the XFyun MAAS provider key.
Expected the upstream provider key in id:secret form, not the local gateway client key.
Fix $APP_DIR/.env.local:
  MAAS_API_KEY=<XFyun provider key, contains ':'>
  MAAS_GATEWAY_API_KEY=<local client key used by PI agents>
Set MAAS_STRICT_KEY_CHECK=0 only if you intentionally use a different upstream auth format.
EOF
    exit 2
  fi
fi

cd "$ROOT_DIR"
exec python3 -m uvicorn gateway.maas_gateway:app \
  --host "$MAAS_GATEWAY_HOST" \
  --port "$MAAS_GATEWAY_PORT"
