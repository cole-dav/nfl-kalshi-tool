#!/usr/bin/env bash
# One-time (and safe to re-run) setup of a fresh Ubuntu 26.04 VPS for
# propsterminal.com. Run as root ON THE SERVER:
#
#   scp deploy/bootstrap.sh root@37.27.158.56:/root/   # from the laptop
#   bash bootstrap.sh
#
# What it does (every step checks before it acts, so re-running is harmless):
#   1. apt packages, unattended-upgrades, journald size cap, 2 GB swapfile
#   2. unprivileged `propsterminal` user, /opt/propsterminal/{app,venv,backups}
#   3. GitHub deploy key for that user; clones the repo (first run stops here
#      and prints the key if GitHub doesn't accept it yet -- add it, re-run)
#   4. Python 3.14 (Ubuntu 26.04's own python3; falls back to uv if the
#      distro python isn't 3.14) + venv + requirements.txt
#   5. cloudflared from Cloudflare's apt repo, run as a `cloudflared` user
#   6. systemd units (app, tunnel, nightly history backup) and ufw (SSH only)
#
# Secrets (.env, keys/, tunnel credentials) are NOT handled here -- they come
# from the laptop via deploy/migrate.sh.
#
# Environment overrides:
#   REPO_URL       git URL to clone   (default: the GitHub repo over SSH)
#   BRANCH         branch to deploy   (default: master)
#   SKIP_SYSTEMD=1 don't enable/start units, ufw, swap (auto in containers)
#   SKIP_CLONE=1   app dir already populated (e.g. rsync'd); don't git clone
set -euo pipefail

APP_USER=propsterminal
APP_ROOT=/opt/propsterminal
APP_DIR=$APP_ROOT/app
VENV=$APP_ROOT/venv
REPO_URL=${REPO_URL:-git@github.com:cole-dav/nfl-kalshi-tool.git}
BRANCH=${BRANCH:-master}
PY_VERSION=3.14

log() { printf '\n==> %s\n' "$*"; }
as_app() { runuser -u "$APP_USER" -- "$@"; }

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

# systemd isn't PID 1 in a plain container; skip what needs it.
if [ "${SKIP_SYSTEMD:-}" = 1 ] || [ ! -d /run/systemd/system ]; then
  SKIP_SYSTEMD=1
  log "systemd not running (container?) -- units are installed but not enabled/started"
else
  SKIP_SYSTEMD=0
fi

export DEBIAN_FRONTEND=noninteractive

log "apt packages"
apt-get update -q
apt-get install -y -q --no-install-recommends \
  ca-certificates curl git gnupg openssh-client rsync sqlite3 ufw \
  unattended-upgrades python3 python3-venv tzdata
# python3.14-venv provides ensurepip for that exact version on 26.04.
apt-get install -y -q --no-install-recommends "python${PY_VERSION}-venv" 2>/dev/null || true

log "unattended-upgrades (security updates, no automatic reboot)"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

log "journald: cap logs at 500 MB / 1 month"
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/propsterminal.conf <<'EOF'
[Journal]
SystemMaxUse=500M
SystemKeepFree=2G
MaxRetentionSec=1month
EOF
[ "$SKIP_SYSTEMD" = 1 ] || systemctl restart systemd-journald

if [ "$SKIP_SYSTEMD" = 0 ] && ! swapon --show=NAME --noheadings | grep -q .; then
  log "2 GB swapfile (cold-start nflverse builds spike RSS to ~1.8 GB)"
  [ -f /swapfile ] || { fallocate -l 2G /swapfile; chmod 600 /swapfile; mkswap /swapfile; }
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo 'vm.swappiness=10' > /etc/sysctl.d/90-propsterminal.conf
  sysctl -q -p /etc/sysctl.d/90-propsterminal.conf
fi

log "user $APP_USER and $APP_ROOT"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$APP_ROOT" \
  --shell /bin/bash "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$APP_ROOT"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$APP_ROOT/backups"
install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$APP_ROOT/.ssh"

if [ "${SKIP_CLONE:-}" != 1 ] && [ ! -d "$APP_DIR/.git" ]; then
  if [[ "$REPO_URL" == git@github.com:* ]]; then
    log "GitHub deploy key"
    if [ ! -f "$APP_ROOT/.ssh/id_ed25519" ]; then
      as_app ssh-keygen -q -t ed25519 -N '' -C "propsterminal-deploy@$(hostname)" -f "$APP_ROOT/.ssh/id_ed25519"
    fi
    if ! grep -q '^github.com ' "$APP_ROOT/.ssh/known_hosts" 2>/dev/null; then
      ssh-keyscan -t ed25519 github.com 2>/dev/null | as_app tee -a "$APP_ROOT/.ssh/known_hosts" >/dev/null
    fi
  fi
  log "clone $REPO_URL ($BRANCH) -> $APP_DIR"
  if ! as_app git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"; then
    cat <<EOF

!! Clone failed. If the repo is private, add this READ-ONLY deploy key at
!! https://github.com/cole-dav/nfl-kalshi-tool/settings/keys  (leave "Allow write access" off)
!! then re-run this script:

$(cat "$APP_ROOT/.ssh/id_ed25519.pub" 2>/dev/null)
EOF
    exit 2
  fi
fi
[ -d "$APP_DIR" ] || { echo "$APP_DIR missing (SKIP_CLONE=1 but nothing there)" >&2; exit 1; }
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$APP_DIR/cache" "$APP_DIR/history" "$APP_DIR/data/bdb"
install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$APP_DIR/keys"

log "Python $PY_VERSION"
PYTHON=""
if command -v "python$PY_VERSION" >/dev/null && "python$PY_VERSION" -c 'import ensurepip, venv' 2>/dev/null; then
  PYTHON=$(command -v "python$PY_VERSION")
  echo "using distro $("$PYTHON" --version)"
else
  # Fallback for a distro without 3.14 (e.g. Ubuntu 24.04 ships 3.12): a uv-
  # managed CPython, stored under $APP_ROOT so the app user can read it.
  command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL=/usr/local/bin sh
  as_app env UV_PYTHON_INSTALL_DIR="$APP_ROOT/python" uv python install "$PY_VERSION"
  PYTHON=$(as_app env UV_PYTHON_INSTALL_DIR="$APP_ROOT/python" uv python find "$PY_VERSION")
  echo "using uv-managed $("$PYTHON" --version)"
fi

log "venv $VENV + requirements"
if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -c "import sys; assert sys.version_info[:2] == ($PY_VERSION)" 2>/dev/null; then
  rm -rf "$VENV"
  as_app "$PYTHON" -m venv "$VENV"
fi
as_app "$VENV/bin/python" -m pip install -q --upgrade pip
as_app "$VENV/bin/python" -m pip install -q -r "$APP_DIR/requirements.txt"
sha256sum "$APP_DIR/requirements.txt" | as_app tee "$VENV/.requirements.sha256" >/dev/null

log "cloudflared (Cloudflare apt repo, suite 'any' -- there's no per-codename suite for 26.04)"
if ! command -v cloudflared >/dev/null; then
  install -d -m 0755 /usr/share/keyrings
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
  echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
    > /etc/apt/sources.list.d/cloudflared.list
  if ! { apt-get update -q && apt-get install -y -q cloudflared; }; then
    # Repo unreachable or key rotated: install the release .deb directly.
    arch=$(dpkg --print-architecture)
    curl -fsSL -o /tmp/cloudflared.deb \
      "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}.deb"
    apt-get install -y -q /tmp/cloudflared.deb
    rm -f /tmp/cloudflared.deb
  fi
fi
cloudflared --version
id cloudflared >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin cloudflared
install -d -o root -g cloudflared -m 0750 /etc/cloudflared
if [ ! -f /etc/cloudflared/config.yml ]; then
  install -o root -g cloudflared -m 0640 "$APP_DIR/deploy/cloudflared/config.yml" /etc/cloudflared/config.yml
fi

log "systemd units"
CLOUDFLARED_BIN=$(command -v cloudflared)
install -m 0644 "$APP_DIR/deploy/systemd/propsterminal.service" /etc/systemd/system/
install -m 0644 "$APP_DIR/deploy/systemd/propsterminal-backup.service" /etc/systemd/system/
install -m 0644 "$APP_DIR/deploy/systemd/propsterminal-backup.timer" /etc/systemd/system/
sed "s|/usr/bin/cloudflared|$CLOUDFLARED_BIN|" "$APP_DIR/deploy/systemd/propsterminal-tunnel.service" \
  > /etc/systemd/system/propsterminal-tunnel.service
chmod 0644 /etc/systemd/system/propsterminal-tunnel.service

if [ "$SKIP_SYSTEMD" = 0 ]; then
  systemctl daemon-reload
  systemctl enable --now propsterminal.service
  systemctl enable --now propsterminal-backup.timer
  # The tunnel is enabled but NOT started: starting it while the laptop's
  # connector is up splits traffic between two servers. See deploy/README.md
  # "Cutover".
  systemctl enable propsterminal-tunnel.service

  log "firewall: inbound SSH only (the tunnel is outbound)"
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  ufw allow OpenSSH >/dev/null
  ufw --force enable >/dev/null
  ufw status verbose | sed -n '1,8p'

  log "sshd: keys only"
  if [ -s /root/.ssh/authorized_keys ]; then
    cat > /etc/ssh/sshd_config.d/60-propsterminal.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
    systemctl reload ssh || systemctl reload sshd || true
  else
    echo "!! /root/.ssh/authorized_keys is empty -- leaving password auth alone"
  fi
fi

log "done"
cat <<EOF
App:     $APP_DIR (user $APP_USER), venv $VENV
Next:    from the laptop run deploy/migrate.sh to copy .env, keys, tunnel
         credentials and history, then follow "Cutover" in deploy/README.md.
Check:   curl -s localhost:8765/healthz ; journalctl -u propsterminal -f
EOF
