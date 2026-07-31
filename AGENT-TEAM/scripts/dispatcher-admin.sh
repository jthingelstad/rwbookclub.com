#!/usr/bin/env bash

set -euo pipefail

LABEL="com.rwbookclub.agent-team-dispatcher"
ROOT="/Users/otto/Projects/rwbookclub.com"
SOURCE="$ROOT/AGENT-TEAM/ops/$LABEL.plist"
DESTINATION="/Users/otto/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
PYTHON="$ROOT/.venv/bin/python"
DISPATCHER="$ROOT/AGENT-TEAM/scripts/dispatcher.py"
CONFIG="$ROOT/AGENT-TEAM/dispatch.toml"
STDOUT_LOG="/Users/otto/Library/Logs/$LABEL.log"
STDERR_LOG="/Users/otto/Library/Logs/$LABEL.err"

install_agent() {
  mkdir -p "/Users/otto/Library/LaunchAgents"
  mkdir -p "/Users/otto/Library/Logs"
  mkdir -p "/Users/otto/Library/Application Support/$LABEL/runs"
  chmod 700 "/Users/otto/Library/Application Support/$LABEL"
  chmod 700 "/Users/otto/Library/Application Support/$LABEL/runs"
  touch "$STDOUT_LOG" "$STDERR_LOG"
  chmod 600 "$STDOUT_LOG" "$STDERR_LOG"
  install -m 600 "$SOURCE" "$DESTINATION"
  launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
  launchctl bootstrap "$DOMAIN" "$DESTINATION"
  echo "Installed and started $LABEL"
}

case "${1:-status}" in
  install)
    install_agent
    ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$LABEL"
    ;;
  stop)
    launchctl bootout "$DOMAIN/$LABEL"
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL"
    "$PYTHON" "$DISPATCHER" --config "$CONFIG" --status
    ;;
  check)
    "$PYTHON" "$DISPATCHER" --config "$CONFIG" --check --live --installed
    ;;
  shadow)
    "$PYTHON" "$DISPATCHER" --config "$CONFIG" --shadow --all
    ;;
  *)
    echo "Usage: $0 {install|restart|stop|status|check|shadow}" >&2
    exit 2
    ;;
esac
