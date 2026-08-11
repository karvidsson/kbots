"""News tools — search any topic via Google News, or pull a curated feed.

- news_search: Google News RSS for any query/ticker/company, any region, with an
  optional source filter (Reuters, AP, TT, …). The universal news feed — one
  call, any subject. Covers wire agencies that have no free direct RSS.
- news_feeds: a curated catalog of verified feeds (central banks, markets,
  corporate wires, crypto, world, tech, science, regulatory, energy, dev, ops,
  Nordic). List them, or fetch one by name.

Both parse feeds with the shared helpers in monitoring.py (SSRF-guarded fetch
with a browser User-Agent, redirect re-validation, item extraction).

Tip: Hacker News supports filtered queries via hnrss.org, e.g.
`hnrss.org/newest?q=<term>&points=<n>` — usable directly with rss_read.
"""

import logging
from urllib.parse import quote_plus

# Sibling import: resolves via the overlay tools/ dir on sys.path (_scan_layer).
# Installing the news extra requires monitoring.py alongside it.
from monitoring import _extract_items, _fetch, _validate_url

from src.core.base import ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

# region → Google News (hl, gl, ceid) locale triples
_REGIONS = {
    "US": ("en-US", "US", "US:en"),
    "GB": ("en-GB", "GB", "GB:en"),
    "SE": ("sv", "SE", "SE:sv"),
    "DE": ("de", "DE", "DE:de"),
    "FR": ("fr", "FR", "FR:fr"),
}

# Curated catalog of feeds verified live. news_feeds fetches these by name.
_CATALOG: dict[str, dict[str, str]] = {
    "central_banks": {
        "fed_monetary": "https://www.federalreserve.gov/feeds/press_monetary.xml",
        "ecb": "https://www.ecb.europa.eu/rss/press.html",
        "riksbank": "https://www.riksbank.se/en-gb/rss/press-releases/",
    },
    "markets": {
        "cnbc_economy": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
        "marketwatch": "https://feeds.marketwatch.com/marketwatch/topstories/",
        "nasdaq": "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
        "seeking_alpha": "https://seekingalpha.com/market_currents.xml",
        "investing_economy": "https://www.investing.com/rss/news_25.rss",
    },
    "corporate_wire": {
        "globenewswire": "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies",
        "businesswire": "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVtRWA==",
        "prnewswire_financial": "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
    },
    "wire_agency": {  # wire agencies have no free direct RSS — via Google News source filter
        "reuters": "https://news.google.com/rss/search?q=source:Reuters&hl=en-US&gl=US&ceid=US:en",
        "ap": 'https://news.google.com/rss/search?q=source:"Associated Press"&hl=en-US&gl=US&ceid=US:en',
        "tt": 'https://news.google.com/rss/search?q="TT Nyhetsbyrån"&hl=sv&gl=SE&ceid=SE:sv',
    },
    "crypto": {
        "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "cointelegraph": "https://cointelegraph.com/rss",
    },
    "world": {
        "bbc_world": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "bbc_business": "https://feeds.bbci.co.uk/news/business/rss.xml",
        "guardian_world": "https://www.theguardian.com/world/rss",
        "npr": "https://feeds.npr.org/1001/rss.xml",
        "aljazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    },
    "tech": {
        "arstechnica": "https://feeds.arstechnica.com/arstechnica/index",
        "theverge": "https://www.theverge.com/rss/index.xml",
        "techcrunch": "https://techcrunch.com/feed/",
        "techcrunch_ai": "https://techcrunch.com/category/artificial-intelligence/feed/",
        "techcrunch_startups": "https://techcrunch.com/category/startups/feed/",
        "hackernews": "https://hnrss.org/frontpage",
        "hackernews_best": "https://hnrss.org/best",
    },
    "science": {
        "nature": "https://www.nature.com/nature.rss",
        "sciencedaily": "https://www.sciencedaily.com/rss/all.xml",
    },
    "regulatory": {
        "sec_press": "https://www.sec.gov/news/pressreleases.rss",
        "sec_edgar": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&company=&output=atom",
    },
    "energy": {
        "eia_today": "https://www.eia.gov/rss/todayinenergy.xml",
        "oilprice": "https://oilprice.com/rss/main",
    },
    "dev": {
        "pypi_updates": "https://pypi.org/rss/updates.xml",
        # Tip: any repo's github.com/<owner>/<repo>/releases.atom works via rss_read.
    },
    "ops": {
        "github_status": "https://www.githubstatus.com/history.rss",
    },
    "nordic": {
        "svt": "https://www.svt.se/nyheter/rss.xml",
        "dn": "https://www.dn.se/rss/",
        "di": "https://www.di.se/rss",
        "thelocal_se": "https://www.thelocal.se/feeds/rss.php",
        "sr_ekot": "https://api.sr.se/api/rss/program/83?format=145",
    },
}

_FEED_INDEX = {name: url for group in _CATALOG.values() for name, url in group.items()}


def _format_items(header: str, items: list[dict], limit: int, show_source: bool) -> str:
    out = [header]
    for it in items[:limit]:
        line = f"- {it['title']}"
        meta = []
        if show_source and it.get("source"):
            meta.append(it["source"])
        if it.get("date"):
            meta.append(it["date"])
        if meta:
            line += f" ({' · '.join(meta)})"
        if it.get("link"):
            line += f"\n  {it['link']}"
        out.append(line)
    return "\n".join(out)


@tool(name="news_search", description="Search news on any topic via Google News", category="research")
async def news_search(ctx: ToolContext, query: str, source: str = "", region: str = "US",
                      when: str = "", limit: int = 15) -> str:
    """Search recent news headlines on any topic, ticker, company, or event.

    Args:
        query: what to search for (e.g. "ECB rate decision", "Volvo layoffs", "NVDA").
        source: optional outlet to restrict to, e.g. "Reuters", "Associated Press",
            "Bloomberg". This reaches wire agencies that have no free direct feed.
        region: US, GB, SE, DE, or FR (controls language + edition). Default US.
        when: optional recency window like "24h", "3d", "7d".
        limit: max headlines (capped at 40).
    Returns headlines with outlet and date. Powered by Google News RSS (keyless).
    """
    if not query.strip() and not source.strip():
        return "Provide a query (and/or a source)."
    hl, gl, ceid = _REGIONS.get(region.upper(), _REGIONS["US"])
    q = query.strip()
    if source.strip():
        src = source.strip()
        q = (f'source:"{src}" ' + q).strip() if " " in src else (f"source:{src} " + q).strip()
    if when.strip():
        q = f"{q} when:{when.strip()}"
    limit = max(1, min(int(limit), 40))

    url = (f"https://news.google.com/rss/search?q={quote_plus(q)}"
           f"&hl={hl}&gl={gl}&ceid={ceid}")
    text, ferr = await _fetch(url)
    if text is None:
        return ferr
    try:
        _, items = _extract_items(text)
    except Exception as e:
        return f"Could not parse Google News response: {e}"
    if not items:
        return f"No news found for: {q}"
    return _format_items(f"**News — {q}** ({min(limit, len(items))} of {len(items)}):",
                         items, limit, show_source=True)


@tool(name="news_feeds", description="List or fetch curated news/finance RSS feeds", category="research")
async def news_feeds(ctx: ToolContext, name: str = "", category: str = "", limit: int = 12) -> str:
    """Browse a curated catalog of verified feeds, or fetch one.

    Args:
        name: a feed name (e.g. "fed_monetary", "reuters", "coindesk") to fetch its
            latest items. Omit to list the catalog.
        category: limit the listing to one group (e.g. "central_banks", "crypto").
        limit: max items when fetching a feed (capped at 40).
    Categories: """ + ", ".join(_CATALOG) + "."
    if name:
        url = _FEED_INDEX.get(name.strip().lower())
        if not url:
            return (f"Unknown feed '{name}'. List available feeds by calling "
                    f"news_feeds with no arguments.")
        err = _validate_url(url)
        if err:
            return err
        text, ferr = await _fetch(url)
        if text is None:
            return ferr
        try:
            feed_title, items = _extract_items(text)
        except Exception as e:
            return f"Could not parse feed '{name}': {e}"
        if not items:
            return f"No items in feed '{name}'."
        limit = max(1, min(int(limit), 40))
        return _format_items(f"**{name}** — {feed_title or ''} ({min(limit, len(items))} items):".replace(" ()", ""),
                             items, limit, show_source=bool(items[0].get("source")))

    # List catalog
    groups = {category.strip().lower(): _CATALOG[category.strip().lower()]} \
        if category.strip().lower() in _CATALOG else _CATALOG
    if category.strip() and category.strip().lower() not in _CATALOG:
        return f"Unknown category '{category}'. Categories: {', '.join(_CATALOG)}"
    out = ["**Curated news feeds** (fetch one with news_feeds name=\"<feed>\"):"]
    for group, feeds in groups.items():
        out.append(f"\n**{group}**: " + ", ".join(feeds))
    out.append("\nAlso: news_search for any topic; hnrss.org/newest?q=<term>&points=<n> "
               "for filtered Hacker News via rss_read.")
    return "\n".join(out)
