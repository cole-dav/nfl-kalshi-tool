#!/usr/bin/env bash
# Ship master to the VPS. Run ON THE LAPTOP from the main checkout:
#
#   deploy/deploy.sh [ssh-host]              deploy origin/master (pushes local master first)
#   deploy/deploy.sh --ref <sha|tag> [host]  deploy a specific commit (e.g. to roll back)
#   deploy/deploy.sh --rollback [host]       redeploy the commit that ran before the last deploy
#
# ssh-host defaults to $PT_HOST or "propsterminal" (an ~/.ssh/config alias that
# logs in as root). On the server it: fetches, hard-resets the checkout to the
# target commit, reinstalls requirements if requirements.txt changed, refreshes
# the systemd units, restarts the app and polls /healthz. Exits non-zero if the
# new process doesn't answer /healthz within $HEALTH_TIMEOUT seconds (default 90).
set -euo pipefail

BRANCH=${BRANCH:-master}
HEALTH_TIMEOUT=${HEALTH_TIMEOUT:-90}
host=${PT_HOST:-propsterminal} ref="" rollback=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) ref=${2:?--ref needs a commit}; shift ;;
    --rollback) rollback=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 1 ;;
    *) host=$1 ;;
  esac
  shift
done

if [ -z "$ref" ] && [ "$rollback" = 0 ]; then
  # Make sure what we deploy is what's on GitHub.
  git fetch -q origin "$BRANCH"
  local_rev=$(git rev-parse "$BRANCH")
  remote_rev=$(git rev-parse "origin/$BRANCH")
  if [ "$local_rev" != "$remote_rev" ]; then
    if git merge-base --is-ancestor "$remote_rev" "$local_rev"; then
      echo "==> pushing $BRANCH ($(git rev-list --count "$remote_rev..$local_rev") new commits)"
      git push -q origin "$BRANCH"
    else
      echo "local $BRANCH and origin/$BRANCH have diverged (or local is behind) -- sort that out first" >&2
      exit 1
    fi
  fi
  ref="origin/$BRANCH"
fi

echo "==> deploying to $host"
ssh -o BatchMode=yes "$host" \
  REF="'$ref'" ROLLBACK="$rollback" BRANCH="$BRANCH" HEALTH_TIMEOUT="$HEALTH_TIMEOUT" bash -s <<'REMOTE'
set -euo pipefail
APP_ROOT=/opt/propsterminal
APP_DIR=$APP_ROOT/app
VENV=$APP_ROOT/venv
as_app() { runuser -u propsterminal -- "$@"; }
cd "$APP_DIR"

as_app git fetch -q origin "$BRANCH"
prev=$(as_app git rev-parse HEAD)
if [ "$ROLLBACK" = 1 ]; then
  [ -s "$APP_ROOT/.previous-rev" ] || { echo "no previous deploy recorded" >&2; exit 1; }
  target=$(cat "$APP_ROOT/.previous-rev")
else
  target=$(as_app git rev-parse --verify "$REF^{commit}")
fi
if [ -n "$(as_app git status --porcelain --untracked-files=no)" ]; then
  echo "!! server checkout had local edits; they are being discarded:"
  as_app git status --short --untracked-files=no
fi
as_app git reset -q --hard
as_app git checkout -q -B "$BRANCH" "$target"
[ "$prev" = "$target" ] || echo "$prev" > "$APP_ROOT/.previous-rev"
echo "code:  ${prev:0:9} -> ${target:0:9}  $(as_app git log -1 --format=%s)"

want=$(sha256sum requirements.txt)
if [ "$want" != "$(cat "$VENV/.requirements.sha256" 2>/dev/null)" ]; then
  echo "requirements.txt changed -- pip install"
  as_app "$VENV/bin/python" -m pip install -q -r requirements.txt
  echo "$want" | as_app tee "$VENV/.requirements.sha256" >/dev/null
fi

# Pick up unit changes shipped in deploy/systemd/.
install -m 0644 deploy/systemd/propsterminal.service deploy/systemd/propsterminal-backup.service \
  deploy/systemd/propsterminal-backup.timer /etc/systemd/system/
sed "s|/usr/bin/cloudflared|$(command -v cloudflared)|" deploy/systemd/propsterminal-tunnel.service \
  > /etc/systemd/system/propsterminal-tunnel.service
systemctl daemon-reload

started=$(date +%s)
systemctl restart propsterminal
for _ in $(seq "$HEALTH_TIMEOUT"); do
  body=$(curl -fsS --max-time 2 http://127.0.0.1:8765/healthz 2>/dev/null) || body=""
  if [ -n "$body" ]; then
    up=$(printf '%s' "$body" | sed -n 's/.*"uptime_s": *\([0-9]*\).*/\1/p')
    if [ -n "$up" ] && [ "$up" -le $(( $(date +%s) - started + 1 )) ]; then
      echo "healthz: $body"
      echo "OK: ${target:0:9} live after $(( $(date +%s) - started ))s"
      exit 0
    fi
  fi
  sleep 1
done
echo "FAILED: no healthy /healthz after ${HEALTH_TIMEOUT}s. Last log lines:" >&2
journalctl -u propsterminal -n 40 --no-pager >&2
echo "Roll back with: deploy/deploy.sh --rollback" >&2
exit 1
REMOTE
