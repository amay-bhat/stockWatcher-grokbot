"""Tests for scheduler decision helpers and the check loop (no live network)."""

from __future__ import annotations

from datetime import datetime, timedelta
import io
import os
import threading
import unittest
from unittest.mock import patch

from src.bot import ET
from src.calendar import next_open_after
from src.main import main as cli_main
from src.scheduler import (
    FAILURE_WARN_STREAK,
    FAILURE_WARN_TEXT,
    SchedulerDecision,
    check_interval_minutes,
    decide,
    interruptible_sleep,
    run_scheduler,
    run_serve,
)


def _et(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ET)


class TestCheckIntervalMinutes(unittest.TestCase):
    def test_defaults_to_five(self) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if key != "CHECK_INTERVAL_MINUTES"
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(check_interval_minutes(), 5)

    def test_reads_env(self) -> None:
        with patch.dict(os.environ, {"CHECK_INTERVAL_MINUTES": "15"}):
            self.assertEqual(check_interval_minutes(), 15)

    def test_rejects_zero_and_non_int(self) -> None:
        with self.assertRaises(RuntimeError):
            check_interval_minutes("0")
        with self.assertRaises(RuntimeError):
            check_interval_minutes("nope")


class TestDecide(unittest.TestCase):
    interval = timedelta(minutes=5)

    def test_open_runs_and_sleeps_interval(self) -> None:
        now = _et(2026, 9, 4, 10, 0)
        decision = decide(now, self.interval)
        self.assertEqual(
            decision,
            SchedulerDecision(
                run_check=True,
                sleep_until=_et(2026, 9, 4, 10, 5),
                reason="open",
                day_key="2026-09-04",
            ),
        )

    def test_first_print_at_open(self) -> None:
        now = _et(2026, 9, 4, 9, 30)
        decision = decide(now, self.interval)
        self.assertTrue(decision.run_check)
        self.assertEqual(decision.day_key, "2026-09-04")
        self.assertEqual(decision.sleep_until, _et(2026, 9, 4, 9, 35))

    def test_near_close_sleeps_until_next_open(self) -> None:
        now = _et(2026, 9, 4, 15, 58)
        decision = decide(now, self.interval)
        self.assertTrue(decision.run_check)
        self.assertEqual(decision.sleep_until, _et(2026, 9, 8, 9, 30))

    def test_exactly_at_close_does_not_run(self) -> None:
        now = _et(2026, 9, 4, 16, 0)
        decision = decide(now, self.interval)
        self.assertFalse(decision.run_check)
        self.assertEqual(decision.reason, "after_close")
        self.assertEqual(decision.sleep_until, _et(2026, 9, 8, 9, 30))

    def test_weekend_sleeps_until_next_session(self) -> None:
        now = _et(2026, 9, 5, 12, 0)
        decision = decide(now, self.interval)
        self.assertFalse(decision.run_check)
        self.assertEqual(decision.reason, "weekend")
        self.assertEqual(decision.sleep_until, _et(2026, 9, 8, 9, 30))
        self.assertEqual(decision.day_key, "2026-09-08")

    def test_holiday_does_not_run(self) -> None:
        now = _et(2026, 9, 7, 10, 45)
        decision = decide(now, self.interval)
        self.assertFalse(decision.run_check)
        self.assertEqual(decision.reason, "holiday")
        self.assertEqual(decision.sleep_until, _et(2026, 9, 8, 9, 30))

    def test_before_open_sleeps_until_open(self) -> None:
        now = _et(2026, 9, 4, 8, 0)
        decision = decide(now, self.interval)
        self.assertFalse(decision.run_check)
        self.assertEqual(decision.reason, "before_open")
        self.assertEqual(decision.sleep_until, _et(2026, 9, 4, 9, 30))

    def test_half_day_open_and_close(self) -> None:
        open_decision = decide(_et(2026, 11, 27, 12, 56), self.interval)
        self.assertTrue(open_decision.run_check)
        self.assertEqual(open_decision.reason, "open_early_close")
        self.assertEqual(open_decision.sleep_until, _et(2026, 11, 30, 9, 30))

        closed = decide(_et(2026, 11, 27, 13, 0), self.interval)
        self.assertFalse(closed.run_check)
        self.assertEqual(closed.reason, "after_close")
        self.assertEqual(closed.sleep_until, _et(2026, 11, 30, 9, 30))

    def test_fresh_day_key_at_next_open(self) -> None:
        friday_close = decide(_et(2026, 9, 4, 16, 1), self.interval)
        monday_open = decide(_et(2026, 9, 8, 9, 30), self.interval)
        self.assertFalse(friday_close.run_check)
        self.assertEqual(friday_close.day_key, "2026-09-08")
        self.assertTrue(monday_open.run_check)
        self.assertEqual(monday_open.day_key, "2026-09-08")
        self.assertEqual(friday_close.day_key, monday_open.day_key)
        self.assertNotEqual(monday_open.day_key, "2026-09-04")


class TestInterruptibleSleep(unittest.TestCase):
    def test_stop_returns_immediately(self) -> None:
        stop = threading.Event()
        stop.set()
        started = datetime.now()
        interruptible_sleep(30.0, stop=stop)
        elapsed = (datetime.now() - started).total_seconds()
        self.assertLess(elapsed, 1.0)

    def test_sleeper_used_without_stop(self) -> None:
        slept: list[float] = []
        interruptible_sleep(2.5, sleeper=slept.append)
        self.assertEqual(slept, [2.5])


class TestRunSchedulerLoop(unittest.TestCase):
    def test_skips_check_when_closed(self) -> None:
        checks: list[datetime] = []
        slept: list[float] = []

        def check(*, now, **_kwargs):
            checks.append(now)
            return 0

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = run_scheduler(
                clock=lambda: _et(2026, 9, 5, 12, 0),
                sleep=slept.append,
                check=check,
                interval_minutes=5,
                max_cycles=1,
            )
        self.assertEqual(code, 0)
        self.assertEqual(checks, [])
        self.assertEqual(len(slept), 1)
        expected = (
            next_open_after(_et(2026, 9, 5, 12, 0)) - _et(2026, 9, 5, 12, 0)
        ).total_seconds()
        self.assertAlmostEqual(slept[0], expected, places=3)
        self.assertIn("market closed (weekend)", stdout.getvalue())

    def test_runs_check_when_open(self) -> None:
        checks: list[datetime] = []

        def check(*, now, **_kwargs):
            checks.append(now)
            return 0

        with patch("sys.stdout", io.StringIO()):
            code = run_scheduler(
                clock=lambda: _et(2026, 9, 4, 10, 0),
                sleep=lambda _s: None,
                check=check,
                interval_minutes=5,
                max_cycles=1,
            )
        self.assertEqual(code, 0)
        self.assertEqual(checks, [_et(2026, 9, 4, 10, 0)])

    def test_check_exception_does_not_crash(self) -> None:
        def boom(*, now, **_kwargs):
            raise RuntimeError("finnhub down")

        stderr = io.StringIO()
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", stderr):
            code = run_scheduler(
                clock=lambda: _et(2026, 9, 4, 10, 0),
                sleep=lambda _s: None,
                check=boom,
                interval_minutes=5,
                max_cycles=1,
            )
        self.assertEqual(code, 0)
        self.assertIn("finnhub down", stderr.getvalue())

    def test_three_consecutive_failures_send_warning(self) -> None:
        warnings: list[str] = []

        def boom(*, now, **_kwargs):
            return 1

        stderr = io.StringIO()
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", stderr):
            code = run_scheduler(
                clock=lambda: _et(2026, 9, 4, 10, 0),
                sleep=lambda _s: None,
                check=boom,
                warn=warnings.append,
                interval_minutes=5,
                max_cycles=FAILURE_WARN_STREAK,
            )
        self.assertEqual(code, 0)
        self.assertEqual(warnings, [FAILURE_WARN_TEXT])
        self.assertIn("3 consecutive", stderr.getvalue())

    def test_success_resets_failure_streak(self) -> None:
        warnings: list[str] = []
        results = iter([1, 0, 1, 1])

        def flaky(*, now, **_kwargs):
            return next(results)

        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            run_scheduler(
                clock=lambda: _et(2026, 9, 4, 10, 0),
                sleep=lambda _s: None,
                check=flaky,
                warn=warnings.append,
                interval_minutes=5,
                max_cycles=4,
            )
        self.assertEqual(warnings, [])


class TestRunServe(unittest.TestCase):
    def test_starts_bot_and_scheduler(self) -> None:
        started = {"bot": False, "sched": False}

        def fake_bot(*, stop, **_kwargs):
            started["bot"] = True
            stop.set()
            return 0

        def fake_sched(*, stop, **_kwargs):
            started["sched"] = True
            stop.wait(timeout=2)
            return 0

        env = {
            "FINNHUB_API_KEY": "k",
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_CHAT_ID": "1",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch("sys.stdout", io.StringIO()),
        ):
            code = run_serve(
                bot_fn=fake_bot,
                scheduler_fn=fake_sched,
                store=object(),
                provider=object(),
            )
        self.assertEqual(code, 0)
        self.assertTrue(started["bot"])
        self.assertTrue(started["sched"])

    def test_missing_env_fails_fast(self) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "FINNHUB_API_KEY"}
        }
        stderr = io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch("sys.stderr", stderr):
            code = run_serve()
        self.assertEqual(code, 1)
        self.assertIn("TELEGRAM_BOT_TOKEN", stderr.getvalue())


class TestCliServe(unittest.TestCase):
    def test_default_is_serve(self) -> None:
        with patch("src.scheduler.run_serve", return_value=0) as serve:
            self.assertEqual(cli_main([]), 0)
            serve.assert_called_once()

    def test_serve_flag(self) -> None:
        with patch("src.scheduler.run_serve", return_value=0) as serve:
            self.assertEqual(cli_main(["--serve"]), 0)
            serve.assert_called_once()

    def test_quotes_flag(self) -> None:
        with patch("src.main.print_quotes", return_value=0) as printer:
            self.assertEqual(cli_main(["--quotes"]), 0)
            printer.assert_called_once()

    def test_rejects_serve_and_bot_together(self) -> None:
        stderr = io.StringIO()
        with patch("sys.stderr", stderr), self.assertRaises(SystemExit):
            cli_main(["--serve", "--bot"])


if __name__ == "__main__":
    unittest.main()
