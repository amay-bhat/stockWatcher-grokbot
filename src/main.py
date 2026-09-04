"""M1 entry: fetch Finnhub quotes for demo tickers and print prices."""

from __future__ import annotations

import sys

from src.provider import FinnhubProvider

DEMO_TICKERS = ["NVDA", "AMD", "AAPL", "MSFT", "GOOGL"]


def format_quote_line(ticker: str, quote: dict[str, float | None] | None) -> str | None:
    """Format one quote. Skip null/zero prices so we never invent -100%."""
    if not quote:
        return None
    price = quote.get("price")
    prev_close = quote.get("prev_close")
    if not _usable_price(price) or not _usable_price(prev_close):
        return None
    change_pct = ((price - prev_close) / prev_close) * 100
    return (
        f"{ticker}  ${price:.2f}  {change_pct:+.2f}%  "
        f"(prev close ${prev_close:.2f})"
    )


def _usable_price(value: float | None) -> bool:
    return isinstance(value, (int, float)) and value != 0 and value == value


def main() -> int:
    try:
        provider = FinnhubProvider()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    quotes = provider.get_quotes(DEMO_TICKERS)
    printed = 0
    for ticker in DEMO_TICKERS:
        line = format_quote_line(ticker, quotes.get(ticker))
        if line is None:
            continue
        print(line)
        printed += 1

    if printed == 0:
        print("no usable quotes (null/zero prices skipped)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
