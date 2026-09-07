"""SQLite persistence for watchlist config and per-day alert state (M4).

I/O lives here. `alerts.py` and `PriceProvider` stay pure. Stdlib `sqlite3` only.

Path comes from `DATABASE_PATH` (default `./data/watchlist.db`). An empty database
is seeded once with the demo tickers at `DEFAULT_THRESHOLD_PCT` and mode `once`.
"""

from __future__ import annotations

from contextlib import contextmanager
import math
import os
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from src.alerts import (
    VALID_DIRECTIONS,
    VALID_MODES,
    VALID_UNITS,
    AlertDirection,
    AlertMode,
    AlertState,
    TickerConfig,
    ThresholdUnit,
)

DEFAULT_THRESHOLD_PCT = 3.0
DEFAULT_DATABASE_PATH = "./data/watchlist.db"
SEED_TICKERS = ("NVDA", "AMD", "AAPL", "MSFT", "GOOGL")

_SETTING_SEEDED = "seeded"
_SETTING_DEFAULT_THRESHOLD = "default_threshold_pct"
_WATCHLIST_COLS = (
    "symbol, threshold_pct, mode, threshold_unit, threshold_usd, direction"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    symbol TEXT PRIMARY KEY,
    threshold_pct REAL NOT NULL,
    mode TEXT NOT NULL DEFAULT 'once',
    threshold_unit TEXT NOT NULL DEFAULT 'pct',
    threshold_usd REAL,
    direction TEXT NOT NULL DEFAULT 'down'
);

CREATE TABLE IF NOT EXISTS alert_state (
    symbol TEXT NOT NULL,
    day_key TEXT NOT NULL,
    fired INTEGER NOT NULL DEFAULT 0,
    last_leg INTEGER NOT NULL DEFAULT 0,
    fired_up INTEGER NOT NULL DEFAULT 0,
    last_leg_up INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, day_key)
);
"""


def database_path() -> str:
    raw = os.environ.get("DATABASE_PATH", DEFAULT_DATABASE_PATH).strip()
    return raw or DEFAULT_DATABASE_PATH


def default_threshold_pct() -> float:
    """Read `DEFAULT_THRESHOLD_PCT` from the environment (not the DB)."""
    raw = os.environ.get("DEFAULT_THRESHOLD_PCT", str(DEFAULT_THRESHOLD_PCT)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"DEFAULT_THRESHOLD_PCT must be a number, got {raw!r}"
        ) from exc
    if value <= 0 or not math.isfinite(value):
        raise RuntimeError("DEFAULT_THRESHOLD_PCT must be a positive number")
    return value


class Store:
    """File-backed watchlist + per-ticker `AlertState` keyed by `day_key`."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path if path is not None else database_path()
        self._ensure_parent()
        with self._connect() as conn:
            self._init_schema(conn)
            self._seed_if_needed(conn)

    def get_watchlist(self) -> list[TickerConfig]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_WATCHLIST_COLS} FROM watchlist ORDER BY rowid"
            ).fetchall()
        return [_row_to_config(row) for row in rows]

    def get_ticker(self, symbol: str) -> TickerConfig | None:
        normalized = _normalize_symbol(symbol)
        if not normalized:
            return None
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_WATCHLIST_COLS} FROM watchlist WHERE symbol = ?",
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_config(row)

    def upsert_ticker(
        self,
        symbol: str,
        threshold_pct: float | None = None,
        mode: str | None = None,
        threshold_unit: str | None = None,
        threshold_usd: float | None = None,
        direction: str | None = None,
    ) -> TickerConfig:
        normalized = _normalize_symbol(symbol)
        if not normalized:
            raise ValueError("ticker symbol is required")
        existing = self.get_ticker(normalized)
        if existing is not None:
            threshold = (
                existing.threshold_pct
                if threshold_pct is None
                else _require_positive_threshold(threshold_pct)
            )
            alert_mode = (
                existing.mode if mode is None else _normalize_mode(mode)
            )
            unit = (
                existing.threshold_unit
                if threshold_unit is None
                else _normalize_unit(threshold_unit)
            )
            usd = (
                existing.threshold_usd
                if threshold_usd is None
                else _require_positive_threshold(threshold_usd, field="threshold_usd")
            )
            alert_direction = (
                existing.direction
                if direction is None
                else _normalize_direction(direction)
            )
        else:
            threshold = (
                self.get_default_threshold_pct()
                if threshold_pct is None
                else _require_positive_threshold(threshold_pct)
            )
            alert_mode = _normalize_mode(mode or "once")
            unit = _normalize_unit(threshold_unit or "pct")
            usd = (
                None
                if threshold_usd is None
                else _require_positive_threshold(threshold_usd, field="threshold_usd")
            )
            alert_direction = _normalize_direction(direction or "down")
        if unit == "usd" and not _is_positive(usd):
            raise ValueError("threshold_usd must be a positive number")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO watchlist (
                    symbol, threshold_pct, mode, threshold_unit, threshold_usd,
                    direction
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    threshold_pct = excluded.threshold_pct,
                    mode = excluded.mode,
                    threshold_unit = excluded.threshold_unit,
                    threshold_usd = excluded.threshold_usd,
                    direction = excluded.direction
                """,
                (normalized, threshold, alert_mode, unit, usd, alert_direction),
            )
        return TickerConfig(
            symbol=normalized,
            threshold_pct=threshold,
            mode=alert_mode,
            threshold_unit=unit,
            threshold_usd=usd,
            direction=alert_direction,
        )

    def remove_ticker(self, symbol: str) -> bool:
        normalized = _normalize_symbol(symbol)
        if not normalized:
            return False
        with self._connect() as conn:
            conn.execute("DELETE FROM alert_state WHERE symbol = ?", (normalized,))
            cursor = conn.execute(
                "DELETE FROM watchlist WHERE symbol = ?", (normalized,)
            )
            return cursor.rowcount > 0

    def get_alert_state(self, symbol: str, day_key: str) -> AlertState | None:
        normalized = _normalize_symbol(symbol)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT day_key, fired, last_leg, fired_up, last_leg_up
                FROM alert_state
                WHERE symbol = ? AND day_key = ?
                """,
                (normalized, day_key),
            ).fetchone()
        if row is None:
            return None
        return _row_to_state(row)

    def save_alert_state(self, symbol: str, state: AlertState) -> None:
        normalized = _normalize_symbol(symbol)
        if not normalized:
            raise ValueError("ticker symbol is required")
        with self._connect() as conn:
            _upsert_state(conn, normalized, state)

    def get_alert_states(
        self, symbols: Sequence[str], day_key: str
    ) -> dict[str, AlertState]:
        """Load saved state for `day_key`. Missing tickers are omitted."""
        if not symbols:
            return {}
        normalized = [_normalize_symbol(symbol) for symbol in symbols]
        normalized = [symbol for symbol in normalized if symbol]
        if not normalized:
            return {}
        placeholders = ",".join("?" * len(normalized))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT symbol, day_key, fired, last_leg, fired_up, last_leg_up
                FROM alert_state
                WHERE day_key = ? AND symbol IN ({placeholders})
                """,
                (day_key, *normalized),
            ).fetchall()
        return {str(row["symbol"]): _row_to_state(row) for row in rows}

    def save_alert_states(self, states: Mapping[str, AlertState]) -> None:
        with self._connect() as conn:
            for symbol, state in states.items():
                normalized = _normalize_symbol(symbol)
                if not normalized:
                    continue
                _upsert_state(conn, normalized, state)

    def get_default_threshold_pct(self) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (_SETTING_DEFAULT_THRESHOLD,),
            ).fetchone()
        if row is None:
            return default_threshold_pct()
        try:
            value = float(row["value"])
        except (TypeError, ValueError):
            return default_threshold_pct()
        if value <= 0 or not math.isfinite(value):
            return default_threshold_pct()
        return value

    def set_default_threshold_pct(self, value: float) -> float:
        threshold = _require_positive_threshold(value)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (_SETTING_DEFAULT_THRESHOLD, _format_threshold(threshold)),
            )
        return threshold

    def _ensure_parent(self) -> None:
        parent = Path(self.path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_SCHEMA)
        _migrate_watchlist(conn)

    def _seed_if_needed(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (_SETTING_SEEDED,)
        ).fetchone()
        if row is not None:
            return
        tickers = SEED_TICKERS
        threshold = default_threshold_pct()
        conn.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (_SETTING_DEFAULT_THRESHOLD, _format_threshold(threshold)),
        )
        for symbol in tickers:
            normalized = _normalize_symbol(symbol)
            if not normalized:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO watchlist (
                    symbol, threshold_pct, mode, threshold_unit
                )
                VALUES (?, ?, 'once', 'pct')
                """,
                (normalized, threshold),
            )
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, '1')",
            (_SETTING_SEEDED,),
        )


def _normalize_symbol(symbol: str) -> str:
    return (symbol or "").strip().upper()


def _normalize_mode(mode: str) -> AlertMode:
    normalized = (mode or "once").strip().lower()
    if normalized in VALID_MODES:
        return normalized  # type: ignore[return-value]
    return "once"


def _normalize_unit(unit: str) -> ThresholdUnit:
    normalized = (unit or "pct").strip().lower()
    if normalized in VALID_UNITS:
        return normalized  # type: ignore[return-value]
    return "pct"


def _normalize_direction(direction: str) -> AlertDirection:
    normalized = (direction or "down").strip().lower()
    if normalized == "rise":
        normalized = "up"
    elif normalized == "drop":
        normalized = "down"
    if normalized in VALID_DIRECTIONS:
        return normalized  # type: ignore[return-value]
    return "down"


def _is_positive(value: float | None) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _require_positive_threshold(value: float, field: str = "threshold_pct") -> float:
    threshold = float(value)
    if threshold <= 0 or not math.isfinite(threshold):
        raise ValueError(f"{field} must be a positive number")
    return threshold


def _migrate_watchlist(conn: sqlite3.Connection) -> None:
    """Add unit/usd/direction columns to DBs created before those fields existed."""
    cols = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(watchlist)").fetchall()
    }
    if not cols:
        return
    if "threshold_unit" not in cols:
        conn.execute(
            "ALTER TABLE watchlist ADD COLUMN threshold_unit TEXT NOT NULL DEFAULT 'pct'"
        )
    if "threshold_usd" not in cols:
        conn.execute("ALTER TABLE watchlist ADD COLUMN threshold_usd REAL")
    if "direction" not in cols:
        conn.execute(
            "ALTER TABLE watchlist ADD COLUMN direction TEXT NOT NULL DEFAULT 'down'"
        )
    _migrate_alert_state(conn)


def _migrate_alert_state(conn: sqlite3.Connection) -> None:
    """Add rise-side memory columns to DBs created before up alerts existed."""
    cols = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(alert_state)").fetchall()
    }
    if not cols:
        return
    if "fired_up" not in cols:
        conn.execute(
            "ALTER TABLE alert_state ADD COLUMN fired_up INTEGER NOT NULL DEFAULT 0"
        )
    if "last_leg_up" not in cols:
        conn.execute(
            "ALTER TABLE alert_state ADD COLUMN last_leg_up INTEGER NOT NULL DEFAULT 0"
        )


def _format_threshold(value: float) -> str:
    return repr(float(value))


def _row_to_config(row: sqlite3.Row) -> TickerConfig:
    keys = set(row.keys())
    unit = "pct"
    if "threshold_unit" in keys and row["threshold_unit"]:
        unit = _normalize_unit(str(row["threshold_unit"]))
    usd = None
    if "threshold_usd" in keys and row["threshold_usd"] is not None:
        usd = float(row["threshold_usd"])
        if not _is_positive(usd):
            usd = None
    direction = "down"
    if "direction" in keys and row["direction"]:
        direction = _normalize_direction(str(row["direction"]))
    return TickerConfig(
        symbol=str(row["symbol"]),
        threshold_pct=float(row["threshold_pct"]),
        mode=_normalize_mode(str(row["mode"])),
        threshold_unit=unit,
        threshold_usd=usd,
        direction=direction,
    )


def _row_to_state(row: sqlite3.Row) -> AlertState:
    keys = set(row.keys())
    fired_up = bool(row["fired_up"]) if "fired_up" in keys else False
    last_leg_up = int(row["last_leg_up"]) if "last_leg_up" in keys else 0
    return AlertState(
        day_key=str(row["day_key"]),
        fired=bool(row["fired"]),
        last_leg=int(row["last_leg"]),
        fired_up=fired_up,
        last_leg_up=last_leg_up,
    )


def _upsert_state(conn: sqlite3.Connection, symbol: str, state: AlertState) -> None:
    conn.execute(
        """
        INSERT INTO alert_state (
            symbol, day_key, fired, last_leg, fired_up, last_leg_up
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, day_key) DO UPDATE SET
            fired = excluded.fired,
            last_leg = excluded.last_leg,
            fired_up = excluded.fired_up,
            last_leg_up = excluded.last_leg_up
        """,
        (
            symbol,
            state.day_key,
            1 if state.fired else 0,
            int(state.last_leg),
            1 if state.fired_up else 0,
            int(state.last_leg_up),
        ),
    )
