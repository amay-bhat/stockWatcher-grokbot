"""US equity regular-session calendar (NYSE hours, holidays, half-days).

No third-party market-calendar package: weekdays + Good Friday (Computus) +
observed federal-style NYSE closures, with early close 13:00 ET on known
half-days. Zoneinfo handles EST/EDT.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


@dataclass(frozen=True)
class SessionHours:
    """Regular-session window on a trading day (ET clock times)."""

    open: time
    close: time
    early_close: bool = False


def to_et(now: datetime) -> datetime:
    """Convert to America/New_York. Naive datetimes are treated as ET."""
    if now.tzinfo is None:
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def trading_day_key(now: datetime) -> str:
    """ISO date in ET. During a live session this is that session's day_key."""
    return to_et(now).date().isoformat()


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def is_holiday(d: date) -> bool:
    return d in nyse_full_closures(d.year)


def is_half_day(d: date) -> bool:
    return d in nyse_half_days(d.year)


def is_trading_day(d: date) -> bool:
    return session_hours(d) is not None


def session_hours(d: date) -> SessionHours | None:
    """Regular session hours in ET, or None if the market is shut all day."""
    if is_weekend(d) or is_holiday(d):
        return None
    if is_half_day(d):
        return SessionHours(open=REGULAR_OPEN, close=EARLY_CLOSE, early_close=True)
    return SessionHours(open=REGULAR_OPEN, close=REGULAR_CLOSE, early_close=False)


def is_session_open(now: datetime) -> bool:
    """True during the regular session: [9:30, close) America/New_York."""
    local = to_et(now)
    hours = session_hours(local.date())
    if hours is None:
        return False
    return hours.open <= local.time() < hours.close


def next_open_after(now: datetime) -> datetime:
    """Next regular-session open strictly after `now` (ET-aware)."""
    local = to_et(now)
    for i in range(0, 16):
        d = local.date() + timedelta(days=i)
        hours = session_hours(d)
        if hours is None:
            continue
        open_dt = datetime.combine(d, hours.open, tzinfo=ET)
        if open_dt > local:
            return open_dt
    raise RuntimeError("no NYSE regular session within 16 days")


def session_close_at(now: datetime) -> datetime | None:
    """Today's regular-session close in ET, or None if today is not a trading day."""
    local = to_et(now)
    hours = session_hours(local.date())
    if hours is None:
        return None
    return datetime.combine(local.date(), hours.close, tzinfo=ET)


def closed_reason(now: datetime) -> str:
    """Short label for why the regular session is not open."""
    local = to_et(now)
    d = local.date()
    if is_weekend(d):
        return "weekend"
    if is_holiday(d):
        return "holiday"
    hours = session_hours(d)
    if hours is None:
        return "closed"
    stamp = datetime.combine(d, hours.open, tzinfo=ET)
    if local < stamp:
        return "before_open"
    return "after_close"


@lru_cache(maxsize=32)
def nyse_full_closures(year: int) -> frozenset[date]:
    """Full-day NYSE closures for `year` (weekends not included)."""
    days: set[date] = set()
    new_years = _new_years_observed(year)
    if new_years is not None:
        days.add(new_years)
    days.add(_nth_weekday(year, 1, 0, 3))  # MLK — 3rd Monday
    days.add(_nth_weekday(year, 2, 0, 3))  # Washington's Birthday
    days.add(_easter_sunday(year) - timedelta(days=2))  # Good Friday
    days.add(_last_weekday(year, 5, 0))  # Memorial Day
    days.add(_observe(_date(year, 6, 19)))  # Juneteenth
    days.add(_observe(_date(year, 7, 4)))  # Independence Day
    days.add(_nth_weekday(year, 9, 0, 1))  # Labor Day
    days.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving
    days.add(_observe(_date(year, 12, 25)))  # Christmas
    return frozenset(days)


@lru_cache(maxsize=32)
def nyse_half_days(year: int) -> frozenset[date]:
    """Early-close dates (13:00 ET) for `year`. Never a weekend or full holiday."""
    closures = nyse_full_closures(year)
    days: set[date] = set()
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    friday = thanksgiving + timedelta(days=1)
    if friday not in closures and friday.weekday() < 5:
        days.add(friday)
    christmas_eve = _date(year, 12, 24)
    if christmas_eve.weekday() < 5 and christmas_eve not in closures:
        days.add(christmas_eve)
    july3 = _date(year, 7, 3)
    if july3.weekday() < 5 and july3 not in closures:
        days.add(july3)
    return frozenset(days)


def _date(year: int, month: int, day: int) -> date:
    return date(year, month, day)


def _new_years_observed(year: int) -> date | None:
    """NYSE: weekday Jan 1, or Monday if Sunday. Saturday is not observed."""
    d = date(year, 1, 1)
    if d.weekday() == 5:
        return None
    if d.weekday() == 6:
        return date(year, 1, 2)
    return d


def _observe(d: date) -> date:
    """Saturday → Friday, Sunday → Monday (typical NYSE observance)."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian Computus (Western Easter)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    el = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * el) // 451
    month, day = divmod(h + el - 7 * m + 114, 31)
    return date(year, month, day + 1)
