"""Tests for the one-shot watchlist check (mocked quotes, no Telegram network)."""

from __future__ import annotations

from datetime import datetime
import io
import os
import unittest
from unittest.mock import patch

from src.alerts import Quote
from src.bot import ET
from src.check_once import (
    default_threshold_pct,
    evaluate_watchlist,
    quotes_from_raw,
    run_check,
)
from src.main import DEMO_TICKERS, main as cli_main


class FakeProvider:
    def __init__(self, quotes: dict[str, dict[str, float | None]]) -> None:
        self.quotes = quotes
        self.requested: list[str] | None = None

    def get_quotes(self, tickers: list[str]) -> dict[str, dict[str, float | None]]:
        self.requested = list(tickers)
        return {ticker: self.quotes.get(ticker, {"price": None, "prev_close": None}) for ticker in tickers}


class TestEvaluateWatchlist(unittest.TestCase):
    def test_batches_only_triggered_tickers(self) -> None:
        quotes = {
            "NVDA": Quote(price=118.40, prev_close=123.60),
            "AMD": Quote(price=92.15, prev_close=95.10),
            "AAPL": Quote(price=200.0, prev_close=200.0),
        }
        states: dict = {}
        fired = evaluate_watchlist(
            quotes,
            ["NVDA", "AMD", "AAPL"],
            threshold_pct=3.0,
            day_key="2026-09-04",
            states=states,
            mode="once",
        )
        self.assertEqual([row.symbol for row in fired], ["NVDA", "AMD"])
        self.assertTrue(states["NVDA"].fired)
        self.assertTrue(states["AMD"].fired)
        self.assertFalse(states["AAPL"].fired)

    def test_once_does_not_refire_in_same_state(self) -> None:
        quotes = {"NVDA": Quote(price=90.0, prev_close=100.0)}
        states: dict = {}
        first = evaluate_watchlist(
            quotes, ["NVDA"], threshold_pct=3.0, day_key="2026-09-04", states=states
        )
        second = evaluate_watchlist(
            quotes, ["NVDA"], threshold_pct=3.0, day_key="2026-09-04", states=states
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_legs_includes_step_on_second_cross(self) -> None:
        states: dict = {}
        first = evaluate_watchlist(
            {"NVDA": Quote(price=97.0, prev_close=100.0)},
            ["NVDA"],
            threshold_pct=3.0,
            day_key="2026-09-04",
            states=states,
            mode="legs",
        )
        second = evaluate_watchlist(
            {"NVDA": Quote(price=94.0, prev_close=100.0)},
            ["NVDA"],
            threshold_pct=3.0,
            day_key="2026-09-04",
            states=states,
            mode="legs",
        )
        self.assertEqual(first[0].leg, 1)
        self.assertEqual(second[0].leg, 2)


class TestQuotesFromRaw(unittest.TestCase):
    def test_missing_ticker_becomes_empty_quote(self) -> None:
        quotes = quotes_from_raw({}, ["NVDA"])
        self.assertEqual(quotes["NVDA"], Quote(price=None, prev_close=None))


class TestDefaultThreshold(unittest.TestCase):
    def test_defaults_to_three(self) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if key != "DEFAULT_THRESHOLD_PCT"
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(default_threshold_pct(), 3.0)

    def test_reads_env(self) -> None:
        with patch.dict(os.environ, {"DEFAULT_THRESHOLD_PCT": "2.5"}):
            self.assertEqual(default_threshold_pct(), 2.5)

    def test_rejects_non_positive(self) -> None:
        with patch.dict(os.environ, {"DEFAULT_THRESHOLD_PCT": "0"}):
            with self.assertRaises(RuntimeError):
                default_threshold_pct()


class TestRunCheck(unittest.TestCase):
    def test_sends_one_batched_message_and_prints_it(self) -> None:
        provider = FakeProvider(
            {
                "NVDA": {"price": 118.40, "prev_close": 123.60},
                "AMD": {"price": 92.15, "prev_close": 95.10},
                "AAPL": {"price": 200.0, "prev_close": 200.0},
                "MSFT": {"price": 400.0, "prev_close": 400.0},
                "GOOGL": {"price": 170.0, "prev_close": 170.0},
            }
        )
        sent: list[str] = []
        now = datetime(2026, 9, 4, 10, 45, tzinfo=ET)
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = run_check(
                provider=provider,
                sender=sent.append,
                now=now,
                tickers=DEMO_TICKERS,
                threshold_pct=3.0,
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("NVDA", sent[0])
        self.assertIn("AMD", sent[0])
        self.assertNotIn("AAPL", sent[0])
        self.assertIn("sent:", stdout.getvalue())
        self.assertIn(sent[0], stdout.getvalue())
        self.assertEqual(provider.requested, DEMO_TICKERS)

    def test_no_alerts_prints_and_does_not_send(self) -> None:
        provider = FakeProvider(
            {ticker: {"price": 100.0, "prev_close": 100.0} for ticker in DEMO_TICKERS}
        )
        sent: list[str] = []
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = run_check(
                provider=provider,
                sender=sent.append,
                now=datetime(2026, 9, 4, 10, 45, tzinfo=ET),
                tickers=DEMO_TICKERS,
                threshold_pct=3.0,
            )
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])
        self.assertEqual(stdout.getvalue().strip(), "no alerts")

    def test_missing_telegram_env_fails_fast(self) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "FINNHUB_API_KEY"}
        }
        stderr = io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch("sys.stderr", stderr):
            code = run_check()
        self.assertEqual(code, 1)
        self.assertIn("TELEGRAM_BOT_TOKEN", stderr.getvalue())

    def test_cli_default_is_not_check(self) -> None:
        with patch("src.main.print_quotes", return_value=0) as printer:
            self.assertEqual(cli_main([]), 0)
            printer.assert_called_once()

    def test_cli_check_dispatches(self) -> None:
        with patch("src.check_once.run_check", return_value=0) as check:
            self.assertEqual(cli_main(["--check"]), 0)
            check.assert_called_once()


if __name__ == "__main__":
    unittest.main()
