"""Notion API tools — search, read, create pages.

API key from vault: NOTION_API_KEY
"""

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.core.base import ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


async def _notion_api(ctx: ToolContext, path: str, method: str = "GET",
                      data: dict | None = None) -> dict:
    """Make an authenticated Notion API call."""
    api_key = (ctx.vault.get("secrets/notion-api-key")
               or ctx.vault.get("NOTION_API_KEY")) if ctx.vault else None
    if not api_key:
        return {"error": "Notion not configured. Add to the vault: "
                         "secrets/notion-api-key"}

    url = f"{NOTION_API}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        return {"error": f"HTTP {e.code}: {error_body[:200]}"}
    except URLError as e:
        return {"error": str(e)}


@tool(name="notion_search", description="Search Notion pages and databases", category="notion")
async def notion_search(ctx: ToolContext, query: str) -> str:
    """Search Notion for pages matching a query."""
    result = await _notion_api(ctx, "/search", method="POST", data={
        "query": query,
        "page_size": 10,
    })

    if "error" in result:
        return f"Notion search failed: {result['error']}"

    results = result.get("results", [])
    if not results:
        return f"No Notion pages found for: {query}"

    lines = [f"Found {len(results)} pages:"]
    for page in results:
        title_parts = page.get("properties", {}).get("title", {}).get("title", [])
        if title_parts:
            title = title_parts[0].get("plain_text", "(untitled)")
        else:
            name_title = page.get("properties", {}).get("Name", {}).get("title", [{}])
            has_name = page.get("properties", {}).get("Name")
            title = name_title[0].get("plain_text", "(untitled)") if has_name else "(untitled)"
        url = page.get("url", "")
        page_type = page.get("object", "page")
        lines.append(f"  - [{page_type}] {title} — {url}")

    return "\n".join(lines)


@tool(name="notion_read", description="Read a Notion page content", category="notion")
async def notion_read(ctx: ToolContext, page_id: str) -> str:
    """Read the content of a Notion page."""
    # Get page metadata
    page = await _notion_api(ctx, f"/pages/{page_id}")
    if "error" in page:
        return f"Failed to read page: {page['error']}"

    # Get page blocks (content)
    blocks = await _notion_api(ctx, f"/blocks/{page_id}/children")
    if "error" in blocks:
        return f"Failed to read content: {blocks['error']}"

    lines = []
    for block in blocks.get("results", []):
        block_type = block.get("type", "")
        text_obj = block.get(block_type, {})
        if "rich_text" in text_obj:
            text = "".join(t.get("plain_text", "") for t in text_obj["rich_text"])
            if text:
                lines.append(text)
        elif "text" in text_obj:
            text = "".join(t.get("plain_text", "") for t in text_obj["text"])
            if text:
                lines.append(text)

    return "\n".join(lines) if lines else "(empty page)"


@tool(name="notion_create", description="Create a Notion page", category="notion")
async def notion_create(ctx: ToolContext, title: str, content: str,
                        parent_page_id: str = "") -> str:
    """Create a new Notion page. Requires HITL approval."""
    if not parent_page_id:
        return "parent_page_id is required — search for a parent page first"

    data = {
        "parent": {"page_id": parent_page_id},
        "properties": {
            "title": {"title": [{"text": {"content": title}}]}
        },
        "children": [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"text": {"content": content}}]
                }
            }
        ]
    }

    result = await _notion_api(ctx, "/pages", method="POST", data=data)
    if "error" in result:
        return f"Failed to create page: {result['error']}"

    return f"Created Notion page: {title} — {result.get('url', '')}"
