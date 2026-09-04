"""Pure drop-alert decision logic (M2).

No I/O, no network, no clock reads. Callers pass `day_key` so daily reset
is explicit. Baseline is previous close:

    pct_change = (current - previous_close) / previous_close

Trigger when that ratio is at or below `-threshold_pct / 100` (threshold is a
positive percent, e.g. 3.0 means -3%).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

AlertMode = Literal["once", "legs", "mute"]
VALID_MODES: frozenset[str] = frozenset({"once", "legs", "mute"})


@dataclass(frozen=True)
class Quote:
    """Latest print vs previous close. Either field may be missing."""

    price: float | None
    prev_close: float | None


@dataclass(frozen=True)
class TickerConfig:
    """Per-ticker alert settings. `mode` defaults to once."""

    symbol: str
    threshold_pct: float
    mode: AlertMode = "once"


@dataclass(frozen=True)
class AlertState:
    """What was already notified on one trading day (`day_key`).

    `fired` is for `once`. `last_leg` is the last fully crossed step for `legs`
    (0 = none yet). Both fields are ignored by `mute`.
    """

    day_key: str
    fired: bool = False
    last_leg: int = 0


@dataclass(frozen=True)
class Decision:
    """Whether to notify, plus the state to persist for the rest of the day."""

    should_alert: bool
    new_state: AlertState
    leg: int | None = None
    pct_change: float | None = None


def fresh_state(day_key: str) -> AlertState:
    """Empty per-day state. Same object shape the evaluator returns."""
    return AlertState(day_key=day_key)


def pct_change(quote: Quote) -> float | None:
    """(current - previous_close) / previous_close, or None if unusable."""
    price = quote.price
    prev_close = quote.prev_close
    if not _usable_price(price) or not _usable_price(prev_close):
        return None
    return (price - prev_close) / prev_close


def evaluate_alert(
    quote: Quote,
    config: TickerConfig,
    state: AlertState | None,
    day_key: str,
) -> Decision:
    """Decide whether this quote should fire a drop alert.

    `day_key` identifies the trading day (caller-supplied; this module never
    reads the clock). A new `day_key` resets notification memory. Invalid
    quotes never alert and leave `state` unchanged.
    """
    change = pct_change(quote)
    if change is None:
        return Decision(
            should_alert=False,
            new_state=state if state is not None else fresh_state(day_key),
            pct_change=None,
        )

    working = _state_for_day(state, day_key)
    mode = _normalize_mode(config.mode)
    threshold_pct = config.threshold_pct
    if threshold_pct <= 0 or not math.isfinite(threshold_pct):
        return Decision(should_alert=False, new_state=working, pct_change=change)

    if mode == "mute":
        return Decision(should_alert=False, new_state=working, pct_change=change)

    steps = _steps_down(change, threshold_pct)
    if mode == "once":
        return _decide_once(working, change, steps)
    return _decide_legs(working, change, steps)


def _decide_once(state: AlertState, change: float, steps: int) -> Decision:
    if steps < 1 or state.fired:
        return Decision(should_alert=False, new_state=state, pct_change=change)
    return Decision(
        should_alert=True,
        new_state=AlertState(day_key=state.day_key, fired=True, last_leg=state.last_leg),
        pct_change=change,
    )


def _decide_legs(state: AlertState, change: float, steps: int) -> Decision:
    if steps <= state.last_leg:
        return Decision(should_alert=False, new_state=state, pct_change=change)
    return Decision(
        should_alert=True,
        new_state=AlertState(day_key=state.day_key, fired=state.fired, last_leg=steps),
        leg=steps,
        pct_change=change,
    )


def _state_for_day(state: AlertState | None, day_key: str) -> AlertState:
    if state is None or state.day_key != day_key:
        return fresh_state(day_key)
    return state


def _normalize_mode(mode: str) -> str:
    normalized = (mode or "once").strip().lower()
    if normalized in VALID_MODES:
        return normalized
    return "once"


def _usable_price(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _steps_down(change: float, threshold_pct: float) -> int:
    """How many full threshold steps the drop has crossed (0 if none).

    Exact threshold (e.g. -3.0% with threshold 3.0) counts as one step.
    A small epsilon absorbs floating-point noise at the boundary.
    """
    drop_pct = -change * 100.0
    return max(0, math.floor(drop_pct / threshold_pct + 1e-9))
