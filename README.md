# Watchlist Drop Alerts

Awareness tool: fetch quotes and (later) notify when a watchlist name drops versus previous close. Nothing here places trades or builds a portfolio.

**Non-goals:** no signals, trading, portfolio tracking, news, or web UI.

## Run Milestone 1

```bash
export FINNHUB_API_KEY=your_key
python -m src.main
```

Prints one line per demo ticker (`NVDA`, `AMD`, `AAPL`, `MSFT`, `GOOGL`):

```
NVDA  $120.50  +1.23%  (prev close $119.04)
```

Null or zero prices are skipped (no fake -100% moves). `config.example.json` is the later watchlist shape; M1 ignores it. `python -m src.main` is still the quote printer (alerts are not wired in yet).

## Tests (Milestone 2)

Alert decisions live in `src/alerts.py` as pure functions (no I/O, no clock). Baseline is previous close; a ticker fires when the drop is at least its `threshold_pct`. Modes: `once` (first cross only), `legs` (again each extra full step down), `mute` (never). Daily reset is the caller-supplied `day_key`. Run tests with stdlib unittest from the repo root:

```bash
python -m unittest tests.test_alerts
```

Or discover everything under `tests/`:

```bash
python -m unittest discover -s tests
```

## Environment variables

| Variable | Used in |
| --- | --- |
| `FINNHUB_API_KEY` | M1 quote fetch |
| `TELEGRAM_BOT_TOKEN` | later (Telegram stub) |
| `TELEGRAM_CHAT_ID` | later (Telegram stub) |
| `CHECK_INTERVAL_MINUTES` | later (scheduler stub) |
| `DEFAULT_THRESHOLD_PCT` | later (alerts stub) |

## Build order

- **M1** — Finnhub `/quote` fetch; print current vs previous close.
- **M2** — Drop alerts vs previous close using a percent threshold (`once` / `legs` / `mute`).
- **M3** — SQLite store for watchlist / last-seen state.
- **M4** — Periodic scheduler (`CHECK_INTERVAL_MINUTES`).
- **M5** — Telegram notifications.
- **M6** — Market calendar (skip closed sessions).
- **M7** — Deploy on Fly.io with a SQLite volume.

Deploy is not part of M1. Host later: Fly.io + SQLite volume.
