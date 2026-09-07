"""One-shot watchlist check: fetch, evaluate, maybe send one Telegram message."""

from __future__ import annotations

from datetime import datetime
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from src.alerts import AlertState, Quote, TickerConfig, evaluate_alert
from src.bot import FiredAlert, format_alert_message, require_telegram_env, send_message
from src.calendar import ET, trading_day_key
from src.store import Store, default_threshold_pct

if TYPE_CHECKING:
    from src.provider import PriceProvider

# Re-export so existing imports (`from src.check_once import default_threshold_pct`) keep working.
__all__ = [
    "default_threshold_pct",
    "evaluate_watchlist",
    "quotes_from_raw",
    "run_check",
    "main",
]


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
    configs: Sequence[TickerConfig],
    *,
    day_key: str,
    states: dict[str, AlertState],
) -> list[FiredAlert]:
    """Evaluate each ticker config; update `states`; return those that fire."""
    fired: list[FiredAlert] = []
    for config in configs:
        quote = quotes.get(config.symbol) or Quote(price=None, prev_close=None)
        decision = evaluate_alert(quote, config, states.get(config.symbol), day_key)
        states[config.symbol] = decision.new_state
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
                symbol=config.symbol,
                price=quote.price,
                prev_close=quote.prev_close,
                pct_change=decision.pct_change,
                leg=decision.leg,
                threshold_unit=config.threshold_unit,
                side=decision.side or "down",
            )
        )
    return fired


def run_check(
    *,
    provider: PriceProvider | None = None,
    sender: Callable[[str], None] | None = None,
    now: datetime | None = None,
    store: Store | None = None,
) -> int:
    """Load watchlist + state from SQLite, evaluate, maybe send, persist state.

    State is keyed by trading `day_key`. A second `--check` the same day (or a
    redeploy mid-day) will not re-alert names that already fired.
    """
    from src.provider import FinnhubProvider

    try:
        if sender is None:
            require_telegram_env()
            sender = send_message
        if provider is None:
            provider = FinnhubProvider()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    db = store if store is not None else Store()
    configs = db.get_watchlist()
    if not configs:
        print("watchlist is empty")
        return 0

    symbols = [config.symbol for config in configs]
    clock = now if now is not None else datetime.now(ET)
    day_key = trading_day_key(clock)
    memory = db.get_alert_states(symbols, day_key)

    raw = provider.get_quotes(symbols)
    if not any(_raw_quote_usable(raw.get(symbol)) for symbol in symbols):
        print("quote fetch failed for all tickers", file=sys.stderr)
        return 1
    quotes = quotes_from_raw(raw, symbols)
    fired = evaluate_watchlist(quotes, configs, day_key=day_key, states=memory)

    if not fired:
        db.save_alert_states(memory)
        print("no alerts")
        return 0

    text = format_alert_message(fired, clock)
    try:
        sender(text)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    db.save_alert_states(memory)
    print("sent:")
    print(text)
    return 0


def _raw_quote_usable(row: Mapping[str, float | None] | None) -> bool:
    if not row:
        return False
    price = row.get("price")
    prev_close = row.get("prev_close")
    return (
        isinstance(price, (int, float))
        and isinstance(prev_close, (int, float))
        and price == price
        and prev_close == prev_close
        and price != 0
        and prev_close != 0
    )


def main() -> int:
    return run_check()


if __name__ == "__main__":
    sys.exit(main())
