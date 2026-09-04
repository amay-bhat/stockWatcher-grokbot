"""CLI: combined serve (default), `--check`, `--bot`, or `--quotes`."""

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
        description=(
            "Watchlist Drop Alerts — scheduler + Telegram bot (default), "
            "one-shot check, bot-only, or quote printer."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--serve",
        action="store_true",
        help="Run Telegram commands and the market-hours scheduler together (default)",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="Fetch quotes, evaluate drop alerts, send one Telegram message if any fire",
    )
    mode.add_argument(
        "--bot",
        action="store_true",
        help="Long-poll Telegram for inbound watchlist commands only",
    )
    mode.add_argument(
        "--quotes",
        action="store_true",
        help="Print current vs previous close for the seed tickers (M1)",
    )
    args = parser.parse_args(argv)
    if args.check:
        from src.check_once import run_check

        return run_check()
    if args.bot:
        from src.bot_commands import run_bot

        return run_bot()
    if args.quotes:
        return print_quotes()
    from src.scheduler import run_serve

    return run_serve()


if __name__ == "__main__":
    sys.exit(main())
