"""Cloudflare API tools — DNS, Workers, zones.

API token from vault: secrets/cloudflare-api-token
All mutations are HITL-gated.
"""

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.core.base import ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

CF_API = "https://api.cloudflare.com/client/v4"


async def _cf_api(ctx: ToolContext, path: str, method: str = "GET",
                  data: dict | None = None) -> dict:
    """Make an authenticated Cloudflare API call."""
    token = (ctx.vault.get("secrets/cloudflare-api-token")
             or ctx.vault.get("CLOUDFLARE_API_TOKEN")) if ctx.vault else None
    if not token:
        return {"error": "Cloudflare not configured. Add to the vault: "
                         "secrets/cloudflare-api-token"}

    url = f"{CF_API}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
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


@tool(name="cloudflare_zones", description="List Cloudflare zones", category="cloudflare")
async def cloudflare_zones(ctx: ToolContext) -> str:
    """List all Cloudflare zones (domains)."""
    result = await _cf_api(ctx, "/zones")
    if "error" in result:
        return f"Cloudflare error: {result['error']}"

    zones = result.get("result", [])
    lines = [f"**Zones ({len(zones)}):**"]
    for z in zones:
        lines.append(f"  - {z['name']} (id: {z['id']}, status: {z['status']})")
    return "\n".join(lines)


@tool(name="cloudflare_dns_list", description="List DNS records for a zone", category="cloudflare")
async def cloudflare_dns_list(ctx: ToolContext, zone_id: str) -> str:
    """List DNS records for a Cloudflare zone."""
    result = await _cf_api(ctx, f"/zones/{zone_id}/dns_records")
    if "error" in result:
        return f"DNS list failed: {result['error']}"

    records = result.get("result", [])
    lines = [f"**DNS Records ({len(records)}):**"]
    for r in records:
        proxied = " (proxied)" if r.get("proxied") else ""
        lines.append(f"  - {r['type']} {r['name']} → {r['content']}{proxied}")
    return "\n".join(lines)


@tool(name="cloudflare_dns_update", description="Update a DNS record", category="cloudflare", hitl=True)
async def cloudflare_dns_update(ctx: ToolContext, zone_id: str, record_id: str,
                                 type: str, name: str, content: str,
                                 proxied: bool = True) -> str:
    """Update a Cloudflare DNS record. HITL-gated."""
    result = await _cf_api(ctx, f"/zones/{zone_id}/dns_records/{record_id}", method="PUT", data={
        "type": type, "name": name, "content": content, "proxied": proxied,
    })
    if "error" in result:
        return f"DNS update failed: {result['error']}"
    return f"Updated DNS: {type} {name} → {content}"
