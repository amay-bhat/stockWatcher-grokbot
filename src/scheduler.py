"""Market-hours check loop (M6).

Polls every CHECK_INTERVAL_MINUTES (default 5) while the NYSE regular session
is open. Sleeps until the next open when closed — no after-hours quote spam.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import os
import sys
import threading
import time
from collections.abc import Callable

from src.calendar import (
    ET,
    closed_reason,
    is_session_open,
    next_open_after,
    session_close_at,
    session_hours,
    to_et,
    trading_day_key,
)
from src.check_once import run_check

DEFAULT_CHECK_INTERVAL_MINUTES = 5
FAILURE_WARN_STREAK = 3
FAILURE_WARN_TEXT = (
    "Watchlist checker: 3 consecutive check cycles failed. Will keep retrying."
)


@dataclass(frozen=True)
class SchedulerDecision:
    """Pure next-step for the loop: run a check now, then sleep until `sleep_until`."""

    run_check: bool
    sleep_until: datetime
    reason: str
    day_key: str | None


def check_interval_minutes(raw: str | None = None) -> int:
    """Positive integer minutes from env (default 5)."""
    if raw is None:
        raw = os.environ.get("CHECK_INTERVAL_MINUTES", str(DEFAULT_CHECK_INTERVAL_MINUTES))
    text = (raw or "").strip() or str(DEFAULT_CHECK_INTERVAL_MINUTES)
    try:
        value = int(text)
    except ValueError as exc:
        raise RuntimeError(
            f"CHECK_INTERVAL_MINUTES must be a positive integer, got {text!r}"
        ) from exc
    if value < 1:
        raise RuntimeError("CHECK_INTERVAL_MINUTES must be a positive integer")
    return value


def decide(now: datetime, interval: timedelta) -> SchedulerDecision:
    """Choose whether to run a check and when to wake next.

    Open: run now, then sleep `interval` (or until the next session if that
    wake would land after today's close). Closed: do not run; sleep until open.
    """
    local = to_et(now)
    if interval <= timedelta(0):
        raise ValueError("interval must be positive")

    if is_session_open(local):
        close_at = session_close_at(local)
        hours = session_hours(local.date())
        assert close_at is not None and hours is not None
        nxt = local + interval
        if nxt >= close_at:
            nxt = next_open_after(close_at)
        reason = "open_early_close" if hours.early_close else "open"
        return SchedulerDecision(
            run_check=True,
            sleep_until=nxt,
            reason=reason,
            day_key=trading_day_key(local),
        )

    nxt_open = next_open_after(local)
    return SchedulerDecision(
        run_check=False,
        sleep_until=nxt_open,
        reason=closed_reason(local),
        day_key=trading_day_key(nxt_open),
    )


def interruptible_sleep(
    seconds: float,
    stop: threading.Event | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> None:
    """Sleep up to `seconds`, returning early if `stop` is set."""
    remaining = max(0.0, float(seconds))
    if remaining == 0.0:
        return
    if stop is not None:
        stop.wait(timeout=remaining)
        return
    (sleeper or time.sleep)(remaining)


def run_scheduler(
    *,
    stop: threading.Event | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] | None = None,
    check: Callable[..., int] | None = None,
    warn: Callable[[str], None] | None = None,
    interval_minutes: int | None = None,
    max_cycles: int | None = None,
    store=None,
    provider=None,
    sender: Callable[[str], None] | None = None,
) -> int:
    """Loop: check while open, sleep until next open while closed.

    API / check failures are logged and skipped (the process stays up). After
    `FAILURE_WARN_STREAK` consecutive failed *open* cycles, send one Telegram
    warning and reset the streak.
    """
    try:
        minutes = (
            interval_minutes
            if interval_minutes is not None
            else check_interval_minutes()
        )
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    interval = timedelta(minutes=minutes)
    now_fn = clock or (lambda: datetime.now(ET))
    check_fn = check or run_check
    failures = 0
    cycles = 0

    print(
        f"scheduler started (interval={minutes}m, market hours 09:30–16:00 ET)",
        flush=True,
    )

    while stop is None or not stop.is_set():
        now = now_fn()
        decision = decide(now, interval)
        if decision.run_check:
            print(
                f"scheduler: {decision.reason} day_key={decision.day_key}",
                flush=True,
            )
            try:
                kwargs: dict = {"now": now}
                if store is not None:
                    kwargs["store"] = store
                if provider is not None:
                    kwargs["provider"] = provider
                if sender is not None:
                    kwargs["sender"] = sender
                code = check_fn(**kwargs)
                if code != 0:
                    failures += 1
                    print(
                        f"check cycle failed (exit {code}); streak={failures}",
                        file=sys.stderr,
                    )
                else:
                    failures = 0
            except Exception as exc:  # noqa: BLE001 — keep the loop alive
                failures += 1
                print(
                    f"check cycle failed: {exc}; streak={failures}",
                    file=sys.stderr,
                )
            if failures >= FAILURE_WARN_STREAK:
                _emit_failure_warning(warn)
                failures = 0
        else:
            wake = to_et(decision.sleep_until)
            print(
                f"scheduler: market closed ({decision.reason}); "
                f"next open {wake.strftime('%Y-%m-%d %H:%M ET')}",
                flush=True,
            )

        if stop is not None and stop.is_set():
            return 0

        wake_at = decision.sleep_until
        remaining = (wake_at - now_fn()).total_seconds()
        if remaining <= 0:
            remaining = 1.0
        if sleep is not None and stop is None:
            sleep(remaining)
        else:
            interruptible_sleep(remaining, stop=stop, sleeper=sleep)

        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return 0

    print("scheduler stopped", flush=True)
    return 0


def run_serve(
    *,
    stop: threading.Event | None = None,
    bot_fn: Callable[..., int] | None = None,
    scheduler_fn: Callable[..., int] | None = None,
    store=None,
    provider=None,
) -> int:
    """Run Telegram long-poll and the market-hours scheduler together."""
    from src.bot import require_telegram_env
    from src.bot_commands import run_bot
    from src.store import Store

    try:
        require_telegram_env()
        if provider is None:
            from src.provider import FinnhubProvider

            provider = FinnhubProvider()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1

    halt = stop if stop is not None else threading.Event()
    db = store if store is not None else Store()
    run_bot_fn = bot_fn or run_bot
    run_sched_fn = scheduler_fn or run_scheduler

    print(
        "serving: Telegram commands + market-hours scheduler (Ctrl+C to stop)",
        flush=True,
    )

    errors: list[BaseException] = []

    def bot_target() -> None:
        try:
            run_bot_fn(store=db, provider=provider, stop=halt)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            halt.set()

    def sched_target() -> None:
        try:
            run_sched_fn(
                stop=halt,
                store=db,
                provider=provider,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            halt.set()

    bot_thread = threading.Thread(target=bot_target, name="telegram-bot", daemon=True)
    sched_thread = threading.Thread(target=sched_target, name="scheduler", daemon=True)
    bot_thread.start()
    sched_thread.start()

    try:
        while bot_thread.is_alive() and sched_thread.is_alive() and not halt.is_set():
            bot_thread.join(timeout=0.5)
    except KeyboardInterrupt:
        print("stopping", flush=True)
        halt.set()

    halt.set()
    bot_thread.join(timeout=2.0)
    sched_thread.join(timeout=2.0)
    if errors:
        print(errors[0], file=sys.stderr)
        return 1
    return 0


def _emit_failure_warning(warn: Callable[[str], None] | None) -> None:
    print(FAILURE_WARN_TEXT, file=sys.stderr)
    sender = warn
    if sender is None:
        try:
            from src.bot import send_message

            sender = send_message
        except Exception as exc:  # noqa: BLE001
            print(f"failure warning not sent: {exc}", file=sys.stderr)
            return
    try:
        sender(FAILURE_WARN_TEXT)
    except Exception as exc:  # noqa: BLE001
        print(f"failure warning not sent: {exc}", file=sys.stderr)


def main() -> int:
    return run_scheduler()


if __name__ == "__main__":
    sys.exit(main())
