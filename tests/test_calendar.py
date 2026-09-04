"""Tests for the NYSE regular-session calendar (no I/O)."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import unittest

from src.calendar import (
    ET,
    SessionHours,
    closed_reason,
    is_half_day,
    is_holiday,
    is_session_open,
    is_trading_day,
    is_weekend,
    next_open_after,
    nyse_full_closures,
    nyse_half_days,
    session_hours,
    to_et,
    trading_day_key,
)


# Published NYSE cash-equity holidays / early closes (ICE / nyse.com).
NYSE_HOLIDAYS_2025 = frozenset(
    {
        date(2025, 1, 1),
        date(2025, 1, 20),
        date(2025, 2, 17),
        date(2025, 4, 18),
        date(2025, 5, 26),
        date(2025, 6, 19),
        date(2025, 7, 4),
        date(2025, 9, 1),
        date(2025, 11, 27),
        date(2025, 12, 25),
    }
)
NYSE_HALF_DAYS_2025 = frozenset(
    {date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)}
)
NYSE_HOLIDAYS_2026 = frozenset(
    {
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    }
)
NYSE_HALF_DAYS_2026 = frozenset({date(2026, 11, 27), date(2026, 12, 24)})
NYSE_HOLIDAYS_2027 = frozenset(
    {
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 3, 26),
        date(2027, 5, 31),
        date(2027, 6, 18),
        date(2027, 7, 5),
        date(2027, 9, 6),
        date(2027, 11, 25),
        date(2027, 12, 24),
    }
)
NYSE_HALF_DAYS_2027 = frozenset({date(2027, 11, 26)})
NYSE_HOLIDAYS_2028 = frozenset(
    {
        date(2028, 1, 17),
        date(2028, 2, 21),
        date(2028, 4, 14),
        date(2028, 5, 29),
        date(2028, 6, 19),
        date(2028, 7, 4),
        date(2028, 9, 4),
        date(2028, 11, 23),
        date(2028, 12, 25),
    }
)
NYSE_HALF_DAYS_2028 = frozenset({date(2028, 7, 3), date(2028, 11, 24)})


def _et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


class TestPublishedNyseCalendar(unittest.TestCase):
    def test_matches_published_2025_through_2028(self) -> None:
        self.assertEqual(nyse_full_closures(2025), NYSE_HOLIDAYS_2025)
        self.assertEqual(nyse_half_days(2025), NYSE_HALF_DAYS_2025)
        self.assertEqual(nyse_full_closures(2026), NYSE_HOLIDAYS_2026)
        self.assertEqual(nyse_half_days(2026), NYSE_HALF_DAYS_2026)
        self.assertEqual(nyse_full_closures(2027), NYSE_HOLIDAYS_2027)
        self.assertEqual(nyse_half_days(2027), NYSE_HALF_DAYS_2027)
        self.assertEqual(nyse_full_closures(2028), NYSE_HOLIDAYS_2028)
        self.assertEqual(nyse_half_days(2028), NYSE_HALF_DAYS_2028)

    def test_saturday_new_years_2028_is_not_observed(self) -> None:
        self.assertFalse(is_holiday(date(2028, 1, 1)))
        self.assertFalse(is_holiday(date(2027, 12, 31)))
        self.assertTrue(is_trading_day(date(2027, 12, 31)))


class TestWeekendHolidayHalfDay(unittest.TestCase):
    def test_weekend_is_closed(self) -> None:
        saturday = date(2026, 9, 5)
        sunday = date(2026, 9, 6)
        self.assertTrue(is_weekend(saturday))
        self.assertTrue(is_weekend(sunday))
        self.assertFalse(is_trading_day(saturday))
        self.assertFalse(is_trading_day(sunday))
        self.assertIsNone(session_hours(saturday))

    def test_labor_day_2026_is_closed(self) -> None:
        labor_day = date(2026, 9, 7)
        self.assertTrue(is_holiday(labor_day))
        self.assertFalse(is_trading_day(labor_day))
        self.assertIsNone(session_hours(labor_day))

    def test_good_friday_2026_is_closed(self) -> None:
        self.assertTrue(is_holiday(date(2026, 4, 3)))
        self.assertFalse(is_trading_day(date(2026, 4, 3)))

    def test_independence_day_observed_2026(self) -> None:
        self.assertTrue(is_holiday(date(2026, 7, 3)))
        self.assertFalse(is_half_day(date(2026, 7, 3)))
        self.assertTrue(is_trading_day(date(2026, 7, 2)))

    def test_regular_friday_is_full_session(self) -> None:
        friday = date(2026, 9, 4)
        hours = session_hours(friday)
        self.assertEqual(
            hours,
            SessionHours(open=time(9, 30), close=time(16, 0), early_close=False),
        )

    def test_day_after_thanksgiving_is_early_close(self) -> None:
        half = date(2026, 11, 27)
        self.assertTrue(is_half_day(half))
        self.assertTrue(is_trading_day(half))
        hours = session_hours(half)
        self.assertEqual(
            hours,
            SessionHours(open=time(9, 30), close=time(13, 0), early_close=True),
        )

    def test_christmas_eve_2026_is_early_close(self) -> None:
        self.assertTrue(is_half_day(date(2026, 12, 24)))
        self.assertTrue(is_holiday(date(2026, 12, 25)))


class TestSessionOpenWindow(unittest.TestCase):
    def test_regular_open_close_boundaries(self) -> None:
        self.assertFalse(is_session_open(_et(2026, 9, 4, 9, 29)))
        self.assertTrue(is_session_open(_et(2026, 9, 4, 9, 30)))
        self.assertTrue(is_session_open(_et(2026, 9, 4, 15, 59)))
        self.assertFalse(is_session_open(_et(2026, 9, 4, 16, 0)))
        self.assertEqual(closed_reason(_et(2026, 9, 4, 9, 29)), "before_open")
        self.assertEqual(closed_reason(_et(2026, 9, 4, 16, 0)), "after_close")

    def test_half_day_closes_at_13_00(self) -> None:
        self.assertTrue(is_session_open(_et(2026, 11, 27, 9, 30)))
        self.assertTrue(is_session_open(_et(2026, 11, 27, 12, 59)))
        self.assertFalse(is_session_open(_et(2026, 11, 27, 13, 0)))
        self.assertFalse(is_session_open(_et(2026, 11, 27, 15, 0)))

    def test_holiday_never_open(self) -> None:
        self.assertFalse(is_session_open(_et(2026, 9, 7, 12, 0)))
        self.assertEqual(closed_reason(_et(2026, 9, 7, 12, 0)), "holiday")

    def test_weekend_reason(self) -> None:
        self.assertEqual(closed_reason(_et(2026, 9, 5, 12, 0)), "weekend")

    def test_utc_instant_converted_to_et(self) -> None:
        # 2026-09-04 is EDT (UTC-4). 13:30 UTC = 09:30 ET, open.
        utc_open = datetime(2026, 9, 4, 13, 30, tzinfo=timezone.utc)
        utc_close = datetime(2026, 9, 4, 20, 0, tzinfo=timezone.utc)
        self.assertTrue(is_session_open(utc_open))
        self.assertFalse(is_session_open(utc_close))
        self.assertEqual(trading_day_key(utc_open), "2026-09-04")

    def test_naive_datetime_treated_as_et(self) -> None:
        naive = datetime(2026, 9, 4, 10, 0)
        self.assertEqual(to_et(naive).tzinfo, ET)
        self.assertTrue(is_session_open(naive))
        self.assertEqual(trading_day_key(naive), "2026-09-04")


class TestNextOpen(unittest.TestCase):
    def test_before_open_same_day(self) -> None:
        self.assertEqual(
            next_open_after(_et(2026, 9, 4, 8, 0)),
            _et(2026, 9, 4, 9, 30),
        )

    def test_during_session_skips_to_next_day(self) -> None:
        self.assertEqual(
            next_open_after(_et(2026, 9, 4, 10, 0)),
            _et(2026, 9, 8, 9, 30),  # Labor Day weekend
        )

    def test_after_close_skips_labor_day_weekend(self) -> None:
        self.assertEqual(
            next_open_after(_et(2026, 9, 4, 16, 0)),
            _et(2026, 9, 8, 9, 30),
        )

    def test_saturday_skips_labor_day(self) -> None:
        self.assertEqual(
            next_open_after(_et(2026, 9, 5, 12, 0)),
            _et(2026, 9, 8, 9, 30),
        )

    def test_exactly_at_open_is_not_next_open(self) -> None:
        self.assertEqual(
            next_open_after(_et(2026, 9, 4, 9, 30)),
            _et(2026, 9, 8, 9, 30),
        )


class TestTradingDayKey(unittest.TestCase):
    def test_et_date_of_the_session(self) -> None:
        self.assertEqual(trading_day_key(_et(2026, 9, 4, 9, 30)), "2026-09-04")
        # 00:30 UTC on Sep 5 is still Sep 4 evening in ET (EDT).
        utc = datetime(2026, 9, 5, 0, 30, tzinfo=timezone.utc)
        self.assertEqual(trading_day_key(utc), "2026-09-04")


if __name__ == "__main__":
    unittest.main()
