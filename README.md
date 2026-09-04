# Watchlist Drop Alerts

Awareness tool: fetch quotes and notify when a watchlist name drops versus previous close. Nothing here places trades or builds a portfolio.

**Non-goals:** no signals, trading, portfolio tracking, news, or web UI.

## Run (Milestone 6 — production entry)

Telegram commands and the market-hours scheduler run in one process. The scheduler fetches quotes every `CHECK_INTERVAL_MINUTES` (default 5) **only during the NYSE regular session** (9:30–16:00 America/New_York, trading days; 13:00 ET close on half-days). Outside those hours it sleeps until the next open — no price polling, no alerts.

```bash
export FINNHUB_API_KEY=your_key
export TELEGRAM_BOT_TOKEN=your_bot_token
export TELEGRAM_CHAT_ID=your_chat_id
export DATABASE_PATH=./data/watchlist.db
# optional; default 5
export CHECK_INTERVAL_MINUTES=5

python -m src.main
# same thing:
python -m src.main --serve
```

Ctrl+C stops both the bot and the scheduler.

Gap-down at the open: the first cycle after 9:30 ET uses a fresh `day_key` (the ET date of that session), so a name that is already through its threshold alerts once.

A quote/check failure is logged and that cycle is skipped (the process stays up). Three consecutive failed open cycles send a one-line Telegram warning, then the streak resets.

## Milestone 1 — quote printer

```bash
export FINNHUB_API_KEY=your_key
python -m src.main --quotes
```

Prints one line per demo ticker (`NVDA`, `AMD`, `AAPL`, `MSFT`, `GOOGL`):

```
NVDA  $120.50  +1.23%  (prev close $119.04)
```

Null or zero prices are skipped (no fake -100% moves). `config.example.json` is the later watchlist shape; M1–M3 ignored it. M4 stores the live watchlist in SQLite instead.

## Tests (Milestone 2)

Alert decisions live in `src/alerts.py` as pure functions (no I/O, no clock). Baseline is previous close. Each ticker has a unit: **`pct` (default)** fires when the drop is at least `threshold_pct`; **`usd`** fires when `prev_close - price` is at least `threshold_usd` dollars. Modes: `once` (first cross only), `legs` (again each extra full step down — 1×, 2×, 3× the same percent or dollar amount), `mute` (never). Daily reset is the caller-supplied `day_key`.

```bash
python3 -m unittest tests.test_alerts
python3 -m unittest discover -s tests
```

## Milestone 3 — Telegram outbound

`--check` evaluates the watchlist and, if anything fires, sends **one batched Telegram message**. Inbound commands are M5. The scheduler (M6) reuses this same check cycle.

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

If nothing is down enough, stdout is `no alerts`. If something fires, stdout prints `sent:` plus the message body. Missing `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` is a hard error (nothing is sent). `--check` and `/check` are not gated on market hours.

## Milestone 5 — Telegram command listener

The bot is the config UI. Long-poll `getUpdates` (stdlib urllib, no telegram library) and persist every mutation through `src/store.py`. Commands from any chat other than `TELEGRAM_CHAT_ID` are ignored silently. Tickers are normalized to uppercase. `/add` asks Finnhub for a quote and rejects unknown / zero / null symbols.

Bot-only (no scheduler):

```bash
export FINNHUB_API_KEY=your_key
export TELEGRAM_BOT_TOKEN=your_bot_token
export TELEGRAM_CHAT_ID=your_chat_id
export DATABASE_PATH=./data/watchlist.db

python -m src.main --bot
# same thing:
python -m src.bot
```

Ctrl+C stops the listener. Prefer `python -m src.main` so commands keep working while the scheduler runs.

| Command | Effect |
| --- | --- |
| `/list` | Tickers with threshold and mode |
| `/add TICKER [pct\|$5]` | Add (optional threshold, else stored default); Finnhub-validated. `$5` or `5usd` stores a dollar drop from previous close |
| `/remove TICKER` | Remove |
| `/set TICKER 5%\|$5` | Change threshold and unit (`5` / `5%` = percent; `$5` / `5usd` = dollars). `/set TICKER pct` switches back to the stored percent |
| `/mode TICKER once\|legs\|mute` | Change mode |
| `/mute TICKER` | Shorthand for mode mute |
| `/status` | Current price and % change |
| `/check` | Immediate poll cycle (`check_once`) |
| `/default pct` | Persist global default threshold |
| `/help` | Command reference |

Mutating commands reply with a one-line echo of the new state (e.g. `TSLA  3%  once` or `NVDA  $5  once`).

Dollar thresholds are still versus **previous close**, not cost basis. Existing SQLite rows migrate in place (`threshold_unit=pct`, `threshold_usd` null).

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

On Fly, `DATABASE_PATH` is `/data/watchlist.db` on the mounted volume (see Deploy below).

## Milestone 6 — market calendar + scheduler

`src/calendar.py` implements the NYSE regular session with `zoneinfo` (`America/New_York`) and a computed holiday/half-day set (Good Friday via Computus, observed federal-style closures). No `exchange_calendars` / `pandas_market_calendars` dependency.

- Regular session: `[09:30, 16:00)` ET on trading days
- Half-days: `[09:30, 13:00)` ET (day after Thanksgiving; Christmas Eve when it is a weekday and not a full holiday; July 3 when that weekday is not already Independence Day observed)
- Weekends and full holidays: closed
- `day_key` is the ET calendar date of the session

`src/scheduler.py` sleeps until the next open when closed, and otherwise runs `check_once` every `CHECK_INTERVAL_MINUTES`. Scheduler-only:

```bash
python -m src.scheduler
```

```bash
python3 -m unittest tests.test_calendar tests.test_scheduler
python3 -m unittest discover -s tests
```

## Environment variables

| Variable | Used in |
| --- | --- |
| `FINNHUB_API_KEY` | Quote fetch |
| `TELEGRAM_BOT_TOKEN` | M3 send / M5 long-poll / M6 combined serve |
| `TELEGRAM_CHAT_ID` | M3 send / M5 allowed chat (others ignored) |
| `DEFAULT_THRESHOLD_PCT` | Seed default (default `3.0`); stored in SQLite after first run; `/default` updates the DB value |
| `DATABASE_PATH` | M4 SQLite file (default `./data/watchlist.db`; Fly `/data/watchlist.db`) |
| `CHECK_INTERVAL_MINUTES` | M6 scheduler interval while the regular session is open (default `5`) |

## Build order

- **M1** — Finnhub `/quote` fetch; print current vs previous close (`--quotes`).
- **M2** — Drop alerts vs previous close using a percent threshold (`once` / `legs` / `mute`).
- **M3** — Telegram outbound for alerts.
- **M4** — SQLite store for watchlist / last-seen state.
- **M5** — Inbound Telegram commands (`/list`, `/add`, …).
- **M6** — Periodic scheduler + market calendar. Combined process: `python -m src.main`.
- **M7** — Deploy on Fly.io with a SQLite volume (this).

## Deploy (Milestone 7 — Fly.io)

Always-on Fly Machine. Telegram long-poll + the market-hours scheduler (`python -m src.main`). SQLite lives on a persistent volume at `/data/watchlist.db` so a redeploy does not reset the watchlist or re-fire today's alerts.

No public HTTP service. The smallest `shared-cpu-1x` / 256MB VM is enough (~$2–3/mo if it runs 24/7, plus a 1GB volume). Keep **one** Machine — a Fly volume can only attach to one at a time.

Secrets stay out of the repo (`fly secrets set` only). `FINNHUB_API_KEY`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID` are required; `CHECK_INTERVAL_MINUTES` and `DEFAULT_THRESHOLD_PCT` keep their code defaults unless you set them.

Three consecutive failed open-session check cycles still send a one-line Telegram warning (M6), then the streak resets. Watch `fly logs` if quotes or Telegram look stuck.

### 1. Install flyctl and log in

macOS:

```bash
brew install flyctl
fly auth login
```

Linux / WSL:

```bash
curl -L https://fly.io/install.sh | sh
fly auth login
```

### 2. Create the app

This repo already has `fly.toml` (app name `stockwatcher-grokbot`) and a `Dockerfile`. From the repo root:

```bash
fly launch --no-deploy --copy-config --yes --ha=false
```

`--ha=false` keeps a single Machine (required for the SQLite volume). If the app name is taken globally, change `app = "..."` in `fly.toml` and re-run, or:

```bash
fly apps create your-app-name
```

Pick the same region as `primary_region` in `fly.toml` (default `sjc`). Change that key first if you want another [Fly region](https://fly.io/docs/reference/regions/).

### 3. Create the data volume

Volume name must match `source` in `fly.toml` (`data`). Size is 1GB (plenty for SQLite):

```bash
fly volumes create data --region sjc --size 1
```

Use the same region as `primary_region`. `fly.toml` also sets `initial_size = "1gb"`, so `fly deploy` can create the volume if this step was skipped.

### 4. Set secrets

```bash
fly secrets set \
  FINNHUB_API_KEY=your_key \
  TELEGRAM_BOT_TOKEN=your_bot_token \
  TELEGRAM_CHAT_ID=your_chat_id
```

Do not put these values in `fly.toml`, `.env`, or git.

### 5. Deploy

```bash
fly deploy
```

Confirm it stays up:

```bash
fly status
fly logs
```

Ctrl+C is local-only. On Fly, `fly apps restart` / a new `fly deploy` recycles the Machine; the volume keeps `/data/watchlist.db`.

### After deploy

| Command | Effect |
| --- | --- |
| `fly logs` | Follow stdout/stderr (scheduler + bot) |
| `fly status` | Machine running? volume attached? |
| `fly ssh console` | Shell on the Machine (`ls /data`) |
| `fly apps restart` | Bounce the process without rebuilding |

In Telegram, `/list` and `/status` should work the same as locally. First boot of an empty volume seeds the demo tickers (`NVDA`, `AMD`, `AAPL`, `MSFT`, `GOOGL`).
