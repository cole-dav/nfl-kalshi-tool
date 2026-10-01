"""
Local web server for the NFL player research tool.

Run:
  python3 server.py [port]

Odds/matchups/injuries browsing needs no Kalshi credentials at all -- market
data reads go through Kalshi's public endpoints. Viewing your own positions
or using the combo/parlay builder requires logging in (POST /api/login with
your own api_key_id + private_key_pem); each session's key lives in server
memory only, keyed by an HttpOnly cookie, and is never written to disk. See
sessions.py.

.env (see .env.example) is optional -- only relevant if you want a default
KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH for local CLI debugging via
kalshi_book.py directly. The HTTP server only falls back to it when
KALSHI_DEFAULT_LOGIN=1, and only for direct requests from this machine.

Serves static/index.html at / and JSON at /api/player?name=<player name>.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

load_dotenv()

import combo
import madden_ratings as mr
import player_rankings as prk
import player_research as pr
import positions_overview as po
import injury_news as inj
import nflverse_data as nd
import sessions
import snapshots as snap
import team_tendencies as tt
import pass_zones as pz
import week_overview as wo
import player_volume as pvol
import engine_agent as engine
import scenario_sim as sim
import video_index as vi

SESSION_COOKIE = "sid"

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
WARM_INTERVAL_SECONDS = 10 * 60

# Snapshot TTLs (seconds). Visitors are served the shared payload; at most one
# rebuild per key per TTL reaches upstream. See snapshots.py.
TTL_LIVE = 30          # odds-driven pages: week, game, player, edges
TTL_VOLUME = 300       # trailing 4h prop volume
TTL_NEWS = 30 * 60
TTL_STATIC = 60 * 60   # player list, game logs, video index
SLATE_REFRESH_SECONDS = 30

# Cache-Control for anonymous, shared payloads. Cloudflare honors s-maxage once
# a Cache Rule marks /api/* as eligible; browsers use max-age.
CACHE_LIVE = "public, max-age=15, s-maxage=30, stale-while-revalidate=60"
CACHE_SLOW = "public, max-age=300, s-maxage=600, stale-while-revalidate=1800"
CACHE_PRIVATE = "private, no-store"

# Engine chat spends the operator's Anthropic credits. Off for public visitors
# unless ENGINE_CHAT_PUBLIC=1, then capped per IP per day.
CHAT_PUBLIC = os.environ.get("ENGINE_CHAT_PUBLIC") == "1"
CHAT_DAILY_LIMIT = int(os.environ.get("ENGINE_CHAT_DAILY_LIMIT", "20"))
_chat_counts: dict[tuple[str, str], int] = {}
_chat_lock = threading.Lock()


def _cache_warmer():
    """Keep nflverse data, rankings and Madden ratings loaded in memory and
    refetch them here when they go stale, so page requests never wait on it."""
    while True:
        for warm in (nd.warm, prk.warm, mr.warm, tt.warm, pz.warm, vi.warm):
            try:
                warm()
            except Exception:
                traceback.print_exc()
        time.sleep(WARM_INTERVAL_SECONDS)


def _slate_refresher():
    """Rebuild the pages every visitor opens first (current week, volume
    ranking) before they go stale, so even the first request after a TTL is
    instant. Per-game and per-player pages refresh on demand (stale entries
    are served while one background rebuild runs)."""
    while True:
        for key, build, ttl in (
            ("week:current", lambda: wo.build_week_overview(None), TTL_LIVE),
            ("player_volume", pvol.player_volume_cached, TTL_VOLUME),
        ):
            try:
                snap.refresh(key, build, ttl)
            except Exception:
                traceback.print_exc()
        time.sleep(SLATE_REFRESH_SECONDS)


def _sanitize(obj):
    """Recursively replace NaN/Inf/NaT with None so json.dumps produces valid JSON."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    try:
        import pandas as pd
        if obj is pd.NaT or (hasattr(pd, "isna") and not isinstance(obj, (list, dict)) and pd.isna(obj) is True):
            return None
    except Exception:
        pass
    return obj


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _session_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        cookie = SimpleCookie()
        cookie.load(raw)
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _is_direct_local(self) -> bool:
        """True only for a browser on this machine hitting the server directly
        -- not via a Cloudflare tunnel, which also connects from loopback but
        adds Cf-Connecting-Ip / X-Forwarded-For and a public Host."""
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        if self.headers.get("Cf-Connecting-Ip") or self.headers.get("X-Forwarded-For"):
            return False
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def _session_client(self):
        """The visiting session's own Kalshi client, or None if not logged
        in. With KALSHI_DEFAULT_LOGIN=1, a direct local visitor with no session
        falls back to the owner's env key (for testing); public/tunneled
        visitors never do."""
        client = sessions.get_session(self._session_token())
        if client is None and self._is_direct_local():
            client = sessions.default_client()
        return client

    def _client_ip(self) -> str:
        # Behind the Cloudflare tunnel every request arrives from loopback;
        # only then is Cf-Connecting-Ip trustworthy.
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return self.headers.get("Cf-Connecting-Ip") or self.client_address[0]
        return self.client_address[0]

    def _chat_allowed(self, count: bool = False) -> bool:
        """Direct local use is unlimited; public visitors only with
        ENGINE_CHAT_PUBLIC=1, up to CHAT_DAILY_LIMIT messages per IP per day."""
        if self._is_direct_local():
            return True
        if not CHAT_PUBLIC:
            return False
        key = (self._client_ip(), time.strftime("%Y-%m-%d"))
        with _chat_lock:
            used = _chat_counts.get(key, 0)
            if used >= CHAT_DAILY_LIMIT:
                return False
            if count:
                if len(_chat_counts) > 50000:
                    _chat_counts.clear()
                _chat_counts[key] = used + 1
        return True

    def _send_json(self, payload: dict, status: int = 200, set_cookie: str | None = None,
                   cache: str = CACHE_PRIVATE):
        body = json.dumps(_sanitize(payload), default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache if status == 200 and set_cookie is None else CACHE_PRIVATE)
        if set_cookie is not None:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str, content_type: str, cache: str = "no-cache"):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/" or parsed.path == "/index.html":
            self._send_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
            return

        if parsed.path == "/api/player":
            qs = parse_qs(parsed.query)
            name = (qs.get("name") or [""])[0].strip()
            if not name:
                self._send_json({"error": "missing 'name' query param"}, status=400)
                return
            try:
                # Logged-in visitors get their own positions attached, so that
                # payload is private. The UI adds acct=1 when connected so a
                # CDN-cached anonymous copy is never served in its place.
                client = self._session_client()
                if client is not None or (parse_qs(parsed.query).get("acct") or [""])[0] == "1":
                    self._send_json(pr.resolve_and_build(name, client=client))
                else:
                    self._send_json(snap.get("player:" + name.lower(),
                                             lambda: pr.resolve_and_build(name, client=None), TTL_LIVE),
                                    cache=CACHE_LIVE)
            except pr.PlayerNotFound as e:
                self._send_json({"error": str(e)}, status=404)
            except Exception as e:
                traceback.print_exc()
                self._send_json({"error": f"internal error: {e}"}, status=500)
            return

        if parsed.path == "/api/gamelog":
            qs = parse_qs(parsed.query)
            gsis_id = (qs.get("id") or [""])[0].strip()
            try:
                season = int((qs.get("season") or [""])[0])
            except ValueError:
                self._send_json({"error": "missing or bad 'season' query param"}, status=400)
                return
            if not gsis_id:
                self._send_json({"error": "missing 'id' query param"}, status=400)
                return
            try:
                data = snap.get(f"gamelog:{gsis_id}:{season}", lambda: {
                    "season": season, "games": vi.attach_to_game_log(gsis_id, nd.player_game_log(gsis_id, season))},
                    TTL_STATIC)
                self._send_json(data, cache=CACHE_SLOW)
            except Exception as e:
                traceback.print_exc()
                self._send_json({"error": f"game log lookup failed: {e}"}, status=502)
            return

        if parsed.path == "/api/pass_zones":
            # player=<gsis id> (or team=<offense> to use its current starting QB),
            # role=pass|target, def=<defense team>, season=<optional>
            qs = parse_qs(parsed.query)
            arg = lambda k: (qs.get(k) or [""])[0].strip()
            defense = nd.kalshi_to_nflverse_team(arg("def").upper())
            role = arg("role") or "pass"
            if not defense or role not in ("pass", "target"):
                self._send_json({"error": "need 'def' and role=pass|target"}, status=400)
                return
            try:
                season = int(arg("season")) if arg("season") else None
            except ValueError:
                self._send_json({"error": "bad 'season' query param"}, status=400)
                return
            try:
                def build():
                    pid = arg("player")
                    if not pid and arg("team"):
                        pid = pz.team_starting_qb(nd.kalshi_to_nflverse_team(arg("team").upper()), season)
                    return pz.matchup_zones(pid or None, defense, role, season)
                key = f"zones:{arg('player') or arg('team').upper()}:{role}:{defense}:{season or ''}"
                self._send_json(snap.get(key, build, TTL_STATIC), cache=CACHE_SLOW)
            except Exception as e:
                traceback.print_exc()
                self._send_json({"error": f"pass zone lookup failed: {e}"}, status=502)
            return

        if parsed.path == "/api/videos/team":
            team = (parse_qs(parsed.query).get("team") or [""])[0].strip().upper()
            if not team:
                self._send_json({"error": "missing 'team' query param"}, status=400)
                return
            try:
                self._send_json(snap.get("videos:" + team, lambda: vi.team_season_videos(
                    nd.kalshi_to_nflverse_team(team)), TTL_STATIC), cache=CACHE_SLOW)
            except Exception as e:
                traceback.print_exc()
                self._send_json({"error": f"video lookup failed: {e}"}, status=502)
            return

        if parsed.path == "/api/players":
            try:
                self._send_json(snap.get("players", lambda: {"players": nd.all_player_names()}, TTL_STATIC),
                                cache=CACHE_SLOW)
            except Exception as e:
                traceback.print_exc()
                self._send_json({"error": f"internal error: {e}"}, status=500)
            return

        if parsed.path == "/api/player_volume":
            try:
                self._send_json(snap.get("player_volume", pvol.player_volume_cached, TTL_VOLUME), cache=CACHE_LIVE)
            except Exception as e:
                traceback.print_exc()
                self._send_json({"error": f"volume lookup failed: {e}"}, status=502)
            return

        if parsed.path == "/api/injury_news":
            qs = parse_qs(parsed.query)
            name = (qs.get("name") or [""])[0].strip()
            if not name:
                self._send_json({"error": "missing 'name' query param"}, status=400)
                return
            try:
                self._send_json(snap.get("news:" + name.lower(), lambda: {
                    "name": name, "items": inj.player_injury_news(name)}, TTL_NEWS), cache=CACHE_SLOW)
            except Exception as e:
                traceback.print_exc()
                self._send_json({"error": f"news lookup failed: {e}"}, status=502)
            return

        if parsed.path == "/api/week":
            try:
                week = (parse_qs(parsed.query).get("week") or [""])[0].strip()
                if week.isdigit():
                    data = snap.get(f"week:{int(week)}", lambda: wo.build_week_overview(int(week)), TTL_LIVE)
                else:
                    data = snap.get("week:current", lambda: wo.build_week_overview(None), TTL_LIVE)
                self._send_json(data, cache=CACHE_LIVE)
            except Exception as e:
                traceback.print_exc()
                self._send_json({"error": f"internal error: {e}"}, status=500)
            return

        if parsed.path == "/api/game":
            qs = parse_qs(parsed.query)
            event_ticker = (qs.get("event") or [""])[0].strip()
            if not event_ticker:
                self._send_json({"error": "missing 'event' query param"}, status=400)
                return
            try:
                self._send_json(snap.get("game:" + event_ticker.upper(),
                                         lambda: wo.build_game_detail(event_ticker), TTL_LIVE), cache=CACHE_LIVE)
            except ValueError as e:
                self._send_json({"error": str(e)}, status=404)
            except Exception as e:
                traceback.print_exc()
                self._send_json({"error": f"internal error: {e}"}, status=500)
            return

        if parsed.path == "/api/combo/quotes":
            client = self._require_session_client()
            if client is None:
                return
            rfq_id = (parse_qs(parsed.query).get("rfq_id") or [""])[0].strip()
            if not rfq_id:
                self._send_json({"error": "missing 'rfq_id' query param"}, status=400)
                return
            self._combo_call(lambda: combo.get_quotes(rfq_id, client))
            return

        if parsed.path == "/api/positions":
            client = self._require_session_client()
            if client is None:
                return
            try:
                self._send_json(po.build_positions_overview(client))
            except Exception as e:
                traceback.print_exc()
                self._send_json({"error": f"internal error: {e}"}, status=500)
            return

        if parsed.path == "/api/engine/status":
            st = engine.status()
            if not st.get("chat_enabled"):
                st["chat_note"] = "Set ANTHROPIC_API_KEY to enable the engine chat."
            elif not self._chat_allowed():
                st["chat_enabled"] = False
                st["chat_note"] = ("Engine chat isn't available on the public site." if not CHAT_PUBLIC
                                   else f"Daily chat limit reached ({CHAT_DAILY_LIMIT} messages). Try again tomorrow.")
            self._send_json(st)
            return

        if parsed.path == "/api/engine/edges":
            qs = parse_qs(parsed.query)
            event = (qs.get("event") or [""])[0].strip() or None
            kind = (qs.get("kind") or ["all"])[0].strip() or "all"
            try:
                min_edge = float((qs.get("min_edge") or ["0.03"])[0])
            except ValueError:
                self._send_json({"error": "bad 'min_edge' query param"}, status=400)
                return
            min_edge = round(min_edge, 3)
            self._engine_call(lambda: snap.get(f"edges:{event}:{kind}:{min_edge}",
                                               lambda: engine.edges(event, min_edge, kind), TTL_LIVE),
                              cache=CACHE_LIVE)
            return

        if parsed.path == "/api/session":
            self._send_json({"connected": self._session_client() is not None})
            return

        self.send_response(404)
        self.end_headers()

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def _require_session_client(self):
        """Returns the visiting session's Kalshi client, or sends a 401 and
        returns None. Used to gate every account-scoped action (positions,
        combo quote/accept) -- there is no server-side fallback account."""
        client = self._session_client()
        if client is None:
            self._send_json({"error": "connect your Kalshi key to use this"}, status=401)
        return client

    def _combo_call(self, fn):
        """Run a combo action, surfacing Kalshi's own error text (e.g.
        INSUFFICIENT_BALANCE, 409 open RFQ) instead of a bare 500."""
        try:
            self._send_json(fn())
        except combo.ComboError as e:
            self._send_json({"error": str(e)}, status=400)
        except RuntimeError as e:  # missing KALSHI_* credentials
            self._send_json({"error": f"Kalshi credentials missing on the server: {e}"}, status=503)
        except Exception as e:
            resp = getattr(e, "response", None)
            if resp is not None:
                self._send_json({"error": f"Kalshi {resp.status_code}: {resp.text[:400]}"}, status=502)
                return
            traceback.print_exc()
            self._send_json({"error": f"internal error: {e}"}, status=500)

    def _engine_call(self, fn, cache: str = CACHE_PRIVATE):
        """Run an engine action: bad input -> 400, no Anthropic key -> 503,
        missing Kalshi credentials -> 503."""
        try:
            self._send_json(fn(), cache=cache)
        except engine.ChatUnavailable as e:
            self._send_json({"error": str(e), "chat_enabled": False}, status=503)
        except (engine.EngineError, sim.ScenarioError, TypeError, ValueError) as e:
            self._send_json({"error": str(e)}, status=400)
        except RuntimeError as e:  # missing KALSHI_* credentials
            self._send_json({"error": f"Kalshi credentials missing on the server: {e}"}, status=503)
        except Exception as e:
            resp = getattr(e, "response", None)
            if resp is not None and getattr(resp, "status_code", None):
                self._send_json({"error": f"upstream {resp.status_code}: {str(getattr(resp, 'text', ''))[:400]}"},
                                status=502)
                return
            traceback.print_exc()
            self._send_json({"error": f"internal error: {e}"}, status=500)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/login":
            try:
                body = self._read_json()
            except (ValueError, json.JSONDecodeError):
                self._send_json({"error": "body must be JSON"}, status=400)
                return
            api_key_id = str(body.get("api_key_id") or "").strip()
            private_key_pem = str(body.get("private_key_pem") or "").strip()
            if not api_key_id or not private_key_pem:
                self._send_json({"error": "api_key_id and private_key_pem are required"}, status=400)
                return
            try:
                token = sessions.create_session(api_key_id, private_key_pem)
            except Exception as e:
                resp = getattr(e, "response", None)
                msg = f"Kalshi {resp.status_code}: {resp.text[:300]}" if resp is not None else str(e)[:300]
                self._send_json({"error": f"couldn't authenticate that key: {msg}"}, status=401)
                return
            cookie = f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={sessions.TTL_SECONDS}"
            self._send_json({"ok": True}, set_cookie=cookie)
            return

        if parsed.path == "/api/logout":
            sessions.destroy_session(self._session_token())
            self._send_json({"ok": True}, set_cookie=f"{SESSION_COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")
            return

        try:
            body = self._read_json()
        except (ValueError, json.JSONDecodeError):
            self._send_json({"error": "body must be JSON"}, status=400)
            return
        legs = body.get("legs") or []

        if parsed.path == "/api/combo/validate":
            self._combo_call(lambda: combo.validate(legs))
            return
        if parsed.path == "/api/combo/quote":
            client = self._require_session_client()
            if client is None:
                return
            self._combo_call(lambda: combo.request_quote(legs, float(body.get("stake_dollars") or 0), client))
            return
        if parsed.path == "/api/combo/cancel":
            client = self._require_session_client()
            if client is None:
                return
            self._combo_call(lambda: combo.cancel_rfq(str(body.get("rfq_id") or ""), client))
            return
        if parsed.path == "/api/combo/accept":
            client = self._require_session_client()
            if client is None:
                return
            self._combo_call(lambda: combo.accept_quote(
                str(body.get("rfq_id") or ""), str(body.get("quote_id") or ""), str(body.get("side") or ""), client))
            return

        if parsed.path == "/api/engine/price":
            self._engine_call(lambda: sim.price_legs(legs, body.get("scenario")))
            return
        if parsed.path == "/api/engine/scenario":
            self._engine_call(lambda: sim.condition(str(body.get("event") or ""), body.get("constraints")))
            return
        if parsed.path == "/api/engine/ladder":
            self._engine_call(lambda: sim.ladder(
                str(body.get("event") or ""), str(body.get("team") or ""), str(body.get("family") or ""),
                body.get("strikes") or [], body.get("stakes"), body.get("scenario"), bool(body.get("include_ml"))))
            return
        if parsed.path == "/api/engine/chat":
            if not self._chat_allowed(count=True):
                msg = ("engine chat is only available on the operator's machine" if not CHAT_PUBLIC
                       else f"daily chat limit reached ({CHAT_DAILY_LIMIT} messages)")
                self._send_json({"error": msg, "chat_enabled": False}, status=429 if CHAT_PUBLIC else 403)
                return
            self._engine_call(lambda: engine.chat(
                body.get("message") or "", body.get("slip") or {}, body.get("conversation_id") or None))
            return

        self.send_response(404)
        self.end_headers()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=_cache_warmer, daemon=True, name="cache-warmer").start()
    threading.Thread(target=_slate_refresher, daemon=True, name="slate-refresher").start()
    print(f"Serving on http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
