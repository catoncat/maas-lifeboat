#!/usr/bin/env bash
set -euo pipefail

PLIST="$HOME/Library/LaunchAgents/rs.chen.maas-gateway.plist"

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
rm -f "$PLIST"
echo "Uninstalled rs.chen.maas-gateway"
