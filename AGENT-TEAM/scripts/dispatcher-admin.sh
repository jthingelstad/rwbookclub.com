#!/usr/bin/env bash

set -euo pipefail

LABEL="com.rwbookclub.agent-team-dispatcher"
ROOT="/Users/otto/Projects/rwbookclub.com"
DESTINATION="/Users/otto/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
PYTHON="$ROOT/.venv/bin/python"
DISPATCHER="$ROOT/AGENT-TEAM/scripts/dispatcher.py"
CONFIG="$ROOT/AGENT-TEAM/dispatch.toml"

uninstall_agent() {
  launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
  rm -f "$DESTINATION"
  echo "Stopped and uninstalled $LABEL"
}

disabled() {
  echo "Automatic dispatcher launch is disabled because shell-launched Codex runs are not normal app-visible project threads." >&2
  echo "Use '$0 shadow', then start the selected role from an active Codex app conversation." >&2
  exit 2
}

case "${1:-status}" in
  install)
    disabled
    ;;
  restart)
    disabled
    ;;
  stop)
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    echo "Stopped $LABEL"
    ;;
  uninstall)
    uninstall_agent
    ;;
  status)
    launchctl print "$DOMAIN/$LABEL" || echo "$LABEL is not loaded"
    "$PYTHON" "$DISPATCHER" --config "$CONFIG" --status
    ;;
  check)
    "$PYTHON" "$DISPATCHER" --config "$CONFIG" --check --live
    ;;
  shadow)
    "$PYTHON" "$DISPATCHER" --config "$CONFIG" --shadow --all
    ;;
  *)
    echo "Usage: $0 {install|restart|stop|uninstall|status|check|shadow}" >&2
    exit 2
    ;;
esac
