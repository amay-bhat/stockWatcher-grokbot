"""Telegram inbound commands (M5). Long-poll getUpdates; persist via store.py."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import sys
import threading
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from src.alerts import VALID_MODES, TickerConfig
from src.bot import get_updates, is_allowed_chat, require_telegram_env, send_message
from src.check_once import run_check
from src.store import Store

if TYPE_CHECKING:
    from src.provider import PriceProvider

HELP_TEXT = """Watchlist commands:
/list — tickers, thresholds, modes, direction
/add TICKER [pct|$5] [up|down|both] — add (validates via Finnhub)
/remove TICKER
/set TICKER 5%|$5 [up|down|both] — percent or dollar move from prev close
/set TICKER up|down|both — alert on drops, rises, or both
/mode TICKER once|legs|mute
/mute TICKER
/status — price and % change
/check — poll now
/default pct — global default percent threshold
/help"""

DIRECTION_WORDS = {
    "up": "up",
    "rise": "up",
    "down": "down",
    "drop": "down",
    "both": "both",
}
_ADD_USAGE = "usage: /add TICKER [pct|$5] [up|down|both]"
_SET_USAGE = "usage: /set TICKER 5%|$5 [up|down|both]"


@dataclass
class ParsedCommand:
    name: str
    args: list[str]


@dataclass(frozen=True)
class ParsedThreshold:
    unit: str
    value: float


@dataclass
class BotDeps:
    """Injected store / quotes / check for handlers (easy to mock in tests)."""

    store: Store
    provider: PriceProvider
    alert_sender: Callable[[str], None] | None = None
    run_check: Callable[..., int] = run_check
    now: datetime | None = None


def parse_command(text: str) -> ParsedCommand | None:
    """Parse `/cmd@bot args`. None if the message is not a slash command."""
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split()
    raw = parts[0][1:]
    if "@" in raw:
        raw = raw.split("@", 1)[0]
    name = raw.strip().lower()
    if not name:
        return None
    return ParsedCommand(name=name, args=parts[1:])


def handle_text(text: str, deps: BotDeps) -> str | None:
    """Dispatch one authorized message. None = no reply (non-commands)."""
    parsed = parse_command(text)
    if parsed is None:
        return None
    handler = _HANDLERS.get(parsed.name)
    if handler is None:
        return "unknown command. /help"
    return handler(parsed.args, deps)


def handle_update(
    update: dict,
    allowed_chat_id: str,
    deps: BotDeps,
) -> str | None:
    """Process one getUpdates payload. Unauthorized chats are ignored silently."""
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    if not is_allowed_chat(chat_id, allowed_chat_id):
        return None
    text = message.get("text")
    if not isinstance(text, str):
        return None
    return handle_text(text, deps)


def run_bot(
    *,
    store: Store | None = None,
    provider: PriceProvider | None = None,
    poll_timeout: int = 25,
    error_sleep: float = 3.0,
    max_polls: int | None = None,
    get_updates_fn: Callable[..., list[dict]] | None = None,
    send_fn: Callable[[str], None] | None = None,
    stop: threading.Event | None = None,
) -> int:
    """Long-poll Telegram and handle commands until interrupted."""
    from src.provider import FinnhubProvider

    try:
        token, chat_id = require_telegram_env()
        quotes = provider if provider is not None else FinnhubProvider()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    db = store if store is not None else Store()
    poll = get_updates_fn or get_updates
    send = send_fn or send_message
    deps = BotDeps(store=db, provider=quotes, alert_sender=send)

    print("bot listening for commands (Ctrl+C to stop)", flush=True)
    offset: int | None = None
    polls = 0
    while True:
        if stop is not None and stop.is_set():
            print("bot stopped", flush=True)
            return 0
        try:
            updates = poll(token=token, offset=offset, timeout=poll_timeout)
        except KeyboardInterrupt:
            print("bot stopped", flush=True)
            return 0
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            time.sleep(error_sleep)
            polls += 1
            if max_polls is not None and polls >= max_polls:
                return 1
            continue

        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = update_id + 1
            try:
                reply = handle_update(update, chat_id, deps)
            except Exception as exc:  # noqa: BLE001 — keep the poll loop alive
                print(f"handler failed: {exc}", file=sys.stderr)
                continue
            if not reply:
                continue
            try:
                send(reply)
            except RuntimeError as exc:
                print(exc, file=sys.stderr)

        polls += 1
        if max_polls is not None and polls >= max_polls:
            return 0


def cmd_help(_args: Sequence[str], _deps: BotDeps) -> str:
    return HELP_TEXT


def cmd_list(_args: Sequence[str], deps: BotDeps) -> str:
    rows = deps.store.get_watchlist()
    if not rows:
        return "watchlist is empty"
    return "\n".join(_format_ticker(row) for row in rows)


def cmd_add(args: Sequence[str], deps: BotDeps) -> str:
    if not args:
        return _ADD_USAGE
    symbol = _normalize_ticker(args[0])
    if not symbol:
        return _ADD_USAGE
    try:
        parsed, direction = _parse_threshold_and_direction(args[1:])
    except ValueError as exc:
        return str(exc)

    if not _finnhub_symbol_ok(deps.provider, symbol):
        return f"unknown symbol: {symbol}"

    existing = deps.store.get_ticker(symbol)
    mode = existing.mode if existing is not None else "once"
    kwargs: dict = {"mode": mode}
    if direction is not None:
        kwargs["direction"] = direction
    if parsed is None:
        config = deps.store.upsert_ticker(symbol, **kwargs)
    elif parsed.unit == "usd":
        config = deps.store.upsert_ticker(
            symbol,
            threshold_unit="usd",
            threshold_usd=parsed.value,
            **kwargs,
        )
    else:
        config = deps.store.upsert_ticker(
            symbol,
            threshold_pct=parsed.value,
            threshold_unit="pct",
            **kwargs,
        )
    return _format_ticker(config)


def cmd_remove(args: Sequence[str], deps: BotDeps) -> str:
    if not args:
        return "usage: /remove TICKER"
    symbol = _normalize_ticker(args[0])
    if not symbol:
        return "usage: /remove TICKER"
    if deps.store.get_ticker(symbol) is None:
        return f"{symbol} is not on the watchlist"
    deps.store.remove_ticker(symbol)
    return f"removed {symbol}"


def cmd_set(args: Sequence[str], deps: BotDeps) -> str:
    if len(args) < 2:
        return _SET_USAGE
    symbol = _normalize_ticker(args[0])
    if not symbol:
        return _SET_USAGE
    existing = deps.store.get_ticker(symbol)
    if existing is None:
        return f"{symbol} is not on the watchlist"
    raw = args[1].strip()
    direction_only = parse_direction(raw)
    if direction_only is not None:
        if len(args) > 2:
            return _SET_USAGE
        config = deps.store.upsert_ticker(symbol, direction=direction_only)
        return _format_ticker(config)
    extra_direction: str | None = None
    if len(args) >= 3:
        extra_direction = parse_direction(args[2])
        if extra_direction is None or len(args) > 3:
            return "direction must be up, down, or both"
    if raw.lower() == "pct":
        config = deps.store.upsert_ticker(
            symbol,
            mode=existing.mode,
            threshold_unit="pct",
            direction=extra_direction,
        )
        return _format_ticker(config)
    try:
        parsed = parse_threshold(raw)
    except ValueError as exc:
        return str(exc)
    if parsed.unit == "usd":
        config = deps.store.upsert_ticker(
            symbol,
            mode=existing.mode,
            threshold_unit="usd",
            threshold_usd=parsed.value,
            direction=extra_direction,
        )
    else:
        config = deps.store.upsert_ticker(
            symbol,
            threshold_pct=parsed.value,
            mode=existing.mode,
            threshold_unit="pct",
            direction=extra_direction,
        )
    return _format_ticker(config)


def cmd_mode(args: Sequence[str], deps: BotDeps) -> str:
    if len(args) < 2:
        return "usage: /mode TICKER once|legs|mute"
    symbol = _normalize_ticker(args[0])
    mode = args[1].strip().lower()
    if not symbol:
        return "usage: /mode TICKER once|legs|mute"
    if mode not in VALID_MODES:
        return "mode must be once, legs, or mute"
    existing = deps.store.get_ticker(symbol)
    if existing is None:
        return f"{symbol} is not on the watchlist"
    config = deps.store.upsert_ticker(
        symbol,
        threshold_pct=existing.threshold_pct,
        mode=mode,
        threshold_unit=existing.threshold_unit,
        threshold_usd=existing.threshold_usd,
    )
    return _format_ticker(config)


def cmd_mute(args: Sequence[str], deps: BotDeps) -> str:
    if not args:
        return "usage: /mute TICKER"
    return cmd_mode([args[0], "mute"], deps)


def cmd_status(_args: Sequence[str], deps: BotDeps) -> str:
    rows = deps.store.get_watchlist()
    if not rows:
        return "watchlist is empty"
    symbols = [row.symbol for row in rows]
    quotes = deps.provider.get_quotes(symbols)
    return "\n".join(_format_status_line(symbol, quotes.get(symbol)) for symbol in symbols)


def cmd_check(_args: Sequence[str], deps: BotDeps) -> str:
    if not deps.store.get_watchlist():
        return "watchlist is empty"
    sent: list[str] = []

    def sender(text: str) -> None:
        sent.append(text)
        if deps.alert_sender is not None:
            deps.alert_sender(text)

    code = deps.run_check(
        provider=deps.provider,
        sender=sender,
        now=deps.now,
        store=deps.store,
    )
    if code != 0:
        return "check failed"
    if sent:
        return "checked: alerts sent"
    return "checked: no alerts"


def cmd_default(args: Sequence[str], deps: BotDeps) -> str:
    if not args:
        return "usage: /default pct"
    try:
        threshold = _parse_pct(args[0])
    except ValueError as exc:
        return str(exc)
    value = deps.store.set_default_threshold_pct(threshold)
    return f"default  {_format_pct(value)}%"


def _normalize_ticker(raw: str) -> str:
    return (raw or "").strip().upper()


def parse_direction(raw: str) -> str | None:
    """Parse `up`/`rise`, `down`/`drop`, or `both`. None if not a direction word."""
    return DIRECTION_WORDS.get((raw or "").strip().lower())


def _parse_threshold_and_direction(
    extras: Sequence[str],
) -> tuple[ParsedThreshold | None, str | None]:
    """Parse optional `[pct|$5] [up|down|both]` after a ticker."""
    if not extras:
        return None, None
    direction = parse_direction(extras[0])
    if direction is not None:
        if len(extras) > 1:
            raise ValueError(_ADD_USAGE)
        return None, direction
    parsed = parse_threshold(extras[0])
    if len(extras) == 1:
        return parsed, None
    direction = parse_direction(extras[1])
    if direction is None or len(extras) > 2:
        raise ValueError("direction must be up, down, or both")
    return parsed, direction


def parse_threshold(raw: str) -> ParsedThreshold:
    """Parse `5`, `5%` (pct) or `$5`, `5usd` (usd). Bare numbers stay pct."""
    text = (raw or "").strip().lower().replace(" ", "")
    if not text:
        raise ValueError("pct must be a positive number")
    if text.startswith("$"):
        return ParsedThreshold("usd", _parse_positive(text[1:], "usd"))
    if text.endswith("usd"):
        return ParsedThreshold("usd", _parse_positive(text[:-3], "usd"))
    if text.endswith("%"):
        return ParsedThreshold("pct", _parse_positive(text[:-1], "pct"))
    return ParsedThreshold("pct", _parse_positive(text, "pct"))


def _parse_pct(raw: str) -> float:
    parsed = parse_threshold(raw)
    if parsed.unit != "pct":
        raise ValueError("pct must be a positive number")
    return parsed.value


def _parse_positive(raw: str, kind: str) -> float:
    text = (raw or "").strip()
    try:
        value = float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{kind} must be a positive number") from exc
    if value <= 0 or not math.isfinite(value):
        raise ValueError(f"{kind} must be a positive number")
    return value


def _usable_price(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _finnhub_symbol_ok(provider: PriceProvider, symbol: str) -> bool:
    quotes = provider.get_quotes([symbol])
    row = quotes.get(symbol) if quotes else None
    if not isinstance(row, dict):
        return False
    return _usable_price(row.get("price"))


def _format_pct(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text


def _format_ticker(config: TickerConfig) -> str:
    direction = config.direction or "down"
    if config.threshold_unit == "usd":
        usd = config.threshold_usd
        amount = _format_pct(usd) if usd is not None else "?"
        return f"{config.symbol}  ${amount}  {config.mode}  {direction}"
    return (
        f"{config.symbol}  {_format_pct(config.threshold_pct)}%  "
        f"{config.mode}  {direction}"
    )


def _format_status_line(ticker: str, quote: dict[str, float | None] | None) -> str:
    if not quote:
        return f"{ticker}  (no quote)"
    price = quote.get("price")
    prev_close = quote.get("prev_close")
    if not _usable_price(price) or not _usable_price(prev_close):
        return f"{ticker}  (no quote)"
    change_pct = ((price - prev_close) / prev_close) * 100
    return (
        f"{ticker}  ${price:.2f}  {change_pct:+.2f}%  "
        f"(prev close ${prev_close:.2f})"
    )


_HANDLERS: dict[str, Callable[[Sequence[str], BotDeps], str]] = {
    "help": cmd_help,
    "start": cmd_help,
    "list": cmd_list,
    "add": cmd_add,
    "remove": cmd_remove,
    "set": cmd_set,
    "mode": cmd_mode,
    "mute": cmd_mute,
    "status": cmd_status,
    "check": cmd_check,
    "default": cmd_default,
}
