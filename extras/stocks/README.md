# Stocks & market analytics

`stocks.py`: `stock_search`, `stock_quote`, `stock_history`.
`stock_analytics.py`: fundamentals, technicals, compare, financials, market overview.
Yahoo Finance endpoints — **no API key**.

Install (both files — analytics imports its sibling):
`cp extras/stocks/stocks.py extras/stocks/stock_analytics.py "$KBOTS_OVERLAY/tools/"`

Bundled skill: `financial_analyst.yaml` →
`cp extras/stocks/financial_analyst.yaml "$KBOTS_OVERLAY/skills/"`.
Tests: `uv run pytest extras/stocks`.
