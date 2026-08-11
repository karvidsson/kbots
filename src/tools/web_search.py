"""Web search tools — Tavily and SerpAPI.

API keys from vault: TAVILY_API_KEY, SERPAPI_KEY
"""

import json
import logging
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.core.base import ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)


@tool(name="web_search", description="Search the web for information", category="research")
async def web_search(ctx: ToolContext, query: str, max_results: int = 5) -> str:
    """Search the web using Tavily API. Falls back to SerpAPI if Tavily unavailable."""
    # Try Tavily first
    tavily_key = (ctx.vault.get("secrets/tavily-api-key") or ctx.vault.get("TAVILY_API_KEY")) if ctx.vault else None
    if tavily_key:
        return await _tavily_search(tavily_key, query, max_results)

    # Fall back to SerpAPI
    serpapi_key = (ctx.vault.get("secrets/serpapi-key") or ctx.vault.get("SERPAPI_KEY")) if ctx.vault else None
    if serpapi_key:
        return await _serpapi_search(serpapi_key, query, max_results)

    return "No search API key configured. Add TAVILY_API_KEY or SERPAPI_KEY to vault."


async def _tavily_search(api_key: str, query: str, max_results: int) -> str:
    """Search via Tavily API."""
    data = json.dumps({
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "include_answer": True,
    }).encode()

    req = Request(
        "https://api.tavily.com/search",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
    except URLError as e:
        return f"Tavily search failed: {e}"

    lines = []
    answer = result.get("answer")
    if answer:
        lines.append(f"**Answer:** {answer}\n")

    results = result.get("results", [])
    for r in results[:max_results]:
        title = r.get("title", "")
        url = r.get("url", "")
        content = r.get("content", "")[:200]
        lines.append(f"**{title}**\n{url}\n{content}\n")

    return "\n".join(lines) if lines else f"No results for: {query}"


async def _serpapi_search(api_key: str, query: str, max_results: int) -> str:
    """Search via SerpAPI."""
    params = urlencode({
        "q": query,
        "api_key": api_key,
        "num": max_results,
        "engine": "google",
    })

    req = Request(f"https://serpapi.com/search.json?{params}")

    try:
        with urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
    except URLError as e:
        return f"SerpAPI search failed: {e}"

    lines = []
    for r in result.get("organic_results", [])[:max_results]:
        title = r.get("title", "")
        link = r.get("link", "")
        snippet = r.get("snippet", "")
        lines.append(f"**{title}**\n{link}\n{snippet}\n")

    return "\n".join(lines) if lines else f"No results for: {query}"
