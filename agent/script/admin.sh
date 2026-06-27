#!/bin/bash
set -e

LABEL="com.rwbookclub.oliver"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AGENT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$AGENT_DIR/.." && pwd)"
LOG_DIR="$AGENT_DIR/logs"
DB="$AGENT_DIR/oliver.db"
BACKUP_DIR="$AGENT_DIR/backups"
KEEP_RESTART_BACKUPS=10

resolve_venv() {
    if [ -n "${OLIVER_VENV:-}" ] && [ -x "$OLIVER_VENV/bin/python" ]; then
        echo "$OLIVER_VENV"
        return
    fi
    if [ -x "$REPO_ROOT/venv/bin/python" ]; then
        echo "$REPO_ROOT/venv"
        return
    fi
    if [ -x "$AGENT_DIR/venv/bin/python" ]; then
        echo "$AGENT_DIR/venv"
        return
    fi
    return 1
}

require_venv() {
    if ! VENV="$(resolve_venv)"; then
        echo "Error: no Python venv found." >&2
        echo "  Looked in: \$OLIVER_VENV, $REPO_ROOT/venv, $AGENT_DIR/venv" >&2
        echo "  Create one with:  python3.13 -m venv $REPO_ROOT/venv && $REPO_ROOT/venv/bin/pip install -r $AGENT_DIR/requirements.txt" >&2
        exit 1
    fi
    echo "$VENV"
}

# Take a consistent snapshot of the SQLite DB, then VACUUM it. Run while Oliver is stopped
# (no open connections), so VACUUM gets its exclusive lock and the snapshot is clean. The DB is
# in WAL mode, so a plain cp is unsafe — sqlite3's online .backup reads through the WAL correctly.
# Best-effort: a backup failure warns and skips the vacuum (never mutate the DB without a copy),
# and never aborts the restart it's part of.
backup_and_vacuum() {
    if [ ! -f "$DB" ]; then
        echo "==> No database at $DB; skipping backup + vacuum."
        return 0
    fi
    if ! VENV="$(resolve_venv)"; then
        echo "==> Warning: no venv found; skipping DB backup + vacuum." >&2
        return 0
    fi
    mkdir -p "$BACKUP_DIR"
    local stamp backup
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    backup="$BACKUP_DIR/oliver-restart-$stamp.db"
    echo "==> Backing up database -> backups/$(basename "$backup")"
    if "$VENV/bin/python" - "$DB" "$backup" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)  # consistent online snapshot; safe with WAL
PY
    then
        echo "    backup ok ($(du -h "$backup" | cut -f1 | tr -d ' '))"
    else
        echo "    Warning: backup failed; leaving the database untouched (no vacuum)." >&2
        rm -f "$backup"
        return 0
    fi
    # Rotate: keep the most recent restart backups, drop older ones (migration snapshots,
    # named differently, are left alone).
    ls -1t "$BACKUP_DIR"/oliver-restart-*.db 2>/dev/null | tail -n "+$((KEEP_RESTART_BACKUPS + 1))" | while read -r old; do
        echo "    pruning old backup backups/$(basename "$old")"
        rm -f "$old"
    done
    echo "==> Vacuuming database..."
    if "$VENV/bin/python" - "$DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute("VACUUM")
con.close()
PY
    then
        echo "    vacuum ok"
    else
        echo "    Warning: vacuum failed (the backup is still good)." >&2
    fi
}

status() {
    if launchctl list | grep -q "$LABEL"; then
        echo "oliver is running."
    else
        echo "oliver is stopped."
    fi
}

stop_bot() {
    echo "==> Stopping oliver..."
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    sleep 1
    status
}

start_bot() {
    if [ ! -f "$PLIST" ]; then
        echo "Error: plist not found at $PLIST"
        echo "Run '$0 install' first."
        exit 1
    fi
    echo "==> Starting oliver..."
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    sleep 3
    status
}

restart_bot() {
    stop_bot
    backup_and_vacuum
    start_bot
}

install_bot() {
    VENV="$(require_venv)"
    mkdir -p "$LOG_DIR"
    echo "==> Installing launchd plist..."
    echo "    venv:    $VENV"
    echo "    cwd:     $REPO_ROOT"
    echo "    logs:    $LOG_DIR"
    mkdir -p "$(dirname "$PLIST")"
    cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV/bin/python</string>
        <string>-m</string>
        <string>agent.bot</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO_ROOT</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$VENV/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/oliver.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/oliver.err</string>
</dict>
</plist>
PLIST
    echo "Installed $PLIST"
}

upgrade_bot() {
    VENV="$(require_venv)"
    stop_bot
    backup_and_vacuum

    echo "==> Pulling latest from origin..."
    (cd "$REPO_ROOT" && git pull origin main)

    echo "==> Updating dependencies..."
    "$VENV/bin/pip" install -q -r "$AGENT_DIR/requirements.txt"

    start_bot
}

tail_logs() {
    mkdir -p "$LOG_DIR"
    touch "$LOG_DIR/oliver.log" "$LOG_DIR/oliver.err"
    echo "==> Tailing $LOG_DIR/oliver.{log,err} (Ctrl-C to stop)..."
    tail -F "$LOG_DIR/oliver.log" "$LOG_DIR/oliver.err"
}

case "${1:-}" in
    stop)     stop_bot ;;
    start)    start_bot ;;
    restart)  restart_bot ;;
    upgrade)  upgrade_bot ;;
    backup)   backup_and_vacuum ;;
    install)  install_bot ;;
    status)   status ;;
    tail)     tail_logs ;;
    *)
        echo "Usage: $0 {start|stop|restart|upgrade|backup|install|status|tail}"
        exit 1
        ;;
esac
