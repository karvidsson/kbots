"""Deeper stock analytics — fundamentals, technicals, comparison, statements, macro.

Complements the basic tools in stocks.py. Same Yahoo Finance endpoints, no API
key. Symbol formats are the same (AAPL, VOLV-B.ST, ^OMX, USDSEK=X, BTC-USD) —
use stock_search to resolve a company name to a symbol.

These tools return data for analysis; interpreting it (macro impact, whether
something is a good deal) is the agent's job — see skills/financial_analyst.yaml.
"""

import logging
import math
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

# Sibling import: once installed, both files sit in the overlay tools/ dir,
# which _scan_layer puts on sys.path — so the module name is `stocks`, not
# `src.tools.stocks`. Installing stock_analytics requires stocks.py alongside.
from stocks import VALID_RANGES, _chart, _fmt, _yahoo_get

from src.core.base import ToolContext
from src.core.tools import tool

logger = logging.getLogger(__name__)

MAX_COMPARE_SYMBOLS = 6
COMPARE_TABLE_ROWS = 10

# Tool params are named `range` for consistency with stock_history, which
# shadows the builtin inside those functions
_range = range

# Macro basket for market_overview
_MARKET_BASKET = [
    ("Indices", [
        ("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^OMX", "OMXS30"),
        ("^GDAXI", "DAX"), ("^FTSE", "FTSE 100"), ("^N225", "Nikkei 225"),
    ]),
    ("Rates & risk", [("^TNX", "US 10Y yield"), ("^VIX", "VIX")]),
    ("Commodities", [("CL=F", "Crude oil WTI"), ("GC=F", "Gold")]),
    ("FX & crypto", [
        ("EURUSD=X", "EUR/USD"), ("USDSEK=X", "USD/SEK"),
        ("EURSEK=X", "EUR/SEK"), ("BTC-USD", "Bitcoin"),
    ]),
]

# Line items per statement for stock_financials (timeseries endpoint type names,
# without the annual/quarterly prefix)
_STATEMENT_LINES = {
    "income": [
        ("TotalRevenue", "Revenue"),
        ("GrossProfit", "Gross profit"),
        ("OperatingIncome", "Operating income"),
        ("NetIncome", "Net income"),
    ],
    "balance": [
        ("TotalAssets", "Total assets"),
        ("TotalLiabilitiesNetMinorityInterest", "Total liabilities"),
        ("StockholdersEquity", "Equity"),
        ("CashAndCashEquivalents", "Cash & equivalents"),
    ],
    "cashflow": [
        ("OperatingCashFlow", "Operating CF"),
        ("InvestingCashFlow", "Investing CF"),
        ("FinancingCashFlow", "Financing CF"),
        ("FreeCashFlow", "Free cash flow"),
    ],
}


def _fv(section: dict, key: str) -> str | None:
    """Extract Yahoo's {raw, fmt} value as display string, tolerating gaps."""
    node = section.get(key)
    if isinstance(node, dict):
        return node.get("fmt")
    if isinstance(node, (int, float, str)) and node != {}:
        return str(node)
    return None


def _raw(section: dict, key: str) -> float | None:
    """Extract Yahoo's {raw, fmt} value as a number, tolerating gaps."""
    node = section.get(key)
    if isinstance(node, dict):
        raw = node.get("raw")
        return raw if isinstance(raw, (int, float)) else None
    if isinstance(node, (int, float)):
        return node
    return None


def _closes(symbol: str, range_: str) -> tuple[dict, list[tuple[int, float]]] | str:
    """Fetch daily close series for a symbol. Returns (meta, points) or error string."""
    result = _chart(symbol, range_, "1d")
    if isinstance(result, str):
        return result
    timestamps = result.get("timestamp") or []
    closes = (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    points = [(ts, c) for ts, c in zip(timestamps, closes) if c is not None]
    if len(points) < 2:
        return f"Not enough price data for '{symbol}' over range {range_}."
    return result.get("meta", {}), points


def _sma(closes: list[float], window: int) -> float | None:
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window


def _rsi(closes: list[float], window: int = 14) -> float | None:
    """RSI with Wilder's smoothing."""
    if len(closes) < window + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:window]) / window
    avg_loss = sum(losses[:window]) / window
    for g, loss in zip(gains[window:], losses[window:]):
        avg_gain = (avg_gain * (window - 1) + g) / window
        avg_loss = (avg_loss * (window - 1) + loss) / window
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _annualized_volatility(closes: list[float]) -> float | None:
    if len(closes) < 3:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
            if closes[i - 1] > 0 and closes[i] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252) * 100


def _max_drawdown(closes: list[float]) -> float:
    peak = closes[0]
    worst = 0.0
    for c in closes:
        peak = max(peak, c)
        worst = min(worst, (c - peak) / peak * 100)
    return worst


def _pct(new: float, old: float | None) -> str:
    if not old:
        return "?"
    return f"{(new - old) / old * 100:+.2f}%"


@tool(name="stock_fundamentals", description="Get valuation, profitability, and analyst data for a stock",
      category="finance")
async def stock_fundamentals(ctx: ToolContext, symbol: str) -> str:
    """Get fundamental metrics for a stock: valuation (P/E, PEG, P/S, P/B, market
    cap), profitability (margins, ROE/ROA, growth), financial health (debt,
    cash flow), dividend, and analyst recommendations with price targets.

    Small-cap and non-US stocks may have gaps — missing fields are omitted.
    """
    modules = "summaryDetail,defaultKeyStatistics,financialData,recommendationTrend"
    data = _yahoo_get(
        f"/v10/finance/quoteSummary/{quote(symbol)}?{urlencode({'modules': modules})}",
        use_crumb=True,
    )
    if isinstance(data, str):
        return data
    qs = data.get("quoteSummary", {})
    if qs.get("error"):
        err = qs["error"]
        return f"Yahoo Finance error: {err.get('description') or err.get('code')}"
    results = qs.get("result")
    if not results:
        return f"No fundamentals data for '{symbol}'."

    r = results[0]
    summary = r.get("summaryDetail", {})
    keystats = r.get("defaultKeyStatistics", {})
    findata = r.get("financialData", {})
    rec_trend = (r.get("recommendationTrend", {}).get("trend") or [{}])[0]

    def section(title: str, rows: list[tuple[str, str | None]]) -> list[str]:
        filled = [(label, v) for label, v in rows if v is not None]
        if not filled:
            return []
        return [f"\n**{title}**"] + [f"- {label}: {v}" for label, v in filled]

    lines = [f"**Fundamentals: {symbol}**"]
    lines += section("Valuation", [
        ("Market cap", _fv(summary, "marketCap")),
        ("Enterprise value", _fv(keystats, "enterpriseValue")),
        ("P/E (trailing)", _fv(summary, "trailingPE")),
        ("P/E (forward)", _fv(summary, "forwardPE")),
        ("PEG ratio", _fv(keystats, "pegRatio")),
        ("P/S (ttm)", _fv(summary, "priceToSalesTrailing12Months")),
        ("P/B", _fv(keystats, "priceToBook")),
        ("EV/EBITDA", _fv(keystats, "enterpriseToEbitda")),
        ("EPS (trailing)", _fv(keystats, "trailingEps")),
        ("EPS (forward)", _fv(keystats, "forwardEps")),
        ("Beta", _fv(summary, "beta")),
    ])
    lines += section("Profitability & growth", [
        ("Gross margin", _fv(findata, "grossMargins")),
        ("Operating margin", _fv(findata, "operatingMargins")),
        ("Profit margin", _fv(findata, "profitMargins")),
        ("Return on equity", _fv(findata, "returnOnEquity")),
        ("Return on assets", _fv(findata, "returnOnAssets")),
        ("Revenue growth (yoy)", _fv(findata, "revenueGrowth")),
        ("Earnings growth (yoy)", _fv(findata, "earningsGrowth")),
    ])
    lines += section("Financial health", [
        ("Total cash", _fv(findata, "totalCash")),
        ("Total debt", _fv(findata, "totalDebt")),
        ("Debt/equity", _fv(findata, "debtToEquity")),
        ("Current ratio", _fv(findata, "currentRatio")),
        ("Free cash flow", _fv(findata, "freeCashflow")),
        ("EBITDA", _fv(findata, "ebitda")),
    ])
    lines += section("Dividend", [
        ("Dividend rate", _fv(summary, "dividendRate")),
        ("Dividend yield", _fv(summary, "dividendYield")),
        ("Payout ratio", _fv(summary, "payoutRatio")),
        ("Ex-dividend date", _fv(summary, "exDividendDate")),
    ])

    analyst_rows: list[tuple[str, str | None]] = [
        ("Consensus", findata.get("recommendationKey")),
        ("Analysts", _fv(findata, "numberOfAnalystOpinions")),
        ("Target mean", _fv(findata, "targetMeanPrice")),
        ("Target range", None),
        ("Current price", _fv(findata, "currentPrice")),
    ]
    lo, hi = _fv(findata, "targetLowPrice"), _fv(findata, "targetHighPrice")
    if lo and hi:
        analyst_rows[3] = ("Target range", f"{lo} – {hi}")
    counts = [(k, rec_trend.get(k)) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")]
    if any(v for _, v in counts):
        breakdown = ", ".join(f"{k}: {v}" for k, v in counts if isinstance(v, int))
        analyst_rows.append(("Ratings", breakdown))
    lines += section("Analysts", analyst_rows)

    if len(lines) == 1:
        return f"No fundamentals data available for '{symbol}' (indices, FX, and crypto have none)."
    return "\n".join(lines)


@tool(name="stock_technicals", description="Get technical indicators for a stock", category="finance")
async def stock_technicals(ctx: ToolContext, symbol: str, range: str = "1y") -> str:
    """Get technical analysis indicators computed from price history: moving
    averages (SMA 20/50/200) vs current price, RSI-14, annualized volatility,
    max drawdown, returns over multiple horizons, and distance from 52-week
    high/low. Indicators needing more history than available are omitted.
    """
    if range not in VALID_RANGES:
        return f"Invalid range '{range}'. Valid ranges: {', '.join(VALID_RANGES)}"

    fetched = _closes(symbol, range)
    if isinstance(fetched, str):
        return fetched
    meta, points = fetched
    closes = [c for _, c in points]
    price = closes[-1]
    hint = meta.get("priceHint", 2)
    currency = meta.get("currency", "")
    name = meta.get("longName") or meta.get("shortName") or symbol

    lines = [
        f"**Technicals: {name}** ({meta.get('symbol', symbol)}) — {range}, {len(closes)} trading days",
        f"Price: {_fmt(price, hint)} {currency}",
    ]

    # Moving averages
    ma_lines = []
    smas = {w: _sma(closes, w) for w in (20, 50, 200)}
    for w, sma in smas.items():
        if sma is not None:
            pos = "above" if price >= sma else "below"
            ma_lines.append(f"- SMA{w}: {_fmt(sma, hint)} (price {pos}, {_pct(price, sma)})")
    if smas[50] is not None and smas[200] is not None:
        cross = "golden cross (SMA50 > SMA200)" if smas[50] > smas[200] else "death cross (SMA50 < SMA200)"
        ma_lines.append(f"- Trend signal: {cross}")
    if ma_lines:
        lines.append("\n**Moving averages**")
        lines += ma_lines

    # Momentum & risk
    mr_lines = []
    rsi = _rsi(closes)
    if rsi is not None:
        zone = "overbought" if rsi >= 70 else "oversold" if rsi <= 30 else "neutral"
        mr_lines.append(f"- RSI-14: {rsi:.1f} ({zone})")
    vol = _annualized_volatility(closes)
    if vol is not None:
        mr_lines.append(f"- Annualized volatility: {vol:.1f}%")
    mr_lines.append(f"- Max drawdown ({range}): {_max_drawdown(closes):.2f}%")
    lines.append("\n**Momentum & risk**")
    lines += mr_lines

    # Returns over horizons (in trading days)
    ret_lines = []
    for label, days in (("1 week", 5), ("1 month", 21), ("3 months", 63), ("6 months", 126)):
        if len(closes) > days:
            ret_lines.append(f"- {label}: {_pct(price, closes[-1 - days])}")
    ret_lines.append(f"- Full range ({range}): {_pct(price, closes[0])}")
    lines.append("\n**Returns**")
    lines += ret_lines

    # 52-week context
    wk_lo, wk_hi = meta.get("fiftyTwoWeekLow"), meta.get("fiftyTwoWeekHigh")
    if wk_lo and wk_hi:
        lines.append("\n**52-week range**")
        lines.append(f"- {_fmt(wk_lo, hint)} – {_fmt(wk_hi, hint)} "
                     f"(now {_pct(price, wk_hi)} from high, {_pct(price, wk_lo)} from low)")

    return "\n".join(lines)


@tool(name="stock_compare", description="Compare performance of multiple stocks or indices", category="finance")
async def stock_compare(ctx: ToolContext, symbols: str, range: str = "6mo") -> str:
    """Compare stocks/indices side by side over a period: normalized performance
    (% change from period start, so different currencies and price levels are
    comparable) plus per-symbol return, volatility, and high/low.

    symbols: comma-separated, up to 6 (e.g. 'VOLV-B.ST,ERIC-B.ST,^OMX').
    Include an index symbol to benchmark against the market.
    """
    if range not in VALID_RANGES:
        return f"Invalid range '{range}'. Valid ranges: {', '.join(VALID_RANGES)}"
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    if len(syms) < 2:
        return "Provide at least two comma-separated symbols to compare."
    if len(syms) > MAX_COMPARE_SYMBOLS:
        return f"Too many symbols ({len(syms)}) — maximum is {MAX_COMPARE_SYMBOLS}."

    series: dict[str, list[tuple[int, float]]] = {}
    stats: list[str] = []
    failed: list[str] = []
    for sym in syms:
        fetched = _closes(sym, range)
        if isinstance(fetched, str):
            failed.append(f"{sym} ({fetched})")
            continue
        meta, points = fetched
        closes = [c for _, c in points]
        hint = meta.get("priceHint", 2)
        vol = _annualized_volatility(closes)
        stats.append(
            f"| {sym} | {_pct(closes[-1], closes[0])} | "
            f"{f'{vol:.1f}%' if vol is not None else '?'} | "
            f"{_fmt(max(closes), hint)} | {_fmt(min(closes), hint)} | {meta.get('currency', '?')} |"
        )
        series[sym] = points

    if len(series) < 2:
        msg = "Not enough symbols could be fetched to compare."
        if failed:
            msg += " Failed: " + "; ".join(failed)
        return msg

    lines = [f"**Comparison** — {range}, normalized to 0% at period start", ""]
    lines += [
        "| Symbol | Return | Volatility (ann.) | High | Low | Currency |",
        "|--------|--------|-------------------|------|-----|----------|",
    ]
    lines += stats

    # Normalized performance table: sample each series at even fractions of its
    # own span. Dates are taken from the longest series — exchanges have
    # slightly different trading days, so rows are approximately aligned.
    reference = max(series.values(), key=len)
    lines += ["", "| Date | " + " | ".join(series.keys()) + " |",
              "|------|" + "|".join(["------"] * len(series)) + "|"]
    for i in _range(COMPARE_TABLE_ROWS + 1):
        frac = i / COMPARE_TABLE_ROWS
        ts = reference[round(frac * (len(reference) - 1))][0]
        date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        cells = []
        for points in series.values():
            _, close = points[round(frac * (len(points) - 1))]
            cells.append(_pct(close, points[0][1]))
        lines.append(f"| {date} | " + " | ".join(cells) + " |")

    if failed:
        lines.append("\nSkipped: " + "; ".join(failed))
    return "\n".join(lines)


@tool(name="market_overview", description="Get a macro market snapshot: indices, rates, commodities, FX",
      category="finance")
async def market_overview(ctx: ToolContext) -> str:
    """Get a snapshot of the macro environment: major stock indices (US, Sweden,
    Europe, Japan), US 10-year yield, VIX, oil, gold, key FX rates, and Bitcoin
    — each with 1-day and 5-day moves. Use this as context for reasoning about
    how the market environment affects individual stocks.
    """
    lines = ["**Market overview**"]
    for group, symbols in _MARKET_BASKET:
        rows = []
        for sym, label in symbols:
            # A month of history tolerates holiday gaps (e.g. ^TNX around
            # US market holidays) that a bare 5d window does not
            fetched = _closes(sym, "1mo")
            if isinstance(fetched, str):
                logger.info(f"market_overview: skipping {sym}: {fetched}")
                continue
            meta, points = fetched
            closes = [c for _, c in points]
            price = meta.get("regularMarketPrice") or closes[-1]
            hint = meta.get("priceHint", 2)
            day = _pct(price, closes[-2]) if len(closes) >= 2 else "?"
            week = _pct(price, closes[-6]) if len(closes) >= 6 else "?"
            rows.append(f"| {label} | {_fmt(price, hint)} | {day} | {week} |")
        if rows:
            lines += [f"\n**{group}**",
                      "| | Level | 1d | 5d |", "|---|-------|----|----|"]
            lines += rows
    if len(lines) == 1:
        return "Could not fetch market data — Yahoo Finance may be unavailable."
    return "\n".join(lines)


@tool(name="stock_financials", description="Get financial statement history for a stock", category="finance")
async def stock_financials(ctx: ToolContext, symbol: str, statement: str = "income",
                           period: str = "annual") -> str:
    """Get headline financial statement lines for the last periods.

    statement: 'income' (revenue, gross/operating/net income),
    'balance' (assets, liabilities, equity, cash), or
    'cashflow' (operating/investing/financing CF, free cash flow).
    period: 'annual' or 'quarterly'.
    """
    if statement not in _STATEMENT_LINES:
        return f"Invalid statement '{statement}'. Valid: {', '.join(_STATEMENT_LINES)}"
    if period not in ("annual", "quarterly"):
        return f"Invalid period '{period}'. Valid: annual, quarterly"

    items = _STATEMENT_LINES[statement]
    types = ",".join(f"{period}{key}" for key, _ in items)
    now = int(time.time())
    params = urlencode({
        "type": types,
        "period1": now - 6 * 365 * 86400,  # ~6 years back covers 4+ annual periods
        "period2": now,
    })
    data = _yahoo_get(
        f"/ws/fundamentals-timeseries/v1/finance/timeseries/{quote(symbol)}?{params}"
    )
    if isinstance(data, str):
        return data
    ts = data.get("timeseries", {})
    if ts.get("error"):
        err = ts["error"]
        return f"Yahoo Finance error: {err.get('description') or err.get('code')}"
    results = ts.get("result") or []

    # One result entry per requested type; collect {line label: {date: (raw, fmt)}}
    by_line: dict[str, dict[str, tuple[float | None, str]]] = {}
    all_dates: set[str] = set()
    for entry in results:
        type_name = (entry.get("meta", {}).get("type") or [""])[0]
        rows = entry.get(type_name) or []
        label = next((lbl for key, lbl in items if f"{period}{key}" == type_name), None)
        if not label:
            continue
        for row in rows:
            if not row:
                continue
            date = row.get("asOfDate", "?")
            rv = row.get("reportedValue") or {}
            by_line.setdefault(label, {})[date] = (rv.get("raw"), rv.get("fmt") or "?")
            all_dates.add(date)

    if not by_line:
        return (f"No {period} {statement} statement data for '{symbol}' "
                f"(indices, FX, crypto, and some foreign listings have none).")

    dates = sorted(all_dates)[-4:]
    lines = [
        f"**{statement.capitalize()} statement ({period}): {symbol}**",
        "",
        "| | " + " | ".join(dates) + " | Δ latest |",
        "|---|" + "|".join(["------"] * len(dates)) + "|------|",
    ]
    for _, label in items:
        values = by_line.get(label, {})
        cells = [values.get(d, (None, "—"))[1] for d in dates]
        raws = [values.get(d, (None, ""))[0] for d in dates]
        delta = "—"
        if len(dates) >= 2 and raws[-1] is not None and raws[-2]:
            delta = _pct(raws[-1], raws[-2])
        lines.append(f"| {label} | " + " | ".join(cells) + f" | {delta} |")
    return "\n".join(lines)
