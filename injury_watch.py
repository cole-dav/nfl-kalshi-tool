"""
Nightly injury heartbeat for the week feed.

Previews are written on Tuesday, before the new week's injury reports exist
(practice reports start Wednesday, game statuses come out Friday). Every night
at FEED_INJURY_HOUR_ET (default 22, after the day's practice reports) this
checks the injury report for every upcoming game that has a preview, against
the statuses that preview last saw:

  - every status change among notable players (OVR >= NOTABLE_OVR or listed by
    the preview's key_injuries) is logged on the game (Week tab, /api/feed);
  - a material change -- a player of OVR >= MATERIAL_OVR, a QB, or one the
    preview named, crossing between playing (no status / Questionable) and not
    (Doubtful / Out / IR / PUP) -- rewrites the preview, at most
    MAX_REWRITES times per game and not within an hour of kickoff. The old
    version's bets are marked superseded in the ledger (never graded or
    scored) and the new version's bets are recorded.

Store: data/feed/injury_watch.json  event_ticker -> {baseline, updates, rewrites}
plus "_last_run" (ET date). Runs inside week_feed.generate_loop.
"""

from __future__ import annotations

import json
import os
import threading
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import bet_ledger as bl
import engine_agent as ea
import madden_ratings as mr
import nflverse_data as nd

WATCH_PATH = os.path.join(os.path.dirname(__file__), "data", "feed", "injury_watch.json")
ET = ZoneInfo("America/New_York")
NOTABLE_OVR = 75
MATERIAL_OVR = 82
MAX_REWRITES = 2
NO_REWRITE_WITHIN = timedelta(hours=1)
OUT = {"Doubtful", "Out", "IR", "PUP", "NFI"}
_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(WATCH_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _save(d: dict) -> None:
    os.makedirs(os.path.dirname(WATCH_PATH), exist_ok=True)
    tmp = WATCH_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1, default=str)
    os.replace(tmp, WATCH_PATH)


def run_hour() -> int:
    return int(os.environ.get("FEED_INJURY_HOUR_ET") or 22)


def _status(r: dict) -> str:
    s = (r.get("report_status") or r.get("game_status") or "").strip()
    return s or ("practice: " + r["practice_status"] if r.get("practice_status") else "listed")


def snapshot(team: str) -> dict:
    """{player: {team, pos, ovr, status}} for one team's injury report and IR (Kalshi code)."""
    out = {}
    for r in mr.injuries_by_ovr(nd.kalshi_to_nflverse_team(team)):
        out[r["full_name"]] = {"team": team, "pos": r.get("position"), "ovr": r.get("ovr"), "status": _status(r)}
    return out


def playing(status: str | None) -> bool:
    return status is None or status not in OUT


def diff(before: dict, after: dict, named: set[str]) -> list[dict]:
    """Status changes among notable players; `material` marks a playing/not-playing
    crossing by a player of MATERIAL_OVR+ or one the preview named."""
    out = []
    for name in sorted(set(before) | set(after)):
        b, a = before.get(name), after.get(name)
        was, now = (b or {}).get("status"), (a or {}).get("status")
        if was == now:
            continue
        info = a or b
        ovr = info.get("ovr") or 0
        is_named = name.lower() in named or info.get("pos") == "QB"     # QBs always count, rated or not
        if ovr < NOTABLE_OVR and not is_named:
            continue
        crossed = playing(was) != playing(now)
        out.append({"player": name, "team": info.get("team"), "pos": info.get("pos"), "ovr": info.get("ovr"),
                    "from": was or "not listed", "to": now or "off the report",
                    "material": crossed and (ovr >= MATERIAL_OVR or is_named)})
    return out


def _named(post: dict) -> set[str]:
    """Players the preview named as key injuries (lower-cased)."""
    return {(k.get("player") if isinstance(k, dict) else str(k) or "").lower()
            for k in post.get("key_injuries") or []} - {""}


def check_game(ev: str, post: dict, watch: dict, now: datetime, rewrite) -> dict | None:
    """Compare one game's injuries with its baseline; log changes, rewrite on a
    material one. Returns the update entry (None when nothing changed)."""
    w = watch.setdefault(ev, {"updates": [], "rewrites": 0})
    current = {**snapshot(post["away"]), **snapshot(post["home"])}
    if not w.get("baseline"):            # written before the watch existed: start watching from now
        w["baseline"] = current
        return None
    changes = diff(w["baseline"], current, _named(post))
    if not changes:
        w["baseline"] = current
        return None
    entry = {"at": ea._iso(now.timestamp()), "changes": changes, "action": "noted"}
    ko = bl._parse(post.get("kickoff"))
    if any(c["material"] for c in changes) and w["rewrites"] < MAX_REWRITES \
            and (ko is None or ko - now > NO_REWRITE_WITHIN):
        try:
            old_ids, new = rewrite(ev)
            bl.supersede(old_ids)
            w["rewrites"] += 1
            entry.update(action="rewritten", post_id=new.get("post_id"), superseded=old_ids)
        except Exception as e:
            if not isinstance(e, ea.EngineError):
                traceback.print_exc()
            entry.update(action="rewrite failed", error=str(e))
    w["updates"].append(entry)
    w["baseline"] = current
    return entry


def heartbeat(now: datetime | None = None, force: bool = False) -> list[str]:
    """Run the nightly check if it's due (or `force`). One log line per game changed."""
    import week_feed as wf
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ET)
    with _lock:
        watch = _load()
        if not force and (local.hour < run_hour() or watch.get("_last_run") == local.date().isoformat()):
            return []
        watch["_last_run"] = local.date().isoformat()
        _save(watch)
    models = wf._auto_models()
    live_model = (os.environ.get("FEED_AUTO_LIVE_MODEL") or "").strip()
    posts = wf._load()
    ab = wf._load(wf.AB_PATH)
    games = {}
    for ev, p in posts.items():
        games[ev] = p
    for ev, entry in ab.items():
        if ev not in games:
            v = next((x for x in (entry.get("variants") or {}).values() if not x.get("error")), None)
            if v:
                games[ev] = v

    def rewrite(ev):
        if models:
            old = [v.get("post_id") for v in (ab.get(ev, {}).get("variants") or {}).values() if v.get("post_id")]
            out = wf.write_ab(ev, models)
            if ev in posts:
                old.append(posts[ev].get("post_id"))
                if live_model and live_model in out and not out[live_model].get("error"):
                    return old, wf.promote(ev, live_model)
            return old, next((p for p in out.values() if not p.get("error")), {})
        old = [posts[ev].get("post_id")]
        return old, wf.write_preview(ev)

    lines = []
    for ev, post in games.items():
        ko = bl._parse(post.get("kickoff"))
        if ko is None or ko <= now:
            continue
        try:
            with _lock:
                watch = _load()
            e = check_game(ev, post, watch, now, rewrite)
            with _lock:
                fresh = _load()
                fresh[ev] = watch[ev]
                _save(fresh)
            if e:
                lines.append(f"{ev}: {len(e['changes'])} change(s), {e['action']}: "
                             + "; ".join(f"{c['player']} {c['from']} -> {c['to']}" for c in e["changes"][:5]))
        except Exception as e:
            traceback.print_exc()
            lines.append(f"{ev}: FAILED {e}")
    return lines


def updates_for(ev: str) -> list[dict]:
    return (_load().get(ev) or {}).get("updates") or []


def note_written(ev: str, away: str, home: str) -> None:
    """Snapshot both teams' injury reports when a preview is (re)written: the
    statuses the heartbeat compares against. Never fails the write."""
    try:
        base = {**snapshot(away), **snapshot(home)}
    except Exception:
        traceback.print_exc()
        return
    with _lock:
        watch = _load()
        watch.setdefault(ev, {"updates": [], "rewrites": 0})["baseline"] = base
        _save(watch)
