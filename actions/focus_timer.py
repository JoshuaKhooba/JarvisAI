"""
focus_timer.py — JARVIS Focus / Pomodoro Sessions

Tracks a single active focus session (start time, duration, label) in a small
JSON state file. main.py's background loop polls `check_and_consume_completion()`
periodically to announce when a session ends, and the proactive engine should
check `is_active()` before speaking unprompted so Jarvis stays quiet during
a focus block.

Actions:
  start_focus   — begin a focus session for N minutes (refuses if one's already running)
  focus_status  — how much time is left in the current session
  cancel_focus  — end the current session early
"""

import json
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


STATE_PATH = _base_dir() / "memory" / "focus_state.json"
_lock      = threading.Lock()
_HISTORY_MAX = 20


def _empty_state() -> dict:
    return {"active": False, "label": "", "start": "", "end": "",
            "announced_end": True, "history": []}


def _load() -> dict:
    with _lock:
        if not STATE_PATH.exists():
            return _empty_state()
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = _empty_state()
                base.update(data)
                return base
            return _empty_state()
        except Exception:
            return _empty_state()


def _save(state: dict) -> None:
    with _lock:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Public helpers used by main.py / ui.py ──────────────────────────────────

def is_active() -> bool:
    return _load().get("active", False)


def get_ui_status() -> dict:
    """
    Cheap, local-only status snapshot (just a small JSON file read — no
    subprocess/network) safe to call directly from the UI thread on a timer.
    Returns {"active": bool, "label": str, "remaining": "MM:SS"}.
    """
    state = _load()
    if not state.get("active"):
        return {"active": False, "label": "", "remaining": ""}
    try:
        end = datetime.fromisoformat(state["end"])
    except Exception:
        return {"active": False, "label": "", "remaining": ""}
    remaining = max(0, int((end - datetime.now()).total_seconds()))
    return {
        "active": True,
        "label": state.get("label") or "Focus session",
        "remaining": f"{remaining // 60:02d}:{remaining % 60:02d}",
    }


def check_and_consume_completion() -> str | None:
    """
    Called periodically by main.py's background loop. Returns an announcement
    string exactly once when an active session's end time has passed, then
    clears the active flag so it never fires twice for the same session.
    """
    state = _load()
    if not state.get("active"):
        return None
    try:
        end = datetime.fromisoformat(state["end"])
    except Exception:
        return None
    if datetime.now() < end:
        return None

    label = state.get("label") or "Focus session"
    state["history"] = ([{"label": label, "start": state["start"], "end": state["end"], "completed": True}]
                         + state.get("history", []))[:_HISTORY_MAX]
    state["active"] = False
    state["announced_end"] = True
    _save(state)
    return f"{label} is complete — {timedelta_minutes(state)} min session finished."


def timedelta_minutes(state: dict) -> int:
    try:
        start = datetime.fromisoformat(state["history"][0]["start"])
        end   = datetime.fromisoformat(state["history"][0]["end"])
        return int((end - start).total_seconds() // 60)
    except Exception:
        return 0


# ── Actions ──────────────────────────────────────────────────────────────────

def _start_focus(duration_minutes: int, label: str = "") -> str:
    if duration_minutes <= 0:
        return "Please provide a focus duration in minutes."

    state = _load()
    if state.get("active"):
        remaining = _remaining_str(state)
        return f"A focus session is already running ({state.get('label', 'Focus')}, {remaining} left). Cancel it first if you want to start a new one."

    now = datetime.now()
    end = now + timedelta(minutes=duration_minutes)
    label = label.strip() or "Focus session"

    state.update({
        "active": True,
        "label": label,
        "start": now.isoformat(),
        "end": end.isoformat(),
        "announced_end": False,
    })
    _save(state)
    return f"Starting {duration_minutes}-minute {label}. I'll stay quiet until it's done and let you know when time's up."


def _remaining_str(state: dict) -> str:
    try:
        end = datetime.fromisoformat(state["end"])
    except Exception:
        return "unknown time"
    remaining = end - datetime.now()
    mins = max(0, int(remaining.total_seconds() // 60))
    secs = max(0, int(remaining.total_seconds() % 60))
    return f"{mins}m {secs}s"


def _focus_status() -> str:
    state = _load()
    if not state.get("active"):
        return "No active focus session."
    return f"{state.get('label', 'Focus session')}: {_remaining_str(state)} remaining."


def _cancel_focus() -> str:
    state = _load()
    if not state.get("active"):
        return "No active focus session to cancel."
    label = state.get("label", "Focus session")
    state["history"] = ([{"label": label, "start": state["start"], "end": datetime.now().isoformat(), "completed": False}]
                         + state.get("history", []))[:_HISTORY_MAX]
    state["active"] = False
    state["announced_end"] = True
    _save(state)
    return f"Cancelled: {label}."


def focus_timer(
    parameters:     dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    action = params.get("action", "").lower().strip()
    result = "Unknown focus action."

    try:
        if action == "start_focus":
            result = _start_focus(int(params.get("duration_minutes", 25)), params.get("label", ""))
        elif action == "focus_status":
            result = _focus_status()
        elif action == "cancel_focus":
            result = _cancel_focus()
        else:
            result = f"Unknown focus action: '{action}'"
    except Exception as e:
        result = f"Focus timer error ({action}): {e}"

    if player:
        player.write_log(f"[Focus] {result[:70]}")
    return result
