"""
habit_tracker.py — JARVIS Habit Tracking

Voice-logged habits (water, exercise, meditation, etc.) with streak tracking.
Stored in memory/long_term.json under "habits" (same file as identity/
preferences, written directly so it isn't subject to the size-based trimming
that applies to conversational memory entries).

If an Obsidian vault is configured (see obsidian_control.py), each log also
gets a checkbox line appended to today's daily note Tasks section, using the
#habit tag so it's distinguishable from regular #todo items but still
trackable via the vault's Tasks plugin.

Actions:
  log_habit    — record that a habit was done today (or on a given date)
  list_habits  — show tracked habits with current streaks
  define_habit — start tracking a new habit (optional, log_habit auto-creates too)
  remove_habit — stop tracking a habit
"""

import json
from datetime import datetime, timedelta


def _slug(name: str) -> str:
    return "_".join(name.strip().lower().split())


def _load() -> dict:
    from memory.memory_manager import load_memory
    data = load_memory().get("habits", {})
    return data if isinstance(data, dict) else {}


def _save(habits: dict) -> None:
    from memory.memory_manager import load_memory, MEMORY_PATH, _lock
    memory = load_memory()
    memory["habits"] = habits
    with _lock:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _streak(dates: list[str]) -> int:
    """Consecutive-day streak counting backward from today (or yesterday if
    today isn't logged yet, so a streak isn't lost just because you haven't
    logged today's instance yet)."""
    if not dates:
        return 0
    date_set = set(dates)
    today = datetime.now().date()

    if today.strftime("%Y-%m-%d") in date_set:
        cursor = today
    elif (today - timedelta(days=1)).strftime("%Y-%m-%d") in date_set:
        cursor = today - timedelta(days=1)
    else:
        return 0

    streak = 0
    while cursor.strftime("%Y-%m-%d") in date_set:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def _log_to_obsidian(habit_name: str) -> None:
    """Best-effort — silently skipped if no vault is configured."""
    try:
        from actions.obsidian_control import _vault_path, _append_task_line
        vault = _vault_path()
        if not vault:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        _append_task_line(vault, f"- [x] #habit {habit_name} ✅ {today}")
    except Exception as e:
        print(f"[HabitTracker] ⚠️ Obsidian log skipped: {e}")


def _log_habit(habit_name: str, date_str: str = "") -> str:
    if not habit_name.strip():
        return "Please specify which habit to log."

    habits = _load()
    slug = _slug(habit_name)
    date_str = date_str.strip() or datetime.now().strftime("%Y-%m-%d")
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return "Couldn't parse that date — use YYYY-MM-DD."

    if slug not in habits:
        habits[slug] = {
            "name":    habit_name.strip(),
            "created": datetime.now().strftime("%Y-%m-%d"),
            "log":     [],
        }

    if date_str in habits[slug]["log"]:
        return f"Already logged '{habits[slug]['name']}' for {date_str}."

    habits[slug]["log"].append(date_str)
    habits[slug]["log"].sort()
    _save(habits)

    streak = _streak(habits[slug]["log"])
    if date_str == datetime.now().strftime("%Y-%m-%d"):
        _log_to_obsidian(habits[slug]["name"])

    streak_note = f" — {streak} day streak!" if streak > 1 else ""
    return f"Logged '{habits[slug]['name']}' for {date_str}.{streak_note}"


def _list_habits() -> str:
    habits = _load()
    if not habits:
        return "No habits are being tracked yet."

    lines = ["Tracked habits:\n"]
    for slug, h in habits.items():
        streak = _streak(h.get("log", []))
        last = h["log"][-1] if h.get("log") else "never"
        total = len(h.get("log", []))
        lines.append(f"  • {h['name']}: {streak}-day streak, {total} total logs, last: {last}")
    return "\n".join(lines)


def get_top_streaks(limit: int = 3) -> list[tuple[str, int]]:
    """
    Cheap, local-only (just a small JSON file read) — safe to call directly
    from the UI thread on a timer. Returns [(habit_name, streak), ...] sorted
    by streak descending.
    """
    habits = _load()
    rows = [(h["name"], _streak(h.get("log", []))) for h in habits.values()]
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:limit]


def _define_habit(habit_name: str) -> str:
    if not habit_name.strip():
        return "Please specify a habit name."
    habits = _load()
    slug = _slug(habit_name)
    if slug in habits:
        return f"Already tracking '{habits[slug]['name']}'."
    habits[slug] = {
        "name":    habit_name.strip(),
        "created": datetime.now().strftime("%Y-%m-%d"),
        "log":     [],
    }
    _save(habits)
    return f"Now tracking: {habit_name.strip()}"


def _remove_habit(habit_name: str) -> str:
    habits = _load()
    slug = _slug(habit_name)
    if slug not in habits:
        return f"Not tracking a habit called '{habit_name}'."
    name = habits[slug]["name"]
    del habits[slug]
    _save(habits)
    return f"Stopped tracking: {name}"


def habit_tracker(
    parameters:     dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    action = params.get("action", "").lower().strip()
    result = "Unknown habit action."

    try:
        if action == "log_habit":
            result = _log_habit(params.get("habit", ""), params.get("date", ""))
        elif action == "list_habits":
            result = _list_habits()
        elif action == "define_habit":
            result = _define_habit(params.get("habit", ""))
        elif action == "remove_habit":
            result = _remove_habit(params.get("habit", ""))
        else:
            result = f"Unknown habit action: '{action}'"
    except Exception as e:
        result = f"Habit error ({action}): {e}"

    if player:
        player.write_log(f"[Habits] {result[:70]}")
    return result
