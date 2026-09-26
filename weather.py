"""Open-Meteo forecast lookup for a stadium + game date/time. No API key needed."""

from __future__ import annotations

import time
from datetime import datetime

import requests

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


def get_game_weather(lat: float, lon: float, game_date: str, game_time_local: str | None, roof: str) -> dict:
    """game_date: 'YYYY-MM-DD'. game_time_local: 'HH:MM' (24h) or None.
    Returns None-safe dict; if roof is a dome/closed retractable, weather
    doesn't matter for gameplay and we say so instead of guessing."""
    if roof in ("fixed_dome", "closed"):
        return {"roof": roof, "forecast_available": False, "note": "Fixed dome -- weather is not a game factor."}

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation_probability,wind_speed_10m,wind_gusts_10m",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "timezone": "auto",
        "start_date": game_date,
        "end_date": game_date,
    }
    data, last_err = None, None
    for attempt in range(3):
        try:
            resp = requests.get(OPEN_METEO_URL, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    if data is None:
        return {"roof": roof, "forecast_available": False, "error": str(last_err)}

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    if not times:
        return {"roof": roof, "forecast_available": False, "note": "No forecast data returned."}

    target_hour = f"{game_date}T{(game_time_local or '13:00')[:5]}"
    idx = _closest_index(times, target_hour)

    result = {
        "roof": roof,
        "forecast_available": True,
        "time": times[idx],
        "temperature_f": hourly.get("temperature_2m", [None])[idx],
        "precipitation_probability_pct": hourly.get("precipitation_probability", [None])[idx],
        "wind_speed_mph": hourly.get("wind_speed_10m", [None])[idx],
        "wind_gusts_mph": hourly.get("wind_gusts_10m", [None])[idx],
    }
    if roof == "retractable":
        result["note"] = "Retractable roof -- may be closed regardless of forecast."
    return result


def _closest_index(times: list[str], target: str) -> int:
    try:
        target_dt = datetime.fromisoformat(target)
    except ValueError:
        return 0
    best_i, best_diff = 0, None
    for i, t in enumerate(times):
        try:
            dt = datetime.fromisoformat(t)
        except ValueError:
            continue
        diff = abs((dt - target_dt).total_seconds())
        if best_diff is None or diff < best_diff:
            best_diff, best_i = diff, i
    return best_i
