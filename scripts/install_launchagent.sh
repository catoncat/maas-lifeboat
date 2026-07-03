#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT_DIR"
PLIST="$HOME/Library/LaunchAgents/rs.chen.maas-gateway.plist"
STDOUT_LOG="$ROOT_DIR/logs/maas-gateway.launchd.out.log"
STDERR_LOG="$ROOT_DIR/logs/maas-gateway.launchd.err.log"

if [[ ! -f "$APP_DIR/.env.local" ]]; then
  echo "Missing $APP_DIR/.env.local. Copy .env.example to .env.local and fill MAAS_API_KEY first." >&2
  exit 2
fi

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT_DIR/logs"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>rs.chen.maas-gateway</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP_DIR/scripts/start_gateway.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$ROOT_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$STDOUT_LOG</string>
  <key>StandardErrorPath</key>
  <string>$STDERR_LOG</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/rs.chen.maas-gateway"

echo "Installed and started rs.chen.maas-gateway"
echo "OpenAI base URL: http://127.0.0.1:18788/v1"
echo "Logs: $ROOT_DIR/logs/gateway_requests.jsonl"
echo "Launchd stdout: $STDOUT_LOG"
echo "Launchd stderr: $STDERR_LOG"
