"""One-shot watchlist check: fetch, evaluate, maybe send one Telegram message."""

from __future__ import annotations

from datetime import datetime
import math
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from src.alerts import AlertState, Quote, TickerConfig, evaluate_alert
from src.bot import FiredAlert, ET, format_alert_message, require_telegram_env, send_message

if TYPE_CHECKING:
    from src.provider import PriceProvider

DEFAULT_THRESHOLD_PCT = 3.0


def default_threshold_pct() -> float:
    raw = os.environ.get("DEFAULT_THRESHOLD_PCT", str(DEFAULT_THRESHOLD_PCT)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"DEFAULT_THRESHOLD_PCT must be a number, got {raw!r}"
        ) from exc
    if value <= 0 or not math.isfinite(value):
        raise RuntimeError("DEFAULT_THRESHOLD_PCT must be a positive number")
    return value


def quotes_from_raw(
    raw: Mapping[str, Mapping[str, float | None] | None],
    tickers: Sequence[str],
) -> dict[str, Quote]:
    quotes: dict[str, Quote] = {}
    for ticker in tickers:
        row = raw.get(ticker) or {}
        quotes[ticker] = Quote(price=row.get("price"), prev_close=row.get("prev_close"))
    return quotes


def evaluate_watchlist(
    quotes: Mapping[str, Quote],
    tickers: Sequence[str],
    *,
    threshold_pct: float,
    day_key: str,
    states: dict[str, AlertState],
    mode: str = "once",
) -> list[FiredAlert]:
    """Evaluate each ticker; update in-memory `states`; return those that fire."""
    fired: list[FiredAlert] = []
    for symbol in tickers:
        quote = quotes.get(symbol) or Quote(price=None, prev_close=None)
        config = TickerConfig(symbol=symbol, threshold_pct=threshold_pct, mode=mode)
        decision = evaluate_alert(quote, config, states.get(symbol), day_key)
        states[symbol] = decision.new_state
        if not decision.should_alert:
            continue
        if (
            quote.price is None
            or quote.prev_close is None
            or decision.pct_change is None
        ):
            continue
        fired.append(
            FiredAlert(
                symbol=symbol,
                price=quote.price,
                prev_close=quote.prev_close,
                pct_change=decision.pct_change,
                leg=decision.leg,
            )
        )
    return fired


def run_check(
    *,
    provider: PriceProvider | None = None,
    sender: Callable[[str], None] | None = None,
    now: datetime | None = None,
    tickers: Sequence[str] | None = None,
    threshold_pct: float | None = None,
    states: dict[str, AlertState] | None = None,
    mode: str = "once",
) -> int:
    """Fetch demo tickers, evaluate `once` alerts, send one batched Telegram message.

    `states` is in-memory only for this process (no SQLite yet). A one-shot CLI
    run starts empty, so anything currently through the threshold fires.
    """
    from src.main import DEMO_TICKERS
    from src.provider import FinnhubProvider

    try:
        threshold = default_threshold_pct() if threshold_pct is None else threshold_pct
        if sender is None:
            require_telegram_env()
            sender = send_message
        if provider is None:
            provider = FinnhubProvider()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    symbols = list(tickers if tickers is not None else DEMO_TICKERS)
    clock = now if now is not None else datetime.now(ET)
    day_key = clock.astimezone(ET).date().isoformat() if clock.tzinfo else clock.date().isoformat()
    memory = states if states is not None else {}

    raw = provider.get_quotes(symbols)
    quotes = quotes_from_raw(raw, symbols)
    fired = evaluate_watchlist(
        quotes,
        symbols,
        threshold_pct=threshold,
        day_key=day_key,
        states=memory,
        mode=mode,
    )

    if not fired:
        print("no alerts")
        return 0

    text = format_alert_message(fired, clock)
    try:
        sender(text)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    print("sent:")
    print(text)
    return 0


def main() -> int:
    return run_check()


if __name__ == "__main__":
    sys.exit(main())
