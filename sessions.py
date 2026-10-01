"""
In-memory per-visitor Kalshi sessions.

A visitor's API key id + private key PEM are held only in this process's
memory, keyed by a random token handed to them as a cookie -- never written
to disk, never logged. Cleared on logout, TTL expiry, or server restart.
"""

from __future__ import annotations

import os
import secrets
import threading
import time

from kalshi_book import KalshiClient

TTL_SECONDS = 12 * 3600

_lock = threading.Lock()
_sessions: dict[str, dict] = {}  # token -> {"client": KalshiClient, "touched": float}


def create_session(api_key_id: str, private_key_pem: str) -> str:
    """Build a client from the pasted credentials and confirm Kalshi actually
    accepts them before handing back a session token. Raises on bad input or
    a Kalshi-side rejection -- the caller should surface that error text."""
    client = KalshiClient(api_key_id=api_key_id, private_key_pem=private_key_pem)
    client.get_balance()  # raises if Kalshi rejects the key
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = {"client": client, "touched": time.time()}
    return token


def get_session(token: str | None) -> KalshiClient | None:
    if not token:
        return None
    with _lock:
        entry = _sessions.get(token)
        if entry is None:
            return None
        if time.time() - entry["touched"] > TTL_SECONDS:
            del _sessions[token]
            return None
        entry["touched"] = time.time()
        return entry["client"]


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with _lock:
        _sessions.pop(token, None)


_default: dict = {"client": None, "tried": False}


def default_client() -> KalshiClient | None:
    """The owner's env-configured client (KALSHI_API_KEY_ID /
    KALSHI_PRIVATE_KEY_PATH), only when KALSHI_DEFAULT_LOGIN=1. The server
    restricts this to direct local requests."""
    if os.environ.get("KALSHI_DEFAULT_LOGIN") != "1":
        return None
    with _lock:
        if not _default["tried"]:
            _default["tried"] = True
            try:
                _default["client"] = KalshiClient()
            except Exception as e:
                print(f"default Kalshi login unavailable: {e}")
        return _default["client"]
