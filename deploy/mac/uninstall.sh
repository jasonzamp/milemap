#!/bin/sh
# Stop and remove the MileMap collector launchd agent (data and state are kept).
set -eu
launchctl bootout "gui/$(id -u)/uk.milemap.collector" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/uk.milemap.collector.plist"
echo "Removed. The next run after reinstalling will record the gap automatically."
