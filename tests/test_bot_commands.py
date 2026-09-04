"""Unit tests for Telegram command parsing and handlers (mocked I/O)."""

from __future__ import annotations

from datetime import datetime
import io
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from src.bot import ET
from src.bot_commands import (
    HELP_TEXT,
    BotDeps,
    handle_text,
    handle_update,
    parse_command,
    run_bot,
)
from src.main import DEMO_TICKERS, main as cli_main
from src.store import SEED_TICKERS, Store


class FakeProvider:
    def __init__(self, quotes: dict[str, dict[str, float | None]] | None = None) -> None:
        self.quotes = quotes or {}
        self.requested: list[list[str]] = []

    def get_quotes(self, tickers: list[str]) -> dict[str, dict[str, float | None]]:
        self.requested.append(list(tickers))
        return {
            ticker: self.quotes.get(ticker, {"price": None, "prev_close": None})
            for ticker in tickers
        }


class CommandTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = os.path.join(self._tmpdir.name, "watchlist.db")
        self.store = Store(self.path)
        self.provider = FakeProvider(
            {
                "TSLA": {"price": 250.0, "prev_close": 248.0},
                "NVDA": {"price": 120.0, "prev_close": 119.0},
                "AMD": {"price": 90.0, "prev_close": 100.0},
                "AAPL": {"price": 200.0, "prev_close": 200.0},
                "MSFT": {"price": 400.0, "prev_close": 400.0},
                "GOOGL": {"price": 160.0, "prev_close": 160.0},
                "META": {"price": 500.0, "prev_close": 490.0},
            }
        )
        self.deps = BotDeps(store=self.store, provider=self.provider)

    def _ask(self, text: str) -> str | None:
        return handle_text(text, self.deps)


class TestParseCommand(unittest.TestCase):
    def test_plain_command_and_args(self) -> None:
        parsed = parse_command("  /add  tsla  2.5 ")
        assert parsed is not None
        self.assertEqual(parsed.name, "add")
        self.assertEqual(parsed.args, ["tsla", "2.5"])

    def test_strips_bot_mention_and_lowercases(self) -> None:
        parsed = parse_command("/ADD@MyBot nvda")
        assert parsed is not None
        self.assertEqual(parsed.name, "add")
        self.assertEqual(parsed.args, ["nvda"])

    def test_non_commands_are_none(self) -> None:
        self.assertIsNone(parse_command("hello"))
        self.assertIsNone(parse_command("/"))
        self.assertIsNone(parse_command(""))


class TestUnauthorizedUpdates(CommandTestCase):
    def test_wrong_chat_is_ignored_silently(self) -> None:
        reply = handle_update(
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": 999},
                    "text": "/list",
                },
            },
            "42",
            self.deps,
        )
        self.assertIsNone(reply)

    def test_matching_int_chat_id_is_allowed(self) -> None:
        reply = handle_update(
            {
                "update_id": 2,
                "message": {
                    "chat": {"id": 42},
                    "text": "/help",
                },
            },
            "42",
            self.deps,
        )
        self.assertEqual(reply, HELP_TEXT)

    def test_non_command_from_allowed_chat_is_silent(self) -> None:
        reply = handle_update(
            {
                "update_id": 3,
                "message": {"chat": {"id": "42"}, "text": "hello there"},
            },
            "42",
            self.deps,
        )
        self.assertIsNone(reply)


class TestListHelpUnknown(CommandTestCase):
    def test_list_shows_seed_tickers_threshold_and_mode(self) -> None:
        text = self._ask("/list")
        assert text is not None
        for symbol in SEED_TICKERS:
            self.assertIn(symbol, text)
        self.assertIn("3%  once", text)

    def test_list_empty(self) -> None:
        for symbol in list(SEED_TICKERS):
            self.store.remove_ticker(symbol)
        self.assertEqual(self._ask("/list"), "watchlist is empty")

    def test_help_lists_every_command(self) -> None:
        text = self._ask("/help")
        self.assertEqual(text, HELP_TEXT)
        for name in (
            "/list",
            "/add",
            "/remove",
            "/set",
            "/mode",
            "/mute",
            "/status",
            "/check",
            "/default",
            "/help",
        ):
            self.assertIn(name, text)
        self.assertEqual(self._ask("/start"), HELP_TEXT)

    def test_unknown_command(self) -> None:
        self.assertEqual(self._ask("/nope"), "unknown command. /help")


class TestMutatingCommands(CommandTestCase):
    def test_add_validates_and_persists_uppercase(self) -> None:
        reply = self._ask("/add tsla")
        self.assertEqual(reply, "TSLA  3%  once")
        row = Store(self.path).get_ticker("TSLA")
        assert row is not None
        self.assertEqual(row.threshold_pct, 3.0)
        self.assertEqual(row.mode, "once")
        self.assertEqual(self.provider.requested[-1], ["TSLA"])

    def test_add_optional_threshold(self) -> None:
        self.assertEqual(self._ask("/add META 2.5"), "META  2.5%  once")
        row = self.store.get_ticker("META")
        assert row is not None
        self.assertEqual(row.threshold_pct, 2.5)

    def test_add_rejects_unknown_and_zero_quotes(self) -> None:
        self.assertEqual(self._ask("/add ZZZZ"), "unknown symbol: ZZZZ")
        self.assertIsNone(self.store.get_ticker("ZZZZ"))
        self.provider.quotes["ZERO"] = {"price": 0.0, "prev_close": 0.0}
        self.assertEqual(self._ask("/add ZERO"), "unknown symbol: ZERO")
        self.assertIsNone(self.store.get_ticker("ZERO"))

    def test_add_existing_keeps_mode_when_threshold_changes(self) -> None:
        self.store.upsert_ticker("NVDA", threshold_pct=3.0, mode="legs")
        self.assertEqual(self._ask("/add NVDA 4"), "NVDA  4%  legs")
        row = self.store.get_ticker("NVDA")
        assert row is not None
        self.assertEqual(row.mode, "legs")

    def test_add_usage(self) -> None:
        self.assertEqual(self._ask("/add"), "usage: /add TICKER [pct|$5]")
        self.assertEqual(self._ask("/add NVDA 0"), "pct must be a positive number")

    def test_add_dollar_threshold(self) -> None:
        self.assertEqual(self._ask("/add META $5"), "META  $5  once")
        row = self.store.get_ticker("META")
        assert row is not None
        self.assertEqual(row.threshold_unit, "usd")
        self.assertEqual(row.threshold_usd, 5.0)
        self.assertEqual(row.threshold_pct, 3.0)

    def test_add_usd_suffix(self) -> None:
        self.assertEqual(self._ask("/add META 5usd"), "META  $5  once")
        self.assertEqual(self._ask("/add META 2.5USD"), "META  $2.5  once")
        row = self.store.get_ticker("META")
        assert row is not None
        self.assertEqual(row.threshold_usd, 2.5)

    def test_add_dollar_rejects_non_positive(self) -> None:
        self.assertEqual(self._ask("/add META $0"), "usd must be a positive number")
        self.assertEqual(self._ask("/add META -5usd"), "usd must be a positive number")
        self.assertIsNone(self.store.get_ticker("META"))

    def test_add_existing_keeps_mode_when_switching_to_usd(self) -> None:
        self.store.upsert_ticker("NVDA", threshold_pct=3.0, mode="legs")
        self.assertEqual(self._ask("/add NVDA $5"), "NVDA  $5  legs")
        row = self.store.get_ticker("NVDA")
        assert row is not None
        self.assertEqual(row.mode, "legs")
        self.assertEqual(row.threshold_unit, "usd")

    def test_add_without_threshold_keeps_usd(self) -> None:
        self.store.upsert_ticker("NVDA", threshold_unit="usd", threshold_usd=5.0)
        self.assertEqual(self._ask("/add NVDA"), "NVDA  $5  once")
        self.assertEqual(self.store.get_ticker("NVDA").threshold_unit, "usd")

    def test_add_bare_number_switches_usd_back_to_pct(self) -> None:
        self.store.upsert_ticker("NVDA", threshold_unit="usd", threshold_usd=5.0)
        self.assertEqual(self._ask("/add NVDA 4"), "NVDA  4%  once")
        self.assertEqual(self.store.get_ticker("NVDA").threshold_unit, "pct")

    def test_remove_and_missing(self) -> None:
        self.assertEqual(self._ask("/remove nvda"), "removed NVDA")
        self.assertIsNone(Store(self.path).get_ticker("NVDA"))
        self.assertEqual(self._ask("/remove NVDA"), "NVDA is not on the watchlist")
        self.assertEqual(self._ask("/remove"), "usage: /remove TICKER")

    def test_set_threshold(self) -> None:
        self.assertEqual(self._ask("/set nvda 2.5"), "NVDA  2.5%  once")
        row = self.store.get_ticker("NVDA")
        assert row is not None
        self.assertEqual(row.threshold_pct, 2.5)
        self.assertEqual(row.threshold_unit, "pct")
        self.assertEqual(self._ask("/set ZZZZ 2"), "ZZZZ is not on the watchlist")
        self.assertEqual(self._ask("/set NVDA"), "usage: /set TICKER 5%|$5")

    def test_set_dollar_and_switch_back_to_pct(self) -> None:
        self.assertEqual(self._ask("/set nvda $5"), "NVDA  $5  once")
        row = self.store.get_ticker("NVDA")
        assert row is not None
        self.assertEqual(row.threshold_unit, "usd")
        self.assertEqual(row.threshold_usd, 5.0)
        self.assertEqual(self._ask("/set NVDA 5%"), "NVDA  5%  once")
        self.assertEqual(self.store.get_ticker("NVDA").threshold_unit, "pct")
        self.assertEqual(self._ask("/set NVDA $7.5"), "NVDA  $7.5  once")
        self.assertEqual(self._ask("/set NVDA pct"), "NVDA  5%  once")
        self.assertEqual(self.store.get_ticker("NVDA").threshold_unit, "pct")

    def test_list_shows_usd_unit(self) -> None:
        self.store.upsert_ticker("NVDA", threshold_unit="usd", threshold_usd=5.0)
        text = self._ask("/list")
        assert text is not None
        self.assertIn("NVDA  $5  once", text)
        self.assertIn("AMD  3%  once", text)

    def test_mode_preserves_usd_threshold(self) -> None:
        self.store.upsert_ticker("NVDA", threshold_unit="usd", threshold_usd=5.0)
        self.assertEqual(self._ask("/mode nvda legs"), "NVDA  $5  legs")
        self.assertEqual(self._ask("/mute nvda"), "NVDA  $5  mute")

    def test_mode_and_mute(self) -> None:
        self.assertEqual(self._ask("/mode nvda legs"), "NVDA  3%  legs")
        self.assertEqual(self.store.get_ticker("NVDA").mode, "legs")
        self.assertEqual(self._ask("/mute nvda"), "NVDA  3%  mute")
        self.assertEqual(self.store.get_ticker("NVDA").mode, "mute")
        self.assertEqual(self._ask("/mode NVDA nope"), "mode must be once, legs, or mute")
        self.assertEqual(self._ask("/mode"), "usage: /mode TICKER once|legs|mute")
        self.assertEqual(self._ask("/mode ZZZZ once"), "ZZZZ is not on the watchlist")

    def test_default_persists(self) -> None:
        self.assertEqual(self._ask("/default 4"), "default  4%")
        self.assertEqual(Store(self.path).get_default_threshold_pct(), 4.0)
        self.assertEqual(self._ask("/add META"), "META  4%  once")
        self.assertEqual(self._ask("/default"), "usage: /default pct")
        self.assertEqual(self._ask("/default -1"), "pct must be a positive number")


class TestStatusAndCheck(CommandTestCase):
    def test_status_prices(self) -> None:
        text = self._ask("/status")
        assert text is not None
        self.assertIn("NVDA  $120.00  +0.84%", text)
        self.assertIn("AMD  $90.00  -10.00%", text)
        self.assertEqual(self.provider.requested[-1], list(DEMO_TICKERS))

    def test_status_marks_missing_quotes(self) -> None:
        self.provider.quotes["NVDA"] = {"price": None, "prev_close": None}
        text = self._ask("/status")
        assert text is not None
        self.assertIn("NVDA  (no quote)", text)

    def test_check_no_alerts(self) -> None:
        # Seed names are flat except AMD which is already -10% vs 100.
        self.provider.quotes["AMD"] = {"price": 100.0, "prev_close": 100.0}
        alerts: list[str] = []
        self.deps.alert_sender = alerts.append
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            reply = self._ask("/check")
        self.assertEqual(reply, "checked: no alerts")
        self.assertEqual(alerts, [])

    def test_check_reuses_run_check_and_sends_alerts(self) -> None:
        alerts: list[str] = []
        self.deps.alert_sender = alerts.append
        self.deps.now = datetime(2026, 9, 4, 10, 45, tzinfo=ET)
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            reply = self._ask("/check")
        self.assertEqual(reply, "checked: alerts sent")
        self.assertEqual(len(alerts), 1)
        self.assertIn("AMD", alerts[0])
        self.assertIn("checked: alerts sent", reply)
        saved = Store(self.path).get_alert_state("AMD", "2026-09-04")
        assert saved is not None
        self.assertTrue(saved.fired)

        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            again = self._ask("/check")
        self.assertEqual(again, "checked: no alerts")

    def test_check_injected_run_check_failure(self) -> None:
        self.deps.run_check = lambda **_kwargs: 1
        self.assertEqual(self._ask("/check"), "check failed")

    def test_check_empty_watchlist(self) -> None:
        for symbol in list(SEED_TICKERS):
            self.store.remove_ticker(symbol)
        self.assertEqual(self._ask("/check"), "watchlist is empty")


class TestRunBotLoop(CommandTestCase):
    def test_polls_once_and_replies_to_authorized_chat_only(self) -> None:
        sent: list[str] = []
        updates = [
            {
                "update_id": 10,
                "message": {"chat": {"id": 99}, "text": "/remove NVDA"},
            },
            {
                "update_id": 11,
                "message": {"chat": {"id": 42}, "text": "/list"},
            },
        ]

        def fake_get_updates(*, token: str, offset: int | None, timeout: int):
            self.assertEqual(token, "tok")
            self.assertIsNone(offset)
            return updates

        env = {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "42",
            "FINNHUB_API_KEY": "k",
        }
        stdout = io.StringIO()
        with patch.dict(os.environ, env, clear=False), patch("sys.stdout", stdout):
            code = run_bot(
                store=self.store,
                provider=self.provider,
                max_polls=1,
                poll_timeout=0,
                get_updates_fn=fake_get_updates,
                send_fn=sent.append,
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("NVDA", sent[0])
        self.assertIsNotNone(self.store.get_ticker("NVDA"))

    def test_missing_env_fails_fast(self) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "FINNHUB_API_KEY"}
        }
        stderr = io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch("sys.stderr", stderr):
            code = run_bot(store=self.store, provider=self.provider, max_polls=1)
        self.assertEqual(code, 1)
        self.assertIn("TELEGRAM_BOT_TOKEN", stderr.getvalue())

    def test_stop_event_exits_without_polling(self) -> None:
        stop = threading.Event()
        stop.set()
        polled: list[int] = []

        def fake_get_updates(**_kwargs):
            polled.append(1)
            return []

        env = {
            "TELEGRAM_BOT_TOKEN": "tok",
            "TELEGRAM_CHAT_ID": "42",
            "FINNHUB_API_KEY": "k",
        }
        with patch.dict(os.environ, env, clear=False), patch("sys.stdout", io.StringIO()):
            code = run_bot(
                store=self.store,
                provider=self.provider,
                get_updates_fn=fake_get_updates,
                send_fn=lambda _text: None,
                stop=stop,
            )
        self.assertEqual(code, 0)
        self.assertEqual(polled, [])


class TestCliBotFlag(unittest.TestCase):
    def test_cli_bot_dispatches(self) -> None:
        with patch("src.bot_commands.run_bot", return_value=0) as bot:
            self.assertEqual(cli_main(["--bot"]), 0)
            bot.assert_called_once()

    def test_bot_module_entrypoint(self) -> None:
        from src.bot import main as bot_main

        with patch("src.bot_commands.run_bot", return_value=0) as bot:
            self.assertEqual(bot_main(), 0)
            bot.assert_called_once()

    def test_cli_rejects_check_and_bot_together(self) -> None:
        stderr = io.StringIO()
        with patch("sys.stderr", stderr), self.assertRaises(SystemExit):
            cli_main(["--check", "--bot"])


if __name__ == "__main__":
    unittest.main()
