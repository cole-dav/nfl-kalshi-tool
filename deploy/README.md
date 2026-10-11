# Running propsterminal.com on a VPS

This moves the live site off the MacBook onto an always-on Ubuntu server.
Nothing about the public setup changes: the same named Cloudflare tunnel
(`propsterminal`, id `e278a4c1-40d7-45d6-9916-37688134dc98`) serves both
hostnames. Only the machine its connector runs on changes. The tunnel only
makes outbound connections, so the server opens no web ports. Its firewall
lets in SSH and nothing else.

| File | Runs on | Does |
|---|---|---|
| `bootstrap.sh` | server, as root | One-time setup. Safe to re-run. |
| `migrate.sh` | laptop | Copies secrets, tunnel credentials, history DB, and optionally `data/bdb` and caches. |
| `deploy.sh` | laptop | Pushes `master`, updates the server, restarts the app, and checks `/healthz`. |
| `backup-history.sh` | server (timer) | Nightly `sqlite3 .backup` of the odds-history DB. Keeps 14 days. |
| `systemd/` | server | `propsterminal` (app), `propsterminal-tunnel` (cloudflared), and `propsterminal-backup` (timer + service). |
| `cloudflared/config.yml` | server | Becomes `/etc/cloudflared/config.yml`. Same tunnel and ingress as the laptop, `protocol: http2`. |

## The server

**Target: Hetzner Cloud CX23.** It has 2 shared x86 vCPUs, 4 GB RAM (3.7 GiB
usable), a 40 GB disk (38 GB root) and 20 TB of traffic, for about $6.49/mo.
Prices change, so check the current price. It runs Ubuntu 26.04 LTS
(`resolute`) in Helsinki at `37.27.158.56`.

Why this size is enough:

- **RAM.** The app sits around 0.3–0.6 GB RSS. A cold start with an empty
  `cache/` peaked at about 1.8 GB while nflverse data downloaded and built
  (measured on the laptop), then dropped to about 1.1 GB. To cover that:
  - `bootstrap.sh` adds a 2 GB swapfile (swappiness 10).
  - The unit sets `MemoryHigh=60%` (about 2.2 GB, where the kernel starts reclaiming) and `MemoryMax=75%` (about 2.8 GB, where the app is OOM-killed and restarted by systemd).
  - `migrate.sh --cache` copies the laptop's warm `cache/`, so the first start skips most of that spike.
  - Viz Lab replays of the large Kaggle tracking files are the other big allocation. If the box swaps a lot, move to CX33 (8 GB).
- **CPU.** Work is mostly I/O-bound (Kalshi is throttled to `KALSHI_MAX_RPS`=8). The pandas builds are short bursts, so 2 shared vCPUs are fine.
- **Disk (38 GB).** Rough budget:

  | | size |
  |---|---|
  | Ubuntu + packages | ~4 GB |
  | venv (pandas, numpy, polars, pyarrow…) | ~1 GB |
  | `data/bdb` (Kaggle Big Data Bowl tracking, optional) | 3.9 GB |
  | `cache/` (nflverse parquet, snapshots, Kalshi HTTP cache; regenerable) | ~0.2 GB, grows ~50 MB/season |
  | `history/odds.sqlite3` (Phase 3) | grows; watch it |
  | `backups/` (14 × gzipped DB) | roughly 14 × ⅓ DB size |
  | journald (capped by bootstrap) | ≤ 500 MB |
  | swapfile | 2 GB |

  That is about 11 GB plus history, which leaves 25+ GB of headroom. Check
  it with `df -h /` and `du -sh /opt/propsterminal/*`. If history grows past
  a few GB, lower `BACKUP_KEEP_DAYS` in `.env` or push backups off-box.

**Location caveat (Helsinki).** Kalshi is a US exchange.
- **Public market data.** This is all most visitors trigger. It worked from
  the laptop's US IP, but check it from the VPS before cutover (step 4
  below).
- **Latency.** Each Kalshi call takes roughly 100 ms longer from Finland.
  That's fine for the background warmers.
- **Logged-in actions** (positions, combo quotes and accepts) are sent to
  Kalshi from the server's IP. Kalshi restricts non-US access, so those
  requests may be refused, or may conflict with Kalshi's terms, from an EU
  address.

If that matters, rebuild in Hetzner's Ashburn or Hillsboro locations. The CX
line is EU-only, so pick CPX21 (~$10/mo). Nothing in this kit is
location-specific.

**Python.** Ubuntu 26.04 ships Python 3.14 (3.14.4), the same minor version
as the laptop's `.venv`, so the server uses the system `python3.14` plus a
venv. All pinned wheels (pandas 3.0.6, numpy 2.5.3, polars, pyarrow 25)
install on x86_64 3.14 without compiling. The code itself also parses under
3.12, but the pins are only tested on 3.14. On a distro without 3.14 (for
example Ubuntu 24.04), `bootstrap.sh` falls back to a uv-managed CPython 3.14
in `/opt/propsterminal/python`.

### Layout on the server

```
/opt/propsterminal/            home of the unprivileged `propsterminal` user
  app/                         git checkout of master (deploy.sh hard-resets it)
    .env                       0600, from the laptop (also the systemd EnvironmentFile)
    keys/                      0700/0600, Kalshi private key(s)
    cache/                     regenerable (nflverse parquet, snapshots, Kalshi HTTP cache)
    data/bdb/                  optional Big Data Bowl tracking (migrate.sh --bdb)
    history/odds.sqlite3       Phase 3 odds history -- NOT regenerable (override: ODDS_DB_PATH)
  venv/                        python3.14 venv
  backups/odds-*.sqlite3.gz    nightly history backups (14 days)
  .ssh/id_ed25519              read-only GitHub deploy key
/etc/cloudflared/              config.yml + <tunnel-id>.json (root:cloudflared 0640)
```

## First-time setup

Do these steps in order. Steps 1–5 are invisible to visitors; the laptop
keeps serving the site until step 6.

**0. Laptop SSH alias** (the scripts default to the host `propsterminal`):

```
# ~/.ssh/config
Host propsterminal
  HostName 37.27.158.56
  User root
```

`ssh propsterminal true` should succeed without a password prompt. Every
script also takes the host as an argument, for example
`deploy/deploy.sh root@37.27.158.56`.

**1. Bootstrap the server.** Run as root on the server:

```sh
scp deploy/bootstrap.sh propsterminal:/root/      # from the laptop (main checkout)
ssh propsterminal bash /root/bootstrap.sh
```

- The repo is private, so the first run stops at the clone and prints a
  public key. Add it at
  <https://github.com/cole-dav/nfl-kalshi-tool/settings/keys>. Give it a
  title like "propsterminal VPS" and leave **Allow write access unchecked**.
- Then re-run `ssh propsterminal bash /root/bootstrap.sh`. Re-running is
  always safe.

When it finishes:
- `propsterminal.service` is running on 127.0.0.1:8765 with no `.env` yet
  (public market data needs no credentials). It starts warming caches.
- `propsterminal-tunnel.service` is enabled but **not started**.
- ufw allows SSH only, and sshd is keys-only (root login with key allowed).

**2. Copy secrets and the tunnel credentials** from the laptop (main
checkout):

```sh
deploy/migrate.sh                 # = --secrets --tunnel
```

- It lists which `.env` variable *names* are present and never prints the
  values.
- The laptop's `.env` currently has no `ANTHROPIC_API_KEY` line. If the
  engine chat should work on the server, add it to
  `/opt/propsterminal/app/.env` there.
- `KALSHI_PRIVATE_KEY_PATH=./keys/...` resolves against the app dir, so it
  needs no change.

**3. Optional bulk data**, any time before cutover:

```sh
deploy/migrate.sh --cache         # warm nflverse parquet + snapshots (~200 MB)
deploy/migrate.sh --bdb           # 3.9 GB of Kaggle tracking for Viz Lab (resumable)
ssh propsterminal systemctl restart propsterminal
```

The site works without `data/bdb`. Only the Viz Lab replay tab uses it
(`bdb_tracking.py`), and without it that tab falls back to auto-downloading
the 2019 one-game sample (~35 MB). The Kaggle seasons (`data/bdb/2023`,
`2026`, `2027`) need a Kaggle login to re-download, so copy them.

**4. Check the VPS app privately.** No public traffic reaches it yet.

```sh
ssh propsterminal 'curl -s localhost:8765/healthz; echo; journalctl -u propsterminal -n 20 --no-pager'
ssh -N -L 18765:127.0.0.1:8765 propsterminal     # then open http://localhost:18765
```

- Click through the week, a game, a player and Viz Lab.
- Watch `journalctl -u propsterminal -f` for Kalshi 403/451 errors, which
  are the geo check.
- A browser through this SSH forward counts as "direct local". With
  `KALSHI_DEFAULT_LOGIN=1` set, it uses the owner's key, just like
  `localhost:8765` on the laptop.

**5. Uptime monitor.** UptimeRobot's free plan works:
- Monitor type HTTP(s), URL `https://propsterminal.com/healthz`, interval 5 min, alert by email.
- Optionally add a keyword monitor for `"ok": true`.
- `/healthz` is `Cache-Control: no-store` and never calls upstream, so frequent polling is free.

**6. Cutover.** See below.

## Cutover (laptop → VPS)

**Why the order matters.** One tunnel with two live connectors makes
Cloudflare spread requests across both. Here that means two different
servers, with separate in-memory login sessions and separate odds-history
DBs. That state should last only a few seconds.

Find the laptop's connector:
- The process serving the site is the manually started
  `cloudflared tunnel --protocol http2 run propsterminal`. It is not under
  launchd, so killing it is permanent until it's restarted by hand.
- A separate root LaunchDaemon (`com.cloudflare.cloudflared`, config
  `/usr/local/etc/cloudflared/config.yml`, which has only `logDirectory`)
  names no tunnel and is unrelated. Leave it alone.

```sh
# a. Final history copy. This stops/starts the VPS app; nobody is on it yet.
#    Rows the laptop records between now and step (d) stay only in the
#    laptop's DB (about a minute of data).
deploy/migrate.sh --history
deploy/migrate.sh --snapshots          # optional: freshest saved pages
ssh propsterminal 'systemctl restart propsterminal; sleep 5; curl -s localhost:8765/healthz'

# b. Start the VPS connector, wait until it is registered, and cut the laptop at once:
ssh propsterminal 'systemctl start propsterminal-tunnel && sleep 3 && curl -s localhost:20241/ready'
pkill -f "cloudflared tunnel --protocol http2 run propsterminal"   # on the laptop

# c. Verify. Only the VPS connector should be listed (origin IP 37.27.158.56):
cloudflared tunnel info propsterminal
curl -s https://propsterminal.com/healthz                     # uptime_s matches the VPS's
ssh propsterminal 'journalctl -u propsterminal -f'            # visitor requests arriving

# d. Stop the laptop app so it stops polling Kalshi and recording history:
pkill -f "server.py 8765"
```

`/ready` returns 200 once the connector has at least one edge connection.
Between (b)'s two commands both connectors are up for a few seconds, which
is fine because both run the same code. At no point does the site have zero
connectors.

### Roll back to the laptop

```sh
cd /Users/coledavis/Downloads/nfl-kalshi-tool
nohup .venv/bin/python server.py 8765 >> server.log 2>&1 &
sleep 5; curl -s localhost:8765/healthz
nohup cloudflared tunnel --protocol http2 run propsterminal >> ~/cloudflared.log 2>&1 &
sleep 5; ssh propsterminal 'systemctl stop propsterminal-tunnel; systemctl disable propsterminal-tunnel'
cloudflared tunnel info propsterminal          # only the laptop connector
```

To carry over history recorded on the VPS:

```sh
ssh propsterminal "sqlite3 /opt/propsterminal/app/history/odds.sqlite3 \".backup '/tmp/odds.sqlite3'\""
scp propsterminal:/tmp/odds.sqlite3 /tmp/
# stop the laptop app, then: mv /tmp/odds.sqlite3 history/odds.sqlite3; start it again
```

## Day to day

**Deploy.** Commit on `master` on the laptop, then:

```sh
deploy/deploy.sh                 # push master if needed, update, restart, poll /healthz
```

- It prints `code: <old> -> <new>` and the `/healthz` JSON.
- It exits non-zero, and prints the last 40 log lines, if the new process
  isn't healthy within 90 s (set with `HEALTH_TIMEOUT`).
- It reinstalls requirements only when `requirements.txt` changed. It also
  re-installs the systemd units from `deploy/systemd/` and runs
  `daemon-reload`.
- Each restart is a few seconds of 502s at most. Snapshots persist in
  `cache/snapshots`, so the restarted app answers from them at once.

**Roll back a bad deploy:**

```sh
deploy/deploy.sh --rollback      # the commit that ran before the last deploy
deploy/deploy.sh --ref 6aee9e4   # or any commit
```

The next plain `deploy/deploy.sh` goes back to `origin/master`, so revert or
fix on `master` first.

**Logs, status, restart:**

```sh
ssh propsterminal journalctl -u propsterminal -f              # app (stdout is unbuffered)
ssh propsterminal journalctl -u propsterminal-tunnel -n 50    # cloudflared
ssh propsterminal systemctl status propsterminal propsterminal-tunnel
ssh propsterminal systemctl restart propsterminal
ssh propsterminal 'systemctl show propsterminal -p MemoryCurrent -p NRestarts'
```

**Change config/secrets.** Edit `/opt/propsterminal/app/.env` on the server
(keep it 0600 and owned by `propsterminal`), then
`systemctl restart propsterminal`.
- systemd reads it as an `EnvironmentFile`, and `python-dotenv` reads it
  again; values already in the environment win.
- Keep it to plain `KEY=value` lines.

## Backups

- `propsterminal-backup.timer` runs `deploy/backup-history.sh` daily at
  09:30 UTC. The script:
  - takes a consistent online `sqlite3 .backup` (WAL-safe)
  - runs `PRAGMA integrity_check` on the copy
  - gzips it to `/opt/propsterminal/backups/odds-<UTC stamp>.sqlite3.gz`
  - deletes backups older than `BACKUP_KEEP_DAYS` (default 14)
- If the DB doesn't exist yet (Phase 3 not deployed), it exits 0.

```sh
ssh propsterminal systemctl list-timers propsterminal-backup.timer
ssh propsterminal systemctl start propsterminal-backup.service       # run one now
ssh propsterminal journalctl -u propsterminal-backup -n 20
```

**Off-box copy (recommended once history matters).** A dead disk takes local
backups with it.

1. `apt install rclone` on the server.
2. As `propsterminal`, run `rclone config` and point it at a cheap bucket.
   Cloudflare R2 has 10 GB free; Backblaze B2 also works.
3. Add `BACKUP_RCLONE_DEST=r2:propsterminal-backups/history` to `.env`.

Every run then copies the last 48 h of backups there. Hetzner's own server
backups (+20% of the server price, daily snapshots of the whole disk) are a
zero-config alternative.

Or pull a copy to the laptop now and then:
`rsync -a propsterminal:/opt/propsterminal/backups/ ~/propsterminal-backups/`.

**Restore:**

```sh
ssh propsterminal 'set -e; systemctl stop propsterminal
  cd /opt/propsterminal/app/history
  mv odds.sqlite3 odds.sqlite3.broken; rm -f odds.sqlite3-wal odds.sqlite3-shm
  gunzip -c /opt/propsterminal/backups/odds-YYYYMMDD-HHMMSS.sqlite3.gz > odds.sqlite3
  chown propsterminal:propsterminal odds.sqlite3; chmod 640 odds.sqlite3
  systemctl start propsterminal'
```

Everything else on the box can be rebuilt:
- **Code:** git.
- **Caches:** they refill on their own.
- **Secrets:** they live on the laptop; re-run `migrate.sh`.
- **The whole server:** a fresh server plus `bootstrap.sh` plus `migrate.sh --all` rebuilds it.

## Rebuilding / moving the server

1. Create the new server and point the `propsterminal` SSH alias at it.
2. Run steps 1–4 above.
3. For history, copy from the old server instead of the laptop. Use
   `sqlite3 .backup` there, `scp` it over, and swap it in like the Restore
   block above.
4. Cut over the same way: start the new tunnel unit, then stop the old one.
