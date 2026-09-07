"""SQLite store tests against a temp DB file (no shared process state)."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.alerts import AlertState
from src.store import (
    DEFAULT_THRESHOLD_PCT,
    SEED_TICKERS,
    Store,
    default_threshold_pct,
)


class TestDefaultThresholdEnv(unittest.TestCase):
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


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = os.path.join(self._tmpdir.name, "watchlist.db")

    def _store(self) -> Store:
        return Store(self.path)


class TestStoreSeed(StoreTestCase):
    def test_seeds_demo_tickers_on_empty_db(self) -> None:
        watchlist = self._store().get_watchlist()
        self.assertEqual([row.symbol for row in watchlist], list(SEED_TICKERS))
        for row in watchlist:
            self.assertEqual(row.mode, "once")
            self.assertEqual(row.threshold_pct, DEFAULT_THRESHOLD_PCT)
            self.assertEqual(row.threshold_unit, "pct")
            self.assertIsNone(row.threshold_usd)
            self.assertEqual(row.direction, "down")

    def test_seed_uses_env_threshold(self) -> None:
        with patch.dict(os.environ, {"DEFAULT_THRESHOLD_PCT": "2.5"}):
            watchlist = self._store().get_watchlist()
        self.assertTrue(watchlist)
        for row in watchlist:
            self.assertEqual(row.threshold_pct, 2.5)
        self.assertEqual(self._store().get_default_threshold_pct(), 2.5)

    def test_does_not_reseed_after_user_edits(self) -> None:
        store = self._store()
        self.assertTrue(store.remove_ticker("NVDA"))
        store.upsert_ticker("TSLA", threshold_pct=4.0, mode="legs")

        restarted = Store(self.path)
        symbols = [row.symbol for row in restarted.get_watchlist()]
        self.assertNotIn("NVDA", symbols)
        self.assertIn("TSLA", symbols)
        self.assertNotEqual(symbols, list(SEED_TICKERS))

    def test_removing_every_ticker_does_not_reseed(self) -> None:
        store = self._store()
        for symbol in list(SEED_TICKERS):
            store.remove_ticker(symbol)
        self.assertEqual(Store(self.path).get_watchlist(), [])


class TestStoreWatchlist(StoreTestCase):
    def test_upsert_round_trip_and_update(self) -> None:
        store = self._store()
        created = store.upsert_ticker("tsla", threshold_pct=2.5, mode="LEGS")
        self.assertEqual(created.symbol, "TSLA")
        self.assertEqual(created.threshold_pct, 2.5)
        self.assertEqual(created.mode, "legs")

        updated = store.upsert_ticker("TSLA", threshold_pct=1.5, mode="mute")
        self.assertEqual(updated.mode, "mute")
        self.assertEqual(updated.threshold_pct, 1.5)

        row = next(item for item in Store(self.path).get_watchlist() if item.symbol == "TSLA")
        self.assertEqual(row, updated)

    def test_get_ticker_normalizes_and_misses(self) -> None:
        store = self._store()
        row = store.get_ticker("nvda")
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.symbol, "NVDA")
        self.assertIsNone(store.get_ticker("ZZZZ"))
        self.assertIsNone(store.get_ticker("  "))

    def test_upsert_defaults_threshold_from_store(self) -> None:
        store = self._store()
        store.set_default_threshold_pct(4.0)
        row = store.upsert_ticker("META")
        self.assertEqual(row.threshold_pct, 4.0)
        self.assertEqual(row.mode, "once")
        self.assertEqual(row.threshold_unit, "pct")
        self.assertIsNone(row.threshold_usd)
        self.assertEqual(row.direction, "down")

    def test_unknown_mode_falls_back_to_once(self) -> None:
        row = self._store().upsert_ticker("AMD", threshold_pct=3.0, mode="nope")
        self.assertEqual(row.mode, "once")

    def test_remove_missing_returns_false(self) -> None:
        self.assertFalse(self._store().remove_ticker("ZZZZ"))

    def test_rejects_empty_symbol_and_bad_threshold(self) -> None:
        store = self._store()
        with self.assertRaises(ValueError):
            store.upsert_ticker("  ")
        with self.assertRaises(ValueError):
            store.upsert_ticker("NVDA", threshold_pct=0)
        with self.assertRaises(ValueError):
            store.set_default_threshold_pct(-1)

    def test_get_set_default_threshold_survives_restart(self) -> None:
        self._store().set_default_threshold_pct(2.0)
        self.assertEqual(Store(self.path).get_default_threshold_pct(), 2.0)

    def test_upsert_usd_round_trip(self) -> None:
        store = self._store()
        created = store.upsert_ticker(
            "NVDA", threshold_unit="usd", threshold_usd=5.0, mode="legs"
        )
        self.assertEqual(created.threshold_unit, "usd")
        self.assertEqual(created.threshold_usd, 5.0)
        self.assertEqual(created.mode, "legs")
        self.assertEqual(created.threshold_pct, DEFAULT_THRESHOLD_PCT)

        loaded = Store(self.path).get_ticker("NVDA")
        self.assertEqual(loaded, created)

        switched = store.upsert_ticker("NVDA", threshold_pct=2.5, threshold_unit="pct")
        self.assertEqual(switched.threshold_unit, "pct")
        self.assertEqual(switched.threshold_pct, 2.5)
        self.assertEqual(switched.mode, "legs")
        self.assertEqual(switched.threshold_usd, 5.0)
        self.assertEqual(switched.direction, "down")

    def test_upsert_direction_round_trip(self) -> None:
        store = self._store()
        created = store.upsert_ticker("NVDA", direction="up")
        self.assertEqual(created.direction, "up")
        self.assertEqual(Store(self.path).get_ticker("NVDA").direction, "up")

        both = store.upsert_ticker("NVDA", direction="both")
        self.assertEqual(both.direction, "both")
        self.assertEqual(both.threshold_pct, DEFAULT_THRESHOLD_PCT)

        rise = store.upsert_ticker("META", direction="rise")
        self.assertEqual(rise.direction, "up")

        unknown = store.upsert_ticker("AMD", direction="sideways")
        self.assertEqual(unknown.direction, "down")

    def test_mode_update_preserves_direction(self) -> None:
        store = self._store()
        store.upsert_ticker("NVDA", direction="up")
        updated = store.upsert_ticker("NVDA", mode="legs")
        self.assertEqual(updated.direction, "up")
        self.assertEqual(updated.mode, "legs")

    def test_mode_update_preserves_usd_unit(self) -> None:
        store = self._store()
        store.upsert_ticker("NVDA", threshold_unit="usd", threshold_usd=5.0, mode="once")
        updated = store.upsert_ticker("NVDA", mode="mute")
        self.assertEqual(updated.threshold_unit, "usd")
        self.assertEqual(updated.threshold_usd, 5.0)
        self.assertEqual(updated.mode, "mute")

    def test_usd_upsert_requires_positive_amount(self) -> None:
        store = self._store()
        with self.assertRaises(ValueError):
            store.upsert_ticker("META", threshold_unit="usd")
        with self.assertRaises(ValueError):
            store.upsert_ticker("META", threshold_unit="usd", threshold_usd=0)

    def test_migrates_legacy_watchlist_without_unit_columns(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.executescript(
                """
                CREATE TABLE settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE watchlist (
                    symbol TEXT PRIMARY KEY,
                    threshold_pct REAL NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'once'
                );
                CREATE TABLE alert_state (
                    symbol TEXT NOT NULL,
                    day_key TEXT NOT NULL,
                    fired INTEGER NOT NULL DEFAULT 0,
                    last_leg INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (symbol, day_key)
                );
                INSERT INTO settings (key, value) VALUES ('seeded', '1');
                INSERT INTO settings (key, value)
                    VALUES ('default_threshold_pct', '3.0');
                INSERT INTO watchlist (symbol, threshold_pct, mode)
                    VALUES ('NVDA', 3.0, 'once');
                """
            )
        store = Store(self.path)
        row = store.get_ticker("NVDA")
        assert row is not None
        self.assertEqual(row.threshold_unit, "pct")
        self.assertIsNone(row.threshold_usd)
        self.assertEqual(row.threshold_pct, 3.0)
        self.assertEqual(row.direction, "down")
        self.assertEqual([item.symbol for item in store.get_watchlist()], ["NVDA"])

    def test_migrates_legacy_alert_state_without_up_columns(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.executescript(
                """
                CREATE TABLE settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE watchlist (
                    symbol TEXT PRIMARY KEY,
                    threshold_pct REAL NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'once',
                    threshold_unit TEXT NOT NULL DEFAULT 'pct',
                    threshold_usd REAL
                );
                CREATE TABLE alert_state (
                    symbol TEXT NOT NULL,
                    day_key TEXT NOT NULL,
                    fired INTEGER NOT NULL DEFAULT 0,
                    last_leg INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (symbol, day_key)
                );
                INSERT INTO settings (key, value) VALUES ('seeded', '1');
                INSERT INTO settings (key, value)
                    VALUES ('default_threshold_pct', '3.0');
                INSERT INTO watchlist (symbol, threshold_pct, mode)
                    VALUES ('NVDA', 3.0, 'once');
                INSERT INTO alert_state (symbol, day_key, fired, last_leg)
                    VALUES ('NVDA', '2026-09-04', 1, 2);
                """
            )
        store = Store(self.path)
        row = store.get_ticker("NVDA")
        assert row is not None
        self.assertEqual(row.direction, "down")
        state = store.get_alert_state("NVDA", "2026-09-04")
        self.assertEqual(
            state,
            AlertState(
                day_key="2026-09-04",
                fired=True,
                last_leg=2,
                fired_up=False,
                last_leg_up=0,
            ),
        )


class TestStoreAlertState(StoreTestCase):
    def test_missing_state_is_none(self) -> None:
        self.assertIsNone(self._store().get_alert_state("NVDA", "2026-09-04"))

    def test_round_trip_state(self) -> None:
        store = self._store()
        state = AlertState(
            day_key="2026-09-04",
            fired=True,
            last_leg=2,
            fired_up=True,
            last_leg_up=1,
        )
        store.save_alert_state("nvda", state)
        loaded = store.get_alert_state("NVDA", "2026-09-04")
        self.assertEqual(loaded, state)
        self.assertIsNone(store.get_alert_state("NVDA", "2026-09-05"))

    def test_state_survives_new_connection(self) -> None:
        first = self._store()
        first.save_alert_state(
            "AMD", AlertState(day_key="2026-09-04", fired=True, last_leg=0)
        )
        first.save_alert_states(
            {
                "AAPL": AlertState(day_key="2026-09-04", fired=False, last_leg=1),
                "MSFT": AlertState(day_key="2026-09-04", fired=True, last_leg=3),
            }
        )

        restarted = Store(self.path)
        self.assertEqual(
            restarted.get_alert_state("AMD", "2026-09-04"),
            AlertState(day_key="2026-09-04", fired=True, last_leg=0),
        )
        batch = restarted.get_alert_states(["AMD", "AAPL", "MSFT", "GOOGL"], "2026-09-04")
        self.assertEqual(set(batch), {"AMD", "AAPL", "MSFT"})
        self.assertFalse(batch["AAPL"].fired)
        self.assertEqual(batch["AAPL"].last_leg, 1)
        self.assertEqual(batch["MSFT"].last_leg, 3)
        self.assertNotIn("GOOGL", batch)

    def test_overwrite_same_day_state(self) -> None:
        store = self._store()
        store.save_alert_state(
            "NVDA", AlertState(day_key="2026-09-04", fired=False, last_leg=1)
        )
        store.save_alert_state(
            "NVDA", AlertState(day_key="2026-09-04", fired=True, last_leg=2)
        )
        loaded = store.get_alert_state("NVDA", "2026-09-04")
        self.assertEqual(loaded, AlertState(day_key="2026-09-04", fired=True, last_leg=2))

    def test_remove_ticker_drops_its_alert_state(self) -> None:
        store = self._store()
        store.save_alert_state(
            "NVDA", AlertState(day_key="2026-09-04", fired=True, last_leg=0)
        )
        store.remove_ticker("NVDA")
        self.assertIsNone(Store(self.path).get_alert_state("NVDA", "2026-09-04"))

    def test_file_exists_on_disk(self) -> None:
        self._store()
        self.assertTrue(os.path.isfile(self.path))
        with sqlite3.connect(self.path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertTrue({"watchlist", "alert_state", "settings"} <= tables)


class TestDatabasePathEnv(StoreTestCase):
    def test_creates_parent_directories(self) -> None:
        nested = os.path.join(self._tmpdir.name, "nested", "dir", "watchlist.db")
        Store(nested)
        self.assertTrue(os.path.isfile(nested))

    def test_store_reads_database_path(self) -> None:
        with patch.dict(os.environ, {"DATABASE_PATH": self.path}):
            Store()
        self.assertTrue(os.path.isfile(self.path))
        self.assertEqual(
            [row.symbol for row in Store(self.path).get_watchlist()],
            list(SEED_TICKERS),
        )


if __name__ == "__main__":
    unittest.main()
