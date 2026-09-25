"""
meeting_prep.py — JARVIS Meeting Prep

Ties Calendar + Gmail + Obsidian together: finds the target meeting on the
calendar, searches the inbox for related context, and creates a pre-filled
Meeting Notes note in Obsidian (matching the vault's real template via
obsidian_control.py) — then briefs the user with a short spoken summary.

Each dependency degrades gracefully: if Gmail isn't configured, related-email
search is just skipped; if Obsidian isn't configured, the note-creation step
is skipped and only the spoken briefing happens.

Actions:
  prep_meeting — find a meeting (by title, or the next one today) and prep it
"""

from datetime import datetime, timedelta

from actions.calendar_control import find_events_structured
from actions.obsidian_control import _vault_path as _obsidian_vault_path, _meeting_note


def _find_target_event(title: str) -> dict | None:
    now = datetime.now()
    # Search from now through the end of today, plus a bit into tomorrow
    # morning, so "prep my next meeting" works even right before midnight.
    window_end = now.replace(hour=23, minute=59) + timedelta(hours=6)
    events = find_events_structured(now, window_end, title=title, max_results=10)
    return events[0] if events else None


def _related_emails(query: str, max_results: int = 3) -> str:
    """Best-effort — returns '' if Gmail isn't configured or search fails."""
    try:
        from actions.email_control import _get_service, _list_messages
        service = _get_service()
        return _list_messages(service, query, max_results)
    except Exception as e:
        print(f"[MeetingPrep] ⚠️ Email search skipped: {e}")
        return ""


def _prep_meeting(title: str = "", create_note: bool = True) -> str:
    event = _find_target_event(title)
    if not event:
        if title:
            return f"Couldn't find an upcoming meeting matching '{title}' today."
        return "No upcoming meetings found for the rest of today."

    when = event["start"].strftime("%I:%M %p")
    minutes_away = int((event["start"] - datetime.now()).total_seconds() // 60)
    time_phrase = (
        f"in {minutes_away} minutes" if 0 <= minutes_away < 180
        else f"at {when}"
    )

    lines = [f"Next up: '{event['title']}' {time_phrase}."]
    if event.get("location"):
        lines.append(f"Location: {event['location']}.")

    search_term = title or event["title"]
    email_summary = _related_emails(search_term)
    if email_summary and not email_summary.startswith("No messages"):
        # Keep the spoken part short — just flag that context was found.
        lines.append("I found related email context — check the meeting note for details.")

    note_status = ""
    if create_note:
        vault = _obsidian_vault_path()
        if vault:
            agenda_items = []
            if event.get("notes"):
                agenda_items = [ln.strip("-• ").strip() for ln in event["notes"].split("\n") if ln.strip()][:6]
            note_result = _meeting_note(
                vault,
                event["title"],
                context=event.get("location", ""),
                attendees="",
                agenda_items=agenda_items,
            )
            if email_summary and not email_summary.startswith("No messages"):
                _append_email_context_to_note(vault, event["title"], email_summary)
            note_status = f" {note_result}"
        else:
            note_status = " (Obsidian not configured — skipped note creation.)"

    return " ".join(lines) + note_status


def _append_email_context_to_note(vault, meeting_title: str, email_summary: str) -> None:
    """Best-effort — append the related-email findings into the just-created note's Notes section."""
    try:
        from actions.obsidian_control import _find_note, _insert_into_section
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y-%m-%d")
        note = _find_note(vault, f"{meeting_title} - {today}")
        if not note:
            return
        content = note.read_text(encoding="utf-8", errors="ignore")
        block = "**Related email context (auto-added by Jarvis):**\n" + email_summary[:1500]
        updated = _insert_into_section(content, "## 📓 Notes", block)
        note.write_text(updated, encoding="utf-8")
    except Exception as e:
        print(f"[MeetingPrep] ⚠️ Could not attach email context to note: {e}")


def meeting_prep(
    parameters:     dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """Entry point called from main.py's tool dispatcher (the
    "meeting_prep" tool). A good example of one tool composing others
    directly by import rather than via Gemini — it calls into
    calendar_control.py and obsidian_control.py's functions itself."""
    params = parameters or {}
    action = params.get("action", "prep_meeting").lower().strip()
    result = "Unknown meeting_prep action."

    try:
        if action == "prep_meeting":
            result = _prep_meeting(
                params.get("title", ""),
                bool(params.get("create_note", True)),
            )
        else:
            result = f"Unknown meeting_prep action: '{action}'"
    except Exception as e:
        result = f"Meeting prep error: {e}"

    if player:
        player.write_log(f"[MeetingPrep] {result[:70]}")
    return result
