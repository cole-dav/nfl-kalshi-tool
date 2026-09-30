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
kalshi_book.py directly; the HTTP server itself never falls back to it.

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
import team_tendencies as tt
import week_overview as wo
import player_volume as pvol
import engine_agent as engine
import scenario_sim as sim

SESSION_COOKIE = "sid"

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
WARM_INTERVAL_SECONDS = 10 * 60


def _cache_warmer():
    """Keep nflverse data, rankings and Madden ratings loaded in memory and
    refetch them here when they go stale, so page requests never wait on it."""
    while True:
        for warm in (nd.warm, prk.warm, mr.warm, tt.warm):
            try:
                warm()
            except Exception:
                traceback.print_exc()
        time.sleep(WARM_INTERVAL_SECONDS)


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

    def _session_client(self):
        """The visiting session's own Kalshi client, or None if not logged
        in. Never falls back to a server-side/.env default -- account-scoped
        actions must either use this or refuse."""
        return sessions.get_session(self._session_token())

    def _send_json(self, payload: dict, status: int = 200, set_cookie: str | None = None):
        body = json.dumps(_sanitize(payload), default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if set_cookie is not None:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str, content_type: str):
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
                data = pr.resolve_and_build(name, client=self._session_client())
                self._send_json(data)
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
                self._send_json({"season": season, "games": nd.player_game_log(gsis_id, season)})
            except Exception as e:
                traceback.print_exc()
                self._send_json({"error": f"game log lookup failed: {e}"}, status=502)
            return

        if parsed.path == "/api/players":
            try:
                self._send_json({"players": nd.all_player_names()})
            except Exception as e:
                traceback.print_exc()
                self._send_json({"error": f"internal error: {e}"}, status=500)
            return

        if parsed.path == "/api/player_volume":
            try:
                self._send_json(pvol.player_volume_cached())
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
                self._send_json({"name": name, "items": inj.player_injury_news(name)})
            except Exception as e:
                traceback.print_exc()
                self._send_json({"error": f"news lookup failed: {e}"}, status=502)
            return

        if parsed.path == "/api/week":
            try:
                week = (parse_qs(parsed.query).get("week") or [""])[0].strip()
                self._send_json(wo.build_week_overview(int(week) if week.isdigit() else None))
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
                self._send_json(wo.build_game_detail(event_ticker))
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
            self._send_json(engine.status())
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
            self._engine_call(lambda: engine.edges(event, min_edge, kind))
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

    def _engine_call(self, fn):
        """Run an engine action: bad input -> 400, no Anthropic key -> 503,
        missing Kalshi credentials -> 503."""
        try:
            self._send_json(fn())
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
            self._engine_call(lambda: engine.chat(
                body.get("message") or "", body.get("slip") or {}, body.get("conversation_id") or None))
            return

        self.send_response(404)
        self.end_headers()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=_cache_warmer, daemon=True, name="cache-warmer").start()
    print(f"Serving on http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
