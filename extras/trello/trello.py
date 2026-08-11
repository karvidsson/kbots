"""Trello tools — boards, lists, cards."""

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.core.base import ToolContext, resolve_config_file
from src.core.tools import tool

logger = logging.getLogger(__name__)

TRELLO_API = "https://api.trello.com/1"

# Per-agent board restrictions — loaded from agents.yaml at startup
_agent_allowed_boards: dict[str, list[str]] = {}


def load_board_restrictions() -> None:
    """Load per-agent Trello board restrictions from agents.yaml."""
    import yaml
    agents_file = resolve_config_file("agents.yaml")
    if not agents_file.exists():
        return
    with open(agents_file) as f:
        cfg = yaml.safe_load(f) or {}
    for agent_id, agent_cfg in cfg.get("agents", {}).items():
        boards = agent_cfg.get("trello_allowed_boards")
        if boards:
            _agent_allowed_boards[agent_id] = boards


def _board_allowed(ctx: ToolContext, board_id: str) -> bool:
    """Check if agent is allowed to access this board."""
    if not _agent_allowed_boards:
        load_board_restrictions()
    allowed = _agent_allowed_boards.get(ctx.agent_id)
    if allowed is None:
        return True  # No restriction
    return board_id in allowed


def _get_creds(vault) -> tuple[str, str] | None:
    raw = vault.get("secrets/trello-credentials.json") if vault else None
    if not raw:
        return None
    creds = json.loads(raw)
    return creds.get("key", ""), creds.get("token", "")


async def _trello_api(ctx: ToolContext, endpoint: str, method: str = "GET",
                      params: dict | None = None, data: dict | None = None) -> dict | list:
    creds = _get_creds(ctx.vault)
    if not creds:
        return {"error": "Trello credentials not found in vault"}

    key, token = creds
    base_params = {"key": key, "token": token}
    if params:
        base_params.update(params)

    url = f"{TRELLO_API}{endpoint}?{urlencode(base_params)}"
    body = json.dumps(data).encode() if data else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        return {"error": f"HTTP {e.code}: {error_body[:200]}"}
    except URLError as e:
        return {"error": str(e)}


@tool(name="trello_boards", description="List your Trello boards", category="trello")
async def trello_boards(ctx: ToolContext) -> str:
    """List all Trello boards the token has access to."""
    result = await _trello_api(ctx, "/members/me/boards", params={"fields": "name,url,dateLastActivity,shortLink"})

    if isinstance(result, dict) and "error" in result:
        return f"Trello error: {result['error']}"

    if not result:
        return "No Trello boards found."

    # Apply per-agent board filtering
    result = [b for b in result if _board_allowed(ctx, b.get("shortLink", b.get("id", "")))]

    if not result:
        return "No Trello boards available for this agent."

    lines = [f"**Trello Boards ({len(result)}):**"]
    for b in result:
        lines.append(f"  - {b['name']} — <{b.get('url', '')}> (last activity: {b.get('dateLastActivity', '?')[:10]})")
    return "\n".join(lines)


@tool(name="trello_lists", description="List all lists on a Trello board", category="trello")
async def trello_lists(ctx: ToolContext, board_id: str) -> str:
    """Get all lists on a board.

    Args:
        board_id: Trello board ID (from trello_boards)
    """
    if not _board_allowed(ctx, board_id):
        return "Access denied: this board is not available to your agent."
    result = await _trello_api(ctx, f"/boards/{board_id}/lists", params={"fields": "name,pos"})

    if isinstance(result, dict) and "error" in result:
        return f"Trello error: {result['error']}"

    lines = [f"**Lists ({len(result)}):**"]
    for lst in result:
        lines.append(f"  - {lst['name']} (ID: {lst['id']})")
    return "\n".join(lines)


@tool(name="trello_cards", description="List cards on a Trello board or list", category="trello")
async def trello_cards(ctx: ToolContext, board_id: str, list_id: str = "") -> str:
    """Get cards from a board or specific list.

    Args:
        board_id: Trello board ID
        list_id: Optional list ID to filter cards (if empty, returns all cards on board)
    """
    if not _board_allowed(ctx, board_id):
        return "Access denied: this board is not available to your agent."
    if list_id:
        endpoint = f"/lists/{list_id}/cards"
    else:
        endpoint = f"/boards/{board_id}/cards"

    result = await _trello_api(ctx, endpoint, params={
        "fields": "id,name,desc,due,labels,url,shortLink,idList,dateLastActivity",
    })

    if isinstance(result, dict) and "error" in result:
        return f"Trello error: {result['error']}"

    if not result:
        return "No cards found."

    lines = [f"**Cards ({len(result)}):**"]
    for c in result:
        due = f" (due: {c['due'][:10]})" if c.get("due") else ""
        labels = ", ".join(lb.get("name", lb.get("color", "?")) for lb in c.get("labels", []))
        label_str = f" [{labels}]" if labels else ""
        activity = c.get("dateLastActivity", "")[:10]
        activity_str = f" (updated: {activity})" if activity else ""
        desc_preview = c.get("desc", "")[:80]
        desc_str = f"\n    {desc_preview}..." if desc_preview else ""
        card_id = c.get("id", "?")
        lines.append(f"  - [{card_id}] {c['name']}{due}{label_str}{activity_str}{desc_str}")
    return "\n".join(lines)


@tool(name="trello_activity", description="Get recent activity/changes on a Trello board", category="trello")
async def trello_activity(ctx: ToolContext, board_id: str, days: int = 3, limit: int = 50) -> str:
    """Get recent board activity — card moves, creates, updates, comments.

    This is the digest tool: shows what changed recently, not the full card list.

    Args:
        board_id: Trello board ID
        days: How many days back to look (default 3)
        limit: Max actions to return (default 50)
    """
    if not _board_allowed(ctx, board_id):
        return "Access denied: this board is not available to your agent."

    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    result = await _trello_api(ctx, f"/boards/{board_id}/actions", params={
        "filter": ("createCard,updateCard,moveCardToBoard,moveCardFromBoard,"
                   "commentCard,addMemberToCard,addAttachmentToCard"),
        "limit": str(limit),
        "since": since,
        "fields": "type,date,data,memberCreator",
        "member_fields": "fullName,username",
        "memberCreator_fields": "fullName,username",
    })

    if isinstance(result, dict) and "error" in result:
        return f"Trello error: {result['error']}"

    if not result:
        return f"No activity in the last {days} days on this board."

    lines = [f"**Board activity (last {days} days, {len(result)} actions):**"]
    for action in result:
        atype = action.get("type", "?")
        date = action.get("date", "")[:16].replace("T", " ")
        who = action.get("memberCreator", {}).get("fullName", "?")
        data = action.get("data", {})
        card_name = data.get("card", {}).get("name", "?")

        if atype == "commentCard":
            text = data.get("text", "")[:100]
            lines.append(f"  - {date} | {who} commented on **{card_name}**: {text}")
        elif atype == "createCard":
            list_name = data.get("list", {}).get("name", "?")
            lines.append(f"  - {date} | {who} created **{card_name}** in {list_name}")
        elif atype == "updateCard":
            if "listBefore" in data:
                lines.append(f"  - {date} | {who} moved **{card_name}**: "
                             f"{data['listBefore']['name']} → {data['listAfter']['name']}")
            elif "old" in data and "due" in data.get("old", {}):
                new_due = data.get("card", {}).get("due", "?")
                lines.append(f"  - {date} | {who} updated due date on **{card_name}** "
                             f"→ {new_due[:10] if new_due else 'removed'}")
            elif "old" in data and "closed" in data.get("old", {}):
                closed = data.get("card", {}).get("closed", False)
                lines.append(f"  - {date} | {who} {'archived' if closed else 'unarchived'} **{card_name}**")
            else:
                lines.append(f"  - {date} | {who} updated **{card_name}**")
        elif atype == "addMemberToCard":
            lines.append(f"  - {date} | {who} added member to **{card_name}**")
        elif atype == "addAttachmentToCard":
            lines.append(f"  - {date} | {who} added attachment to **{card_name}**")
        else:
            lines.append(f"  - {date} | {who} {atype} on **{card_name}**")

    return "\n".join(lines)


@tool(name="trello_create_card", description="Create a new Trello card", category="trello")
async def trello_create_card(ctx: ToolContext, list_id: str, name: str,
                              desc: str = "", due: str = "") -> str:
    """Create a new card on a Trello list. Requires HITL approval.

    Args:
        list_id: ID of the list to add the card to
        name: Card title
        desc: Card description (optional)
        due: Due date in ISO 8601 format (optional)
    """
    params = {"name": name, "idList": list_id}
    if desc:
        params["desc"] = desc
    if due:
        params["due"] = due

    result = await _trello_api(ctx, "/cards", method="POST", params=params)

    if isinstance(result, dict) and "error" in result:
        return f"Failed to create card: {result['error']}"

    return f"Card created: {result.get('name', name)} — <{result.get('url', '')}>"


@tool(name="trello_archive_card", description="Archive (close) a Trello card", category="trello")
async def trello_archive_card(ctx: ToolContext, card_id: str) -> str:
    """Archive a Trello card. Requires HITL approval.

    Args:
        card_id: ID of the card to archive
    """
    result = await _trello_api(ctx, f"/cards/{card_id}", method="PUT",
                               params={"closed": "true"})

    if isinstance(result, dict) and "error" in result:
        return f"Failed to archive card: {result['error']}"

    return f"Card archived: {result.get('name', card_id)}"


@tool(name="trello_delete_card", description="Permanently delete a Trello card", category="trello")
async def trello_delete_card(ctx: ToolContext, card_id: str) -> str:
    """Permanently delete a Trello card. This cannot be undone. Requires HITL approval.

    Args:
        card_id: ID of the card to delete
    """
    result = await _trello_api(ctx, f"/cards/{card_id}", method="DELETE")

    if isinstance(result, dict) and "error" in result:
        return f"Failed to delete card: {result['error']}"

    return "Card deleted permanently."


@tool(name="trello_card_detail", category="trello",
      description=("Get full details of a Trello card — description, comments, "
                   "checklists, due date, labels, members"))
async def trello_card_detail(ctx: ToolContext, card_id: str) -> str:
    """Get complete card info including comments and checklists.

    Args:
        card_id: ID of the card
    """
    result = await _trello_api(ctx, f"/cards/{card_id}", params={
        "fields": "name,desc,due,dueComplete,closed,labels,url",
        "members": "true",
        "member_fields": "fullName",
        "checklists": "all",
        "checklist_fields": "name",
        "checkItems": "all",
        "checkItem_fields": "name,state",
    })

    if isinstance(result, dict) and "error" in result:
        return f"Trello error: {result['error']}"

    lines = [f"**{result.get('name', '?')}**"]

    if result.get("due"):
        done = " (complete)" if result.get("dueComplete") else ""
        lines.append(f"Due: {result['due'][:10]}{done}")

    labels = result.get("labels", [])
    if labels:
        lines.append(f"Labels: {', '.join(lb.get('name', lb.get('color', '?')) for lb in labels)}")

    members = result.get("members", [])
    if members:
        lines.append(f"Members: {', '.join(m.get('fullName', '?') for m in members)}")

    desc = result.get("desc", "").strip()
    if desc:
        lines.append(f"\n**Description:**\n{desc[:1000]}")

    checklists = result.get("checklists", [])
    for cl in checklists:
        items = cl.get("checkItems", [])
        lines.append(f"\n**Checklist: {cl.get('name', '?')}**")
        for item in items:
            check = "x" if item.get("state") == "complete" else " "
            lines.append(f"  [{check}] {item.get('name', '?')}")

    # Fetch comments separately
    comments = await _trello_api(ctx, f"/cards/{card_id}/actions", params={
        "filter": "commentCard",
        "limit": "20",
        "fields": "data,date,memberCreator",
        "memberCreator_fields": "fullName",
    })

    if isinstance(comments, list) and comments:
        lines.append(f"\n**Comments ({len(comments)}):**")
        for c in comments:
            date = c.get("date", "")[:16].replace("T", " ")
            who = c.get("memberCreator", {}).get("fullName", "?")
            text = c.get("data", {}).get("text", "")[:300]
            lines.append(f"  {date} | {who}: {text}")

    lines.append(f"\nURL: <{result.get('url', '?')}>")
    return "\n".join(lines)
