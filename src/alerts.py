"""Pure drop-alert decision logic (M2).

No I/O, no network, no clock reads. Callers pass `day_key` so daily reset
is explicit. Baseline is previous close:

    pct_change = (current - previous_close) / previous_close
    dollar_drop = previous_close - current   # positive when down

Each ticker has a unit (`pct` default, or `usd`):

- pct: fire when `pct_change <= -threshold_pct / 100`
- usd: fire when `dollar_drop >= threshold_usd`
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

AlertMode = Literal["once", "legs", "mute"]
ThresholdUnit = Literal["pct", "usd"]
VALID_MODES: frozenset[str] = frozenset({"once", "legs", "mute"})
VALID_UNITS: frozenset[str] = frozenset({"pct", "usd"})


@dataclass(frozen=True)
class Quote:
    """Latest print vs previous close. Either field may be missing."""

    price: float | None
    prev_close: float | None


@dataclass(frozen=True)
class TickerConfig:
    """Per-ticker alert settings. `mode` defaults to once; unit defaults to pct."""

    symbol: str
    threshold_pct: float
    mode: AlertMode = "once"
    threshold_unit: ThresholdUnit = "pct"
    threshold_usd: float | None = None


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
    dollar_drop: float | None = None


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


def dollar_drop(quote: Quote) -> float | None:
    """previous_close - current (positive when down), or None if unusable."""
    price = quote.price
    prev_close = quote.prev_close
    if not _usable_price(price) or not _usable_price(prev_close):
        return None
    return prev_close - price


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
    drop = dollar_drop(quote)
    if change is None or drop is None:
        return Decision(
            should_alert=False,
            new_state=state if state is not None else fresh_state(day_key),
            pct_change=None,
            dollar_drop=None,
        )

    working = _state_for_day(state, day_key)
    mode = _normalize_mode(config.mode)
    if mode == "mute":
        return Decision(
            should_alert=False,
            new_state=working,
            pct_change=change,
            dollar_drop=drop,
        )

    steps = _steps_for_config(change, drop, config)
    if steps is None:
        return Decision(
            should_alert=False,
            new_state=working,
            pct_change=change,
            dollar_drop=drop,
        )

    if mode == "once":
        return _decide_once(working, change, drop, steps)
    return _decide_legs(working, change, drop, steps)


def _decide_once(
    state: AlertState, change: float, drop: float, steps: int
) -> Decision:
    if steps < 1 or state.fired:
        return Decision(
            should_alert=False,
            new_state=state,
            pct_change=change,
            dollar_drop=drop,
        )
    return Decision(
        should_alert=True,
        new_state=AlertState(day_key=state.day_key, fired=True, last_leg=state.last_leg),
        pct_change=change,
        dollar_drop=drop,
    )


def _decide_legs(
    state: AlertState, change: float, drop: float, steps: int
) -> Decision:
    if steps <= state.last_leg:
        return Decision(
            should_alert=False,
            new_state=state,
            pct_change=change,
            dollar_drop=drop,
        )
    return Decision(
        should_alert=True,
        new_state=AlertState(day_key=state.day_key, fired=state.fired, last_leg=steps),
        leg=steps,
        pct_change=change,
        dollar_drop=drop,
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


def _normalize_unit(unit: str) -> str:
    normalized = (unit or "pct").strip().lower()
    if normalized in VALID_UNITS:
        return normalized
    return "pct"


def _usable_price(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _positive_threshold(value: float | None) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _steps_for_config(change: float, drop: float, config: TickerConfig) -> int | None:
    """Full threshold steps crossed, or None if the active threshold is invalid."""
    unit = _normalize_unit(config.threshold_unit)
    if unit == "usd":
        threshold = config.threshold_usd
        if not _positive_threshold(threshold):
            return None
        return _steps_down_usd(drop, float(threshold))
    threshold_pct = config.threshold_pct
    if not _positive_threshold(threshold_pct):
        return None
    return _steps_down(change, threshold_pct)


def _steps_down(change: float, threshold_pct: float) -> int:
    """How many full threshold steps the drop has crossed (0 if none).

    Exact threshold (e.g. -3.0% with threshold 3.0) counts as one step.
    A small epsilon absorbs floating-point noise at the boundary.
    """
    drop_pct = -change * 100.0
    return max(0, math.floor(drop_pct / threshold_pct + 1e-9))


def _steps_down_usd(drop: float, threshold_usd: float) -> int:
    """How many full `threshold_usd` steps the dollar drop has crossed (0 if none)."""
    if drop <= 0:
        return 0
    return max(0, math.floor(drop / threshold_usd + 1e-9))
