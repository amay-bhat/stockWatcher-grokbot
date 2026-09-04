"""Tests for the one-shot watchlist check (mocked quotes, no Telegram network)."""

from __future__ import annotations

from datetime import datetime
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from src.alerts import AlertState, Quote, TickerConfig
from src.bot import ET
from src.check_once import (
    default_threshold_pct,
    evaluate_watchlist,
    quotes_from_raw,
    run_check,
)
from src.main import DEMO_TICKERS, main as cli_main
from src.store import Store


class FakeProvider:
    def __init__(self, quotes: dict[str, dict[str, float | None]]) -> None:
        self.quotes = quotes
        self.requested: list[str] | None = None

    def get_quotes(self, tickers: list[str]) -> dict[str, dict[str, float | None]]:
        self.requested = list(tickers)
        return {ticker: self.quotes.get(ticker, {"price": None, "prev_close": None}) for ticker in tickers}


def _configs(
    symbols: list[str],
    *,
    threshold_pct: float = 3.0,
    mode: str = "once",
) -> list[TickerConfig]:
    return [
        TickerConfig(symbol=symbol, threshold_pct=threshold_pct, mode=mode)
        for symbol in symbols
    ]


class TestEvaluateWatchlist(unittest.TestCase):
    def test_batches_only_triggered_tickers(self) -> None:
        quotes = {
            "NVDA": Quote(price=118.40, prev_close=123.60),
            "AMD": Quote(price=92.15, prev_close=95.10),
            "AAPL": Quote(price=200.0, prev_close=200.0),
        }
        states: dict[str, AlertState] = {}
        fired = evaluate_watchlist(
            quotes,
            _configs(["NVDA", "AMD", "AAPL"]),
            day_key="2026-09-04",
            states=states,
        )
        self.assertEqual([row.symbol for row in fired], ["NVDA", "AMD"])
        self.assertTrue(states["NVDA"].fired)
        self.assertTrue(states["AMD"].fired)
        self.assertFalse(states["AAPL"].fired)

    def test_once_does_not_refire_in_same_state(self) -> None:
        quotes = {"NVDA": Quote(price=90.0, prev_close=100.0)}
        states: dict[str, AlertState] = {}
        first = evaluate_watchlist(
            quotes, _configs(["NVDA"]), day_key="2026-09-04", states=states
        )
        second = evaluate_watchlist(
            quotes, _configs(["NVDA"]), day_key="2026-09-04", states=states
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_legs_includes_step_on_second_cross(self) -> None:
        states: dict[str, AlertState] = {}
        first = evaluate_watchlist(
            {"NVDA": Quote(price=97.0, prev_close=100.0)},
            _configs(["NVDA"], mode="legs"),
            day_key="2026-09-04",
            states=states,
        )
        second = evaluate_watchlist(
            {"NVDA": Quote(price=94.0, prev_close=100.0)},
            _configs(["NVDA"], mode="legs"),
            day_key="2026-09-04",
            states=states,
        )
        self.assertEqual(first[0].leg, 1)
        self.assertEqual(second[0].leg, 2)

    def test_uses_per_ticker_threshold(self) -> None:
        quotes = {
            "NVDA": Quote(price=97.5, prev_close=100.0),  # -2.5%
            "AMD": Quote(price=97.5, prev_close=100.0),
        }
        configs = [
            TickerConfig(symbol="NVDA", threshold_pct=3.0, mode="once"),
            TickerConfig(symbol="AMD", threshold_pct=2.0, mode="once"),
        ]
        fired = evaluate_watchlist(quotes, configs, day_key="2026-09-04", states={})
        self.assertEqual([row.symbol for row in fired], ["AMD"])


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
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = os.path.join(self._tmpdir.name, "watchlist.db")

    def _store(self) -> Store:
        return Store(self.path)

    def _quotes(self, **overrides: dict[str, float | None]) -> dict[str, dict[str, float | None]]:
        quotes = {ticker: {"price": 100.0, "prev_close": 100.0} for ticker in DEMO_TICKERS}
        quotes.update(overrides)
        return quotes

    def test_sends_one_batched_message_and_prints_it(self) -> None:
        provider = FakeProvider(
            self._quotes(
                NVDA={"price": 118.40, "prev_close": 123.60},
                AMD={"price": 92.15, "prev_close": 95.10},
            )
        )
        sent: list[str] = []
        now = datetime(2026, 9, 4, 10, 45, tzinfo=ET)
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = run_check(
                provider=provider,
                sender=sent.append,
                now=now,
                store=self._store(),
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
        provider = FakeProvider(self._quotes())
        sent: list[str] = []
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = run_check(
                provider=provider,
                sender=sent.append,
                now=datetime(2026, 9, 4, 10, 45, tzinfo=ET),
                store=self._store(),
            )
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])
        self.assertEqual(stdout.getvalue().strip(), "no alerts")
        saved = Store(self.path).get_alert_state("NVDA", "2026-09-04")
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertFalse(saved.fired)
        self.assertEqual(saved.day_key, "2026-09-04")

    def test_persists_state_so_restart_does_not_realert(self) -> None:
        quotes = self._quotes(
            NVDA={"price": 90.0, "prev_close": 100.0},
            AMD={"price": 92.15, "prev_close": 95.10},
        )
        now = datetime(2026, 9, 4, 10, 45, tzinfo=ET)
        first_sent: list[str] = []
        with patch("sys.stdout", io.StringIO()):
            code = run_check(
                provider=FakeProvider(quotes),
                sender=first_sent.append,
                now=now,
                store=self._store(),
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(first_sent), 1)
        self.assertIn("NVDA", first_sent[0])
        self.assertIn("AMD", first_sent[0])

        restarted = Store(self.path)
        nvda = restarted.get_alert_state("NVDA", "2026-09-04")
        amd = restarted.get_alert_state("AMD", "2026-09-04")
        self.assertIsNotNone(nvda)
        self.assertIsNotNone(amd)
        assert nvda is not None and amd is not None
        self.assertTrue(nvda.fired)
        self.assertTrue(amd.fired)

        second_sent: list[str] = []
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = run_check(
                provider=FakeProvider(quotes),
                sender=second_sent.append,
                now=now,
                store=Store(self.path),
            )
        self.assertEqual(code, 0)
        self.assertEqual(second_sent, [])
        self.assertEqual(stdout.getvalue().strip(), "no alerts")

    def test_new_day_alerts_again_after_restart(self) -> None:
        quotes = self._quotes(NVDA={"price": 90.0, "prev_close": 100.0})
        with patch("sys.stdout", io.StringIO()):
            run_check(
                provider=FakeProvider(quotes),
                sender=lambda _text: None,
                now=datetime(2026, 9, 4, 10, 45, tzinfo=ET),
                store=self._store(),
            )
        sent: list[str] = []
        with patch("sys.stdout", io.StringIO()):
            code = run_check(
                provider=FakeProvider(quotes),
                sender=sent.append,
                now=datetime(2026, 9, 5, 10, 45, tzinfo=ET),
                store=Store(self.path),
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("NVDA", sent[0])

    def test_honors_per_ticker_mode_from_store(self) -> None:
        store = self._store()
        store.upsert_ticker("NVDA", threshold_pct=3.0, mode="mute")
        sent: list[str] = []
        with patch("sys.stdout", io.StringIO()):
            code = run_check(
                provider=FakeProvider(self._quotes(NVDA={"price": 90.0, "prev_close": 100.0})),
                sender=sent.append,
                now=datetime(2026, 9, 4, 10, 45, tzinfo=ET),
                store=store,
            )
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])

    def test_failed_send_does_not_persist_fired_state(self) -> None:
        def boom(_text: str) -> None:
            raise RuntimeError("Telegram send failed: boom")

        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            code = run_check(
                provider=FakeProvider(self._quotes(NVDA={"price": 90.0, "prev_close": 100.0})),
                sender=boom,
                now=datetime(2026, 9, 4, 10, 45, tzinfo=ET),
                store=self._store(),
            )
        self.assertEqual(code, 1)
        self.assertIn("Telegram send failed", stderr.getvalue())
        self.assertIsNone(Store(self.path).get_alert_state("NVDA", "2026-09-04"))

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

    def test_all_quotes_failed_does_not_persist_or_send(self) -> None:
        quotes = {
            ticker: {"price": None, "prev_close": None} for ticker in DEMO_TICKERS
        }
        sent: list[str] = []
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            code = run_check(
                provider=FakeProvider(quotes),
                sender=sent.append,
                now=datetime(2026, 9, 4, 10, 45, tzinfo=ET),
                store=self._store(),
            )
        self.assertEqual(code, 1)
        self.assertEqual(sent, [])
        self.assertIn("quote fetch failed", stderr.getvalue())
        self.assertIsNone(Store(self.path).get_alert_state("NVDA", "2026-09-04"))

    def test_cli_default_is_serve(self) -> None:
        with patch("src.scheduler.run_serve", return_value=0) as serve:
            self.assertEqual(cli_main([]), 0)
            serve.assert_called_once()

    def test_cli_check_dispatches(self) -> None:
        with patch("src.check_once.run_check", return_value=0) as check:
            self.assertEqual(cli_main(["--check"]), 0)
            check.assert_called_once()


if __name__ == "__main__":
    unittest.main()
