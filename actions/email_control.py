"""
email_control.py — JARVIS Gmail Integration

Uses the Gmail API (OAuth) to read, search, and send email.

One-time setup required (see readme / setup instructions):
  1. Create an OAuth Client ID (Desktop app) in Google Cloud Console with the
     Gmail API enabled, download the client secret JSON, and save it as:
       config/gmail_credentials.json
  2. The first time an email action runs, a browser window will open asking
     you to sign in and grant access. After that, a refresh token is cached
     at config/gmail_token.json and no further sign-in is needed.

Actions:
  list_unread  — summary of unread messages
  search       — Gmail search query (same syntax as the Gmail search bar)
  read         — full content of a specific message
  send         — send a new email
  mark_read    — mark a message as read
"""

import base64
import json
import sys
from email.mime.text import MIMEText
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_DIR       = _base_dir() / "config"
CREDENTIALS_PATH = CONFIG_DIR / "gmail_credentials.json"
TOKEN_PATH       = CONFIG_DIR / "gmail_token.json"

_SETUP_MSG = (
    "Gmail isn't connected yet. Download an OAuth Client ID (Desktop app) JSON "
    "from Google Cloud Console with the Gmail API enabled, save it as "
    "config/gmail_credentials.json, then ask me to check email again — "
    "a browser window will open once to sign in."
)


def _get_service():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as e:
        raise RuntimeError(
            "Gmail packages not installed. Run: pip install google-api-python-client "
            "google-auth-httplib2 google-auth-oauthlib"
        ) from e

    creds = None
    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise RuntimeError(_SETUP_MSG)
            flow  = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds)


def _header(headers: list, name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _list_messages(service, query: str, max_results: int = 10) -> str:
    resp = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()
    ids = resp.get("messages", [])
    if not ids:
        return f"No messages found for: {query or '(all)'}"

    lines = [f"{len(ids)} message(s) for '{query or 'inbox'}':\n"]
    for i, m in enumerate(ids, 1):
        msg = service.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = msg.get("payload", {}).get("headers", [])
        frm     = _header(headers, "From")
        subj    = _header(headers, "Subject") or "(no subject)"
        snippet = msg.get("snippet", "")
        lines.append(f"{i}. From: {frm}")
        lines.append(f"   Subject: {subj}")
        if snippet:
            lines.append(f"   {snippet[:120]}")
        lines.append(f"   id: {m['id']}")
        lines.append("")
    return "\n".join(lines).strip()


def _read_message(service, query_or_id: str) -> str:
    msg_id = query_or_id.strip()
    # If it doesn't look like a raw Gmail message id, treat it as a search query
    if not msg_id.isalnum() or len(msg_id) < 10:
        resp = service.users().messages().list(userId="me", q=msg_id, maxResults=1).execute()
        ids = resp.get("messages", [])
        if not ids:
            return f"No message found matching '{query_or_id}'."
        msg_id = ids[0]["id"]

    msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    headers = msg.get("payload", {}).get("headers", [])
    frm  = _header(headers, "From")
    to   = _header(headers, "To")
    subj = _header(headers, "Subject") or "(no subject)"
    date = _header(headers, "Date")

    body_text = _extract_body(msg.get("payload", {}))
    body_text = body_text[:3000] + ("...[truncated]" if len(body_text) > 3000 else "")

    return (
        f"From: {frm}\nTo: {to}\nDate: {date}\nSubject: {subj}\n\n{body_text}"
    )


def _extract_body(payload: dict) -> str:
    def _decode(data: str) -> str:
        try:
            return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="ignore")
        except Exception:
            return ""

    mime = payload.get("mimeType", "")
    if mime == "text/plain" and payload.get("body", {}).get("data"):
        return _decode(payload["body"]["data"])

    for part in payload.get("parts", []) or []:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return _decode(part["body"]["data"])
    for part in payload.get("parts", []) or []:
        text = _extract_body(part)
        if text:
            return text

    if payload.get("body", {}).get("data"):
        return _decode(payload["body"]["data"])
    return ""


def _send_email(service, to: str, subject: str, body: str) -> str:
    if not to.strip():
        return "Please provide a recipient."
    msg = MIMEText(body)
    msg["to"]      = to
    msg["subject"] = subject or "(no subject)"
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return f"Email sent to {to}."


def _mark_read(service, query_or_id: str) -> str:
    msg_id = query_or_id.strip()
    if not msg_id.isalnum() or len(msg_id) < 10:
        resp = service.users().messages().list(userId="me", q=msg_id, maxResults=1).execute()
        ids = resp.get("messages", [])
        if not ids:
            return f"No message found matching '{query_or_id}'."
        msg_id = ids[0]["id"]

    service.users().messages().modify(
        userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()
    return "Marked as read."


def get_unread_digest(max_results: int = 8) -> str:
    """
    Non-interactive digest fetch for the morning briefing. Returns '' (not an
    error string) if Gmail isn't configured yet or the fetch fails for any
    reason — callers should silently skip rather than nagging about it every
    single morning just because OAuth hasn't been set up.
    """
    try:
        service = _get_service()
        result = _list_messages(service, "is:unread", max_results)
        return "" if result.startswith("No messages") else result
    except Exception as e:
        print(f"[Email] Digest fetch skipped: {e}")
        return ""


def get_unread_count() -> int:
    """
    Fast count-only check for the UI panel. Uses Gmail's resultSizeEstimate
    instead of fetching/parsing full messages. Returns 0 (not an error) if
    Gmail isn't configured or the call fails — callers should treat 0 as
    "unknown/none" rather than surfacing an error in the HUD.
    """
    try:
        service = _get_service()
        resp = service.users().messages().list(
            userId="me", q="is:unread", maxResults=1
        ).execute()
        return int(resp.get("resultSizeEstimate", 0))
    except Exception as e:
        print(f"[Email] Unread count skipped: {e}")
        return 0


def email_control(
    parameters:     dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    action = params.get("action", "").lower().strip()
    result = "Unknown email action."

    try:
        service = _get_service()

        if action == "list_unread":
            result = _list_messages(service, "is:unread", int(params.get("max_results", 10)))
        elif action == "search":
            result = _list_messages(service, params.get("query", ""), int(params.get("max_results", 10)))
        elif action == "read":
            result = _read_message(service, params.get("query") or params.get("message_id", ""))
        elif action == "send":
            result = _send_email(
                service,
                params.get("to", ""),
                params.get("subject", ""),
                params.get("body", ""),
            )
        elif action == "mark_read":
            result = _mark_read(service, params.get("query") or params.get("message_id", ""))
        else:
            result = f"Unknown email action: '{action}'"

    except Exception as e:
        result = str(e) if str(e) == _SETUP_MSG else f"Email error ({action}): {e}"

    if player:
        player.write_log(f"[Email] {result[:70]}")
    return result
