"""Unit tests for pure drop-alert decisions (stdlib unittest)."""

from __future__ import annotations

import math
import unittest

from src.alerts import (
    AlertState,
    Decision,
    Quote,
    TickerConfig,
    dollar_drop,
    dollar_rise,
    evaluate_alert,
    fresh_state,
    pct_change,
)

DAY = "2026-09-04"
NEXT_DAY = "2026-09-05"
PREV = 100.0


def _cfg(
    symbol: str = "NVDA",
    threshold_pct: float = 3.0,
    mode: str = "once",
    threshold_unit: str = "pct",
    threshold_usd: float | None = None,
    direction: str = "down",
) -> TickerConfig:
    return TickerConfig(
        symbol=symbol,
        threshold_pct=threshold_pct,
        mode=mode,
        threshold_unit=threshold_unit,
        threshold_usd=threshold_usd,
        direction=direction,
    )


def _quote(price: float | None, prev_close: float | None = PREV) -> Quote:
    return Quote(price=price, prev_close=prev_close)


def _eval(
    price: float | None,
    *,
    mode: str = "once",
    threshold_pct: float = 3.0,
    threshold_unit: str = "pct",
    threshold_usd: float | None = None,
    direction: str = "down",
    state: AlertState | None = None,
    day_key: str = DAY,
    prev_close: float | None = PREV,
) -> Decision:
    return evaluate_alert(
        _quote(price, prev_close),
        _cfg(
            threshold_pct=threshold_pct,
            mode=mode,
            threshold_unit=threshold_unit,
            threshold_usd=threshold_usd,
            direction=direction,
        ),
        state,
        day_key,
    )


class TestOnceMode(unittest.TestCase):
    def test_first_fire_when_threshold_crossed(self) -> None:
        decision = _eval(97.0, mode="once")
        self.assertTrue(decision.should_alert)
        self.assertIsNone(decision.leg)
        self.assertTrue(decision.new_state.fired)
        self.assertEqual(decision.new_state.day_key, DAY)
        self.assertAlmostEqual(decision.pct_change, -0.03)

    def test_no_refire_same_day_even_if_it_falls_further(self) -> None:
        first = _eval(97.0, mode="once")
        second = _eval(90.0, mode="once", state=first.new_state)
        self.assertTrue(first.should_alert)
        self.assertFalse(second.should_alert)
        self.assertTrue(second.new_state.fired)
        self.assertEqual(second.new_state.day_key, DAY)

    def test_no_alert_above_threshold(self) -> None:
        decision = _eval(98.0, mode="once")  # -2%
        self.assertFalse(decision.should_alert)
        self.assertFalse(decision.new_state.fired)

    def test_gap_down_alerts_once_like_first_cross(self) -> None:
        decision = _eval(90.0, mode="once")  # opens already -10%
        self.assertTrue(decision.should_alert)
        self.assertTrue(decision.new_state.fired)
        again = _eval(85.0, mode="once", state=decision.new_state)
        self.assertFalse(again.should_alert)

    def test_new_day_resets_once_memory(self) -> None:
        first = _eval(97.0, mode="once", day_key=DAY)
        nxt = _eval(97.0, mode="once", state=first.new_state, day_key=NEXT_DAY)
        self.assertTrue(nxt.should_alert)
        self.assertEqual(nxt.new_state.day_key, NEXT_DAY)
        self.assertTrue(nxt.new_state.fired)


class TestLegsMode(unittest.TestCase):
    def test_first_leg_at_one_threshold(self) -> None:
        decision = _eval(97.0, mode="legs")
        self.assertTrue(decision.should_alert)
        self.assertEqual(decision.leg, 1)
        self.assertEqual(decision.new_state.last_leg, 1)

    def test_second_leg_on_additional_full_step(self) -> None:
        first = _eval(97.0, mode="legs")
        second = _eval(94.0, mode="legs", state=first.new_state)
        self.assertTrue(second.should_alert)
        self.assertEqual(second.leg, 2)
        self.assertEqual(second.new_state.last_leg, 2)

    def test_third_leg(self) -> None:
        state = _eval(94.0, mode="legs").new_state
        third = _eval(91.0, mode="legs", state=state)
        self.assertTrue(third.should_alert)
        self.assertEqual(third.leg, 3)

    def test_no_refire_at_same_leg(self) -> None:
        first = _eval(97.0, mode="legs")
        # Still between -3% and -6%.
        same = _eval(96.0, mode="legs", state=first.new_state)
        self.assertFalse(same.should_alert)
        self.assertEqual(same.new_state.last_leg, 1)

    def test_gap_down_fires_deepest_leg_once(self) -> None:
        decision = _eval(91.0, mode="legs")  # -9% → three 3% steps
        self.assertTrue(decision.should_alert)
        self.assertEqual(decision.leg, 3)
        self.assertEqual(decision.new_state.last_leg, 3)
        again = _eval(91.0, mode="legs", state=decision.new_state)
        self.assertFalse(again.should_alert)

    def test_bounce_does_not_alert_or_rewind_legs(self) -> None:
        down = _eval(94.0, mode="legs")  # -6% → leg 2
        bounce = _eval(98.0, mode="legs", state=down.new_state)  # back to -2%
        self.assertFalse(bounce.should_alert)
        self.assertEqual(bounce.new_state.last_leg, 2)
        still = _eval(94.0, mode="legs", state=bounce.new_state)
        self.assertFalse(still.should_alert)

    def test_further_drop_after_bounce_can_fire_next_leg(self) -> None:
        down = _eval(94.0, mode="legs")
        bounce = _eval(98.0, mode="legs", state=down.new_state)
        deeper = _eval(91.0, mode="legs", state=bounce.new_state)
        self.assertTrue(deeper.should_alert)
        self.assertEqual(deeper.leg, 3)


class TestMuteMode(unittest.TestCase):
    def test_mute_never_fires(self) -> None:
        decision = _eval(50.0, mode="mute")
        self.assertFalse(decision.should_alert)
        self.assertIsNone(decision.leg)
        self.assertFalse(decision.new_state.fired)
        self.assertEqual(decision.new_state.last_leg, 0)


class TestInvalidQuotes(unittest.TestCase):
    def test_none_price_no_alert_state_unchanged(self) -> None:
        prior = AlertState(day_key=DAY, fired=True, last_leg=2)
        decision = _eval(None, mode="once", state=prior)
        self.assertFalse(decision.should_alert)
        self.assertIsNone(decision.pct_change)
        self.assertEqual(decision.new_state, prior)

    def test_none_prev_close_no_alert_state_unchanged(self) -> None:
        prior = fresh_state(DAY)
        decision = _eval(90.0, prev_close=None, state=prior)
        self.assertFalse(decision.should_alert)
        self.assertEqual(decision.new_state, prior)

    def test_zero_price_no_alert(self) -> None:
        prior = fresh_state(DAY)
        decision = _eval(0.0, state=prior)
        self.assertFalse(decision.should_alert)
        self.assertEqual(decision.new_state, prior)

    def test_zero_prev_close_no_alert(self) -> None:
        prior = fresh_state(DAY)
        decision = _eval(90.0, prev_close=0.0, state=prior)
        self.assertFalse(decision.should_alert)
        self.assertEqual(decision.new_state, prior)

    def test_nan_price_no_alert(self) -> None:
        prior = fresh_state(DAY)
        decision = _eval(float("nan"), state=prior)
        self.assertFalse(decision.should_alert)
        self.assertEqual(decision.new_state, prior)

    def test_invalid_quote_does_not_reset_day(self) -> None:
        prior = AlertState(day_key=DAY, fired=True, last_leg=1)
        decision = evaluate_alert(
            _quote(None),
            _cfg(mode="once"),
            prior,
            NEXT_DAY,
        )
        self.assertFalse(decision.should_alert)
        self.assertEqual(decision.new_state, prior)


class TestBounceAndBoundary(unittest.TestCase):
    def test_exact_threshold_boundary_fires(self) -> None:
        decision = _eval(97.0, mode="once", threshold_pct=3.0)
        self.assertTrue(decision.should_alert)
        legs = _eval(97.0, mode="legs", threshold_pct=3.0)
        self.assertTrue(legs.should_alert)
        self.assertEqual(legs.leg, 1)

    def test_just_inside_threshold_does_not_fire(self) -> None:
        # -2.99% is not enough for a 3% threshold.
        decision = _eval(97.01, mode="once", threshold_pct=3.0)
        self.assertFalse(decision.should_alert)
        self.assertLess(-decision.pct_change * 100, 3.0)

    def test_once_bounce_up_does_not_alert(self) -> None:
        first = _eval(97.0, mode="once")
        bounce = _eval(101.0, mode="once", state=first.new_state)
        self.assertFalse(bounce.should_alert)
        self.assertTrue(bounce.new_state.fired)

    def test_up_day_never_alerts(self) -> None:
        decision = _eval(105.0, mode="once")
        self.assertFalse(decision.should_alert)
        legs = _eval(105.0, mode="legs")
        self.assertFalse(legs.should_alert)
        self.assertEqual(legs.new_state.last_leg, 0)


class TestHelpers(unittest.TestCase):
    def test_pct_change_formula(self) -> None:
        self.assertAlmostEqual(pct_change(_quote(97.0, 100.0)), -0.03)
        self.assertIsNone(pct_change(_quote(None, 100.0)))
        self.assertIsNone(pct_change(_quote(90.0, 0.0)))

    def test_fresh_state(self) -> None:
        state = fresh_state(DAY)
        self.assertEqual(state.day_key, DAY)
        self.assertFalse(state.fired)
        self.assertEqual(state.last_leg, 0)
        self.assertFalse(state.fired_up)
        self.assertEqual(state.last_leg_up, 0)

    def test_dollar_rise_formula(self) -> None:
        self.assertAlmostEqual(dollar_rise(_quote(105.0, 100.0)), 5.0)
        self.assertAlmostEqual(dollar_rise(_quote(97.0, 100.0)), -3.0)
        self.assertIsNone(dollar_rise(_quote(None, 100.0)))
        self.assertIsNone(dollar_rise(_quote(90.0, 0.0)))

    def test_non_finite_threshold_does_not_alert(self) -> None:
        decision = _eval(90.0, threshold_pct=math.nan)
        self.assertFalse(decision.should_alert)

    def test_dollar_drop_formula(self) -> None:
        self.assertAlmostEqual(dollar_drop(_quote(97.0, 100.0)), 3.0)
        self.assertAlmostEqual(dollar_drop(_quote(105.0, 100.0)), -5.0)
        self.assertIsNone(dollar_drop(_quote(None, 100.0)))
        self.assertIsNone(dollar_drop(_quote(90.0, 0.0)))


class TestUsdOnceMode(unittest.TestCase):
    def test_first_fire_when_dollar_threshold_crossed(self) -> None:
        decision = _eval(95.0, mode="once", threshold_unit="usd", threshold_usd=5.0)
        self.assertTrue(decision.should_alert)
        self.assertIsNone(decision.leg)
        self.assertTrue(decision.new_state.fired)
        self.assertAlmostEqual(decision.dollar_drop, 5.0)
        self.assertAlmostEqual(decision.pct_change, -0.05)

    def test_no_refire_same_day_even_if_it_falls_further(self) -> None:
        first = _eval(95.0, mode="once", threshold_unit="usd", threshold_usd=5.0)
        second = _eval(90.0, mode="once", threshold_unit="usd", threshold_usd=5.0, state=first.new_state)
        self.assertTrue(first.should_alert)
        self.assertFalse(second.should_alert)
        self.assertTrue(second.new_state.fired)

    def test_no_alert_when_drop_is_short(self) -> None:
        decision = _eval(96.0, mode="once", threshold_unit="usd", threshold_usd=5.0)
        self.assertFalse(decision.should_alert)
        self.assertFalse(decision.new_state.fired)
        self.assertAlmostEqual(decision.dollar_drop, 4.0)

    def test_gap_down_alerts_once(self) -> None:
        decision = _eval(85.0, mode="once", threshold_unit="usd", threshold_usd=5.0)
        self.assertTrue(decision.should_alert)
        self.assertTrue(decision.new_state.fired)
        again = _eval(80.0, mode="once", threshold_unit="usd", threshold_usd=5.0, state=decision.new_state)
        self.assertFalse(again.should_alert)

    def test_new_day_resets_once_memory(self) -> None:
        first = _eval(95.0, mode="once", threshold_unit="usd", threshold_usd=5.0, day_key=DAY)
        nxt = _eval(
            95.0,
            mode="once",
            threshold_unit="usd",
            threshold_usd=5.0,
            state=first.new_state,
            day_key=NEXT_DAY,
        )
        self.assertTrue(nxt.should_alert)
        self.assertEqual(nxt.new_state.day_key, NEXT_DAY)

    def test_pct_config_does_not_use_usd_threshold(self) -> None:
        # -2% is $2; usd $1 would fire, but unit is pct at 3%.
        decision = _eval(
            98.0,
            mode="once",
            threshold_pct=3.0,
            threshold_unit="pct",
            threshold_usd=1.0,
        )
        self.assertFalse(decision.should_alert)


class TestUsdLegsMode(unittest.TestCase):
    def test_first_leg_at_one_threshold(self) -> None:
        decision = _eval(95.0, mode="legs", threshold_unit="usd", threshold_usd=5.0)
        self.assertTrue(decision.should_alert)
        self.assertEqual(decision.leg, 1)
        self.assertEqual(decision.new_state.last_leg, 1)

    def test_second_leg_on_additional_full_step(self) -> None:
        first = _eval(95.0, mode="legs", threshold_unit="usd", threshold_usd=5.0)
        second = _eval(90.0, mode="legs", threshold_unit="usd", threshold_usd=5.0, state=first.new_state)
        self.assertTrue(second.should_alert)
        self.assertEqual(second.leg, 2)
        self.assertEqual(second.new_state.last_leg, 2)

    def test_third_leg(self) -> None:
        state = _eval(90.0, mode="legs", threshold_unit="usd", threshold_usd=5.0).new_state
        third = _eval(85.0, mode="legs", threshold_unit="usd", threshold_usd=5.0, state=state)
        self.assertTrue(third.should_alert)
        self.assertEqual(third.leg, 3)

    def test_no_refire_at_same_leg(self) -> None:
        first = _eval(95.0, mode="legs", threshold_unit="usd", threshold_usd=5.0)
        same = _eval(93.0, mode="legs", threshold_unit="usd", threshold_usd=5.0, state=first.new_state)
        self.assertFalse(same.should_alert)
        self.assertEqual(same.new_state.last_leg, 1)

    def test_gap_down_fires_deepest_leg_once(self) -> None:
        decision = _eval(85.0, mode="legs", threshold_unit="usd", threshold_usd=5.0)
        self.assertTrue(decision.should_alert)
        self.assertEqual(decision.leg, 3)
        again = _eval(85.0, mode="legs", threshold_unit="usd", threshold_usd=5.0, state=decision.new_state)
        self.assertFalse(again.should_alert)

    def test_bounce_does_not_alert_or_rewind_legs(self) -> None:
        down = _eval(90.0, mode="legs", threshold_unit="usd", threshold_usd=5.0)
        bounce = _eval(98.0, mode="legs", threshold_unit="usd", threshold_usd=5.0, state=down.new_state)
        self.assertFalse(bounce.should_alert)
        self.assertEqual(bounce.new_state.last_leg, 2)
        still = _eval(90.0, mode="legs", threshold_unit="usd", threshold_usd=5.0, state=bounce.new_state)
        self.assertFalse(still.should_alert)

    def test_further_drop_after_bounce_can_fire_next_leg(self) -> None:
        down = _eval(90.0, mode="legs", threshold_unit="usd", threshold_usd=5.0)
        bounce = _eval(98.0, mode="legs", threshold_unit="usd", threshold_usd=5.0, state=down.new_state)
        deeper = _eval(85.0, mode="legs", threshold_unit="usd", threshold_usd=5.0, state=bounce.new_state)
        self.assertTrue(deeper.should_alert)
        self.assertEqual(deeper.leg, 3)


class TestUsdMuteMode(unittest.TestCase):
    def test_mute_never_fires(self) -> None:
        decision = _eval(50.0, mode="mute", threshold_unit="usd", threshold_usd=5.0)
        self.assertFalse(decision.should_alert)
        self.assertIsNone(decision.leg)
        self.assertFalse(decision.new_state.fired)
        self.assertEqual(decision.new_state.last_leg, 0)
        self.assertAlmostEqual(decision.dollar_drop, 50.0)


class TestUsdInvalidAndBoundary(unittest.TestCase):
    def test_exact_dollar_threshold_fires(self) -> None:
        decision = _eval(95.0, mode="once", threshold_unit="usd", threshold_usd=5.0)
        self.assertTrue(decision.should_alert)
        legs = _eval(95.0, mode="legs", threshold_unit="usd", threshold_usd=5.0)
        self.assertTrue(legs.should_alert)
        self.assertEqual(legs.leg, 1)

    def test_just_inside_dollar_threshold_does_not_fire(self) -> None:
        decision = _eval(95.01, mode="once", threshold_unit="usd", threshold_usd=5.0)
        self.assertFalse(decision.should_alert)
        self.assertLess(decision.dollar_drop, 5.0)

    def test_up_day_never_alerts(self) -> None:
        decision = _eval(105.0, mode="once", threshold_unit="usd", threshold_usd=5.0)
        self.assertFalse(decision.should_alert)
        self.assertAlmostEqual(decision.dollar_drop, -5.0)
        legs = _eval(105.0, mode="legs", threshold_unit="usd", threshold_usd=5.0)
        self.assertFalse(legs.should_alert)
        self.assertEqual(legs.new_state.last_leg, 0)

    def test_missing_usd_threshold_does_not_alert(self) -> None:
        prior = AlertState(day_key=DAY, fired=True, last_leg=2)
        decision = _eval(90.0, mode="once", threshold_unit="usd", threshold_usd=None, state=prior)
        self.assertFalse(decision.should_alert)
        self.assertEqual(decision.new_state, prior)

    def test_zero_usd_threshold_does_not_alert(self) -> None:
        decision = _eval(90.0, mode="once", threshold_unit="usd", threshold_usd=0.0)
        self.assertFalse(decision.should_alert)

    def test_negative_usd_threshold_does_not_alert(self) -> None:
        decision = _eval(90.0, mode="once", threshold_unit="usd", threshold_usd=-5.0)
        self.assertFalse(decision.should_alert)

    def test_nan_usd_threshold_does_not_alert(self) -> None:
        decision = _eval(90.0, mode="once", threshold_unit="usd", threshold_usd=math.nan)
        self.assertFalse(decision.should_alert)

    def test_invalid_quote_leaves_usd_state_unchanged(self) -> None:
        prior = AlertState(day_key=DAY, fired=True, last_leg=2)
        decision = _eval(
            None, mode="once", threshold_unit="usd", threshold_usd=5.0, state=prior
        )
        self.assertFalse(decision.should_alert)
        self.assertIsNone(decision.dollar_drop)
        self.assertEqual(decision.new_state, prior)

    def test_unknown_unit_falls_back_to_pct(self) -> None:
        decision = _eval(
            97.0,
            mode="once",
            threshold_pct=3.0,
            threshold_unit="nope",
            threshold_usd=50.0,
        )
        self.assertTrue(decision.should_alert)


class TestUpOnceMode(unittest.TestCase):
    def test_first_fire_when_threshold_crossed(self) -> None:
        decision = _eval(103.0, mode="once", direction="up")
        self.assertTrue(decision.should_alert)
        self.assertEqual(decision.side, "up")
        self.assertIsNone(decision.leg)
        self.assertTrue(decision.new_state.fired_up)
        self.assertFalse(decision.new_state.fired)
        self.assertAlmostEqual(decision.pct_change, 0.03)

    def test_no_refire_same_day_even_if_it_rises_further(self) -> None:
        first = _eval(103.0, mode="once", direction="up")
        second = _eval(110.0, mode="once", direction="up", state=first.new_state)
        self.assertTrue(first.should_alert)
        self.assertFalse(second.should_alert)
        self.assertTrue(second.new_state.fired_up)

    def test_no_alert_below_threshold(self) -> None:
        decision = _eval(102.0, mode="once", direction="up")  # +2%
        self.assertFalse(decision.should_alert)
        self.assertFalse(decision.new_state.fired_up)

    def test_gap_up_alerts_once(self) -> None:
        decision = _eval(110.0, mode="once", direction="up")
        self.assertTrue(decision.should_alert)
        self.assertTrue(decision.new_state.fired_up)
        again = _eval(115.0, mode="once", direction="up", state=decision.new_state)
        self.assertFalse(again.should_alert)

    def test_new_day_resets_once_memory(self) -> None:
        first = _eval(103.0, mode="once", direction="up", day_key=DAY)
        nxt = _eval(
            103.0, mode="once", direction="up", state=first.new_state, day_key=NEXT_DAY
        )
        self.assertTrue(nxt.should_alert)
        self.assertEqual(nxt.new_state.day_key, NEXT_DAY)
        self.assertTrue(nxt.new_state.fired_up)

    def test_down_day_never_alerts(self) -> None:
        decision = _eval(90.0, mode="once", direction="up")
        self.assertFalse(decision.should_alert)
        self.assertFalse(decision.new_state.fired_up)

    def test_rise_alias_is_up(self) -> None:
        decision = _eval(103.0, mode="once", direction="rise")
        self.assertTrue(decision.should_alert)
        self.assertEqual(decision.side, "up")


class TestUpLegsMode(unittest.TestCase):
    def test_first_leg_at_one_threshold(self) -> None:
        decision = _eval(103.0, mode="legs", direction="up")
        self.assertTrue(decision.should_alert)
        self.assertEqual(decision.leg, 1)
        self.assertEqual(decision.side, "up")
        self.assertEqual(decision.new_state.last_leg_up, 1)
        self.assertEqual(decision.new_state.last_leg, 0)

    def test_second_leg_on_additional_full_step(self) -> None:
        first = _eval(103.0, mode="legs", direction="up")
        second = _eval(106.0, mode="legs", direction="up", state=first.new_state)
        self.assertTrue(second.should_alert)
        self.assertEqual(second.leg, 2)
        self.assertEqual(second.new_state.last_leg_up, 2)

    def test_third_leg(self) -> None:
        state = _eval(106.0, mode="legs", direction="up").new_state
        third = _eval(109.0, mode="legs", direction="up", state=state)
        self.assertTrue(third.should_alert)
        self.assertEqual(third.leg, 3)

    def test_no_refire_at_same_leg(self) -> None:
        first = _eval(103.0, mode="legs", direction="up")
        same = _eval(104.0, mode="legs", direction="up", state=first.new_state)
        self.assertFalse(same.should_alert)
        self.assertEqual(same.new_state.last_leg_up, 1)

    def test_gap_up_fires_highest_leg_once(self) -> None:
        decision = _eval(109.0, mode="legs", direction="up")  # +9% → three 3% steps
        self.assertTrue(decision.should_alert)
        self.assertEqual(decision.leg, 3)
        again = _eval(109.0, mode="legs", direction="up", state=decision.new_state)
        self.assertFalse(again.should_alert)

    def test_bounce_down_does_not_alert_or_rewind_legs(self) -> None:
        up = _eval(106.0, mode="legs", direction="up")  # +6% → leg 2
        bounce = _eval(102.0, mode="legs", direction="up", state=up.new_state)  # +2%
        self.assertFalse(bounce.should_alert)
        self.assertEqual(bounce.new_state.last_leg_up, 2)
        still = _eval(106.0, mode="legs", direction="up", state=bounce.new_state)
        self.assertFalse(still.should_alert)

    def test_further_rise_after_bounce_can_fire_next_leg(self) -> None:
        up = _eval(106.0, mode="legs", direction="up")
        bounce = _eval(102.0, mode="legs", direction="up", state=up.new_state)
        higher = _eval(109.0, mode="legs", direction="up", state=bounce.new_state)
        self.assertTrue(higher.should_alert)
        self.assertEqual(higher.leg, 3)


class TestUpMuteAndBoundary(unittest.TestCase):
    def test_mute_never_fires(self) -> None:
        decision = _eval(150.0, mode="mute", direction="up")
        self.assertFalse(decision.should_alert)
        self.assertFalse(decision.new_state.fired_up)
        self.assertEqual(decision.new_state.last_leg_up, 0)

    def test_exact_threshold_boundary_fires(self) -> None:
        decision = _eval(103.0, mode="once", direction="up", threshold_pct=3.0)
        self.assertTrue(decision.should_alert)
        legs = _eval(103.0, mode="legs", direction="up", threshold_pct=3.0)
        self.assertTrue(legs.should_alert)
        self.assertEqual(legs.leg, 1)

    def test_just_inside_threshold_does_not_fire(self) -> None:
        decision = _eval(102.99, mode="once", direction="up", threshold_pct=3.0)
        self.assertFalse(decision.should_alert)
        self.assertLess(decision.pct_change * 100, 3.0)

    def test_once_bounce_down_does_not_alert(self) -> None:
        first = _eval(103.0, mode="once", direction="up")
        bounce = _eval(97.0, mode="once", direction="up", state=first.new_state)
        self.assertFalse(bounce.should_alert)
        self.assertTrue(bounce.new_state.fired_up)


class TestUsdUp(unittest.TestCase):
    def test_once_fires_on_dollar_rise(self) -> None:
        decision = _eval(
            105.0, mode="once", threshold_unit="usd", threshold_usd=5.0, direction="up"
        )
        self.assertTrue(decision.should_alert)
        self.assertEqual(decision.side, "up")
        self.assertTrue(decision.new_state.fired_up)
        self.assertAlmostEqual(decision.dollar_drop, -5.0)

    def test_no_alert_when_rise_is_short(self) -> None:
        decision = _eval(
            104.0, mode="once", threshold_unit="usd", threshold_usd=5.0, direction="up"
        )
        self.assertFalse(decision.should_alert)
        self.assertAlmostEqual(decision.dollar_drop, -4.0)

    def test_legs_and_bounce(self) -> None:
        first = _eval(
            105.0, mode="legs", threshold_unit="usd", threshold_usd=5.0, direction="up"
        )
        second = _eval(
            110.0,
            mode="legs",
            threshold_unit="usd",
            threshold_usd=5.0,
            direction="up",
            state=first.new_state,
        )
        self.assertEqual(first.leg, 1)
        self.assertEqual(second.leg, 2)
        bounce = _eval(
            102.0,
            mode="legs",
            threshold_unit="usd",
            threshold_usd=5.0,
            direction="up",
            state=second.new_state,
        )
        self.assertFalse(bounce.should_alert)
        self.assertEqual(bounce.new_state.last_leg_up, 2)

    def test_down_day_never_alerts(self) -> None:
        decision = _eval(
            90.0, mode="once", threshold_unit="usd", threshold_usd=5.0, direction="up"
        )
        self.assertFalse(decision.should_alert)


class TestBothDirection(unittest.TestCase):
    def test_down_once_then_up_once_same_day(self) -> None:
        down = _eval(97.0, mode="once", direction="both")
        self.assertTrue(down.should_alert)
        self.assertEqual(down.side, "down")
        self.assertTrue(down.new_state.fired)
        self.assertFalse(down.new_state.fired_up)

        up = _eval(103.0, mode="once", direction="both", state=down.new_state)
        self.assertTrue(up.should_alert)
        self.assertEqual(up.side, "up")
        self.assertTrue(up.new_state.fired)
        self.assertTrue(up.new_state.fired_up)

        again = _eval(110.0, mode="once", direction="both", state=up.new_state)
        self.assertFalse(again.should_alert)
        self.assertTrue(again.new_state.fired)
        self.assertTrue(again.new_state.fired_up)

    def test_up_once_then_down_once_same_day(self) -> None:
        up = _eval(103.0, mode="once", direction="both")
        down = _eval(97.0, mode="once", direction="both", state=up.new_state)
        self.assertTrue(up.should_alert)
        self.assertEqual(up.side, "up")
        self.assertTrue(down.should_alert)
        self.assertEqual(down.side, "down")

    def test_legs_are_independent(self) -> None:
        down = _eval(94.0, mode="legs", direction="both")  # -6% → down leg 2
        self.assertEqual(down.leg, 2)
        self.assertEqual(down.side, "down")
        self.assertEqual(down.new_state.last_leg, 2)
        self.assertEqual(down.new_state.last_leg_up, 0)

        up = _eval(106.0, mode="legs", direction="both", state=down.new_state)
        self.assertTrue(up.should_alert)
        self.assertEqual(up.leg, 2)
        self.assertEqual(up.side, "up")
        self.assertEqual(up.new_state.last_leg, 2)
        self.assertEqual(up.new_state.last_leg_up, 2)

    def test_default_direction_is_down_only(self) -> None:
        decision = _eval(105.0, mode="once")
        self.assertFalse(decision.should_alert)
        self.assertFalse(decision.new_state.fired_up)

    def test_unknown_direction_falls_back_to_down(self) -> None:
        decision = _eval(97.0, mode="once", direction="sideways")
        self.assertTrue(decision.should_alert)
        self.assertEqual(decision.side, "down")
        rise = _eval(103.0, mode="once", direction="sideways")
        self.assertFalse(rise.should_alert)

    def test_mute_both_never_fires(self) -> None:
        down = _eval(50.0, mode="mute", direction="both")
        up = _eval(150.0, mode="mute", direction="both")
        self.assertFalse(down.should_alert)
        self.assertFalse(up.should_alert)


class TestUsdBoth(unittest.TestCase):
    def test_down_and_up_once_same_day(self) -> None:
        down = _eval(
            95.0, mode="once", threshold_unit="usd", threshold_usd=5.0, direction="both"
        )
        up = _eval(
            105.0,
            mode="once",
            threshold_unit="usd",
            threshold_usd=5.0,
            direction="both",
            state=down.new_state,
        )
        self.assertTrue(down.should_alert)
        self.assertEqual(down.side, "down")
        self.assertTrue(up.should_alert)
        self.assertEqual(up.side, "up")
        self.assertTrue(up.new_state.fired)
        self.assertTrue(up.new_state.fired_up)


if __name__ == "__main__":
    unittest.main()
