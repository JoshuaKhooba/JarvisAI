"""
app_launcher.py — JARVIS App Launcher

Small helpers for bringing native macOS apps forward. Currently unused by
the HUD itself (the Reminders/Calendar live panels render their own data
in-app rather than linking out), but kept as a lightweight utility in case
a future feature wants to jump to the real app window.

macOS only. Safe to call from a background thread (they block briefly on
an `open` subprocess call) — never call them directly on the Qt UI thread.

Functions:
  open_reminders()  — bring the Reminders app forward
  open_calendar()    — bring the Calendar app forward
"""

import subprocess
import sys


def _is_mac() -> bool:
    return sys.platform == "darwin"


def open_reminders() -> str:
    if not _is_mac():
        return "Reminders can only be opened on macOS."
    try:
        subprocess.run(["open", "-a", "Reminders"], check=False, timeout=10)
        return "Opened Reminders."
    except Exception as e:
        return f"Could not open Reminders: {e}"


def open_calendar() -> str:
    if not _is_mac():
        return "Calendar can only be opened on macOS."
    try:
        subprocess.run(["open", "-a", "Calendar"], check=False, timeout=10)
        return "Opened Calendar."
    except Exception as e:
        return f"Could not open Calendar: {e}"
