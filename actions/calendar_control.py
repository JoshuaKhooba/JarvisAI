"""
calendar_control.py — JARVIS macOS Calendar Integration

Controls the native Calendar app via AppleScript. No sign-in or API keys
needed — it works with whatever calendars are already syncing into
Calendar.app (iCloud, Google, Outlook, Exchange, etc.), because it talks to
the app itself rather than any one provider's API.

macOS only. The first time this runs, macOS will prompt for Calendar
automation permission (System Settings → Privacy & Security → Automation).

Actions:
  create_event  — add a new event
  list_events   — list events for today / tomorrow / this week / a date
  delete_event  — remove an event matching a title on a given date
  list_calendars— list available calendar names
"""

import platform
import subprocess
from datetime import datetime, timedelta


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


def _as_date_literal(var: str, dt: datetime) -> str:
    """
    Builds AppleScript statements that set `var` to a specific date/time by
    assigning year/month/day/hours/minutes individually — this avoids the
    locale-dependent string parsing that AppleScript's `date "..."` literal
    relies on, which breaks on non-US date formats.
    """
    return (
        f'set {var} to (current date)\n'
        f'set year of {var} to {dt.year}\n'
        f'set month of {var} to {dt.month}\n'
        f'set day of {var} to {dt.day}\n'
        f'set hours of {var} to {dt.hour}\n'
        f'set minutes of {var} to {dt.minute}\n'
        f'set seconds of {var} to 0\n'
    )


def _escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def _create_event(
    title: str, start_dt: datetime, end_dt: datetime,
    calendar_name: str = "", location: str = "", notes: str = "",
) -> str:
    if not title.strip():
        return "Please provide an event title."

    cal_clause = f'calendar "{_escape(calendar_name)}"' if calendar_name else "calendar 1"

    script = (
        'tell application "Calendar"\n'
        f'{_as_date_literal("startDate", start_dt)}'
        f'{_as_date_literal("endDate", end_dt)}'
        f'  tell {cal_clause}\n'
        f'    set newEvent to make new event with properties '
        f'{{summary:"{_escape(title)}", start date:startDate, end date:endDate'
        + (f', location:"{_escape(location)}"' if location else "")
        + (f', description:"{_escape(notes)}"' if notes else "")
        + '}\n'
        '  end tell\n'
        'end tell\n'
    )

    ok, out = _run_applescript(script)
    if not ok:
        return f"Could not create event: {out}"
    friendly = start_dt.strftime("%B %d at %I:%M %p")
    return f"Event '{title}' created for {friendly}."


def _list_events(start_dt: datetime, end_dt: datetime, calendar_name: str = "") -> str:
    cal_clause = f'calendar "{_escape(calendar_name)}"' if calendar_name else "every calendar"

    script = (
        'tell application "Calendar"\n'
        f'{_as_date_literal("rangeStart", start_dt)}'
        f'{_as_date_literal("rangeEnd", end_dt)}'
        '  set output to {}\n'
        f'  repeat with cal in {cal_clause}\n'
        '    try\n'
        '      set matchingEvents to (every event of cal whose start date ≥ rangeStart and start date ≤ rangeEnd)\n'
        '      repeat with ev in matchingEvents\n'
        '        set evLine to (summary of ev as string) & " | " & (start date of ev as string)\n'
        '        set end of output to evLine\n'
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
        return f"Could not read calendar: {out}"
    if not out.strip():
        return "No events found in that range."

    lines = [ln for ln in out.split("\n") if ln.strip()]
    formatted = [f"  • {ln.split(' | ')[0]} — {ln.split(' | ', 1)[1] if ' | ' in ln else ''}" for ln in lines]
    return f"Found {len(formatted)} event(s):\n" + "\n".join(formatted)


_FIELD_SEP  = ""   # unit separator — won't collide with real event text
_RECORD_SEP = ""   # record separator


def find_events_structured(
    start_dt: datetime, end_dt: datetime, title: str = "",
    calendar_name: str = "", max_results: int = 5,
) -> list[dict]:
    """
    Like _list_events but returns structured data (title, start datetime,
    location, notes) for programmatic use — e.g. by meeting_prep.py. Reads
    the date back via individual numeric components rather than 'as string'
    to avoid locale-dependent date-string parsing.
    """
    cal_clause    = f'calendar "{_escape(calendar_name)}"' if calendar_name else "every calendar"
    title_filter  = f' and summary contains "{_escape(title)}"' if title.strip() else ""

    script = (
        'tell application "Calendar"\n'
        f'{_as_date_literal("rangeStart", start_dt)}'
        f'{_as_date_literal("rangeEnd", end_dt)}'
        '  set output to {}\n'
        f'  repeat with cal in {cal_clause}\n'
        '    try\n'
        '      set matchingEvents to (every event of cal whose start date ≥ rangeStart '
        f'and start date ≤ rangeEnd{title_filter})\n'
        '      repeat with ev in matchingEvents\n'
        '        set evLoc to ""\n'
        '        try\n'
        '          set evLoc to (location of ev) as string\n'
        '        end try\n'
        '        set evNotes to ""\n'
        '        try\n'
        '          set evNotes to (description of ev) as string\n'
        '        end try\n'
        '        set sd to start date of ev\n'
        '        set evLine to (summary of ev as string) & "' + _FIELD_SEP + '" & '
        '(year of sd as string) & "' + _FIELD_SEP + '" & (month of sd as integer as string) & "' + _FIELD_SEP + '" & '
        '(day of sd as string) & "' + _FIELD_SEP + '" & (hours of sd as string) & "' + _FIELD_SEP + '" & '
        '(minutes of sd as string) & "' + _FIELD_SEP + '" & evLoc & "' + _FIELD_SEP + '" & evNotes\n'
        '        set end of output to evLine\n'
        '      end repeat\n'
        '    end try\n'
        '  end repeat\n'
        '  set AppleScript\'s text item delimiters to "' + _RECORD_SEP + '"\n'
        '  set outputText to output as string\n'
        '  set AppleScript\'s text item delimiters to ""\n'
        '  return outputText\n'
        'end tell\n'
    )

    ok, out = _run_applescript(script, timeout=25)
    if not ok or not out.strip():
        return []

    events = []
    for record in out.split(_RECORD_SEP):
        parts = record.split(_FIELD_SEP)
        if len(parts) < 6:
            continue
        try:
            summary, year, month, day, hour, minute = parts[:6]
            location = parts[6] if len(parts) > 6 else ""
            notes    = parts[7] if len(parts) > 7 else ""
            start    = datetime(int(year), int(month), int(day), int(hour), int(minute))
            events.append({
                "title": summary, "start": start,
                "location": location.strip(), "notes": notes.strip(),
            })
        except Exception:
            continue

    events.sort(key=lambda e: e["start"])
    return events[:max_results]


def _delete_event(title: str, start_dt: datetime, end_dt: datetime, calendar_name: str = "") -> str:
    if not title.strip():
        return "Please specify which event to delete."

    cal_clause = f'calendar "{_escape(calendar_name)}"' if calendar_name else "every calendar"

    script = (
        'tell application "Calendar"\n'
        f'{_as_date_literal("rangeStart", start_dt)}'
        f'{_as_date_literal("rangeEnd", end_dt)}'
        '  set deletedCount to 0\n'
        f'  repeat with cal in {cal_clause}\n'
        '    try\n'
        '      set matchingEvents to (every event of cal whose summary contains '
        f'"{_escape(title)}" and start date ≥ rangeStart and start date ≤ rangeEnd)\n'
        '      repeat with ev in matchingEvents\n'
        '        delete ev\n'
        '        set deletedCount to deletedCount + 1\n'
        '      end repeat\n'
        '    end try\n'
        '  end repeat\n'
        '  return deletedCount as string\n'
        'end tell\n'
    )

    ok, out = _run_applescript(script)
    if not ok:
        return f"Could not delete event: {out}"
    count = out.strip() or "0"
    if count == "0":
        return f"No event matching '{title}' found in that range."
    return f"Deleted {count} event(s) matching '{title}'."


def _list_calendars() -> str:
    script = 'tell application "Calendar" to return name of every calendar'
    ok, out = _run_applescript(script)
    if not ok:
        return f"Could not list calendars: {out}"
    names = [n.strip() for n in out.split(",") if n.strip()]
    if not names:
        return "No calendars found."
    return "Calendars:\n" + "\n".join(f"  • {n}" for n in names)


def _resolve_range(params: dict) -> tuple[datetime, datetime]:
    """Turns a natural range keyword or explicit date into a (start, end) window."""
    rng = (params.get("range") or "").lower().strip()
    date_str = (params.get("date") or "").strip()
    now = datetime.now()

    if date_str:
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d")
            return day.replace(hour=0, minute=0), day.replace(hour=23, minute=59)
        except ValueError:
            pass

    if rng == "tomorrow":
        day = now + timedelta(days=1)
        return day.replace(hour=0, minute=0), day.replace(hour=23, minute=59)
    if rng in ("week", "this week"):
        return now.replace(hour=0, minute=0), now + timedelta(days=7)
    if rng in ("month", "this month"):
        return now.replace(hour=0, minute=0), now + timedelta(days=30)

    # default: today
    return now.replace(hour=0, minute=0), now.replace(hour=23, minute=59)


def calendar_control(
    parameters:     dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    if not _is_mac():
        return "Calendar control is only available on macOS."

    params = parameters or {}
    action = params.get("action", "").lower().strip()
    result = "Unknown calendar action."

    try:
        if action == "create_event":
            date_str  = params.get("date", "").strip()
            time_str  = params.get("start_time", "09:00").strip()
            end_time  = params.get("end_time", "").strip()
            title     = params.get("title", "").strip()
            calendar  = params.get("calendar", "").strip()
            location  = params.get("location", "").strip()
            notes     = params.get("notes", "").strip()

            if not date_str:
                return "Please provide a date (YYYY-MM-DD) for the event."
            try:
                start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            except ValueError:
                return "Couldn't parse the date/time. Use YYYY-MM-DD and HH:MM."

            if end_time:
                end_dt = datetime.strptime(f"{date_str} {end_time}", "%Y-%m-%d %H:%M")
            else:
                end_dt = start_dt + timedelta(hours=1)

            result = _create_event(title, start_dt, end_dt, calendar, location, notes)

        elif action == "list_events":
            start_dt, end_dt = _resolve_range(params)
            result = _list_events(start_dt, end_dt, params.get("calendar", "").strip())

        elif action == "delete_event":
            start_dt, end_dt = _resolve_range(params)
            result = _delete_event(params.get("title", ""), start_dt, end_dt, params.get("calendar", "").strip())

        elif action == "list_calendars":
            result = _list_calendars()

        else:
            result = f"Unknown calendar action: '{action}'"

    except Exception as e:
        result = f"Calendar error ({action}): {e}"

    if player:
        player.write_log(f"[Calendar] {result[:70]}")
    return result
