"""
weather.py — JARVIS Weather Lookup

Current-conditions lookup for a given lat/lon, used by the HUD's Global Ops
page (see ui.py GlobalOpsOverlay) to show real weather at the user's
resolved location alongside the globe.

Uses Open-Meteo (https://open-meteo.com) — free, no API key required, no
account/signup. macOS's built-in Weather app has no AppleScript/Shortcuts
automation interface (unlike Calendar/Reminders), so this hits a weather API
directly instead of trying to script that app.

Result is cached in-process per rounded lat/lon for a few minutes, so
repeated calls (e.g. a UI refresh timer) don't hammer the API.

Not to be confused with actions/weather_report.py — that one is the
Gemini "weather_report" voice tool (opens a browser search, no structured
data). This module is not a Gemini tool at all; it's called directly by
ui.py's GlobalOpsOverlay for its on-screen readout.
"""

import time

_CACHE: dict[tuple, tuple[float, dict | None]] = {}
_CACHE_TTL_SECS = 600  # 10 minutes — weather doesn't need to be fresher than that

_WMO_CONDITIONS = {
    0: "Clear", 1: "Mainly Clear", 2: "Partly Cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime Fog",
    51: "Light Drizzle", 53: "Drizzle", 55: "Dense Drizzle",
    56: "Freezing Drizzle", 57: "Freezing Drizzle",
    61: "Light Rain", 63: "Rain", 65: "Heavy Rain",
    66: "Freezing Rain", 67: "Freezing Rain",
    71: "Light Snow", 73: "Snow", 75: "Heavy Snow", 77: "Snow Grains",
    80: "Rain Showers", 81: "Rain Showers", 82: "Violent Showers",
    85: "Snow Showers", 86: "Snow Showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ Hail", 99: "Thunderstorm w/ Hail",
}


def _condition_for(code: int) -> str:
    return _WMO_CONDITIONS.get(code, "Unknown")


def get_current_weather(lat: float, lon: float) -> dict | None:
    """
    Returns {"temp_f": float, "condition": str, "wind_mph": float, "code": int}
    for the given coordinates, or None if the request fails for any reason
    (offline, API hiccup, etc.) — callers should treat None as "no data yet",
    not an error to surface loudly.
    """
    key = (round(lat, 2), round(lon, 2))
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < _CACHE_TTL_SECS:
        return cached[1]

    result = None
    try:
        import requests
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current_weather": "true",
                "temperature_unit": "fahrenheit",
                "windspeed_unit": "mph",
            },
            timeout=6,
        )
        if resp.status_code == 200:
            cw = resp.json().get("current_weather", {})
            if "temperature" in cw:
                code = int(cw.get("weathercode", -1))
                result = {
                    "temp_f": float(cw["temperature"]),
                    "condition": _condition_for(code),
                    "wind_mph": float(cw.get("windspeed", 0.0)),
                    "code": code,
                }
    except Exception as e:
        print(f"[Weather] Lookup failed: {e}")
        result = None

    _CACHE[key] = (now, result)
    return result
