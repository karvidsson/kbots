"""Stock tools — formatting + error paths, with Yahoo responses mocked."""

import stocks

from src.core.base import ToolContext

CTX = ToolContext(agent_id="t", channel_id="c", user_id="u")


async def test_search_formats_results(monkeypatch):
    monkeypatch.setattr(stocks, "_yahoo_get", lambda p: {"quotes": [
        {"symbol": "AAPL", "longname": "Apple Inc.", "exchDisp": "NASDAQ", "typeDisp": "Equity"},
    ]})
    out = await stocks.stock_search(CTX, "apple")
    assert "**AAPL**" in out and "Apple Inc." in out and "NASDAQ" in out


async def test_search_no_results(monkeypatch):
    monkeypatch.setattr(stocks, "_yahoo_get", lambda p: {"quotes": []})
    assert "No symbols found" in await stocks.stock_search(CTX, "zzzz")


async def test_search_propagates_error(monkeypatch):
    monkeypatch.setattr(stocks, "_yahoo_get", lambda p: "Yahoo Finance error: boom")
    assert "boom" in await stocks.stock_search(CTX, "x")


async def test_quote_computes_change(monkeypatch):
    monkeypatch.setattr(stocks, "_yahoo_get", lambda p: {"chart": {"result": [{"meta": {
        "regularMarketPrice": 100.0, "chartPreviousClose": 90.0, "currency": "USD",
        "shortName": "Foo", "symbol": "FOO", "fullExchangeName": "NYSE",
    }}]}})
    out = await stocks.stock_quote(CTX, "FOO")
    assert "Price: 100.00 USD" in out
    assert "+11.11%" in out            # (100-90)/90
    assert "▲" in out


async def test_quote_no_price(monkeypatch):
    monkeypatch.setattr(stocks, "_yahoo_get", lambda p: {"chart": {"result": [{"meta": {}}]}})
    assert "No price data" in await stocks.stock_quote(CTX, "FOO")


async def test_history_change_and_table(monkeypatch):
    monkeypatch.setattr(stocks, "_yahoo_get", lambda p: {"chart": {"result": [{
        "meta": {"currency": "USD", "shortName": "Foo", "symbol": "FOO"},
        "timestamp": [1_000_000, 2_000_000, 3_000_000],
        "indicators": {"quote": [{"close": [10.0, None, 20.0]}]},  # None gap filtered
    }]}})
    out = await stocks.stock_history(CTX, "FOO", range="1mo")
    assert "+100.00%" in out           # 10 -> 20
    assert "| Date | Close |" in out
    assert "Period high: 20.00" in out


async def test_history_invalid_range():
    out = await stocks.stock_history(CTX, "FOO", range="bogus")
    assert "Invalid range" in out and "1mo" in out
