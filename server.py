"""
Local web server for the NFL player research tool.

Run:
  python3 server.py [port]

Reads KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH from a .env file in the
project root (see .env.example), or from the environment if already set.

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
import week_overview as wo
import player_volume as pvol

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
WARM_INTERVAL_SECONDS = 10 * 60


def _cache_warmer():
    """Keep nflverse data, rankings and Madden ratings loaded in memory and
    refetch them here when they go stale, so page requests never wait on it."""
    while True:
        for warm in (nd.warm, prk.warm, mr.warm):
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

    def _send_json(self, payload: dict, status: int = 200):
        body = json.dumps(_sanitize(payload), default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
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
                data = pr.resolve_and_build(name)
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
                self._send_json(wo.build_week_overview())
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
            rfq_id = (parse_qs(parsed.query).get("rfq_id") or [""])[0].strip()
            if not rfq_id:
                self._send_json({"error": "missing 'rfq_id' query param"}, status=400)
                return
            self._combo_call(lambda: combo.get_quotes(rfq_id))
            return

        if parsed.path == "/api/positions":
            try:
                self._send_json(po.build_positions_overview())
            except Exception as e:
                traceback.print_exc()
                self._send_json({"error": f"internal error: {e}"}, status=500)
            return

        self.send_response(404)
        self.end_headers()

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

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

    def do_POST(self):
        parsed = urlparse(self.path)
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
            self._combo_call(lambda: combo.request_quote(legs, float(body.get("stake_dollars") or 0)))
            return
        if parsed.path == "/api/combo/cancel":
            self._combo_call(lambda: combo.cancel_rfq(str(body.get("rfq_id") or "")))
            return
        if parsed.path == "/api/combo/accept":
            self._combo_call(lambda: combo.accept_quote(
                str(body.get("rfq_id") or ""), str(body.get("quote_id") or ""), str(body.get("side") or "")))
            return

        self.send_response(404)
        self.end_headers()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    if not os.environ.get("KALSHI_API_KEY_ID") or not os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
        print("WARNING: KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH not set -- Kalshi calls will fail.",
              file=sys.stderr)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=_cache_warmer, daemon=True, name="cache-warmer").start()
    print(f"Serving on http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
