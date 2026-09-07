"""Pure drop/rise-alert decision logic (M2).

No I/O, no network, no clock reads. Callers pass `day_key` so daily reset
is explicit. Baseline is previous close:

    pct_change = (current - previous_close) / previous_close
    dollar_drop = previous_close - current   # positive when down
    dollar_rise = current - previous_close   # positive when up

Each ticker has a unit (`pct` default, or `usd`) and a direction
(`down` default, or `up` / `both`):

- pct down: fire when `pct_change <= -threshold_pct / 100`
- pct up: fire when `pct_change >= +threshold_pct / 100`
- usd down: fire when `dollar_drop >= threshold_usd`
- usd up: fire when `dollar_rise >= threshold_usd`
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Literal

AlertMode = Literal["once", "legs", "mute"]
AlertDirection = Literal["down", "up", "both"]
AlertSide = Literal["down", "up"]
ThresholdUnit = Literal["pct", "usd"]
VALID_MODES: frozenset[str] = frozenset({"once", "legs", "mute"})
VALID_DIRECTIONS: frozenset[str] = frozenset({"down", "up", "both"})
VALID_UNITS: frozenset[str] = frozenset({"pct", "usd"})
DIRECTION_ALIASES: dict[str, AlertDirection] = {"rise": "up", "drop": "down"}


@dataclass(frozen=True)
class Quote:
    """Latest print vs previous close. Either field may be missing."""

    price: float | None
    prev_close: float | None


@dataclass(frozen=True)
class TickerConfig:
    """Per-ticker alert settings. `mode` defaults to once; unit to pct; direction to down."""

    symbol: str
    threshold_pct: float
    mode: AlertMode = "once"
    threshold_unit: ThresholdUnit = "pct"
    threshold_usd: float | None = None
    direction: AlertDirection = "down"


@dataclass(frozen=True)
class AlertState:
    """What was already notified on one trading day (`day_key`).

    Down and up are independent so a `both` ticker can fire once each way.
    `fired` / `last_leg` are the drop side. `fired_up` / `last_leg_up` are
    the rise side. `mute` ignores all four.
    """

    day_key: str
    fired: bool = False
    last_leg: int = 0
    fired_up: bool = False
    last_leg_up: int = 0


@dataclass(frozen=True)
class Decision:
    """Whether to notify, plus the state to persist for the rest of the day."""

    should_alert: bool
    new_state: AlertState
    leg: int | None = None
    pct_change: float | None = None
    dollar_drop: float | None = None
    side: AlertSide | None = None


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


def dollar_rise(quote: Quote) -> float | None:
    """current - previous_close (positive when up), or None if unusable."""
    drop = dollar_drop(quote)
    if drop is None:
        return None
    return -drop


def evaluate_alert(
    quote: Quote,
    config: TickerConfig,
    state: AlertState | None,
    day_key: str,
) -> Decision:
    """Decide whether this quote should fire a drop and/or rise alert.

    `day_key` identifies the trading day (caller-supplied; this module never
    reads the clock). A new `day_key` resets notification memory. Invalid
    quotes never alert and leave `state` unchanged.

    `both` evaluates down and up independently. A print is on one side of
    previous close, so at most one side fires in a single call; a later
    reverse through the other threshold can still fire the same day.
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

    direction = _normalize_direction(config.direction)
    watch_down = direction in ("down", "both")
    watch_up = direction in ("up", "both")

    new_state = working
    down_decision: Decision | None = None
    up_decision: Decision | None = None

    if watch_down:
        down_steps = _steps_for_config(change, drop, config, side="down")
        if down_steps is not None:
            if mode == "once":
                down_decision = _decide_once(working, change, drop, down_steps, "down")
            else:
                down_decision = _decide_legs(working, change, drop, down_steps, "down")
            new_state = replace(
                new_state,
                fired=down_decision.new_state.fired,
                last_leg=down_decision.new_state.last_leg,
            )

    if watch_up:
        up_steps = _steps_for_config(change, drop, config, side="up")
        if up_steps is not None:
            if mode == "once":
                up_decision = _decide_once(working, change, drop, up_steps, "up")
            else:
                up_decision = _decide_legs(working, change, drop, up_steps, "up")
            new_state = replace(
                new_state,
                fired_up=up_decision.new_state.fired_up,
                last_leg_up=up_decision.new_state.last_leg_up,
            )

    chosen = None
    if down_decision is not None and down_decision.should_alert:
        chosen = down_decision
    elif up_decision is not None and up_decision.should_alert:
        chosen = up_decision

    if chosen is None:
        return Decision(
            should_alert=False,
            new_state=new_state,
            pct_change=change,
            dollar_drop=drop,
        )
    return replace(chosen, new_state=new_state)


def _decide_once(
    state: AlertState, change: float, drop: float, steps: int, side: AlertSide
) -> Decision:
    already = state.fired if side == "down" else state.fired_up
    if steps < 1 or already:
        return Decision(
            should_alert=False,
            new_state=state,
            pct_change=change,
            dollar_drop=drop,
            side=side,
        )
    if side == "down":
        new_state = replace(state, fired=True)
    else:
        new_state = replace(state, fired_up=True)
    return Decision(
        should_alert=True,
        new_state=new_state,
        pct_change=change,
        dollar_drop=drop,
        side=side,
    )


def _decide_legs(
    state: AlertState, change: float, drop: float, steps: int, side: AlertSide
) -> Decision:
    last = state.last_leg if side == "down" else state.last_leg_up
    if steps <= last:
        return Decision(
            should_alert=False,
            new_state=state,
            pct_change=change,
            dollar_drop=drop,
            side=side,
        )
    if side == "down":
        new_state = replace(state, last_leg=steps)
    else:
        new_state = replace(state, last_leg_up=steps)
    return Decision(
        should_alert=True,
        new_state=new_state,
        leg=steps,
        pct_change=change,
        dollar_drop=drop,
        side=side,
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


def _normalize_direction(direction: str) -> str:
    normalized = (direction or "down").strip().lower()
    normalized = DIRECTION_ALIASES.get(normalized, normalized)
    if normalized in VALID_DIRECTIONS:
        return normalized
    return "down"


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


def _steps_for_config(
    change: float, drop: float, config: TickerConfig, side: AlertSide
) -> int | None:
    """Full threshold steps crossed on `side`, or None if the threshold is invalid."""
    unit = _normalize_unit(config.threshold_unit)
    if unit == "usd":
        threshold = config.threshold_usd
        if not _positive_threshold(threshold):
            return None
        amount = float(threshold)
        if side == "up":
            return _steps_up_usd(-drop, amount)
        return _steps_down_usd(drop, amount)
    threshold_pct = config.threshold_pct
    if not _positive_threshold(threshold_pct):
        return None
    if side == "up":
        return _steps_up(change, threshold_pct)
    return _steps_down(change, threshold_pct)


def _steps_down(change: float, threshold_pct: float) -> int:
    """How many full threshold steps the drop has crossed (0 if none).

    Exact threshold (e.g. -3.0% with threshold 3.0) counts as one step.
    A small epsilon absorbs floating-point noise at the boundary.
    """
    drop_pct = -change * 100.0
    return max(0, math.floor(drop_pct / threshold_pct + 1e-9))


def _steps_up(change: float, threshold_pct: float) -> int:
    """How many full threshold steps the rise has crossed (0 if none)."""
    rise_pct = change * 100.0
    return max(0, math.floor(rise_pct / threshold_pct + 1e-9))


def _steps_down_usd(drop: float, threshold_usd: float) -> int:
    """How many full `threshold_usd` steps the dollar drop has crossed (0 if none)."""
    if drop <= 0:
        return 0
    return max(0, math.floor(drop / threshold_usd + 1e-9))


def _steps_up_usd(rise: float, threshold_usd: float) -> int:
    """How many full `threshold_usd` steps the dollar rise has crossed (0 if none)."""
    if rise <= 0:
        return 0
    return max(0, math.floor(rise / threshold_usd + 1e-9))
