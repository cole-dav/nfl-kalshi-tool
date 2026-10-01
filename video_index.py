"""
YouTube video index: official NFL game highlights and per-player "every
play" cut-ups, matched to nflverse games and players so the UI can embed them
(team-logo hover, game log rows).

Sources are playlists, which keep each week's videos together:
  - NFL channel: "Game Recaps (Week N)" / "Game Highlights (Week N)" and
    "Player Highlights (Week N)" for the season.
  - Curtain Call Replays (fan channel): "NFL | <season> | Week N" playlists of
    "<Player> Week N Highlights vs <Opp> | Every Play/Run/Target and Catch".
  - STACKED Fantasy (fan channel): "Week N <season>" playlists of
    "<Player>: Every Touch|Dropback of <season> Week N | Full Film Compilation"
    -- the widest per-player coverage (~100-180 offensive players a week).
  - Hall Highlights (fan channel): its uploads list, titled
    "<Player> Week N Highlights (Every Target) | NFL <season> - <Team>".

With YOUTUBE_API_KEY set, playlists are listed through the YouTube Data API
(complete, ~1 quota unit per 50 videos). Without it, the public channel and
playlist pages are read instead (following YouTube's own "load more"
continuations past the first 100 videos) -- that only sees a channel's newest
~30 playlists, but everything seen is kept in cache/videos_<season>.json, so a
server that runs through the season keeps every week.

We only ever store video ids and embed them with YouTube's own player; nothing
is downloaded or re-hosted. New ids are checked once against YouTube's oEmbed
endpoint, which refuses videos that are removed or have embedding disabled.
Fan uploads can get taken down, and a later playlist refresh drops them.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

import nflverse_data as nd

# "uploads": index the channel's whole uploads list instead of picking weekly
# playlists (Hall files by team, not week; titles carry the week).
CHANNELS = {
    "nfl": {"id": "UCDVYQ4Zhbm3S2dlz7P1GBDg", "handle": "NFL"},
    "ccr": {"id": "UC_hzY5r_WT4C5gi0Tl2u0yg", "handle": "curtaincallreplays"},
    "stacked": {"id": "UCgaRVS9c1T-4rK0bZMs1WUQ", "handle": "stackedfantasyfilm"},
    "hall": {"id": "UCipM1ooeLtPlfIv9b02Z9jg", "handle": "HallsHighlights", "uploads": True},
}
SOURCE_LABEL = {"nfl": "NFL", "ccr": "Curtain Call Replays", "stacked": "STACKED Fantasy", "hall": "Hall Highlights"}
# The NFL blocks its own uploads -- and fan uploads its Content ID claims --
# from playing in players on other sites ("NFL has blocked it from display on
# this website or application"), even though oEmbed and the Data API still call
# them embeddable; the block only shows up in the player. Checked per channel in
# a browser: NFL and STACKED videos are blocked, Curtain Call and Hall play.
# Blocked sources render as a thumbnail that opens YouTube; the page also falls
# back to that if an "embeddable" video errors in the player.
SOURCE_EMBEDS = {"nfl": False, "ccr": True, "stacked": False, "hall": True}
UPLOADS_MAX_PAGES = 5

# Playlists for the current/last week keep changing (late uploads, takedowns);
# older weeks settle, so re-read them daily rather than hourly.
FRESH_TTL = 3600
SETTLED_TTL = 24 * 3600
LISTING_TTL = 3600

NICKNAME_TO_TEAM = {
    "cardinals": "ARI", "falcons": "ATL", "ravens": "BAL", "bills": "BUF", "panthers": "CAR",
    "bears": "CHI", "bengals": "CIN", "browns": "CLE", "cowboys": "DAL", "broncos": "DEN",
    "lions": "DET", "packers": "GB", "texans": "HOU", "colts": "IND", "jaguars": "JAX",
    "chiefs": "KC", "raiders": "LV", "chargers": "LAC", "rams": "LA", "dolphins": "MIA",
    "vikings": "MIN", "patriots": "NE", "saints": "NO", "giants": "NYG", "jets": "NYJ",
    "eagles": "PHI", "steelers": "PIT", "49ers": "SF", "seahawks": "SEA", "buccaneers": "TB",
    "titans": "TEN", "commanders": "WAS",
}

KIND_LABEL = {
    "game": "GAME HIGHLIGHTS",
    "every_play": "EVERY PLAY",
    "every_touch": "EVERY TOUCH",
    "every_run": "EVERY RUN",
    "every_target": "EVERY TARGET",
    "every_catch": "EVERY CATCH",
    "every_dropback": "EVERY DROPBACK",
    "best_plays": "BEST PLAYS",
    "highlights": "HIGHLIGHTS",
}
# Player-video preference when a game has several: the fullest cut-up first.
KIND_ORDER = ["every_play", "every_touch", "every_dropback", "every_run", "every_target", "every_catch",
              "best_plays", "highlights"]

OFFENSE_POSITIONS = {"QB", "RB", "FB", "WR", "TE"}

_UA = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US"}


# ---------- title parsing (pure) ----------

def team_from_text(text: str) -> str | None:
    """nflverse code for the team nickname in `text` ("Philadelphia Eagles",
    "Eagles", "49ers"), or None."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    for w in reversed(words):
        if w in NICKNAME_TO_TEAM:
            return NICKNAME_TO_TEAM[w]
    return None


def classify_playlist(title: str, season: int) -> tuple[str, int] | None:
    """("game" | "player", week) for a playlist we index, else None."""
    t = title.strip()
    m = re.match(r"^Game (?:Recaps|Highlights) \(Week ?(\d+)\) \| (?:NFL )?(\d{4})", t)
    if m and int(m.group(2)) == season:
        return "game", int(m.group(1))
    m = re.match(r"^Player Highlights \(Week ?(\d+)\) \| (?:NFL )?(\d{4})", t)
    if m and int(m.group(2)) == season:
        return "player", int(m.group(1))
    m = re.match(r"^NFL \| (\d{4}) \| Week (\d+)$", t)
    if m and int(m.group(1)) == season:
        return "player", int(m.group(2))
    m = re.match(r"^Week (\d+) (\d{4})$", t)  # STACKED
    if m and int(m.group(2)) == season:
        return "player", int(m.group(1))
    return None


def parse_game_title(title: str, week: int | None = None) -> dict | None:
    """'Philadelphia Eagles vs. Chicago Bears Game Highlights | 2026 NFL Season Week 3'
    -> {away, home, week}. NFL titles list the away team first. `week` (the
    playlist's) fills in when the title leaves it out."""
    m = re.match(r"^(.+?) (?:vs\.?|@|at) (.+?) Game Highlights\b(.*)$", title)
    if not m:
        return None
    away, home = team_from_text(m.group(1)), team_from_text(m.group(2))
    wk = re.search(r"\bWeek (\d+)", m.group(3))
    week = int(wk.group(1)) if wk else week
    if not away or not home or away == home or week is None:
        return None
    return {"away": away, "home": home, "week": week}


def _kind_from_every(text: str) -> str:
    t = text.lower()
    if "dropback" in t:
        return "every_dropback"
    if "play" in t or "throw" in t or "pass" in t:
        return "every_play"
    if "touch" in t:
        return "every_touch"
    run = "run" in t or "carr" in t
    rec = "target" in t or "catch" in t
    if run and rec:
        return "every_touch"
    if run:
        return "every_run"
    return "every_target" if "target" in t else "every_catch"


def parse_player_title(title: str, week: int | None = None) -> dict | None:
    """Player cut-up titles -> {name, week, opp, team (nflverse or None), kind,
    season (when the title names one)}. `week` (the playlist's) fills in when
    the title leaves it out.

    STACKED:      'Rome Odunze: Every Touch of 2026 Week 3 | Full Film Compilation'
                  'Jalen Hurts: Every Dropback of 2026 Week 3 | Full Film Compilation'
    Hall:         'Chris Olave Week 3 Highlights (Every Target) | NFL 2026 - New Orleans Saints'
                  'Keldric Faulk Week 2 Every Play | NFL 2026 - Tennesee Titans'
                  'C. J. Henderson Highlights | Week 3 NFL 2026 - Atlanta Falcons'
    Curtain Call: 'Jeremiyah Love Week 3 Highlights vs 49ers | Every Play'
                  'Devin Neal Week 14 Highlights | Every Run, Target, and Catch vs Buccaneers'
                  'Kaleb Johnson Packers Debut Highlights | Every Run'
    NFL:          "Case Keenum's best plays from 3-TD game vs. Eagles | Week 3"
                  "Jared Goff's best throws from 4-TD game vs. Bills | Week 2"
                  "Every catch from Brock Bowers' 116-yard game vs. Saints | Week 3"
                  "Every Kenyon Sadiq catch from 105-yard game | Week 3"
    """
    t = title.strip()

    def out(name, wk, opp, kind, team=None, season=None):
        if wk is None:
            return None
        return {"name": name.strip().rstrip("'’"), "week": wk, "opp": opp, "team": team, "kind": kind,
                "season": season}

    m = re.match(r"^(.+?): Every (\w+) of (\d{4}) Week (\d+)\b", t)
    if m:
        return out(m.group(1), int(m.group(4)), None, _kind_from_every(m.group(2)), season=int(m.group(3)))
    m = re.match(r"^(.+?)(?: (?:NFL )?Debut)? (?:Week (\d+) )?(Highlights(?: \(([^)]*)\))?|Every [\w ,]+?) \| "
                 r"(?:Week (\d+) )?NFL (\d{4}) - (.+)$", t)
    if m:
        what, paren = m.group(3), m.group(4)
        kind = _kind_from_every(paren or what) if (paren or what.startswith("Every")) else "highlights"
        wk = m.group(2) or m.group(5)
        return out(m.group(1), int(wk) if wk else week, None, kind, team_from_text(m.group(7)), int(m.group(6)))

    m = re.match(r"^(.+?) Week (\d+) Highlights(?: vs\.? ([^|]+?))? \| (Every [^|]*?)(?: vs\.? (.+))?$", t)
    if m:
        opp = m.group(3) or m.group(5) or ""
        return out(m.group(1), int(m.group(2)), team_from_text(opp) if opp else None, _kind_from_every(m.group(4)))
    m = re.match(r"^(.+?) (?:([\w.]+) )?(?:Debut|Return) Highlights \| (Every .+)$", t)
    if m:
        return out(m.group(1), week, None, _kind_from_every(m.group(3)), team_from_text(m.group(2) or ""))

    wk = re.search(r"\| Week (\d+)\s*$", t)
    week = int(wk.group(1)) if wk else week
    opp_m = re.search(r"\bvs\.? ([^|]+?)\s*(?:\||$)", t)
    opp = team_from_text(opp_m.group(1)) if opp_m else None
    poss = r"(?:'s'?|'|’s|’)?"
    m = re.match(r"^Every (catch|run|target|throw|play|touch)\w* from (.+?)" + poss + r" \S+ game\b", t, re.I)
    if m:
        return out(m.group(2), week, opp, _kind_from_every(m.group(1)))
    m = re.match(r"^Every (.+?) (catch|run|target|throw|play|touch)\w* from ", t, re.I)
    if m:
        return out(m.group(1), week, opp, _kind_from_every(m.group(2)))
    m = re.match(r"^(.+?)" + poss + r" best (?:plays?|catch(?:es)?|throws?|runs?)\b", t)
    if m and not re.search(r"\bdefen[cs]e\b", m.group(1), re.I):
        return out(m.group(1), week, opp, "best_plays")
    return None


# ---------- fetching ----------

def _http_json(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=20) as r:
        return json.loads(r.read().decode())


def _page_html(url: str) -> tuple[dict, str]:
    """(ytInitialData, innertube client version) of a YouTube page."""
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=20) as r:
        html = r.read().decode()
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html)
    if not m:
        raise RuntimeError(f"no ytInitialData on {url}")
    ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
    return json.loads(m.group(1)), (ver.group(1) if ver else "2.20250101.00.00")


def _page_data(url: str) -> dict:
    return _page_html(url)[0]


def _continuations(obj, out: list) -> list:
    if isinstance(obj, dict):
        if "continuationCommand" in obj:
            out.append(obj["continuationCommand"]["token"])
        for x in obj.values():
            _continuations(x, out)
    elif isinstance(obj, list):
        for x in obj:
            _continuations(x, out)
    return out


def _paged_lockups(url: str, max_pages: int) -> list[tuple[str, str]]:
    """Tiles on a page plus up to max_pages-1 of its "load more" continuations
    (a playlist page shows 100 videos; the rest come from these)."""
    data, ver = _page_html(url)
    items = _lockups(data, [])
    tokens = _continuations(data, [])
    for _ in range(max_pages - 1):
        if not tokens:
            break
        body = {"context": {"client": {"clientName": "WEB", "clientVersion": ver, "hl": "en"}}, "continuation": tokens[-1]}
        req = urllib.request.Request("https://www.youtube.com/youtubei/v1/browse?prettyPrint=false",
                                     data=json.dumps(body).encode(), headers={**_UA, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            more = json.loads(r.read().decode()).get("onResponseReceivedActions", [])
        items += _lockups(more, [])
        tokens = _continuations(more, [])
    return items


def _lockups(obj, out: list) -> list:
    """(id, title) for every playlist/video tile in a YouTube page's data."""
    if isinstance(obj, dict):
        if "lockupViewModel" in obj:
            lv = obj["lockupViewModel"]
            meta = lv.get("metadata", {}).get("lockupMetadataViewModel", {})
            title = meta.get("title", {}).get("content")
            if lv.get("contentId") and title:
                out.append((lv["contentId"], title))
            return out
        if "playlistVideoRenderer" in obj:
            v = obj["playlistVideoRenderer"]
            runs = v.get("title", {}).get("runs") or []
            if v.get("videoId") and runs:
                out.append((v["videoId"], runs[0]["text"]))
            return out
        for x in obj.values():
            _lockups(x, out)
    elif isinstance(obj, list):
        for x in obj:
            _lockups(x, out)
    return out


def _api_key() -> str | None:
    return os.environ.get("YOUTUBE_API_KEY") or None


def _api_paged(endpoint: str, params: dict, max_pages: int = 100) -> list[dict]:
    items, token = [], None
    for _ in range(max_pages):
        q = {**params, "key": _api_key(), "maxResults": 50, **({"pageToken": token} if token else {})}
        data = _http_json(f"https://www.googleapis.com/youtube/v3/{endpoint}?{urllib.parse.urlencode(q)}")
        items += data.get("items", [])
        token = data.get("nextPageToken")
        if not token:
            break
    return items


def list_channel_playlists(channel: str) -> list[tuple[str, str]]:
    ch = CHANNELS[channel]
    if _api_key():
        items = _api_paged("playlists", {"part": "snippet", "channelId": ch["id"]})
        return [(it["id"], it["snippet"]["title"]) for it in items]
    return _lockups(_page_data(f"https://www.youtube.com/@{ch['handle']}/playlists"), [])


def list_playlist_videos(playlist_id: str, max_pages: int = 10) -> list[tuple[str, str]]:
    """Newest-first for uploads lists; max_pages caps both paths (50 or 100 a page)."""
    if _api_key():
        items = _api_paged("playlistItems", {"part": "snippet", "playlistId": playlist_id}, max_pages * 2)
        return [(it["snippet"]["resourceId"]["videoId"], it["snippet"]["title"]) for it in items
                if it["snippet"].get("resourceId", {}).get("videoId")]
    return _paged_lockups(f"https://www.youtube.com/playlist?list={playlist_id}", max_pages)


def embeddable(video_id: str) -> bool:
    """YouTube's oEmbed answers 200 only for public videos that allow embedding."""
    url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(f"https://www.youtube.com/watch?v={video_id}")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


# ---------- matching to nflverse ----------

def _game_row(sched: pd.DataFrame, week: int, a: str, b: str | None) -> pd.Series | None:
    rows = sched[sched["week"] == week]
    if b:
        rows = rows[((rows["away_team"] == a) & (rows["home_team"] == b)) | ((rows["away_team"] == b) & (rows["home_team"] == a))]
    else:
        rows = rows[(rows["away_team"] == a) | (rows["home_team"] == a)]
    return rows.iloc[0] if len(rows) == 1 else None


def match_player(name: str, team: str | None, rosters: pd.DataFrame) -> str | None:
    """gsis_id for `name` (on `team` when known). Exact normalized name first,
    then a unique last-name match on the team (covers nicknames). Namesakes
    (e.g. a practice-squad DB sharing a starting WR's name) resolve to the
    active player, then to the offensive one -- what these cut-ups cover."""
    norm = nd._normalize_name(name)
    if not norm:
        return None
    names = rosters["full_name"].fillna("").map(nd._normalize_name)
    on_team = rosters["team"] == team if team else pd.Series(False, index=rosters.index)
    last = norm.split()[-1]
    for hit in (
        rosters[on_team & (names == norm)],
        rosters[on_team & names.map(lambda n: n.split()[-1:] == [last])],
        rosters[names == norm],  # traded since, or no opponent in the title
    ):
        col = lambda c: hit[c] if c in hit.columns else pd.Series(None, index=hit.index, dtype=object)
        for narrowed in (hit, hit[col("status") == "ACT"], hit[col("position").isin(OFFENSE_POSITIONS)]):
            ids = narrowed["gsis_id"].dropna().unique()
            if len(ids) == 1:
                return ids[0]
    return None


# ---------- index ----------

class VideoIndex:
    def __init__(self, season: int):
        self.season = season
        self.path = os.path.join(nd.CACHE_DIR, f"videos_{season}.json")
        self.lock = threading.Lock()
        self._built = None
        self.state = {"listed_at": {}, "playlists": {}, "embeddable": {}}
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    self.state.update(json.load(f))
            except Exception:
                traceback.print_exc()

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.path)

    def refresh(self, now: float | None = None) -> None:
        """Refetch whatever is due. Works on a copy of the state and swaps it in
        at the end, so request threads reading the index never see it mid-update."""
        now = now or time.time()
        with self.lock:
            state = json.loads(json.dumps(self.state))
        pls = state["playlists"]
        for channel, ch in CHANNELS.items():
            if ch.get("uploads"):
                # A channel's uploads list is the playlist "UU" + its id minus "UC".
                pls.setdefault("UU" + ch["id"][2:], {
                    "channel": channel, "title": f"{SOURCE_LABEL[channel]} uploads", "kind": "player",
                    "week": None, "uploads": True, "fetched_at": 0, "videos": []})
                continue
            if now - state["listed_at"].get(channel, 0) < LISTING_TTL:
                continue
            try:
                for pid, title in list_channel_playlists(channel):
                    kind = classify_playlist(title, self.season)
                    if kind and pid not in pls:
                        pls[pid] = {"channel": channel, "title": title, "kind": kind[0], "week": kind[1],
                                    "fetched_at": 0, "videos": []}
                state["listed_at"][channel] = now
            except Exception:
                traceback.print_exc()

        newest = max((p["week"] for p in pls.values() if p["week"] is not None), default=0)
        due = [pid for pid, p in pls.items()
               if now - p["fetched_at"] > (FRESH_TTL if p["week"] is None or p["week"] >= newest - 1 else SETTLED_TTL)]
        for pid in due:
            p = pls[pid]
            try:
                if p.get("uploads"):
                    # Only the newest pages are re-read; older uploads are kept
                    # from earlier fetches rather than refetched every hour.
                    fresh = list_playlist_videos(pid, UPLOADS_MAX_PAGES)
                    ids = {vid for vid, _ in fresh}
                    p["videos"] = fresh + [v for v in p["videos"] if v[0] not in ids]
                else:
                    p["videos"] = list_playlist_videos(pid)
                p["fetched_at"] = now
            except Exception:
                traceback.print_exc()

        emb = state["embeddable"]
        unchecked = sorted({vid for p in pls.values() for vid, _ in p["videos"]} - emb.keys())
        if unchecked:
            with ThreadPoolExecutor(max_workers=8) as pool:
                for vid, ok in zip(unchecked, pool.map(embeddable, unchecked)):
                    emb[vid] = ok
        with self.lock:
            self.state = state
            self._save()
            self._built = None

    def _build(self) -> dict:
        """{games: {game_id: [video]}, players: {(gsis_id, game_id): [video]}}"""
        if not self.state["playlists"]:
            return {"games": {}, "players": {}}  # never indexed (e.g. a past season)
        sched = nd.load_schedules(self.season)
        rosters = nd.load_rosters(self.season)
        emb = self.state["embeddable"]
        games: dict[str, list] = {}
        players: dict[tuple, list] = {}
        seen = set()
        for p in self.state["playlists"].values():
            for vid, title in p["videos"]:
                if vid in seen or not emb.get(vid):
                    continue
                seen.add(vid)
                base = {"id": vid, "title": title, "source": SOURCE_LABEL[p["channel"]],
                        "embed": SOURCE_EMBEDS[p["channel"]], "url": f"https://www.youtube.com/watch?v={vid}",
                        "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"}
                # Classify by title, not playlist: game recaps turn up in the
                # player playlists too.
                g = parse_game_title(title, p["week"])
                if g:
                    row = _game_row(sched, g["week"], g["away"], g["home"])
                    if row is not None:
                        games.setdefault(row["game_id"], []).append({**base, "kind": "game", "label": KIND_LABEL["game"]})
                    continue
                pp = parse_player_title(title, p["week"])
                if not pp or (pp["season"] and pp["season"] != self.season):
                    continue
                row = _game_row(sched, pp["week"], pp["opp"], None) if pp["opp"] else None
                team = pp["team"]
                if row is not None:
                    team = row["home_team"] if row["away_team"] == pp["opp"] else row["away_team"]
                elif team:
                    row = _game_row(sched, pp["week"], team, None)
                gsis = match_player(pp["name"], team, rosters)
                if gsis and row is None:
                    # No opponent in the title: find the player's game that week.
                    t = rosters.loc[rosters["gsis_id"] == gsis, "team"]
                    row = _game_row(sched, pp["week"], t.iloc[0], None) if len(t) else None
                if gsis and row is not None:
                    players.setdefault((gsis, row["game_id"]), []).append(
                        {**base, "kind": pp["kind"], "label": KIND_LABEL[pp["kind"]]})
        for vids in players.values():
            vids.sort(key=lambda v: KIND_ORDER.index(v["kind"]))
        return {"games": games, "players": players}

    def built(self) -> dict:
        with self.lock:
            if self._built is None:
                self._built = self._build()
            return self._built


_indexes: dict[int, VideoIndex] = {}
_indexes_lock = threading.Lock()


def get_index(season: int | None = None) -> VideoIndex:
    season = season or nd.current_season()
    with _indexes_lock:
        if season not in _indexes:
            _indexes[season] = VideoIndex(season)
        return _indexes[season]


def warm() -> None:
    """Called from the server's cache warmer; the index only refetches what's due."""
    get_index().refresh()


# ---------- queries ----------

def game_videos(game_id: str, season: int | None = None) -> list[dict]:
    return get_index(season).built()["games"].get(game_id, [])


def attach_to_game_log(gsis_id: str, games: list[dict]) -> list[dict]:
    """Adds `videos` (player cut-ups first, then the game's highlights) to
    game-log rows from nflverse_data."""
    by_season: dict[int, dict] = {}
    for g in games:
        s = int(g["season"]) if g.get("season") is not None else None
        if s is None or not g.get("game_id"):
            continue
        if s not in by_season:
            try:
                by_season[s] = get_index(s).built()
            except Exception:
                traceback.print_exc()
                by_season[s] = {"games": {}, "players": {}}
        b = by_season[s]
        g["videos"] = b["players"].get((gsis_id, g["game_id"]), []) + b["games"].get(g["game_id"], [])
    return games


def team_season_videos(team: str, season: int | None = None) -> dict:
    """A team's (nflverse code) played games this season with their highlight
    video, newest first -- the playlist behind the team-logo hover."""
    season = season or nd.current_season()
    sched = nd.load_schedules(season)
    rows = sched[((sched["away_team"] == team) | (sched["home_team"] == team)) & sched["result"].notna()]
    games = get_index(season).built()["games"]
    out = []
    for g in rows.sort_values("gameday", ascending=False).to_dict(orient="records"):
        home = g["home_team"] == team
        pts, opp_pts = (g["home_score"], g["away_score"]) if home else (g["away_score"], g["home_score"])
        vids = games.get(g["game_id"], [])
        out.append({
            "game_id": g["game_id"], "week": int(g["week"]), "season_type": g["game_type"],
            "opponent": g["away_team"] if home else g["home_team"], "is_home": home,
            "score": f"{int(pts)}-{int(opp_pts)}",
            "result": "W" if pts > opp_pts else "L" if pts < opp_pts else "T",
            "video": vids[0] if vids else None,
        })
    return {"team": team, "season": season, "games": out}
