# Watchlist Drop Alerts

Awareness tool: fetch quotes and notify when a watchlist name drops versus previous close. Nothing here places trades or builds a portfolio.

**Non-goals:** no signals, trading, portfolio tracking, news, or web UI.

## Run Milestone 1 (quote printer)

```bash
export FINNHUB_API_KEY=your_key
python -m src.main
```

Prints one line per demo ticker (`NVDA`, `AMD`, `AAPL`, `MSFT`, `GOOGL`):

```
NVDA  $120.50  +1.23%  (prev close $119.04)
```

Null or zero prices are skipped (no fake -100% moves). `config.example.json` is the later watchlist shape; M1–M3 ignored it. M4 stores the live watchlist in SQLite instead.

## Tests (Milestone 2)

Alert decisions live in `src/alerts.py` as pure functions (no I/O, no clock). Baseline is previous close; a ticker fires when the drop is at least its `threshold_pct`. Modes: `once` (first cross only), `legs` (again each extra full step down), `mute` (never). Daily reset is the caller-supplied `day_key`.

```bash
python3 -m unittest tests.test_alerts
python3 -m unittest discover -s tests
```

## Milestone 3 — Telegram outbound

`--check` evaluates the watchlist and, if anything fires, sends **one batched Telegram message**. Inbound commands are M5.

### Create a bot and get a chat id

1. In Telegram, open [@BotFather](https://t.me/BotFather) and send `/newbot`. Follow the prompts and copy the bot token.
2. Open your new bot and send it any message (e.g. `/start`) so a chat exists.
3. Get your chat id: visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser after messaging the bot, then copy `message.chat.id` (a number; for a group it may be negative).

### Run a one-shot check

```bash
export FINNHUB_API_KEY=your_key
export TELEGRAM_BOT_TOKEN=your_bot_token
export TELEGRAM_CHAT_ID=your_chat_id
# optional; default 3.0 (used when seeding an empty DB)
export DEFAULT_THRESHOLD_PCT=3.0
# optional; default ./data/watchlist.db
export DATABASE_PATH=./data/watchlist.db

python -m src.main --check
# same thing:
python -m src.check_once
```

If nothing is down enough, stdout is `no alerts`. If something fires, stdout prints `sent:` plus the message body. Missing `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` is a hard error (nothing is sent). Default `python -m src.main` is still the quote printer and does not need Telegram.

## Milestone 5 — Telegram command listener

The bot is the config UI. Long-poll `getUpdates` (stdlib urllib, no telegram library) and persist every mutation through `src/store.py`. Commands from any chat other than `TELEGRAM_CHAT_ID` are ignored silently. Tickers are normalized to uppercase. `/add` asks Finnhub for a quote and rejects unknown / zero / null symbols.

```bash
export FINNHUB_API_KEY=your_key
export TELEGRAM_BOT_TOKEN=your_bot_token
export TELEGRAM_CHAT_ID=your_chat_id
export DATABASE_PATH=./data/watchlist.db

python -m src.main --bot
# same thing:
python -m src.bot
```

Ctrl+C stops the listener. `--check` and the default quote printer are unchanged.

| Command | Effect |
| --- | --- |
| `/list` | Tickers with threshold and mode |
| `/add TICKER [pct]` | Add (optional threshold, else stored default); Finnhub-validated |
| `/remove TICKER` | Remove |
| `/set TICKER pct` | Change threshold |
| `/mode TICKER once\|legs\|mute` | Change mode |
| `/mute TICKER` | Shorthand for mode mute |
| `/status` | Current price and % change |
| `/check` | Immediate poll cycle (`check_once`) |
| `/default pct` | Persist global default threshold |
| `/help` | Command reference |

Mutating commands reply with a one-line echo of the new state (e.g. `TSLA  3%  once`).

```bash
python3 -m unittest tests.test_bot_commands tests.test_bot
python3 -m unittest discover -s tests
```

## Milestone 4 — SQLite persistence

Watchlist config and per-day alert state live in a local SQLite file so a process restart (or a mid-day redeploy) does **not** re-alert names that already fired this trading day.

On first run / empty DB the store seeds `NVDA`, `AMD`, `AAPL`, `MSFT`, `GOOGL` at `DEFAULT_THRESHOLD_PCT` (default `3.0`) with mode `once`. After that, ticker rows and `AlertState` (`day_key` / `fired` / `last_leg`) are loaded and written by `check_once`.

```bash
export DATABASE_PATH=./data/watchlist.db
python3 -m unittest tests.test_store tests.test_check_once
python3 -m unittest discover -s tests
```

I/O is in `src/store.py` (stdlib `sqlite3` only). `src/alerts.py` and `PriceProvider` stay pure. `data/` and `*.db` are gitignored.

On Fly later, point `DATABASE_PATH` at the mounted volume, e.g. `/data/watchlist.db` (M7).

## Environment variables

| Variable | Used in |
| --- | --- |
| `FINNHUB_API_KEY` | Quote fetch |
| `TELEGRAM_BOT_TOKEN` | M3 send / M5 long-poll |
| `TELEGRAM_CHAT_ID` | M3 send / M5 allowed chat (others ignored) |
| `DEFAULT_THRESHOLD_PCT` | Seed default (default `3.0`); stored in SQLite after first run; `/default` updates the DB value |
| `DATABASE_PATH` | M4 SQLite file (default `./data/watchlist.db`; Fly later `/data/watchlist.db`) |
| `CHECK_INTERVAL_MINUTES` | later (scheduler stub) |

## Build order

- **M1** — Finnhub `/quote` fetch; print current vs previous close.
- **M2** — Drop alerts vs previous close using a percent threshold (`once` / `legs` / `mute`).
- **M3** — Telegram outbound for alerts.
- **M4** — SQLite store for watchlist / last-seen state.
- **M5** — Inbound Telegram commands (`/list`, `/add`, …) (this).
- **M6** — Periodic scheduler + market calendar (skip closed sessions).
- **M7** — Deploy on Fly.io with a SQLite volume.

Deploy is not part of M5. No scheduler daemon here — that is M6. Host later: Fly.io + SQLite volume.
