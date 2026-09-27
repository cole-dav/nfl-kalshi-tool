"""
Local web server for the NFL player research tool.

Run:
  export KALSHI_API_KEY_ID=...
  export KALSHI_PRIVATE_KEY_PATH=./keys/kalshi_private_key.pem
  python3 server.py [port]

Serves static/index.html at / and JSON at /api/player?name=<player name>.
"""

from __future__ import annotations

import json
import math
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import player_research as pr
import positions_overview as po
import injury_news as inj
import nflverse_data as nd
import week_overview as wo
import player_volume as pvol

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


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

        if parsed.path == "/api/positions":
            try:
                self._send_json(po.build_positions_overview())
            except Exception as e:
                traceback.print_exc()
                self._send_json({"error": f"internal error: {e}"}, status=500)
            return

        self.send_response(404)
        self.end_headers()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    if not os.environ.get("KALSHI_API_KEY_ID") or not os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
        print("WARNING: KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH not set -- Kalshi calls will fail.",
              file=sys.stderr)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Serving on http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
