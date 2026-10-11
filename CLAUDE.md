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

## Commit after every change — don't ask

When a change is done and verified (server restarted / checks pass), commit it
to the current branch yourself — the user has pre-approved this. One commit per
logical change, with a descriptive message. Don't push unless asked.
