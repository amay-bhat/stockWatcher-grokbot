"""Telegram HTTP: outbound sendMessage and inbound getUpdates (stdlib urllib)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from typing import Sequence
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
TELEGRAM_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"
USER_AGENT = "stockWatcher-grokbot/m5"


@dataclass(frozen=True)
class FiredAlert:
    """One ticker that crossed its drop threshold this check."""

    symbol: str
    price: float
    prev_close: float
    pct_change: float
    leg: int | None = None


def require_telegram_env() -> tuple[str, str]:
    """Return (bot token, chat id) or raise with the missing names."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    missing: list[str] = []
    if not token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not chat_id:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise RuntimeError(
            "missing required env: " + ", ".join(missing)
        )
    return token, chat_id


def format_alert_message(alerts: Sequence[FiredAlert], now: datetime) -> str:
    """Build one batched watchlist-drop message (flat, factual)."""
    if not alerts:
        return ""
    lines = ["📉 Watchlist drop", ""]
    for alert in alerts:
        lines.append(_format_row(alert))
    lines.append("")
    lines.append(_format_et_clock(now))
    return "\n".join(lines)


def send_message(
    text: str,
    *,
    token: str | None = None,
    chat_id: str | None = None,
    timeout: float = 10.0,
) -> None:
    """POST text to Telegram sendMessage. Does not print the bot token."""
    if not token or not chat_id:
        env_token, env_chat_id = require_telegram_env()
        token = token or env_token
        chat_id = chat_id or env_chat_id
    if not text:
        raise RuntimeError("refusing to send an empty Telegram message")

    url = TELEGRAM_SEND_URL.format(token=token)
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _telegram_error_detail(exc)
        raise RuntimeError(f"Telegram send failed: HTTP {exc.code}{detail}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Telegram send failed: {exc}") from None

    if not isinstance(body, dict) or not body.get("ok"):
        desc = (
            body.get("description", "unknown error")
            if isinstance(body, dict)
            else "invalid response"
        )
        raise RuntimeError(f"Telegram send failed: {desc}")


def is_allowed_chat(chat_id: object, allowed: str) -> bool:
    """True when `chat_id` matches `TELEGRAM_CHAT_ID` (string compare)."""
    if chat_id is None or allowed is None:
        return False
    return str(chat_id).strip() == str(allowed).strip()


def get_updates(
    *,
    token: str | None = None,
    offset: int | None = None,
    timeout: int = 25,
    limit: int = 100,
) -> list[dict]:
    """Long-poll Telegram getUpdates. Does not print the bot token."""
    if not token:
        token, _ = require_telegram_env()

    params: dict[str, int | str] = {"timeout": int(timeout), "limit": int(limit)}
    if offset is not None:
        params["offset"] = int(offset)
    url = TELEGRAM_UPDATES_URL.format(token=token) + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT},
        method="GET",
    )
    http_timeout = float(timeout) + 10.0
    try:
        with urllib.request.urlopen(request, timeout=http_timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = _telegram_error_detail(exc)
        raise RuntimeError(f"Telegram getUpdates failed: HTTP {exc.code}{detail}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"Telegram getUpdates failed: {exc}") from None

    if not isinstance(body, dict) or not body.get("ok"):
        desc = (
            body.get("description", "unknown error")
            if isinstance(body, dict)
            else "invalid response"
        )
        raise RuntimeError(f"Telegram getUpdates failed: {desc}")

    result = body.get("result")
    if not isinstance(result, list):
        return []
    return [item for item in result if isinstance(item, dict)]


def _format_row(alert: FiredAlert) -> str:
    pct = _format_pct(alert.pct_change, alert.leg)
    return (
        f"{alert.symbol:<7}{_money(alert.price)}   {pct}   "
        f"(prev close {_money(alert.prev_close)})"
    )


def _money(value: float) -> str:
    """`$118.40` / `$ 92.15` so the dollars column lines up."""
    return f"${value:6.2f}"


def _format_pct(pct_change: float, leg: int | None) -> str:
    pct = pct_change * 100.0
    sign = "−" if pct < 0 else "+"
    text = f"{sign}{abs(pct):.1f}%"
    if leg is not None:
        text += f" ({_ordinal(leg)} leg)"
    return text


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _format_et_clock(now: datetime) -> str:
    if now.tzinfo is None:
        local = now.replace(tzinfo=ET)
    else:
        local = now.astimezone(ET)
    return local.strftime("%H:%M ET")


def _telegram_error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeError, AttributeError, TypeError):
        return ""
    if isinstance(payload, dict) and payload.get("description"):
        return f": {payload['description']}"
    return ""


def main() -> int:
    from src.bot_commands import run_bot

    return run_bot()


if __name__ == "__main__":
    import sys

    sys.exit(main())
