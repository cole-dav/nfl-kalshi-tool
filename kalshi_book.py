"""
Kalshi API client: RSA-PSS request signing, positions, and scenario P&L.

Auth reads:
  KALSHI_API_KEY_ID      - the API key id (uuid) from the Kalshi UI
  KALSHI_PRIVATE_KEY_PATH - path to the matching RSA private key .pem
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Iterable

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# GET responses are cached to disk for this many seconds so that repeated
# local dev refreshes (browser reload -> re-fetch same markets) don't re-hit
# the network every time. Kalshi odds move on a much slower cadence than a
# dev iteration loop, so a short TTL keeps data fresh enough for real use
# while making "refresh the page a few times while tweaking code" instant.
# Set KALSHI_CACHE_TTL=0 to disable (always hit the network).
CACHE_TTL_SECONDS = float(os.environ.get("KALSHI_CACHE_TTL", "20"))
_API_CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache", "kalshi_api")


def _cache_key(path: str, params: dict | None) -> str:
    raw = json.dumps({"path": path, "params": params or {}}, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_read(path: str, params: dict | None) -> dict | None:
    if CACHE_TTL_SECONDS <= 0:
        return None
    fpath = os.path.join(_API_CACHE_DIR, _cache_key(path, params) + ".json")
    try:
        if time.time() - os.path.getmtime(fpath) > CACHE_TTL_SECONDS:
            return None
        with open(fpath) as f:
            return json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _cache_write(path: str, params: dict | None, data: dict) -> None:
    if CACHE_TTL_SECONDS <= 0:
        return
    os.makedirs(_API_CACHE_DIR, exist_ok=True)
    fpath = os.path.join(_API_CACHE_DIR, _cache_key(path, params) + ".json")
    with open(fpath, "w") as f:
        json.dump(data, f)


def public_get(path: str, params: dict | None = None) -> dict[str, Any]:
    """Unauthenticated GET for public market-data endpoints (markets, events,
    multivariate collections). Shares the signed client's disk cache; `path`
    is the same /trade-api/v2/... form."""
    cached = _cache_read(path, params)
    if cached is not None:
        return cached
    host = KALSHI_BASE_URL.split("/trade-api", 1)[0]
    resp = requests.get(host + path, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    _cache_write(path, params, data)
    return data


def public_get_events(
    series_ticker: str | None = None, status: str | None = None, with_nested_markets: bool = True
) -> list[dict]:
    """Unauthenticated, cursor-paginated /trade-api/v2/events listing -- the
    keyless equivalent of KalshiClient.get_events, for browsing odds/matchups
    without any Kalshi credentials at all."""
    params: dict[str, Any] = {"with_nested_markets": with_nested_markets}
    if series_ticker:
        params["series_ticker"] = series_ticker
    if status:
        params["status"] = status
    events: list[dict] = []
    cursor = None
    while True:
        if cursor:
            params["cursor"] = cursor
        data = public_get("/trade-api/v2/events", params=params)
        events.extend(data.get("events", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return events


def _load_private_key():
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_path:
        raise RuntimeError("KALSHI_PRIVATE_KEY_PATH is not set")
    key_path = os.path.expanduser(key_path)
    with open(key_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


class KalshiClient:
    """Minimal Kalshi trade-api v2 client using RSA-PSS signed requests."""

    def __init__(
        self,
        api_key_id: str | None = None,
        private_key_path: str | None = None,
        private_key_pem: str | None = None,
        base_url: str = KALSHI_BASE_URL,
    ):
        self.api_key_id = api_key_id or os.environ.get("KALSHI_API_KEY_ID")
        if not self.api_key_id:
            raise RuntimeError("KALSHI_API_KEY_ID is not set")

        if private_key_pem:
            # In-memory only -- never written to disk, e.g. a key pasted into
            # a per-session login rather than configured via env/file.
            pem_bytes = private_key_pem.encode("utf-8") if isinstance(private_key_pem, str) else private_key_pem
            self._private_key = serialization.load_pem_private_key(pem_bytes, password=None)
        elif private_key_path:
            with open(os.path.expanduser(private_key_path), "rb") as f:
                self._private_key = serialization.load_pem_private_key(f.read(), password=None)
        else:
            self._private_key = _load_private_key()

        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()

    # -- auth -----------------------------------------------------------

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        """Kalshi signs the concatenation of timestamp + HTTP method + path
        (path only, no query string, must include the /trade-api/v2 prefix)
        using RSA-PSS with SHA256 and MGF1(SHA256), salt length = digest size.
        """
        message = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _headers(self, method: str, path: str) -> dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        signature = self._sign(timestamp_ms, method, path)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, max_retries: int = 3, **kwargs) -> dict[str, Any]:
        """`path` must start with /trade-api/v2/... it is what gets signed.

        Kalshi's API occasionally times out or 5xx's transiently under load;
        retries with backoff since each attempt re-signs with a fresh
        timestamp (a stale signature would otherwise be rejected on retry)."""
        url = f"https://{self.base_url.split('://', 1)[1].split('/', 1)[0]}{path}"
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                headers = self._headers(method, path)
                resp = self._session.request(method, url, headers=headers, timeout=20, **kwargs)
                resp.raise_for_status()
                # accept/delete endpoints answer 204 with no body
                return resp.json() if resp.content else {}
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_exc = e
            except requests.exceptions.HTTPError as e:
                if e.response is not None and (e.response.status_code == 429 or e.response.status_code >= 500):
                    last_exc = e
                else:
                    raise
            if attempt < max_retries - 1:
                time.sleep(0.5 * (2 ** attempt))
        raise last_exc

    def get(self, path: str, params: dict | None = None) -> dict[str, Any]:
        cached = _cache_read(path, params)
        if cached is not None:
            return cached
        data = self._request("GET", path, params=params)
        _cache_write(path, params, data)
        return data

    # Writes are never cached and never retried: a timed-out POST may still
    # have landed (e.g. an RFQ was created), and blindly re-sending it would
    # double-submit.
    def post(self, path: str, body: dict) -> dict[str, Any]:
        return self._request("POST", path, max_retries=1, json=body)

    def put(self, path: str, body: dict) -> dict[str, Any]:
        return self._request("PUT", path, max_retries=1, json=body)

    def delete(self, path: str) -> dict[str, Any]:
        return self._request("DELETE", path, max_retries=1)

    # -- market discovery -------------------------------------------------

    def get_series_list(self, category: str | None = None) -> list[dict]:
        params = {"category": category} if category else None
        data = self.get("/trade-api/v2/series", params=params)
        return data.get("series", [])

    def get_series(self, series_ticker: str) -> dict:
        return self.get(f"/trade-api/v2/series/{series_ticker}")

    def get_markets(
        self,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict:
        params = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return self.get("/trade-api/v2/markets", params=params)

    def get_all_markets(self, **kwargs) -> list[dict]:
        markets: list[dict] = []
        cursor = None
        while True:
            page = self.get_markets(cursor=cursor, **kwargs)
            markets.extend(page.get("markets", []))
            cursor = page.get("cursor")
            if not cursor:
                break
        return markets

    def get_events(self, series_ticker: str | None = None, status: str | None = None, with_nested_markets: bool = True) -> list[dict]:
        params: dict[str, Any] = {"with_nested_markets": with_nested_markets}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        events: list[dict] = []
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            data = self.get("/trade-api/v2/events", params=params)
            events.extend(data.get("events", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return events

    def get_market(self, ticker: str) -> dict:
        return self.get(f"/trade-api/v2/markets/{ticker}")

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        return self.get(f"/trade-api/v2/markets/{ticker}/orderbook", params={"depth": depth})

    # -- account / positions ----------------------------------------------

    def get_balance(self) -> dict:
        return self.get("/trade-api/v2/portfolio/balance")

    def get_positions(self, settlement_status: str = "unsettled") -> list[dict]:
        data = self.get(
            "/trade-api/v2/portfolio/positions",
            params={"settlement_status": settlement_status},
        )
        return data.get("market_positions", [])

    def get_fills(self, ticker: str | None = None, limit: int = 200) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        data = self.get("/trade-api/v2/portfolio/fills", params=params)
        return data.get("fills", [])


# -- scenario P&L ----------------------------------------------------------


@dataclass
class Position:
    ticker: str
    side: str  # "yes" or "no"
    contracts: int  # positive = long that side
    avg_price_cents: float  # average entry price in cents (of the held side)


def position_from_kalshi(raw: dict) -> Position:
    """Convert a /portfolio/positions market_position entry into a Position.

    Kalshi reports net YES contracts (negative = net NO) as `position`
    (legacy, integer cents-era field) or `position_fp` (current, float).
    Exposure is likewise `market_exposure` (cents) or `market_exposure_dollars`
    (current). Prefer whichever pair is actually present in the response.
    """
    net = raw.get("position")
    if net is None:
        net = float(raw.get("position_fp", 0))
    net = int(round(net))
    side = "yes" if net >= 0 else "no"
    contracts = abs(net)
    exposure = raw.get("market_exposure")
    if exposure is None:
        exposure = float(raw.get("market_exposure_dollars", 0)) * 100
    exposure = abs(exposure)
    avg_price = (exposure / contracts) if contracts else 0.0
    return Position(
        ticker=raw["ticker"],
        side=side,
        contracts=contracts,
        avg_price_cents=avg_price,
    )


def scenario_pnl(position: Position, resolved_yes_price_cents: float) -> float:
    """P&L in dollars if the market resolves such that YES is worth
    `resolved_yes_price_cents` (100 = YES wins, 0 = NO wins; anything else
    for a partial "what if the price moves to X" scenario, not a real
    settlement).

    `position.avg_price_cents` is the average entry price of the side
    actually held (e.g. a NO position bought at 30c has avg_price_cents=30,
    not 70) -- so value-at-resolution of that same side, minus entry, is all
    that's needed; no extra 100-x conversion for the NO case."""
    if position.side == "yes":
        value_per_contract = resolved_yes_price_cents
    else:
        value_per_contract = 100 - resolved_yes_price_cents
    pnl_cents_per_contract = value_per_contract - position.avg_price_cents
    return (pnl_cents_per_contract * position.contracts) / 100.0


def scenario_pnl_table(
    position: Position, price_points_cents: Iterable[float] = (0, 25, 50, 75, 100)
) -> dict[float, float]:
    return {p: scenario_pnl(position, p) for p in price_points_cents}


def portfolio_scenario_pnl(
    positions: Iterable[Position], yes_prices_cents: dict[str, float]
) -> dict[str, float]:
    """Given a hypothetical settlement/price for each ticker, sum P&L per ticker."""
    out = {}
    for pos in positions:
        if pos.ticker in yes_prices_cents:
            out[pos.ticker] = scenario_pnl(pos, yes_prices_cents[pos.ticker])
    return out


if __name__ == "__main__":
    client = KalshiClient()
    balance = client.get_balance()
    print("Balance (cents):", balance)
    positions = client.get_positions()
    print(f"{len(positions)} open positions")
    for raw in positions:
        pos = position_from_kalshi(raw)
        print(pos, "-> scenario P&L:", scenario_pnl_table(pos))
