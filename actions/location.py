"""
location.py — JARVIS Location Resolver

Resolves the user's approximate location for the HUD's rotating Earth
globe (a glowing marker at the user's position). Priority:

  1. Manual override in config/api_keys.json — set "user_latitude" and
     "user_longitude" (and optionally "user_city" for the label) for an
     exact, private location that never triggers a network call.
  2. Approximate IP-based geolocation (city-level accuracy, no API key
     required) via ipapi.co, used only if no manual override is set.

The result is cached in-process for the life of the run (including
failures, so a missing/unreachable network doesn't retry every 60 seconds
forever) — call get_location() as often as you like after the first call;
it's just a dict lookup after that.
"""

import json
import sys
import threading
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = _base_dir() / "config" / "api_keys.json"

_lock          = threading.Lock()
_cached: dict | None = None
_cache_failed  = False


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _manual_override() -> dict | None:
    cfg = _load_config()
    lat, lon = cfg.get("user_latitude"), cfg.get("user_longitude")
    if lat in (None, "") or lon in (None, ""):
        return None
    try:
        return {
            "lat": float(lat), "lon": float(lon),
            "city": (cfg.get("user_city") or "").strip(),
            "source": "config",
        }
    except (TypeError, ValueError):
        return None


def _ip_geolocate_ipapi() -> dict | None:
    import requests
    resp = requests.get("https://ipapi.co/json/", timeout=5)
    if resp.status_code != 200:
        return None
    data = resp.json()
    lat, lon = data.get("latitude"), data.get("longitude")
    if lat is None or lon is None:
        return None
    return {
        "lat": float(lat), "lon": float(lon),
        "city": (data.get("city") or "").strip(),
        "source": "ip",
    }


def _ip_geolocate_ipapicom() -> dict | None:
    import requests
    resp = requests.get("http://ip-api.com/json/", timeout=5)
    if resp.status_code != 200:
        return None
    data = resp.json()
    if data.get("status") != "success":
        return None
    lat, lon = data.get("lat"), data.get("lon")
    if lat is None or lon is None:
        return None
    return {
        "lat": float(lat), "lon": float(lon),
        "city": (data.get("city") or "").strip(),
        "source": "ip",
    }


def _ip_geolocate() -> dict | None:
    """Tries a couple of free IP-geolocation providers in order, in case one
    is rate-limited/unreachable — falls back to the next before giving up."""
    for provider in (_ip_geolocate_ipapi, _ip_geolocate_ipapicom):
        try:
            result = provider()
            if result:
                return result
        except Exception as e:
            print(f"[Location] {provider.__name__} failed: {e}")
    return None


def get_location(force_refresh: bool = False) -> dict | None:
    """
    Returns {"lat": float, "lon": float, "city": str, "source": "config"|"ip"}
    or None if location can't be determined by either method. Cheap to call
    repeatedly — only does real work (a possible network call) once, then
    returns the cached result (or cached failure) on subsequent calls unless
    force_refresh=True.
    """
    global _cached, _cache_failed
    with _lock:
        if _cached is not None and not force_refresh:
            return _cached
        if _cache_failed and not force_refresh:
            return None

        result = _manual_override() or _ip_geolocate()

        if result is None:
            _cache_failed = True
            return None

        _cached = result
        return result
