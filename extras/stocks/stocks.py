"""Stock market tools — quotes, price history, and symbol search via Yahoo Finance.

No API key required. Symbol formats:
- US equities: plain ticker (AAPL, MSFT)
- Other exchanges: ticker + exchange suffix (ERIC-B.ST, VOLV-B.ST for Stockholm)
- Indices: ^ prefix (^OMX for OMXS30, ^GSPC for S&P 500)
- FX pairs: PAIR=X (USDSEK=X)
- Crypto: PAIR with dash (BTC-USD)

Use stock_search to resolve a company name to a symbol when unsure.
"""

import json
import logging
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from src.core.base import ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

_BASE = "https://query1.finance.yahoo.com"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; kbots/1.0)"}

VALID_RANGES = ["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"]

# Default interval per range — keeps point counts reasonable
_DEFAULT_INTERVALS = {
    "1d": "5m", "5d": "30m", "1mo": "1d", "3mo": "1d", "6mo": "1d",
    "1y": "1wk", "2y": "1wk", "ytd": "1d",
    "5y": "1mo", "10y": "1mo", "max": "1mo",
}

MAX_HISTORY_ROWS = 20


# Cookie+crumb pair for endpoints that require it (quoteSummary).
# Fetched lazily, cached at module level, refreshed once on auth failure.
_yahoo_auth: dict[str, str | None] = {"cookie": None, "crumb": None}


def _ensure_auth() -> dict[str, str] | str:
    """Get a valid Yahoo cookie+crumb pair. Returns the pair, or an error string."""
    if _yahoo_auth["cookie"] and _yahoo_auth["crumb"]:
        return {"cookie": _yahoo_auth["cookie"], "crumb": _yahoo_auth["crumb"]}

    # Any request to fc.yahoo.com sets the session cookie (the 404 is expected)
    cookie = ""
    try:
        with urlopen(Request("https://fc.yahoo.com", headers=_HEADERS), timeout=15) as resp:
            cookie = resp.headers.get("Set-Cookie") or ""
    except HTTPError as e:
        cookie = e.headers.get("Set-Cookie") or ""
    except URLError as e:
        return f"Yahoo Finance auth failed (cookie): {e}"
    cookie = cookie.split(";")[0]
    if not cookie:
        return "Yahoo Finance auth failed: no session cookie received."

    try:
        req = Request(f"{_BASE}/v1/test/getcrumb", headers={**_HEADERS, "Cookie": cookie})
        with urlopen(req, timeout=15) as resp:
            crumb = resp.read().decode().strip()
    except (HTTPError, URLError) as e:
        return f"Yahoo Finance auth failed (crumb): {e}"
    if not crumb or "<html" in crumb.lower():
        return "Yahoo Finance auth failed: no crumb received."

    _yahoo_auth["cookie"], _yahoo_auth["crumb"] = cookie, crumb
    return {"cookie": cookie, "crumb": crumb}


def _yahoo_get(path: str, use_crumb: bool = False) -> dict | str:
    """GET a Yahoo Finance endpoint. Returns parsed JSON dict, or an error string.

    use_crumb: authenticate with the cached cookie+crumb pair (required by
    some endpoints, e.g. quoteSummary). Refreshes the pair once on 401/403.
    """
    for attempt in (1, 2):
        headers = dict(_HEADERS)
        url = f"{_BASE}{path}"
        if use_crumb:
            auth = _ensure_auth()
            if isinstance(auth, str):
                return auth
            headers["Cookie"] = auth["cookie"]
            sep = "&" if "?" in path else "?"
            url = f"{url}{sep}crumb={quote(auth['crumb'])}"

        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            # Stale crumb — invalidate the cache and retry once with a fresh pair
            if use_crumb and e.code in (401, 403) and attempt == 1:
                _yahoo_auth["cookie"] = _yahoo_auth["crumb"] = None
                continue
            # Yahoo returns JSON error bodies on 404 (unknown symbol) etc.
            try:
                body = json.loads(e.read().decode())
                err = (body.get("chart") or body.get("finance") or {}).get("error") or {}
                desc = err.get("description") or err.get("code") or str(e)
                return f"Yahoo Finance error: {desc}"
            except Exception:
                return f"Yahoo Finance request failed: HTTP {e.code}"
        except URLError as e:
            return f"Yahoo Finance request failed: {e}"
        except json.JSONDecodeError:
            return "Yahoo Finance returned an unparseable response."
    return "Yahoo Finance request failed after crumb refresh."


def _chart(symbol: str, range_: str, interval: str) -> dict | str:
    """Fetch chart data for a symbol. Returns the result dict, or an error string."""
    params = urlencode({"range": range_, "interval": interval})
    data = _yahoo_get(f"/v8/finance/chart/{quote(symbol)}?{params}")
    if isinstance(data, str):
        return data
    chart = data.get("chart", {})
    error = chart.get("error")
    if error:
        return f"Yahoo Finance error: {error.get('description') or error.get('code')}"
    results = chart.get("result")
    if not results:
        return f"No data found for symbol '{symbol}'."
    return results[0]


def _fmt(value: float | None, hint: int = 2) -> str:
    """Format a price value, tolerating None."""
    if value is None:
        return "?"
    return f"{value:,.{hint}f}"


@tool(name="stock_search", description="Find stock ticker symbols by company name", category="finance")
async def stock_search(ctx: ToolContext, query: str, max_results: int = 5) -> str:
    """Search for stocks, indices, ETFs, and currencies by name or partial symbol.

    Use this first when you only know a company name — it returns the exact
    symbols that stock_quote and stock_history expect (e.g. 'volvo' finds
    VOLV-B.ST on the Stockholm exchange).
    """
    params = urlencode({"q": query, "quotesCount": max(1, min(max_results, 10)), "newsCount": 0})
    data = _yahoo_get(f"/v1/finance/search?{params}")
    if isinstance(data, str):
        return data

    quotes = [q for q in data.get("quotes", []) if q.get("symbol")]
    if not quotes:
        return f"No symbols found for '{query}'."

    lines = []
    for q in quotes[:max_results]:
        name = q.get("longname") or q.get("shortname") or ""
        exchange = q.get("exchDisp") or q.get("exchange") or "?"
        kind = q.get("typeDisp") or q.get("quoteType") or ""
        lines.append(f"**{q['symbol']}** — {name} ({exchange}, {kind})")
    return "\n".join(lines)


@tool(name="stock_quote", description="Get the current price and key stats for a stock", category="finance")
async def stock_quote(ctx: ToolContext, symbol: str) -> str:
    """Get a current snapshot for a stock, index, FX pair, or crypto.

    Returns price, change vs previous close, day range, volume, and 52-week
    range. Symbol must be a Yahoo Finance symbol (use stock_search to find it).
    """
    result = _chart(symbol, "1d", "1d")
    if isinstance(result, str):
        return result

    meta = result.get("meta", {})
    price = meta.get("regularMarketPrice")
    if price is None:
        return f"No price data available for '{symbol}'."

    prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    hint = meta.get("priceHint", 2)
    name = meta.get("longName") or meta.get("shortName") or symbol
    currency = meta.get("currency", "")

    lines = [
        f"**{name}** ({meta.get('symbol', symbol)}) — {meta.get('fullExchangeName', '?')}",
        f"Price: {_fmt(price, hint)} {currency}",
    ]
    if prev_close:
        change = price - prev_close
        pct = change / prev_close * 100
        arrow = "▲" if change >= 0 else "▼"
        lines.append(f"Change: {arrow} {change:+,.{hint}f} ({pct:+.2f}%) vs previous close {_fmt(prev_close, hint)}")
    day_lo, day_hi = meta.get("regularMarketDayLow"), meta.get("regularMarketDayHigh")
    if day_lo is not None and day_hi is not None:
        lines.append(f"Day range: {_fmt(day_lo, hint)} – {_fmt(day_hi, hint)}")
    wk_lo, wk_hi = meta.get("fiftyTwoWeekLow"), meta.get("fiftyTwoWeekHigh")
    if wk_lo is not None and wk_hi is not None:
        lines.append(f"52-week range: {_fmt(wk_lo, hint)} – {_fmt(wk_hi, hint)}")
    volume = meta.get("regularMarketVolume")
    if volume:
        lines.append(f"Volume: {volume:,}")
    market_time = meta.get("regularMarketTime")
    if market_time:
        ts = datetime.fromtimestamp(market_time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"As of: {ts}")
    return "\n".join(lines)


@tool(name="stock_history", description="Get historical price development for a stock", category="finance")
async def stock_history(ctx: ToolContext, symbol: str, range: str = "1mo", interval: str = "") -> str:
    """Get how a stock has developed over a period.

    Returns start/end price, absolute and percent change, period high/low, and
    a sampled table of closing prices. Range is one of: 1d, 5d, 1mo, 3mo, 6mo,
    1y, 2y, 5y, 10y, ytd, max. Interval (e.g. 1d, 1wk, 1mo) is auto-picked to
    fit the range when not given.
    """
    if range not in VALID_RANGES:
        return f"Invalid range '{range}'. Valid ranges: {', '.join(VALID_RANGES)}"
    interval = interval or _DEFAULT_INTERVALS[range]

    result = _chart(symbol, range, interval)
    if isinstance(result, str):
        return result

    meta = result.get("meta", {})
    timestamps = result.get("timestamp") or []
    quotes = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = quotes.get("close") or []

    # Yahoo pads with None for missing periods — keep only real data points
    points = [(ts, c) for ts, c in zip(timestamps, closes) if c is not None]
    if len(points) < 2:
        return f"Not enough price data for '{symbol}' over range {range}."

    hint = meta.get("priceHint", 2)
    name = meta.get("longName") or meta.get("shortName") or symbol
    currency = meta.get("currency", "")

    start_ts, start_price = points[0]
    end_ts, end_price = points[-1]
    change = end_price - start_price
    pct = (change / start_price * 100) if start_price else 0.0
    period_high = max(c for _, c in points)
    period_low = min(c for _, c in points)

    intraday = interval.endswith(("m", "h"))
    date_fmt = "%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d"

    def fmt_ts(ts: int) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(date_fmt)

    arrow = "▲" if change >= 0 else "▼"
    lines = [
        f"**{name}** ({meta.get('symbol', symbol)}) — {range} history, {interval} interval",
        f"{fmt_ts(start_ts)}: {_fmt(start_price, hint)} {currency} → "
        f"{fmt_ts(end_ts)}: {_fmt(end_price, hint)} {currency}",
        f"Change: {arrow} {change:+,.{hint}f} ({pct:+.2f}%)",
        f"Period high: {_fmt(period_high, hint)}, low: {_fmt(period_low, hint)}",
        "",
        "| Date | Close |",
        "|------|-------|",
    ]

    # Sample down to at most MAX_HISTORY_ROWS rows, always keeping the last point
    step = max(1, (len(points) + MAX_HISTORY_ROWS - 1) // MAX_HISTORY_ROWS)
    sampled = points[::step]
    if sampled[-1][0] != end_ts:
        sampled.append((end_ts, end_price))
    for ts, close in sampled:
        lines.append(f"| {fmt_ts(ts)} | {_fmt(close, hint)} |")
    return "\n".join(lines)
