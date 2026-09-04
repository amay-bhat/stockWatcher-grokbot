"""Tests for Telegram message formatting and sendMessage (no live network)."""

from __future__ import annotations

from datetime import datetime
import json
import os
import unittest
from unittest.mock import patch
import urllib.error

from src.bot import (
    ET,
    FiredAlert,
    format_alert_message,
    get_updates,
    is_allowed_chat,
    require_telegram_env,
    send_message,
)


def _nvda_amd() -> list[FiredAlert]:
    return [
        FiredAlert(
            symbol="NVDA",
            price=118.40,
            prev_close=123.60,
            pct_change=(118.40 - 123.60) / 123.60,
        ),
        FiredAlert(
            symbol="AMD",
            price=92.15,
            prev_close=95.10,
            pct_change=(92.15 - 95.10) / 95.10,
        ),
    ]


class TestFormatAlertMessage(unittest.TestCase):
    def test_batched_spec_example(self) -> None:
        now = datetime(2026, 9, 4, 10, 45, tzinfo=ET)
        text = format_alert_message(_nvda_amd(), now)
        expected = (
            "📉 Watchlist drop\n"
            "\n"
            "NVDA   $118.40   −4.2%   (prev close $123.60)\n"
            "AMD    $ 92.15   −3.1%   (prev close $ 95.10)\n"
            "\n"
            "10:45 ET"
        )
        self.assertEqual(text, expected)

    def test_legs_renotify_marks_step(self) -> None:
        now = datetime(2026, 9, 4, 10, 45, tzinfo=ET)
        alerts = [
            FiredAlert(
                symbol="NVDA",
                price=116.00,
                prev_close=123.60,
                pct_change=(116.00 - 123.60) / 123.60,
                leg=2,
            )
        ]
        text = format_alert_message(alerts, now)
        self.assertIn("−6.1% (2nd leg)", text)
        self.assertNotIn("1st leg", text)

    def test_usd_drop_shows_dollars_not_percent(self) -> None:
        now = datetime(2026, 9, 4, 10, 45, tzinfo=ET)
        alerts = [
            FiredAlert(
                symbol="NVDA",
                price=118.40,
                prev_close=123.60,
                pct_change=(118.40 - 123.60) / 123.60,
                threshold_unit="usd",
            )
        ]
        text = format_alert_message(alerts, now)
        self.assertIn("NVDA   $118.40   −$5.20   (prev close $123.60)", text)
        self.assertNotIn("%", text.split("\n")[2])

    def test_usd_legs_marks_step(self) -> None:
        now = datetime(2026, 9, 4, 10, 45, tzinfo=ET)
        alerts = [
            FiredAlert(
                symbol="NVDA",
                price=113.60,
                prev_close=123.60,
                pct_change=(113.60 - 123.60) / 123.60,
                leg=2,
                threshold_unit="usd",
            )
        ]
        text = format_alert_message(alerts, now)
        self.assertIn("−$10.00 (2nd leg)", text)

    def test_empty_alerts_is_empty_string(self) -> None:
        now = datetime(2026, 9, 4, 10, 45, tzinfo=ET)
        self.assertEqual(format_alert_message([], now), "")

    def test_naive_datetime_treated_as_et(self) -> None:
        now = datetime(2026, 9, 4, 16, 2)
        text = format_alert_message(_nvda_amd()[:1], now)
        self.assertTrue(text.endswith("16:02 ET"))


class TestTelegramEnv(unittest.TestCase):
    def test_missing_both(self) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                require_telegram_env()
        message = str(ctx.exception)
        self.assertIn("TELEGRAM_BOT_TOKEN", message)
        self.assertIn("TELEGRAM_CHAT_ID", message)

    def test_blank_values_count_as_missing(self) -> None:
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "  ", "TELEGRAM_CHAT_ID": ""},
        ):
            with self.assertRaises(RuntimeError) as ctx:
                require_telegram_env()
        self.assertIn("TELEGRAM_BOT_TOKEN", str(ctx.exception))

    def test_present(self) -> None:
        with patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"},
        ):
            self.assertEqual(require_telegram_env(), ("tok", "123"))


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class TestSendMessage(unittest.TestCase):
    def test_posts_json_without_parse_mode(self) -> None:
        captured: dict = {}

        def fake_urlopen(request, timeout=10.0):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return _FakeResponse({"ok": True, "result": {"message_id": 1}})

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            send_message("hello", token="secret-token", chat_id="42")

        self.assertIn("/botsecret-token/sendMessage", captured["url"])
        self.assertEqual(captured["body"], {"chat_id": "42", "text": "hello"})
        self.assertEqual(captured["timeout"], 10.0)

    def test_ok_false_raises(self) -> None:
        with patch(
            "urllib.request.urlopen",
            return_value=_FakeResponse({"ok": False, "description": "Forbidden"}),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                send_message("hello", token="t", chat_id="1")
        self.assertIn("Forbidden", str(ctx.exception))
        self.assertNotIn("/bot", str(ctx.exception))

    def test_http_error_does_not_leak_token(self) -> None:
        error = urllib.error.HTTPError(
            url="https://api.telegram.org/botsecret-token/sendMessage",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )

        def raise_http(*args, **kwargs):
            raise error

        with patch("urllib.request.urlopen", side_effect=raise_http):
            with self.assertRaises(RuntimeError) as ctx:
                send_message("hello", token="secret-token", chat_id="1")
        message = str(ctx.exception)
        self.assertIn("HTTP 401", message)
        self.assertNotIn("secret-token", message)

    def test_empty_text_refused(self) -> None:
        with self.assertRaises(RuntimeError):
            send_message("", token="t", chat_id="1")

    def test_missing_env_when_credentials_omitted(self) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                send_message("hello")
        self.assertIn("TELEGRAM_BOT_TOKEN", str(ctx.exception))


class TestAllowedChat(unittest.TestCase):
    def test_int_and_string_match(self) -> None:
        self.assertTrue(is_allowed_chat(123, "123"))
        self.assertTrue(is_allowed_chat("-99", " -99 "))
        self.assertFalse(is_allowed_chat(1, "123"))
        self.assertFalse(is_allowed_chat(None, "123"))


class TestGetUpdates(unittest.TestCase):
    def test_long_poll_query_and_offset(self) -> None:
        captured: dict = {}

        def fake_urlopen(request, timeout=35.0):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["method"] = request.get_method()
            return _FakeResponse(
                {
                    "ok": True,
                    "result": [
                        {"update_id": 7, "message": {"text": "/list"}},
                        "ignored",
                    ],
                }
            )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            updates = get_updates(token="secret-token", offset=4, timeout=25)

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["update_id"], 7)
        self.assertIn("/botsecret-token/getUpdates", captured["url"])
        self.assertIn("offset=4", captured["url"])
        self.assertIn("timeout=25", captured["url"])
        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["timeout"], 35.0)

    def test_http_error_does_not_leak_token(self) -> None:
        error = urllib.error.HTTPError(
            url="https://api.telegram.org/botsecret-token/getUpdates",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )

        def raise_http(*args, **kwargs):
            raise error

        with patch("urllib.request.urlopen", side_effect=raise_http):
            with self.assertRaises(RuntimeError) as ctx:
                get_updates(token="secret-token")
        message = str(ctx.exception)
        self.assertIn("HTTP 401", message)
        self.assertNotIn("secret-token", message)


if __name__ == "__main__":
    unittest.main()
