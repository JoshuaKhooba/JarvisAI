"""
obsidian_control.py — JARVIS Obsidian Vault Integration

Reads, searches, creates, and appends to notes in a local Obsidian vault.
Pure filesystem access — no Obsidian API/plugin required, so it works whether
or not the Obsidian app is currently open.

Vault path is read from config/api_keys.json → "obsidian_vault_path".

This module is built around a specific vault structure (PARA + Zettelkasten
hybrid, matching IdeaVerse's own conventions):
  06 - Daily/YYYY-MM-DD.md          — one daily note per day
  03 - Resources/                    — literature notes (articles/books saved for later)
  02 - Areas/Meetings/ (configurable)— meeting notes not tied to a course
  99 - Meta/00 - Templates/          — the actual templates this module mirrors

Rather than trying to generically execute Templater/QuickAdd JS placeholders,
the Daily/Meeting/Literature note shapes are reproduced directly in Python
from the vault's real templates, with dynamic fields (date, weekday, title,
etc.) filled in — so generated notes match hand-created ones exactly.

Actions:
  search         — find notes whose filename or content matches a query
  read           — return the full content of a note
  list           — list note titles, optionally within a subfolder
  create         — create a freeform new note (won't overwrite an existing one)
  append         — append text to an existing note (creates it if missing)
  journal_append — insert text into today's daily note, in the Journal section
                   (creates today's daily note from the Daily template if missing)
  read_later     — save an article/link as a Literature Note in 03 - Resources
  meeting_note   — create a Meeting Notes note pre-filled with agenda/attendees
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = _base_dir() / "config" / "api_keys.json"

DAILY_FOLDER     = "06 - Daily"
RESOURCES_FOLDER = "03 - Resources"


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _vault_path() -> Path | None:
    cfg = _load_config()
    raw = (cfg.get("obsidian_vault_path") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.exists() and p.is_dir() else None


def _meetings_folder() -> str:
    cfg = _load_config()
    return (cfg.get("obsidian_meeting_notes_folder") or "02 - Areas/Meetings").strip()


def _sanitize_title(title: str) -> str:
    title = title.strip()
    if title.lower().endswith(".md"):
        title = title[:-3]
    for ch in '\\/:*?"<>|':
        title = title.replace(ch, "-")
    return title.strip() or "Untitled"


def _ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _all_notes(vault: Path):
    return [p for p in vault.rglob("*.md") if ".obsidian" not in p.parts and ".trash" not in p.parts]


def _find_note(vault: Path, title_or_path: str) -> Path | None:
    """Resolve a loose title/path reference to an actual note file."""
    title_or_path = title_or_path.strip()
    if not title_or_path:
        return None

    candidate = vault / title_or_path
    if candidate.exists():
        return candidate
    if not title_or_path.lower().endswith(".md"):
        candidate = vault / f"{title_or_path}.md"
        if candidate.exists():
            return candidate

    target = _sanitize_title(title_or_path).lower()
    for note in _all_notes(vault):
        if note.stem.lower() == target:
            return note
    for note in _all_notes(vault):
        if target in note.stem.lower():
            return note
    return None


# ── Search / read / list (generic) ──────────────────────────────────────────

def _search(vault: Path, query: str, max_results: int = 10) -> str:
    query_l = query.lower().strip()
    if not query_l:
        return "Please provide something to search for."

    matches = []
    for note in _all_notes(vault):
        name_hit = query_l in note.stem.lower()
        try:
            text = note.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        content_hit_idx = text.lower().find(query_l)
        if name_hit or content_hit_idx != -1:
            snippet = ""
            if content_hit_idx != -1:
                start = max(0, content_hit_idx - 60)
                end   = min(len(text), content_hit_idx + 100)
                snippet = text[start:end].replace("\n", " ").strip()
            rel = note.relative_to(vault)
            matches.append((str(rel), snippet))
        if len(matches) >= max_results:
            break

    if not matches:
        return f"No notes found matching '{query}'."

    lines = [f"Found {len(matches)} note(s) matching '{query}':\n"]
    for i, (rel, snippet) in enumerate(matches, 1):
        lines.append(f"{i}. {rel}")
        if snippet:
            lines.append(f"   …{snippet}…")
    return "\n".join(lines)


def _read(vault: Path, title: str, max_chars: int = 4000) -> str:
    note = _find_note(vault, title)
    if not note:
        return f"Could not find a note called '{title}'."
    try:
        text = note.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return f"Could not read note: {e}"
    rel = note.relative_to(vault)
    truncated = text[:max_chars]
    if len(text) > max_chars:
        truncated += "\n\n[...truncated...]"
    return f"— {rel} —\n\n{truncated}"


def _list(vault: Path, folder: str = "", max_results: int = 40) -> str:
    base = (vault / folder) if folder else vault
    if not base.exists():
        return f"Folder '{folder}' not found in vault."
    notes = [p for p in _all_notes(vault) if str(p).startswith(str(base))]
    if not notes:
        return f"No notes found in '{folder or 'vault root'}'."
    notes.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    lines = [f"Notes in '{folder or 'vault'}' ({len(notes)} total, showing {min(len(notes), max_results)}):\n"]
    for note in notes[:max_results]:
        lines.append(f"  • {note.relative_to(vault)}")
    return "\n".join(lines)


def _create(vault: Path, title: str, content: str = "", folder: str = "") -> str:
    safe_title = _sanitize_title(title)
    target_dir = (vault / folder) if folder else vault
    target_dir.mkdir(parents=True, exist_ok=True)
    note_path = target_dir / f"{safe_title}.md"

    if note_path.exists():
        return f"A note called '{safe_title}' already exists. Use append to add to it instead."

    body = content.strip()
    if not body:
        body = f"# {safe_title}\n\n"
    note_path.write_text(body, encoding="utf-8")
    return f"Created note: {note_path.relative_to(vault)}"


def _append(vault: Path, title: str, text: str, folder: str = "") -> str:
    note = _find_note(vault, title)
    if not note:
        return _create(vault, title, content=text, folder=folder)
    try:
        with note.open("a", encoding="utf-8") as f:
            f.write(f"\n{text.strip()}\n")
    except Exception as e:
        return f"Could not append to note: {e}"
    return f"Appended to: {note.relative_to(vault)}"


# ── Daily note (matches "(TEMPLATE) Daily.md" exactly) ──────────────────────

def _render_daily_template(dt: datetime) -> str:
    weekday_lower = dt.strftime("%A").lower()
    header_date   = f'{dt.strftime("%A, %B")} {_ordinal(dt.day)}, {dt.strftime("%Y")}'
    yesterday     = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow      = (dt + timedelta(days=1)).strftime("%Y-%m-%d")

    return f"""---
date: {dt.strftime("%Y-%m-%dT%H:%M")}
tags:
  - Daily
cssclasses:
  - daily
  - {weekday_lower}
aliases:
---

# DAILY NOTE
## {header_date}

> [!summary] Day Overview
> _One sentence. What kind of day is this — academic, work, personal, mixed?_

---

### 🌅 Morning Intention

> [!tip] Today's Focus
> _What is the **one** most important thing to get done today?_

---

### ✅ Tasks

```js quickadd
return await this.app.plugins.plugins['obsidian-tasks-plugin'].apiV1.createTaskLineModal();
```

- [ ] #todo
- [ ] #todo
- [ ] #todo

---

### 🎓 Academic

_Active courses today? Link them._

| Course | Activity | Notes |
|---|---|---|
|  |  |  |

---

### 💼 Work & Career

_Job search, interviews, professional development? Log it here._

---

### 📓 Journal

_Write freely. What happened today? What's on your mind?_

...

---

### 💡 Ideas & Captures

> [!idea] New Idea
> _A thought worth keeping. Link it if it belongs somewhere in the vault._

---

### 🌙 Evening Reflection

> [!abstract] Reflection
> - **One win today:**
> - **One thing I'd do differently:**
> - **Gratitude:**

---

### 🔗 Related Notes

- ← Yesterday: [[{yesterday}]]
- → Tomorrow: [[{tomorrow}]]

---

_Tags: #Daily_
"""


def _get_or_create_daily_note(vault: Path, dt: datetime | None = None) -> Path:
    dt = dt or datetime.now()
    daily_dir = vault / DAILY_FOLDER
    daily_dir.mkdir(parents=True, exist_ok=True)
    note_path = daily_dir / f"{dt.strftime('%Y-%m-%d')}.md"
    if not note_path.exists():
        note_path.write_text(_render_daily_template(dt), encoding="utf-8")
    return note_path


def _insert_into_section(text: str, heading: str, new_text: str) -> str:
    """
    Insert `new_text` as a new paragraph at the end of the section that starts
    with `heading` (a line like "### 📓 Journal") and ends at the next '---'
    divider — preserving whatever is already written in that section rather
    than overwriting it.
    """
    lines = text.split("\n")
    start = None
    for i, line in enumerate(lines):
        if line.strip() == heading:
            start = i
            break
    if start is None:
        # Heading not found — fall back to appending at end of file
        return text.rstrip("\n") + f"\n\n{new_text.strip()}\n"

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break

    insertion = [f"", new_text.strip()]
    new_lines = lines[:end] + insertion + lines[end:]
    return "\n".join(new_lines)


def _journal_append(vault: Path, text: str) -> str:
    if not text.strip():
        return "Please provide what to write in the journal."
    note_path = _get_or_create_daily_note(vault)
    content = note_path.read_text(encoding="utf-8", errors="ignore")
    updated = _insert_into_section(content, "### 📓 Journal", text.strip())
    note_path.write_text(updated, encoding="utf-8")
    return f"Added to today's journal ({note_path.relative_to(vault)})."


def _append_task_line(vault: Path, line: str) -> str:
    """Insert a checklist line into today's daily note, in the Tasks section."""
    note_path = _get_or_create_daily_note(vault)
    content = note_path.read_text(encoding="utf-8", errors="ignore")
    updated = _insert_into_section(content, "### ✅ Tasks", line.strip())
    note_path.write_text(updated, encoding="utf-8")
    return note_path


# ── Literature notes / read-it-later (matches "(TEMPLATE) Literature Notes.md") ──

def _render_literature_template(
    title: str, url: str, source_type: str, summary: str, author: str,
) -> str:
    now = datetime.now()
    return f"""---
date: {now.strftime("%Y-%m-%dT%H:%M")}
tags:
  - literature
cssclasses:
aliases:
---

# 📖 {title}

> [!abstract] Source Info
> **Title:** {title}
> **Author(s):** {author}
> **Type:** {source_type}
> **Published:**
> **Read/Watched:** {now.strftime("%Y-%m-%d")}
> **Source URL / Location:** {url}

---

## ⭐ My Rating

`★★★★☆` — _One sentence on why._

---

## 🔑 Core Argument / Main Idea

> [!summary]
> {summary or "_What is the single most important idea in this source? State it in 2–3 sentences as if explaining to a friend._"}

---

## 📌 Key Points

_The most important ideas, arguments, or findings._

1.
2.
3.
4.

---

## 💬 Notable Quotes

> "_Quote here._"
> — Author, p. XX

---

## 🧠 My Thoughts & Reactions

_What do I agree with? Disagree with? What surprised me? What questions does this raise?_

---

## 🔗 Connections to My Vault

_How does this connect to what I already know? Which existing notes does it relate to?_

- Connects to: [[]]
- Challenges: [[]]
- Supports: [[]]
- New note needed: [[]]

---

## 💡 Actionable Takeaways

_What will I actually do differently because of this source?_

- [ ]
- [ ]

---

## Related Notes

- [[]]
- [[]]

---

_Tags: #literature #_
"""


def _read_later(
    vault: Path, title: str, url: str = "", summary: str = "",
    author: str = "", source_type: str = "Article",
) -> str:
    if not title.strip():
        return "Please provide a title for this article/link."
    safe_title = _sanitize_title(title)
    target_dir = vault / RESOURCES_FOLDER
    target_dir.mkdir(parents=True, exist_ok=True)
    note_path = target_dir / f"{safe_title}.md"

    if note_path.exists():
        return f"A literature note called '{safe_title}' already exists in {RESOURCES_FOLDER}."

    note_path.write_text(
        _render_literature_template(safe_title, url, source_type, summary, author),
        encoding="utf-8",
    )
    return f"Saved for later: {note_path.relative_to(vault)}"


# ── Meeting notes (matches "(TEMPLATE) Meeting Notes.md") ───────────────────

def _render_meeting_template(
    meeting_title: str, context: str, attendees: str, agenda_items: list[str],
) -> str:
    now = datetime.now()
    agenda_lines = "\n".join(f"{i+1}. {a}" for i, a in enumerate(agenda_items)) if agenda_items else "1. \n2. \n3. "
    return f"""---
date: {now.strftime("%Y-%m-%dT%H:%M")}
tags:
  - meeting
cssclasses:
aliases:
---

# 📋 {context or "Meeting"} — {meeting_title}

**Date:** {now.strftime("%B")} {_ordinal(now.day)}, {now.strftime("%Y")}
**Course / Context:** {context}
**Attendees / Participants:** {attendees}

---

## 🎯 Agenda / Topics

_What was this meeting or class session about?_

{agenda_lines}

---

## 📓 Notes

_Capture key points, explanations, and discussions._

---

## 💡 Key Insights

> [!tip] Most Important Takeaways
>
> -
> -
> -

---

## ❓ Questions Raised

_Questions that came up that need follow-up._

- [ ]
- [ ]

---

## ✅ Action Items

- [ ]
- [ ]

---

## 🔗 Related Notes

- [[]]
- [[]]

---

_Tags: #meeting #_
"""


def _meeting_note(
    vault: Path, meeting_title: str, context: str = "",
    attendees: str = "", agenda_items: list[str] | None = None,
    folder: str = "",
) -> str:
    if not meeting_title.strip():
        return "Please provide a meeting title."
    safe_title  = _sanitize_title(meeting_title)
    today       = datetime.now().strftime("%Y-%m-%d")
    dest_folder = folder.strip() or _meetings_folder()
    target_dir  = vault / dest_folder
    target_dir.mkdir(parents=True, exist_ok=True)
    note_path = target_dir / f"{safe_title} - {today}.md"

    if note_path.exists():
        return f"A meeting note for '{safe_title}' today already exists."

    note_path.write_text(
        _render_meeting_template(safe_title, context, attendees, agenda_items or []),
        encoding="utf-8",
    )
    return f"Meeting note created: {note_path.relative_to(vault)}"


# ── Public entry point ───────────────────────────────────────────────────────

def obsidian_control(
    parameters:     dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """Entry point called from main.py's tool dispatcher (the
    "obsidian_control" tool). Dispatches on parameters["action"] to the
    matching _* helper above — see the module docstring for the full list
    of actions and what each one does."""
    params = parameters or {}
    action = params.get("action", "").lower().strip()

    vault = _vault_path()
    if not vault:
        return (
            "Obsidian vault isn't configured yet. Set 'obsidian_vault_path' in "
            "config/api_keys.json to the absolute path of your vault folder."
        )

    result = "Unknown Obsidian action."

    try:
        if action == "search":
            result = _search(vault, params.get("query", ""), int(params.get("max_results", 10)))
        elif action == "read":
            result = _read(vault, params.get("title", ""))
        elif action == "list":
            result = _list(vault, params.get("folder", ""))
        elif action == "create":
            result = _create(vault, params.get("title", ""), params.get("content", ""), params.get("folder", ""))
        elif action == "append":
            result = _append(vault, params.get("title", ""), params.get("text", ""), params.get("folder", ""))
        elif action in ("journal_append", "daily_append"):
            result = _journal_append(vault, params.get("text", ""))
        elif action == "read_later":
            result = _read_later(
                vault,
                params.get("title", ""),
                params.get("url", ""),
                params.get("summary", ""),
                params.get("author", ""),
                params.get("source_type", "Article"),
            )
        elif action == "meeting_note":
            result = _meeting_note(
                vault,
                params.get("title", ""),
                params.get("context", ""),
                params.get("attendees", ""),
                params.get("agenda_items", []),
                params.get("folder", ""),
            )
        else:
            result = f"Unknown Obsidian action: '{action}'"
    except Exception as e:
        result = f"Obsidian error ({action}): {e}"

    if player:
        player.write_log(f"[Obsidian] {result[:70]}")
    return result
