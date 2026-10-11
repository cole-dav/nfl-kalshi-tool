# Project notes for Claude

## Production runs on the VPS (since 2026-10-11)

propsterminal.com is served by the Hetzner VPS (`37.27.158.56`, ssh alias
`propsterminal`, Ubuntu 26.04) under systemd, via the Cloudflare tunnel
`propsterminal`. The laptop is only a dev box: its port 8765 server and
tunnel are not live, and restarting them does not affect the public site.

```sh
deploy/deploy.sh                                   # push master, update, restart, poll /healthz
deploy/deploy.sh --rollback                        # previous commit if the deploy is bad
ssh propsterminal systemctl restart propsterminal  # restart without code changes
ssh propsterminal journalctl -u propsterminal -n 50 --no-pager   # logs
curl -s https://propsterminal.com/healthz          # expect {"ok": true, ...}
```

Backend changes reach the public site only via `deploy/deploy.sh`. A non-zero
exit means it wasn't healthy: show the user the log lines it printed.
Secrets live in `/opt/propsterminal/app/.env` on the server; never print them.
The Kalshi API key is deliberately disabled on the server (commented out in its
`.env`, no `keys/*.pem`): the VPS is in Finland and the user chose not to sign
Kalshi requests from there. Don't re-enable it without asking.
Rollback to the laptop and other procedures: `deploy/README.md`.

## Local dev server

For testing changes locally before deploying (frontend-only changes in
`static/` just need a browser refresh):

```sh
cd /Users/coledavis/Downloads/nfl-kalshi-tool
pkill -f "server.py 8765"; while pgrep -f "server.py 8765" >/dev/null; do sleep 1; done
nohup .venv/bin/python server.py 8765 >> server.log 2>&1 &
sleep 4; curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8765/   # expect 200
```

Use `.venv/bin/python` (system python lacks the deps). If it doesn't come back with
200, check the tail of `server.log` and tell the user.

## Commit and push after every change — don't ask

When a change is done and verified (server restarted / checks pass), commit it
to the current branch and `git push` it yourself — the user has pre-approved
both. One commit per logical change, with a descriptive message. The user would
rather roll back or bug-fix than be asked first. Never force-push.
