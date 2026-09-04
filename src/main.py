"""CLI: quote printer (default) or one-shot Telegram drop check (`--check`)."""

from __future__ import annotations

import argparse
import sys

from src.provider import FinnhubProvider
from src.store import SEED_TICKERS

DEMO_TICKERS = list(SEED_TICKERS)


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


def print_quotes() -> int:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Watchlist Drop Alerts — quote printer or one-shot Telegram check.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fetch quotes, evaluate drop alerts, send one Telegram message if any fire",
    )
    args = parser.parse_args(argv)
    if args.check:
        from src.check_once import run_check

        return run_check()
    return print_quotes()


if __name__ == "__main__":
    sys.exit(main())
