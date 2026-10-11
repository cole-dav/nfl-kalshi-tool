# Project notes for Claude

## Restart the server after backend changes — don't ask

The app is served live (propsterminal.com, via a Cloudflare tunnel to localhost:8765)
by `server.py`. After changing any `.py` file the server imports, restart it yourself
so the change takes effect — the user has pre-approved this. Frontend-only changes
(`static/`) just need a browser refresh.

```sh
cd /Users/coledavis/Downloads/nfl-kalshi-tool
pkill -f "server.py 8765"; while pgrep -f "server.py 8765" >/dev/null; do sleep 1; done
nohup .venv/bin/python server.py 8765 >> server.log 2>&1 &
sleep 4; curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8765/   # expect 200
```

Use `.venv/bin/python` (system python lacks the deps). If it doesn't come back with
200, check the tail of `server.log` and tell the user.

## Production server (VPS) — once cutover is done

The laptop instructions above stay valid until the site is cut over to the
Hetzner VPS (`37.27.158.56`, ssh alias `propsterminal`, Ubuntu 26.04). After
cutover (see `deploy/README.md`, "Cutover"), the live app runs there under
systemd, and the laptop's port 8765 is just a dev server:

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

## Commit and push after every change — don't ask

When a change is done and verified (server restarted / checks pass), commit it
to the current branch and `git push` it yourself — the user has pre-approved
both. One commit per logical change, with a descriptive message. The user would
rather roll back or bug-fix than be asked first. Never force-push.
