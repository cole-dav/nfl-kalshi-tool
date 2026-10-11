#!/usr/bin/env bash
# Copy the laptop-only state to the VPS. Run ON THE LAPTOP from the main
# checkout, after deploy/bootstrap.sh has run on the server:
#
#   deploy/migrate.sh [options] [ssh-host]       (ssh-host default: $PT_HOST or "propsterminal")
#   e.g. deploy/migrate.sh --secrets --tunnel root@37.27.158.56
#
#   (no option)   same as --secrets --tunnel
#   --secrets     .env and keys/            -> /opt/propsterminal/app (0600, app user)
#   --tunnel      ~/.cloudflared/<id>.json  -> /etc/cloudflared/       (0640 root:cloudflared)
#   --history     odds-history DB, via sqlite3 .backup (consistent while the laptop
#                 app writes). Stops the VPS app while it swaps the file in.
#   --bdb         data/bdb/ (3.9 GB Big Data Bowl tracking; Viz Lab replays only)
#   --cache       cache/ minus the Kalshi HTTP cache: nflverse parquet + snapshots,
#                 so the VPS starts warm (a cold start peaks near 1.8 GB RSS)
#   --snapshots   only cache/snapshots/ (saved pages under 6h old are reloaded)
#   --all         everything above
#
# The ssh host must log in as root (see deploy/README.md, ~/.ssh/config).
# Never prints secret values.
set -euo pipefail

TUNNEL_ID=e278a4c1-40d7-45d6-9916-37688134dc98
APP_DIR=/opt/propsterminal/app
SRC=${SRC:-$(cd "$(dirname "$0")/.." && pwd)}
CF_DIR=${CF_DIR:-$HOME/.cloudflared}

secrets=0 tunnel=0 history=0 bdb=0 cache=0 snapshots=0 host=${PT_HOST:-propsterminal}
for arg in "$@"; do
  case "$arg" in
    --secrets) secrets=1 ;;
    --tunnel) tunnel=1 ;;
    --history) history=1 ;;
    --bdb) bdb=1 ;;
    --cache) cache=1 ;;
    --snapshots) snapshots=1 ;;
    --all) secrets=1 tunnel=1 history=1 bdb=1 cache=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    -*) echo "unknown option $arg" >&2; exit 1 ;;
    *) host=$arg ;;
  esac
done
if [ $((secrets + tunnel + history + bdb + cache + snapshots)) -eq 0 ]; then
  secrets=1 tunnel=1
fi

log() { printf '\n==> %s\n' "$*"; }
remote() { ssh -o BatchMode=yes "$host" "$@"; }

log "checking $host"
remote "test \"\$(id -u)\" = 0 && test -d $APP_DIR && id propsterminal >/dev/null" \
  || { echo "need root ssh to $host and a bootstrapped $APP_DIR" >&2; exit 1; }
echo "source checkout: $SRC"

if [ "$secrets" = 1 ]; then
  log ".env and keys/"
  [ -f "$SRC/.env" ] || { echo "no $SRC/.env" >&2; exit 1; }
  # Names only, never values.
  for k in KALSHI_API_KEY_ID KALSHI_PRIVATE_KEY_PATH ANTHROPIC_API_KEY YOUTUBE_API_KEY; do
    if grep -Eq "^${k}=.+" "$SRC/.env"; then echo "  .env has $k"; else echo "  .env lacks $k (set it in the server's .env if the site needs it)"; fi
  done
  rsync -a "$SRC/.env" "$host:$APP_DIR/.env"
  [ -d "$SRC/keys" ] && rsync -a "$SRC/keys/" "$host:$APP_DIR/keys/"
  remote "chown -R propsterminal:propsterminal $APP_DIR/.env $APP_DIR/keys \
    && chmod 600 $APP_DIR/.env && chmod 700 $APP_DIR/keys && find $APP_DIR/keys -type f -exec chmod 600 {} +"
fi

if [ "$tunnel" = 1 ]; then
  log "tunnel credentials"
  creds="$CF_DIR/$TUNNEL_ID.json"
  [ -f "$creds" ] || { echo "no $creds" >&2; exit 1; }
  if [ -f "$CF_DIR/config.yml" ] && ! grep -q "$TUNNEL_ID" "$CF_DIR/config.yml"; then
    echo "!! $CF_DIR/config.yml doesn't name tunnel $TUNNEL_ID -- check deploy/cloudflared/config.yml" >&2
  fi
  rsync -a "$creds" "$host:/etc/cloudflared/$TUNNEL_ID.json"
  remote "chown root:cloudflared /etc/cloudflared/$TUNNEL_ID.json && chmod 640 /etc/cloudflared/$TUNNEL_ID.json \
    && cloudflared --config /etc/cloudflared/config.yml tunnel ingress validate"
fi

if [ "$history" = 1 ]; then
  log "odds-history DB"
  db=${ODDS_DB_PATH:-history/odds.sqlite3}
  case "$db" in /*) ;; *) db="$SRC/$db" ;; esac
  if [ ! -f "$db" ]; then
    echo "  no history DB at $db -- skipping"
  else
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    sqlite3 "$db" ".timeout 30000" ".backup '$tmp/odds.sqlite3'"
    [ "$(sqlite3 "$tmp/odds.sqlite3" 'PRAGMA integrity_check;')" = ok ] || { echo "copy failed integrity_check" >&2; exit 1; }
    echo "  snapshot: $(du -h "$tmp/odds.sqlite3" | cut -f1)"
    remote "install -d -o propsterminal -g propsterminal -m 750 $APP_DIR/history"
    rsync -a "$tmp/odds.sqlite3" "$host:$APP_DIR/history/odds.sqlite3.incoming"
    # Keep whatever the VPS had as .before-migrate, then swap in the copy with
    # the app stopped so no WAL/journal from the old file is left behind.
    remote "set -e; cd $APP_DIR/history
      systemctl stop propsterminal
      if [ -f odds.sqlite3 ]; then sqlite3 odds.sqlite3 \".backup 'odds.sqlite3.before-migrate'\"; fi
      rm -f odds.sqlite3-wal odds.sqlite3-shm odds.sqlite3-journal
      mv odds.sqlite3.incoming odds.sqlite3
      chown propsterminal:propsterminal odds.sqlite3*
      chmod 640 odds.sqlite3
      systemctl start propsterminal"
    echo "  installed; VPS app restarted"
  fi
fi

if [ "$bdb" = 1 ]; then
  log "data/bdb (large; resumable -- re-run if interrupted)"
  rsync -a --partial --progress "$SRC/data/bdb/" "$host:$APP_DIR/data/bdb/"
  remote "chown -R propsterminal:propsterminal $APP_DIR/data/bdb"
fi

if [ "$cache" = 1 ]; then
  log "cache/ (without kalshi_api)"
  rsync -a --partial --exclude kalshi_api/ "$SRC/cache/" "$host:$APP_DIR/cache/"
  remote "chown -R propsterminal:propsterminal $APP_DIR/cache"
elif [ "$snapshots" = 1 ]; then
  log "cache/snapshots"
  rsync -a "$SRC/cache/snapshots/" "$host:$APP_DIR/cache/snapshots/"
  remote "chown -R propsterminal:propsterminal $APP_DIR/cache"
fi

log "done"
echo "Restart the app to pick up new .env/cache: ssh $host systemctl restart propsterminal"
