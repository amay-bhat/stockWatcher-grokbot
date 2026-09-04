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

Null or zero prices are skipped (no fake -100% moves). `config.example.json` is the later watchlist shape; M1–M3 ignore it.

## Tests (Milestone 2)

Alert decisions live in `src/alerts.py` as pure functions (no I/O, no clock). Baseline is previous close; a ticker fires when the drop is at least its `threshold_pct`. Modes: `once` (first cross only), `legs` (again each extra full step down), `mute` (never). Daily reset is the caller-supplied `day_key`.

```bash
python3 -m unittest tests.test_alerts
python3 -m unittest discover -s tests
```

## Milestone 3 — Telegram outbound

Still a **static hardcoded watchlist** (the five demo tickers). `--check` evaluates each quote with default threshold **3%** and mode **`once`**, using in-memory state for that process only. If any fire, **one batched Telegram message** is sent. No inbound commands, no SQLite yet.

### Create a bot and get a chat id

1. In Telegram, open [@BotFather](https://t.me/BotFather) and send `/newbot`. Follow the prompts and copy the bot token.
2. Open your new bot and send it any message (e.g. `/start`) so a chat exists.
3. Get your chat id: visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser after messaging the bot, then copy `message.chat.id` (a number; for a group it may be negative).

### Run a one-shot check

```bash
export FINNHUB_API_KEY=your_key
export TELEGRAM_BOT_TOKEN=your_bot_token
export TELEGRAM_CHAT_ID=your_chat_id
# optional; default 3.0
export DEFAULT_THRESHOLD_PCT=3.0

python -m src.main --check
# same thing:
python -m src.check_once
```

If nothing is down enough, stdout is `no alerts`. If something fires, stdout prints `sent:` plus the message body. Missing `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` is a hard error (nothing is sent). Default `python -m src.main` is still the quote printer and does not need Telegram.

Each `--check` process starts with empty alert memory, so a name that is still through the threshold will notify again on the next run (persistence is M4).

## Environment variables

| Variable | Used in |
| --- | --- |
| `FINNHUB_API_KEY` | Quote fetch |
| `TELEGRAM_BOT_TOKEN` | M3 Telegram send |
| `TELEGRAM_CHAT_ID` | M3 Telegram send |
| `DEFAULT_THRESHOLD_PCT` | M3 check (default `3.0`) |
| `CHECK_INTERVAL_MINUTES` | later (scheduler stub) |

## Build order

- **M1** — Finnhub `/quote` fetch; print current vs previous close.
- **M2** — Drop alerts vs previous close using a percent threshold (`once` / `legs` / `mute`).
- **M3** — Telegram outbound for alerts (this).
- **M4** — SQLite store for watchlist / last-seen state.
- **M5** — Inbound Telegram commands (`/list`, `/add`, …).
- **M6** — Periodic scheduler + market calendar (skip closed sessions).
- **M7** — Deploy on Fly.io with a SQLite volume.

Deploy is not part of M3. Host later: Fly.io + SQLite volume.
