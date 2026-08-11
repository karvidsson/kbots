"""Google API tools — Gmail, Drive, Calendar.

Direct API calls using OAuth2 tokens from vault.
No auth proxy needed. HITL-gated for sensitive operations.
"""

import json
import logging
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from src.core.base import KBOTS_TMP, ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

# Lazy-init Google auth, one per account (needs vault).
# KBOTS_GOOGLE_ACCOUNT (set per-agent in .mcp.json, like KBOTS_BOT_ACCOUNT)
# pins an agent to its own Google identity — vault keys GOOGLE_*__<account>.
# Unset means the install's default account.
_google_auth: dict = {}


def _get_auth(ctx: ToolContext):
    import os
    account = os.environ.get("KBOTS_GOOGLE_ACCOUNT", "")
    if account not in _google_auth:
        from src.auth.oauth2 import GoogleAuth
        _google_auth[account] = GoogleAuth(ctx.vault, account=account)
    return _google_auth[account]


async def _google_api(ctx: ToolContext, url: str, method: str = "GET",
                      data: dict | None = None) -> dict:
    """Make an authenticated Google API call."""
    auth = _get_auth(ctx)
    headers = await auth.get_headers()
    headers["Content-Type"] = "application/json"

    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        logger.error(f"Google API error: {e.code} {error_body[:200]}")
        return {"error": f"HTTP {e.code}: {error_body[:200]}"}
    except URLError as e:
        return {"error": str(e)}


# === Gmail ===

@tool(name="gmail_search", description="Search Gmail messages", category="google")
async def gmail_search(ctx: ToolContext, query: str, max_results: int = 5) -> str:
    """Search Gmail for messages matching a query."""
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages?q={quote(query)}&maxResults={max_results}"
    result = await _google_api(ctx, url)

    if "error" in result:
        return f"Gmail search failed: {result['error']}"

    messages = result.get("messages", [])
    if not messages:
        return f"No emails found for: {query}"

    # Fetch message details
    lines = [f"Found {len(messages)} emails for '{query}':"]
    for msg in messages[:max_results]:
        detail = await _google_api(ctx, f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}?format=metadata&metadataHeaders=Subject&metadataHeaders=From&metadataHeaders=Date")
        if "error" in detail:
            continue
        headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
        lines.append(f"  - [{msg['id']}] {headers.get('Subject', '(no subject)')} "
                     f"from {headers.get('From', '?')} ({headers.get('Date', '')})")

    return "\n".join(lines)


@tool(name="gmail_read", description="Read a Gmail message by ID (marks it as read)", category="google")
async def gmail_read(ctx: ToolContext, message_id: str) -> str:
    """Read the full content of a Gmail message and mark it as read."""
    import base64
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}?format=full"
    result = await _google_api(ctx, url)

    if "error" in result:
        return f"Failed to read email: {result['error']}"

    headers = {h["name"]: h["value"] for h in result.get("payload", {}).get("headers", [])}

    # Decode full body from payload parts (recurse into nested multipart)
    payload = result.get("payload", {})

    def _find_body(part, mime="text/plain"):
        """Recursively search MIME parts for a body of the given type."""
        if part.get("mimeType") == mime:
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        for sub in part.get("parts", []):
            found = _find_body(sub, mime)
            if found:
                return found
        return ""

    body = _find_body(payload, "text/plain") or _find_body(payload, "text/html")
    if not body:
        data = payload.get("body", {}).get("data", "")
        if data:
            body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        else:
            body = result.get("snippet", "(no body)")

    # Best effort — a failed modify shouldn't fail the read
    if "UNREAD" in result.get("labelIds", []):
        mark = await _google_api(
            ctx,
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}/modify",
            method="POST",
            data={"removeLabelIds": ["UNREAD"]},
        )
        if "error" in mark:
            logger.warning(f"Could not mark {message_id} as read: {mark['error']}")

    return (
        f"Subject: {headers.get('Subject', '(none)')}\n"
        f"From: {headers.get('From', '?')}\n"
        f"Date: {headers.get('Date', '?')}\n"
        f"To: {headers.get('To', '?')}\n\n"
        f"{body}"
    )


# Gmail's API rejects raw messages over ~35MB; keep headroom for base64 +
# JSON overhead and fail with a clear message instead of an HTTP 413.
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


@tool(name="send_email", category="google", hitl=True,
      description="Send or reply to an email via Gmail, optionally with file attachments")
async def send_email(ctx: ToolContext, to: str, subject: str, body: str,
                     reply_to_id: str = "", attachments: str = "") -> str:
    """Send an email, or reply to an existing thread.

    Args:
        to: Recipient address.
        subject: Subject line.
        body: Plain-text body.
        reply_to_id: Gmail message ID to reply to (threads the reply).
        attachments: Comma-separated local file paths to attach.
    """
    import base64
    import mimetypes
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    thread_id = None

    if reply_to_id:
        orig_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{reply_to_id}?format=metadata&metadataHeaders=Message-ID&metadataHeaders=Subject"
        orig = await _google_api(ctx, orig_url)
        if "error" not in orig:
            thread_id = orig.get("threadId")
            orig_headers = {h["name"]: h["value"] for h in orig.get("payload", {}).get("headers", [])}
            orig_msg_id = orig_headers.get("Message-ID", "")
            if orig_msg_id:
                msg["In-Reply-To"] = orig_msg_id
                msg["References"] = orig_msg_id

    msg.set_content(body)

    paths = [Path(p.strip()).expanduser() for p in attachments.split(",") if p.strip()]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        return f"Not sent — attachment file(s) not found: {', '.join(missing)}"
    total = sum(p.stat().st_size for p in paths)
    if total > _MAX_ATTACHMENT_BYTES:
        return (f"Not sent — attachments total {total // (1024 * 1024)}MB, over the "
                f"{_MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB Gmail limit. Send fewer/"
                "smaller files, or share a link instead (e.g. a Drive upload).")
    for p in paths:
        ctype, _ = mimetypes.guess_type(p.name)
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype,
                           filename=p.name)

    encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    data = {"raw": encoded}
    if thread_id:
        data["threadId"] = thread_id

    result = await _google_api(
        ctx,
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        method="POST",
        data=data,
    )

    if "error" in result:
        return f"Failed to send email: {result['error']}"
    action = "Reply sent" if reply_to_id else "Email sent"
    att_note = f" ({len(paths)} attachment{'s' if len(paths) != 1 else ''})" if paths else ""
    return f"{action} to {to}: {subject}{att_note}"


# === Google Calendar ===

@tool(name="calendar_list", description="List upcoming calendar events", category="google")
async def calendar_list(ctx: ToolContext, max_results: int = 10) -> str:
    """List upcoming events from primary calendar."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    url = (
        f"https://www.googleapis.com/calendar/v3/calendars/primary/events"
        f"?timeMin={quote(now)}&maxResults={max_results}&singleEvents=true&orderBy=startTime"
    )
    result = await _google_api(ctx, url)

    if "error" in result:
        return f"Calendar error: {result['error']}"

    events = result.get("items", [])
    if not events:
        return "No upcoming events."

    lines = ["**Upcoming Events:**"]
    for event in events:
        start = event.get("start", {}).get("dateTime", event.get("start", {}).get("date", ""))
        summary = event.get("summary", "(no title)")
        lines.append(f"  - {start}: {summary}")

    return "\n".join(lines)


@tool(name="calendar_create", description="Create a calendar event", category="google")
async def calendar_create(
    ctx: ToolContext, summary: str, start: str, end: str,
    description: str = "", location: str = ""
) -> str:
    """Create a new calendar event. Start/end in ISO 8601 format."""
    event = {
        "summary": summary,
        "start": {"dateTime": start, "timeZone": "UTC"},
        "end": {"dateTime": end, "timeZone": "UTC"},
    }
    if description:
        event["description"] = description
    if location:
        event["location"] = location

    result = await _google_api(
        ctx,
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        method="POST",
        data=event,
    )

    if "error" in result:
        return f"Failed to create event: {result['error']}"
    return f"Event created: {summary} ({result.get('htmlLink', '')})"


# === Google Meet ===

@tool(name="meet_create", description="Create an instant or scheduled Google Meet with a join link", category="google")
async def meet_create(
    ctx: ToolContext, summary: str = "Quick Meeting",
    attendees: str = "", duration_minutes: int = 60,
    start: str = "", description: str = ""
) -> str:
    """Create a Google Calendar event with an auto-generated Google Meet link.

    Args:
        summary: Meeting title
        attendees: Comma-separated email addresses (optional)
        duration_minutes: Meeting duration in minutes (default 60)
        start: Start time in ISO 8601 (optional — defaults to now for instant meetings)
        description: Meeting description/agenda (optional)
    """
    import re
    import uuid
    from datetime import datetime, timedelta, timezone

    # --- Input validation ---
    if duration_minutes < 1 or duration_minutes > 480:
        return "duration_minutes must be between 1 and 480"

    if len(summary) > 200:
        return "Meeting summary too long (max 200 characters)"

    if attendees:
        email_re = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")
        for email in (e.strip() for e in attendees.split(",") if e.strip()):
            if not email_re.match(email):
                return f"Invalid email address: {email}"

    if description and len(description) > 2000:
        return "Description too long (max 2000 characters)"

    if start:
        start_dt = start
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_dt = (dt + timedelta(minutes=duration_minutes)).isoformat()
        except ValueError:
            return "Invalid start time format. Use ISO 8601 (e.g. 2025-01-15T14:00:00Z)"
    else:
        now = datetime.now(timezone.utc)
        start_dt = now.isoformat()
        end_dt = (now + timedelta(minutes=duration_minutes)).isoformat()

    event = {
        "summary": summary,
        "start": {"dateTime": start_dt, "timeZone": "UTC"},
        "end": {"dateTime": end_dt, "timeZone": "UTC"},
        "conferenceData": {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }

    if description:
        event["description"] = description
    else:
        event["description"] = "Meeting created by kbots. Remember to enable recording / Gemini notes if needed."

    if attendees:
        event["attendees"] = [
            {"email": e.strip()} for e in attendees.split(",") if e.strip()
        ]

    url = "https://www.googleapis.com/calendar/v3/calendars/primary/events?conferenceDataVersion=1"
    result = await _google_api(ctx, url, method="POST", data=event)

    if "error" in result:
        return f"Failed to create meeting: {result['error']}"

    meet_link = ""
    conf = result.get("conferenceData", {})
    conference_id = conf.get("conferenceId", "")
    for ep in conf.get("entryPoints", []):
        if ep.get("entryPointType") == "video":
            meet_link = ep.get("uri", "")
            break

    cal_link = result.get("htmlLink", "")
    event_id = result.get("id", "")
    start_str = result.get("start", {}).get("dateTime", start_dt)

    # Store event metadata for later debrief lookup
    if ctx.memory and event_id:
        try:
            await ctx.memory.store(
                f"meet_event:{event_id}",
                json.dumps({
                    "event_id": event_id,
                    "conference_id": conference_id,
                    "summary": summary,
                    "meet_link": meet_link,
                    "start": start_str,
                    "channel_id": ctx.channel_id,
                }),
                tags=["meet_event", "meeting"],
            )
        except Exception:
            pass  # non-critical

    lines = [
        f"**{summary}**",
        f"Meet link: {meet_link}" if meet_link else "No Meet link generated",
        f"Calendar: {cal_link}" if cal_link else "",
        f"Start: {start_str}",
        f"Duration: {duration_minutes} min",
    ]
    if attendees:
        lines.append(f"Invited: {attendees}")
    if event_id:
        lines.append(f"Event ID: {event_id}")
    lines.append("")
    lines.append("Tip: The host should enable recording and/or Gemini notes once the meeting starts.")

    return "\n".join(line for line in lines if line or line == "")


@tool(name="meet_notes", category="google",
      description="Fetch meeting notes or transcript — searches Drive first, falls back to Meet API")
async def meet_notes(ctx: ToolContext, meeting_title: str = "", event_id: str = "",
                     pick: int = 0, hours_back: int = 24) -> str:
    """Search for meeting notes/transcripts from Google Drive or the Meet API.

    Priority:
    1. If event_id given, look up stored meeting metadata and match by title
    2. Search Drive for Gemini notes/transcripts
    3. Fall back to Google Meet REST API for raw transcripts

    Args:
        meeting_title: Meeting title to search for (optional — uses most recent if empty)
        event_id: Calendar event ID from meet_create (optional — auto-matches)
        pick: If multiple docs found, pick this one (1-indexed). 0 = list all and read newest.
        hours_back: How far back to look in hours (default 24, max 720)
    """
    import re
    from datetime import datetime, timedelta, timezone

    # --- Input validation ---
    hours_back = max(1, min(hours_back, 720))
    pick = max(0, pick)
    if meeting_title and len(meeting_title) > 200:
        return "Meeting title too long (max 200 characters)"
    if event_id and not re.match(r"^[a-zA-Z0-9_-]+$", event_id):
        return "Invalid event_id format"

    # --- Step 0: Resolve title from stored event metadata ---
    if event_id and not meeting_title and ctx.memory:
        try:
            hits = await ctx.memory.search(f"meet_event:{event_id}", limit=1)
            if hits:
                meta = json.loads(hits[0].get("content", "{}"))
                meeting_title = meta.get("summary", "")
        except Exception:
            pass

    if not meeting_title and not event_id and ctx.memory:
        try:
            hits = await ctx.memory.search("meet_event:", limit=1, tags=["meet_event"])
            if hits:
                meta = json.loads(hits[0].get("content", "{}"))
                meeting_title = meta.get("summary", "")
                event_id = meta.get("event_id", "")
        except Exception:
            pass

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()

    # --- Step 1: Search Google Drive for Gemini notes/transcripts ---
    q_parts = []
    if meeting_title:
        safe_title = meeting_title.replace("\\", "\\\\").replace("'", "\\'")
        q_parts.append(f"(name contains '{safe_title}' or name contains 'Meeting notes' or name contains 'transcript')")
    else:
        q_parts.append("(name contains 'Meeting notes' or name contains 'transcript')")

    q_parts.append(f"modifiedTime > '{cutoff}'")
    q_parts.append("trashed = false")

    q = " and ".join(q_parts)
    drive_url = (
        f"https://www.googleapis.com/drive/v3/files"
        f"?q={quote(q)}&pageSize=10"
        f"&fields=files(id,name,mimeType,modifiedTime,webViewLink)"
        f"&orderBy=modifiedTime desc"
    )
    result = await _google_api(ctx, drive_url)
    drive_files = result.get("files", []) if "error" not in result else []

    # --- Step 2: If no Drive results, try Google Meet REST API ---
    meet_transcript = ""
    if not drive_files:
        meet_transcript = await _fetch_meet_transcript(ctx, meeting_title, hours_back)

    # --- Step 3: Build response ---
    if not drive_files and not meet_transcript:
        return (
            f"No meeting notes or transcripts found in the last {hours_back} hours."
            + (f" Searched for: {meeting_title}" if meeting_title else "")
            + "\n\nTip: Make sure Gemini notes were enabled during the meeting, "
            + "and wait 5-10 minutes after the meeting ends for transcripts to appear."
        )

    if not drive_files and meet_transcript:
        return f"**Transcript from Google Meet API:**\n\n{meet_transcript}"

    # Drive docs found — list them
    lines = [f"**Found {len(drive_files)} meeting document(s):**\n"]
    for i, f in enumerate(drive_files, 1):
        lines.append(f"  {i}. {f['name']} — {f.get('webViewLink', f['id'])}")

    # Pick which doc to read
    if pick > 0 and pick <= len(drive_files):
        target = drive_files[pick - 1]
    else:
        target = drive_files[0]

    content_text = await _read_drive_doc(ctx, target)

    if content_text:
        if len(content_text) > 8000:
            content_text = content_text[:8000] + "\n\n... [truncated — full doc linked above]"
        lines.append(f"\n---\n**Content of {target['name']}:**\n\n{content_text}")

    if len(drive_files) > 1 and pick == 0:
        lines.append(f"\n*To read a different document, call meet_notes with pick=N (1-{len(drive_files)})*")

    return "\n".join(lines)


async def _read_drive_doc(ctx: ToolContext, file_meta: dict) -> str:
    """Read the text content of a Google Drive document."""
    import re
    mime = file_meta.get("mimeType", "")
    file_id = file_meta.get("id", "")

    # Validate file_id format (alphanumeric, hyphens, underscores only)
    if not file_id or not re.match(r"^[a-zA-Z0-9_-]+$", file_id):
        return "(Invalid file ID)"

    if mime == "application/vnd.google-apps.document":
        export_url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType=text/plain"
    else:
        export_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"

    auth = _get_auth(ctx)
    headers = await auth.get_headers()
    req = Request(export_url, headers=headers, method="GET")
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except (HTTPError, URLError) as e:
        return f"(Could not read doc content: {e})"


async def _fetch_meet_transcript(ctx: ToolContext, meeting_title: str, hours_back: int) -> str:
    """Fallback: fetch transcript from Google Meet REST API (v2).

    Requires the meetings.space.readonly OAuth scope.
    Returns empty string if unavailable.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (
        f"https://meet.googleapis.com/v2/conferenceRecords"
        f"?filter=end_time>=\"{quote(cutoff)}\""
        f"&pageSize=10"
    )
    result = await _google_api(ctx, url)

    if "error" in result:
        logger.debug(f"Meet API unavailable: {result['error']}")
        return ""

    conferences = result.get("conferenceRecords", [])
    if not conferences:
        return ""

    target_conf = conferences[0]
    conf_name = target_conf.get("name", "")
    if not conf_name:
        return ""

    # Validate resource name format to prevent path traversal
    import re
    if not re.match(r"^conferenceRecords/[a-zA-Z0-9_-]+$", conf_name):
        logger.warning(f"Unexpected conferenceRecord name format: {conf_name}")
        return ""

    transcript_url = f"https://meet.googleapis.com/v2/{conf_name}/transcripts"
    transcript_result = await _google_api(ctx, transcript_url)

    if "error" in transcript_result:
        logger.debug(f"No transcripts via Meet API: {transcript_result['error']}")
        return ""

    transcripts = transcript_result.get("transcripts", [])
    if not transcripts:
        return ""

    transcript_name = transcripts[0].get("name", "")
    if not re.match(r"^conferenceRecords/[a-zA-Z0-9_-]+/transcripts/[a-zA-Z0-9_-]+$", transcript_name):
        logger.warning(f"Unexpected transcript name format: {transcript_name}")
        return ""

    entries_url = f"https://meet.googleapis.com/v2/{transcript_name}/entries?pageSize=100"
    entries_result = await _google_api(ctx, entries_url)

    if "error" in entries_result:
        return ""

    entries = entries_result.get("transcriptEntries", [])
    if not entries:
        return ""

    lines = []
    for entry in entries:
        speaker = entry.get("participant", {}).get("displayName", "Unknown")
        text = entry.get("text", "")
        if text:
            lines.append(f"**{speaker}:** {text}")

    return "\n".join(lines)


# === Google Drive ===

@tool(name="drive_search", description="Search Google Drive files", category="google")
async def drive_search(ctx: ToolContext, query: str, max_results: int = 10, folder_id: str = "") -> str:
    """Search Drive files by name or content.

    Args:
        query: Search term (matches file name or content)
        max_results: Max results to return
        folder_id: Optional — limit search to this folder ID
    """
    safe_query = query.replace("\\", "\\\\").replace("'", "\\'")
    q_parts = [f"(name contains '{safe_query}' or fullText contains '{safe_query}')"]
    if folder_id:
        q_parts.append(f"'{folder_id}' in parents")
    q = quote(" and ".join(q_parts))
    url = f"https://www.googleapis.com/drive/v3/files?q={q}&pageSize={max_results}&fields=files(id,name,mimeType,modifiedTime,webViewLink)"
    result = await _google_api(ctx, url)

    if "error" in result:
        return f"Drive search failed: {result['error']}"

    files = result.get("files", [])
    if not files:
        return f"No files found for: {query}"

    lines = [f"Found {len(files)} files:"]
    for f in files:
        lines.append(f"  - {f['name']} ({f.get('mimeType', '?')}) — {f.get('webViewLink', '')}")

    return "\n".join(lines)


@tool(name="drive_list_folder", description="List files and subfolders inside a Google Drive folder", category="google")
async def drive_list_folder(ctx: ToolContext, folder_id: str, max_results: int = 50) -> str:
    """List contents of a Drive folder by folder ID.

    Args:
        folder_id: The Drive folder ID (from the URL: drive.google.com/drive/folders/XXXXX)
        max_results: Max files to return (default 50)
    """
    q = quote(f"'{folder_id}' in parents and trashed = false")
    url = (
        f"https://www.googleapis.com/drive/v3/files"
        f"?q={q}&pageSize={max_results}"
        f"&fields=files(id,name,mimeType,modifiedTime,webViewLink,size)"
        f"&orderBy=folder,name"
    )
    result = await _google_api(ctx, url)

    if "error" in result:
        return f"Failed to list folder: {result['error']}"

    files = result.get("files", [])
    if not files:
        return f"Folder {folder_id} is empty or not accessible."

    lines = [f"**{len(files)} items in folder:**"]
    for f in files:
        mime = f.get("mimeType", "")
        icon = "\U0001f4c1" if mime == "application/vnd.google-apps.folder" else "\U0001f4c4"
        size = ""
        if f.get("size"):
            size_mb = int(f["size"]) / (1024 * 1024)
            size = f" ({size_mb:.1f}MB)" if size_mb >= 0.1 else f" ({int(f['size'])}B)"
        lines.append(f"  {icon} {f['name']}{size} — `{f['id']}`")

    return "\n".join(lines)


@tool(name="share_drive_file", description="Share a Drive file", category="google")
async def share_drive_file(ctx: ToolContext, file_id: str, email: str,
                           role: str = "reader") -> str:
    """Share a Google Drive file with someone. Requires HITL approval."""
    result = await _google_api(
        ctx,
        f"https://www.googleapis.com/drive/v3/files/{file_id}/permissions",
        method="POST",
        data={"type": "user", "role": role, "emailAddress": email},
    )

    if "error" in result:
        return f"Failed to share: {result['error']}"
    return f"Shared file {file_id} with {email} as {role}"


@tool(name="drive_download", description="Download a file from Google Drive by file ID or URL", category="google")
async def drive_download(ctx: ToolContext, file_id_or_url: str, output_dir: str = "") -> str:
    """Download a Google Drive file to local disk.

    Args:
        file_id_or_url: Drive file ID or share URL (e.g. https://drive.google.com/file/d/xxx/view)
        output_dir: Where to save the file (default: $KBOTS_TMP/docs)
    """
    import re

    import aiohttp

    if not output_dir:
        output_dir = str(KBOTS_TMP / "docs")
        Path(output_dir).mkdir(parents=True, exist_ok=True)

    from src.tools.ingest import validate_file_path
    path_err = validate_file_path(output_dir)
    if path_err:
        return path_err

    # Extract file ID from URL if needed
    file_id = file_id_or_url
    m = re.search(r'/d/([a-zA-Z0-9_-]+)', file_id_or_url)
    if m:
        file_id = m.group(1)
    m = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', file_id_or_url)
    if m:
        file_id = m.group(1)

    # Get file metadata first
    meta = await _google_api(ctx, f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=name,mimeType,size")
    if "error" in meta:
        return f"Failed to get file info: {meta['error']}"

    filename = meta.get("name", f"{file_id}.bin")
    mime = meta.get("mimeType", "")

    # For Google Docs/Sheets/Slides, export instead of download
    export_map = {
        "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
        "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
        "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    }
    if mime in export_map:
        export_mime, ext = export_map[mime]
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType={quote(export_mime)}"
        filename = filename.rsplit(".", 1)[0] + ext if "." in filename else filename + ext
    else:
        url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"

    # Download with auth
    auth = _get_auth(ctx)
    headers = await auth.get_headers()

    out_path = Path(output_dir) / filename
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status != 200:
                return f"Download failed: HTTP {resp.status}"
            out_path.write_bytes(await resp.read())

    size_mb = out_path.stat().st_size / (1024 * 1024)
    return f"Downloaded: {out_path} ({size_mb:.1f}MB, {mime})"


@tool(name="drive_create_doc", category="google",
      description="Create or update a Google Doc from a local file or text content")
async def drive_create_doc(ctx: ToolContext, title: str, content: str = "",
                           file_path: str = "", folder_id: str = "",
                           update_id: str = "") -> str:
    """Create a Google Doc in Drive, or update an existing one.

    Args:
        title: Document title
        content: Markdown/text content (used if file_path is empty)
        file_path: Local .md file to upload (takes priority over content)
        folder_id: Drive folder ID to create in (optional)
        update_id: If set, replaces content of this existing doc instead of creating new
    """

    # Get content from file if path given
    if file_path:
        from src.tools.ingest import validate_file_path
        path_err = validate_file_path(file_path)
        if path_err:
            return path_err
        p = Path(file_path)
        if not p.exists():
            return f"File not found: {file_path}"
        content = p.read_text(encoding="utf-8")

    if not content:
        return "No content provided (set content or file_path)"

    # Strip YAML frontmatter if present
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            content = parts[2].strip()

    auth = _get_auth(ctx)
    headers = await auth.get_headers()

    if update_id:
        # Update existing doc: clear and replace content
        # First, get current doc to find end index
        doc_url = f"https://docs.googleapis.com/v1/documents/{update_id}"
        req = Request(doc_url, headers={**headers, "Content-Type": "application/json"}, method="GET")
        try:
            with urlopen(req, timeout=30) as resp:
                doc = json.loads(resp.read().decode())
        except (HTTPError, URLError) as e:
            return f"Failed to read existing doc: {e}"

        end_index = doc.get("body", {}).get("content", [{}])[-1].get("endIndex", 1)

        # Build batch update: delete all content then insert new
        requests_body = []
        if end_index > 2:
            requests_body.append({
                "deleteContentRange": {
                    "range": {"startIndex": 1, "endIndex": end_index - 1}
                }
            })
        requests_body.append({
            "insertText": {"location": {"index": 1}, "text": content}
        })

        update_url = f"https://docs.googleapis.com/v1/documents/{update_id}:batchUpdate"
        body = json.dumps({"requests": requests_body}).encode()
        req = Request(update_url, data=body, headers={**headers, "Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=30) as resp:
                json.loads(resp.read().decode())
        except (HTTPError, URLError) as e:
            return f"Failed to update doc: {e}"

        return f"Updated doc: https://docs.google.com/document/d/{update_id}/edit"

    else:
        # Create new doc via multipart upload (Drive API)
        import uuid
        boundary = f"boundary_{uuid.uuid4().hex}"

        metadata = {"name": title, "mimeType": "application/vnd.google-apps.document"}
        if folder_id:
            metadata["parents"] = [folder_id]

        # Build multipart body
        body_parts = []
        body_parts.append(f"--{boundary}")
        body_parts.append("Content-Type: application/json; charset=UTF-8")
        body_parts.append("")
        body_parts.append(json.dumps(metadata))
        body_parts.append(f"--{boundary}")
        body_parts.append("Content-Type: text/plain; charset=UTF-8")
        body_parts.append("")
        body_parts.append(content)
        body_parts.append(f"--{boundary}--")

        body_bytes = "\r\n".join(body_parts).encode("utf-8")

        upload_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,webViewLink"
        upload_headers = {**headers, "Content-Type": f"multipart/related; boundary={boundary}"}

        req = Request(upload_url, data=body_bytes, headers=upload_headers, method="POST")
        try:
            with urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
        except HTTPError as e:
            error_body = e.read().decode() if e.fp else str(e)
            return f"Failed to create doc: HTTP {e.code}: {error_body[:300]}"
        except URLError as e:
            return f"Failed to create doc: {e}"

        return f"Created: {result.get('name', title)} — {result.get('webViewLink', result.get('id', '?'))}"


@tool(name="drive_upload", description="Upload a local file to Google Drive", category="google")
async def drive_upload(ctx: ToolContext, file_path: str, folder_id: str = "",
                       name: str = "") -> str:
    """Upload a file from local disk to Google Drive.

    Args:
        file_path: Local path to the file to upload
        folder_id: Drive folder ID to upload into (optional, defaults to root)
        name: Override filename (optional, defaults to local filename)
    """
    import mimetypes
    import uuid

    from src.tools.ingest import validate_file_path
    path_err = validate_file_path(file_path)
    if path_err:
        return path_err
    p = Path(file_path)
    if not p.exists():
        return f"File not found: {file_path}"

    filename = name or p.name
    content_type = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    file_bytes = p.read_bytes()

    auth = _get_auth(ctx)
    headers = await auth.get_headers()

    boundary = f"boundary_{uuid.uuid4().hex}"
    metadata = {"name": filename}
    if folder_id:
        metadata["parents"] = [folder_id]

    # Build multipart body
    parts = []
    parts.append(f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}")
    parts.append(f"--{boundary}\r\nContent-Type: {content_type}\r\n\r\n")

    body = (parts[0].encode("utf-8") + b"\r\n" + parts[1].encode("utf-8")
            + file_bytes + f"\r\n--{boundary}--".encode("utf-8"))

    upload_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,webViewLink"
    req = Request(upload_url, data=body, method="POST",
                  headers={**headers, "Content-Type": f"multipart/related; boundary={boundary}"})

    try:
        with urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
    except HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        return f"Upload failed: HTTP {e.code}: {error_body[:300]}"
    except URLError as e:
        return f"Upload failed: {e}"

    size_mb = len(file_bytes) / (1024 * 1024)
    return (f"Uploaded: {result.get('name', filename)} ({size_mb:.1f}MB) — "
            f"{result.get('webViewLink', result.get('id', '?'))}")


@tool(name="drive_upload_url", category="google",
      description="Download a file from a URL and upload it to Google Drive")
async def drive_upload_url(ctx: ToolContext, url: str, folder_id: str = "",
                           name: str = "") -> str:
    """Download a file from a URL and upload it directly to Google Drive.

    No Bash/shell access needed — handles the download internally.

    Args:
        url: URL to download the file from
        folder_id: Drive folder ID to upload into (optional)
        name: Filename to use in Drive (optional, inferred from URL if not set)
    """
    import mimetypes
    import uuid

    import aiohttp

    from src.tools.ingest import _validate_url
    err, _resolved_ip = _validate_url(url)
    if err:
        return err

    # Download the file
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    return f"Download failed: HTTP {resp.status} from {url}"
                file_bytes = await resp.read()
                content_type = resp.content_type or "application/octet-stream"
                # Infer filename from URL or Content-Disposition
                if not name:
                    cd = resp.headers.get("Content-Disposition", "")
                    if "filename=" in cd:
                        name = cd.split("filename=")[-1].strip('" ')
                    else:
                        name = url.rstrip("/").split("/")[-1].split("?")[0]
                    if not name or name == "":
                        name = "downloaded_file"
    except Exception as e:
        return f"Download failed: {e}"

    # Guess mime type from filename if response didn't provide one
    if content_type == "application/octet-stream":
        guessed = mimetypes.guess_type(name)[0]
        if guessed:
            content_type = guessed

    auth = _get_auth(ctx)
    headers = await auth.get_headers()

    boundary = f"boundary_{uuid.uuid4().hex}"
    metadata = {"name": name}
    if folder_id:
        metadata["parents"] = [folder_id]

    parts = []
    parts.append(f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}")
    parts.append(f"--{boundary}\r\nContent-Type: {content_type}\r\n\r\n")

    body = (parts[0].encode("utf-8") + b"\r\n" + parts[1].encode("utf-8")
            + file_bytes + f"\r\n--{boundary}--".encode("utf-8"))

    upload_url = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,webViewLink"
    req = Request(upload_url, data=body, method="POST",
                  headers={**headers, "Content-Type": f"multipart/related; boundary={boundary}"})

    try:
        with urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
    except HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        return f"Upload failed: HTTP {e.code}: {error_body[:300]}"
    except URLError as e:
        return f"Upload failed: {e}"

    size_mb = len(file_bytes) / (1024 * 1024)
    return (f"Uploaded: {result.get('name', name)} ({size_mb:.1f}MB) — "
            f"{result.get('webViewLink', result.get('id', '?'))}")


@tool(name="drive_delete", description="Delete a file from Google Drive", category="google", hitl=True)
async def drive_delete(ctx: ToolContext, file_id: str) -> str:
    """Delete a Google Drive file by ID. Requires HITL approval."""
    auth = _get_auth(ctx)
    headers = await auth.get_headers()

    url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    req = Request(url, headers=headers, method="DELETE")
    try:
        with urlopen(req, timeout=30) as resp:
            resp.read()
        return f"Deleted file {file_id}"
    except HTTPError as e:
        if e.code == 204:
            return f"Deleted file {file_id}"
        error_body = e.read().decode() if e.fp else str(e)
        return f"Failed to delete: HTTP {e.code}: {error_body[:200]}"
    except URLError as e:
        return f"Failed to delete: {e}"
