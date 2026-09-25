"""
task_manager.py — JARVIS macOS Reminders Integration

A real, persistent to-do list (as opposed to reminder.py's one-off timed
notifications) via the native Reminders app. No sign-in needed — works with
whatever lists already exist and sync there (iCloud, Exchange, etc.).

macOS only. First use will prompt for Reminders automation permission
(System Settings → Privacy & Security → Automation).

Actions:
  create_task    — add a new reminder (optionally with a due date/time, list, notes)
  list_tasks     — list incomplete reminders, optionally filtered by list/date range
  complete_task  — mark a reminder done by matching its title
  delete_task    — remove a reminder by matching its title
  list_lists     — list available Reminders lists
"""

import platform
import subprocess
from datetime import datetime, timedelta

_FIELD_SEP  = "\x1f"
_RECORD_SEP = "\x1e"


def _is_mac() -> bool:
    return platform.system() == "Darwin"


def _run_applescript(script: str, timeout: int = 20) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return False, (result.stderr or result.stdout).strip()
        return True, result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "AppleScript timed out."
    except Exception as e:
        return False, str(e)


def _escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def _as_date_literal(var: str, dt: datetime) -> str:
    """Set an AppleScript date via numeric properties — locale-independent."""
    return (
        f'set {var} to (current date)\n'
        f'set year of {var} to {dt.year}\n'
        f'set month of {var} to {dt.month}\n'
        f'set day of {var} to {dt.day}\n'
        f'set hours of {var} to {dt.hour}\n'
        f'set minutes of {var} to {dt.minute}\n'
        f'set seconds of {var} to 0\n'
    )


def _create_task(
    title: str, list_name: str = "", due_date: str = "", due_time: str = "",
    notes: str = "", priority: str = "",
) -> str:
    if not title.strip():
        return "Please provide a task title."

    list_clause = f'list "{_escape(list_name)}"' if list_name else "default list"

    due_block = ""
    props_extra = ""
    if due_date:
        try:
            dt = datetime.strptime(f"{due_date} {due_time or '09:00'}", "%Y-%m-%d %H:%M")
            due_block = _as_date_literal("dueDate", dt)
            props_extra += ", due date:dueDate"
        except ValueError:
            return "Couldn't parse the due date/time. Use YYYY-MM-DD and HH:MM."

    priority_map = {"low": 1, "medium": 5, "high": 9}
    pnum = priority_map.get(priority.lower().strip(), None)
    if pnum:
        props_extra += f", priority:{pnum}"

    script = (
        'tell application "Reminders"\n'
        f'{due_block}'
        f'  tell {list_clause}\n'
        f'    make new reminder with properties '
        f'{{name:"{_escape(title)}"'
        + (f', body:"{_escape(notes)}"' if notes else '')
        + props_extra
        + '}\n'
        '  end tell\n'
        'end tell\n'
    )

    ok, out = _run_applescript(script)
    if not ok:
        return f"Could not create task: {out}"
    if due_date:
        friendly = datetime.strptime(f"{due_date} {due_time or '09:00'}", "%Y-%m-%d %H:%M").strftime("%B %d at %I:%M %p")
        return f"Task '{title}' added, due {friendly}."
    return f"Task '{title}' added."


def _list_tasks(list_name: str = "", include_completed: bool = False) -> str:
    list_clause = f'list "{_escape(list_name)}"' if list_name else "every list"
    completed_filter = "" if include_completed else " whose completed is false"

    script = (
        'tell application "Reminders"\n'
        '  set output to {}\n'
        f'  repeat with lst in {list_clause}\n'
        '    try\n'
        f'      set matchingReminders to (every reminder of lst{completed_filter})\n'
        '      repeat with r in matchingReminders\n'
        '        set dueStr to ""\n'
        '        try\n'
        '          set dueStr to " | due: " & (due date of r as string)\n'
        '        end try\n'
        '        set end of output to (name of r as string) & dueStr\n'
        '      end repeat\n'
        '    end try\n'
        '  end repeat\n'
        '  set AppleScript\'s text item delimiters to linefeed\n'
        '  set outputText to output as string\n'
        '  set AppleScript\'s text item delimiters to ""\n'
        '  return outputText\n'
        'end tell\n'
    )

    ok, out = _run_applescript(script, timeout=25)
    if not ok:
        return f"Could not read tasks: {out}"
    if not out.strip():
        return "No tasks found."

    lines = [ln for ln in out.split("\n") if ln.strip()]
    return f"{len(lines)} task(s):\n" + "\n".join(f"  • {ln}" for ln in lines)


def _complete_task(title: str, list_name: str = "") -> str:
    if not title.strip():
        return "Please specify which task to complete."
    list_clause = f'list "{_escape(list_name)}"' if list_name else "every list"

    script = (
        'tell application "Reminders"\n'
        '  set doneCount to 0\n'
        f'  repeat with lst in {list_clause}\n'
        '    try\n'
        f'      set matchingReminders to (every reminder of lst whose name contains "{_escape(title)}" and completed is false)\n'
        '      repeat with r in matchingReminders\n'
        '        set completed of r to true\n'
        '        set doneCount to doneCount + 1\n'
        '      end repeat\n'
        '    end try\n'
        '  end repeat\n'
        '  return doneCount as string\n'
        'end tell\n'
    )

    ok, out = _run_applescript(script)
    if not ok:
        return f"Could not complete task: {out}"
    count = out.strip() or "0"
    if count == "0":
        return f"No incomplete task matching '{title}' found."
    return f"Marked {count} task(s) matching '{title}' as complete."


def _delete_task(title: str, list_name: str = "") -> str:
    if not title.strip():
        return "Please specify which task to delete."
    list_clause = f'list "{_escape(list_name)}"' if list_name else "every list"

    script = (
        'tell application "Reminders"\n'
        '  set deletedCount to 0\n'
        f'  repeat with lst in {list_clause}\n'
        '    try\n'
        f'      set matchingReminders to (every reminder of lst whose name contains "{_escape(title)}")\n'
        '      repeat with r in matchingReminders\n'
        '        delete r\n'
        '        set deletedCount to deletedCount + 1\n'
        '      end repeat\n'
        '    end try\n'
        '  end repeat\n'
        '  return deletedCount as string\n'
        'end tell\n'
    )

    ok, out = _run_applescript(script)
    if not ok:
        return f"Could not delete task: {out}"
    count = out.strip() or "0"
    if count == "0":
        return f"No task matching '{title}' found."
    return f"Deleted {count} task(s) matching '{title}'."


def _list_lists() -> str:
    script = 'tell application "Reminders" to return name of every list'
    ok, out = _run_applescript(script)
    if not ok:
        return f"Could not list Reminders lists: {out}"
    names = [n.strip() for n in out.split(",") if n.strip()]
    if not names:
        return "No Reminders lists found."
    return "Lists:\n" + "\n".join(f"  • {n}" for n in names)


def get_open_tasks_structured(list_name: str = "", max_results: int = 20) -> list[dict]:
    """
    Structured incomplete-reminder fetch for the always-visible Reminders
    panel — like _list_tasks but returns [{"title","due","list"}] for
    programmatic rendering instead of a formatted string. This is a
    subprocess call — run it off the UI thread, never inline in a Qt
    paint/timer callback. Returns [] on any error rather than raising.
    Sorted so reminders with a due date come first (soonest first),
    followed by reminders with no due date.
    """
    if not _is_mac():
        return []
    list_clause = f'list "{_escape(list_name)}"' if list_name else "every list"

    script = (
        'tell application "Reminders"\n'
        '  set output to {}\n'
        f'  repeat with lst in {list_clause}\n'
        '    try\n'
        '      set matchingReminders to (every reminder of lst whose completed is false)\n'
        '      repeat with r in matchingReminders\n'
        '        set y to ""\n'
        '        set mo to ""\n'
        '        set da to ""\n'
        '        set hh to ""\n'
        '        set mi to ""\n'
        '        try\n'
        '          set dd to due date of r\n'
        '          set y to (year of dd as string)\n'
        '          set mo to (month of dd as integer as string)\n'
        '          set da to (day of dd as string)\n'
        '          set hh to (hours of dd as string)\n'
        '          set mi to (minutes of dd as string)\n'
        '        end try\n'
        '        set listName to name of lst as string\n'
        f'        set end of output to (name of r as string) & "{_FIELD_SEP}" & y & "{_FIELD_SEP}" & mo & "{_FIELD_SEP}" & da & "{_FIELD_SEP}" & hh & "{_FIELD_SEP}" & mi & "{_FIELD_SEP}" & listName\n'
        '      end repeat\n'
        '    end try\n'
        '  end repeat\n'
        f'  set AppleScript\'s text item delimiters to "{_RECORD_SEP}"\n'
        '  set outputText to output as string\n'
        '  set AppleScript\'s text item delimiters to ""\n'
        '  return outputText\n'
        'end tell\n'
    )

    ok, out = _run_applescript(script, timeout=20)
    if not ok or not out.strip():
        return []

    tasks = []
    for record in out.split(_RECORD_SEP):
        parts = record.split(_FIELD_SEP)
        if len(parts) < 6:
            continue
        title, y, mo, da, hh, mi = parts[:6]
        list_field = parts[6] if len(parts) > 6 else ""
        title = title.strip()
        if not title:
            continue

        due_dt = None
        due_str = ""
        if y and mo and da:
            try:
                due_dt = datetime(int(y), int(mo), int(da), int(hh or 0), int(mi or 0))
                due_str = due_dt.strftime("%b %d, %I:%M %p")
            except Exception:
                due_dt = None

        tasks.append({
            "title": title, "due": due_str, "due_dt": due_dt,
            "list": list_field.strip(),
        })

    tasks.sort(key=lambda t: (t["due_dt"] is None, t["due_dt"] or datetime.max))
    for t in tasks:
        t.pop("due_dt", None)
    return tasks[:max_results]


def get_open_task_count(list_name: str = "") -> int:
    """
    Fast incomplete-reminder count for the UI panel. Uses AppleScript's
    `count` rather than materializing/formatting every reminder, so it's
    cheap enough to poll periodically — but still a subprocess call, so
    run it off the UI thread (e.g. in a background fetch), never inline
    in a Qt paint/timer callback. Returns 0 on any error (not on macOS,
    Reminders not authorized, etc.) rather than raising.
    """
    if not _is_mac():
        return 0
    list_clause = f'list "{_escape(list_name)}"' if list_name else "every list"
    script = (
        'tell application "Reminders"\n'
        '  set totalCount to 0\n'
        f'  repeat with lst in {list_clause}\n'
        '    try\n'
        '      set totalCount to totalCount + (count of (every reminder of lst whose completed is false))\n'
        '    end try\n'
        '  end repeat\n'
        '  return totalCount as string\n'
        'end tell\n'
    )
    ok, out = _run_applescript(script, timeout=10)
    if not ok:
        return 0
    try:
        return int(out.strip() or "0")
    except ValueError:
        return 0


def task_manager(
    parameters:     dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """Entry point called from main.py's tool dispatcher (the
    "task_manager" tool) — routes on parameters["action"] to the matching
    Reminders operation. See the module docstring for the full action list."""
    if not _is_mac():
        return "Task management is only available on macOS."

    params = parameters or {}
    action = params.get("action", "").lower().strip()
    result = "Unknown task action."

    try:
        if action == "create_task":
            result = _create_task(
                params.get("title", ""),
                params.get("list_name", ""),
                params.get("due_date", ""),
                params.get("due_time", ""),
                params.get("notes", ""),
                params.get("priority", ""),
            )
        elif action == "list_tasks":
            result = _list_tasks(
                params.get("list_name", ""),
                bool(params.get("include_completed", False)),
            )
        elif action == "complete_task":
            result = _complete_task(params.get("title", ""), params.get("list_name", ""))
        elif action == "delete_task":
            result = _delete_task(params.get("title", ""), params.get("list_name", ""))
        elif action == "list_lists":
            result = _list_lists()
        else:
            result = f"Unknown task action: '{action}'"
    except Exception as e:
        result = f"Task error ({action}): {e}"

    if player:
        player.write_log(f"[Tasks] {result[:70]}")
    return result
