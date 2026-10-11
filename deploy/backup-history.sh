#!/usr/bin/env bash
# Nightly backup of the odds-history SQLite DB (the one piece of server state
# that can't be regenerated). Run by propsterminal-backup.timer as the app
# user; safe to run by hand any time:
#
#   sudo -u propsterminal /opt/propsterminal/app/deploy/backup-history.sh
#
# Uses sqlite3's online .backup (consistent even while the app is writing),
# checks the copy, gzips it into $BACKUP_DIR and keeps $BACKUP_KEEP_DAYS days.
# Optional off-box copy: set BACKUP_RCLONE_DEST in .env (e.g. "r2:propsterminal-
# backups/history") and install/configure rclone for the propsterminal user.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/propsterminal/app}
BACKUP_DIR=${BACKUP_DIR:-/opt/propsterminal/backups}
KEEP_DAYS=${BACKUP_KEEP_DAYS:-14}
DB=${ODDS_DB_PATH:-history/odds.sqlite3}
case "$DB" in /*) ;; *) DB="$APP_DIR/$DB" ;; esac

if [ ! -f "$DB" ]; then
  echo "no history DB at $DB -- nothing to back up"
  exit 0
fi

mkdir -p "$BACKUP_DIR"
stamp=$(date -u +%Y%m%d-%H%M%S)
tmp="$BACKUP_DIR/.odds-$stamp.sqlite3"
out="$BACKUP_DIR/odds-$stamp.sqlite3.gz"
trap 'rm -f "$tmp"' EXIT

sqlite3 "$DB" ".timeout 30000" ".backup '$tmp'"
check=$(sqlite3 "$tmp" "PRAGMA integrity_check;")
if [ "$check" != "ok" ]; then
  echo "integrity_check failed on the copy: $check" >&2
  exit 1
fi
gzip -c "$tmp" > "$out.part"
mv "$out.part" "$out"
echo "backup: $out ($(du -h "$out" | cut -f1))"

find "$BACKUP_DIR" -maxdepth 1 -name 'odds-*.sqlite3.gz' -mtime +"$((KEEP_DAYS - 1))" -print -delete

if [ -n "${BACKUP_RCLONE_DEST:-}" ]; then
  rclone copy --max-age 48h "$BACKUP_DIR" "$BACKUP_RCLONE_DEST" --include 'odds-*.sqlite3.gz'
  echo "copied off-box to $BACKUP_RCLONE_DEST"
fi
