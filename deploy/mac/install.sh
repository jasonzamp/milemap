#!/bin/sh
# Install (or reinstall) the MileMap collector as a launchd agent for the current user.
set -eu
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
DEST="$HOME/Library/LaunchAgents/uk.milemap.collector.plist"
mkdir -p "$REPO/data/logs" "$HOME/Library/LaunchAgents"
sed -e "s|__REPO__|$REPO|g" -e "s|__UV__|$UV|g" \
  "$REPO/deploy/mac/uk.milemap.collector.plist.template" > "$DEST"
plutil -lint "$DEST"
launchctl bootout "gui/$(id -u)/uk.milemap.collector" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "Installed. Runs every 5 minutes. Log: $REPO/data/logs/collector.log"
echo "Status:  launchctl print gui/$(id -u)/uk.milemap.collector | grep -E 'state|last exit'"
echo "Gaps:    uv run milemap collect gaps"
