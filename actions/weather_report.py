"""
weather_report.py — the "weather_report" tool (see main.py TOOL_DECLARATIONS).

Not to be confused with actions/weather.py (a different, newer module):
this one is the voice tool Gemini calls when the user asks "what's the
weather in <city>" — it just opens a Google search for it in the user's
browser and confirms verbally, no structured data involved.
actions/weather.py instead fetches real numeric conditions from the
Open-Meteo API and is used only by ui.py's GlobalOpsOverlay widget to show
a live temperature/condition readout next to the Earth globe — it isn't a
Gemini tool at all, and neither module calls the other.
"""

import webbrowser
from urllib.parse import quote_plus


def weather_action(
    parameters: dict,
    player=None,
    session_memory=None,
) -> str:
    """Entry point called from main.py's tool dispatcher — opens a Google
    search for "weather in <city> <time>" in the user's real browser and
    returns a short spoken confirmation."""
    city     = parameters.get("city")
    when     = parameters.get("time", "today")  

    if not city or not isinstance(city, str) or not city.strip():
        msg = "Sir, the city is missing for the weather report."
        _log(msg, player)
        return msg

    city = city.strip()
    when = (when or "today").strip()

    search_query  = f"weather in {city} {when}"
    url           = f"https://www.google.com/search?q={quote_plus(search_query)}"

    try:
        opened = webbrowser.open(url)
        if not opened:
            raise RuntimeError("webbrowser.open returned False")
    except Exception as e:
        msg = f"Sir, I couldn't open the browser for the weather report: {e}"
        _log(msg, player)
        return msg

    msg = f"Showing the weather for {city}, {when}, sir."
    _log(msg, player)

    if session_memory:
        try:
            session_memory.set_last_search(query=search_query, response=msg)
        except Exception:
            pass

    return msg


def _log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass