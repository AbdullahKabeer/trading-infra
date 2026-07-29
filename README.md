# EvalPass

Automated VWAP mean-reversion trading bot targeting the **TopStepX Combine** ($50K account, $3K profit target, $2K trailing max loss limit). Trades ES E-mini S&P 500 and NQ E-mini NASDAQ futures live via the TopStepX SignalR API, with a full backtest/optimization pipeline.

The primary implementation is the **Rust bot** (`rust/`). The Python files (`combine_live.py`, `optimize_es.py`, etc.) remain for optimization, Monte Carlo simulation, and reference.

---

## Table of Contents

1. [Strategy Overview](#strategy-overview)
2. [Architecture](#architecture)
3. [File Reference](#file-reference)
4. [Setup](#setup)
5. [Running the Live Bot](#running-the-live-bot)
6. [Dashboard](#dashboard)
7. [Backtesting](#backtesting)
8. [Optimization](#optimization)
9. [Monte Carlo Pass Simulator](#monte-carlo-pass-simulator)
10. [Key Parameters](#key-parameters)
11. [TopStepX Combine Rules](#topstepx-combine-rules)
12. [Data Directories](#data-directories)
13. [Instruments](#instruments)

---

## Strategy Overview

The bot trades **VWAP z-score reversion** on 1-minute bars during NYSE Regular Trading Hours (9:30–16:00 ET).

### Entry Logic

A signal fires when price deviates from the session VWAP by at least `Z_THRESH` standard deviations:

- **LONG**: price is `Z_THRESH` σ *below* VWAP → fade back to VWAP
- **SHORT**: price is `Z_THRESH` σ *above* VWAP → fade back to VWAP

The take-profit buffer scales dynamically with the entry z-score depth:
`tp_buffer_ticks = max(3, int(z_abs * 2))`

### Filter Stack (all must pass before entry)

| Filter | Parameter | What it blocks |
|---|---|---|
| Session warm-up | 45 bars minimum | Avoids entering with insufficient VWAP data |
| VPOC stability | `max_migration=0.15 pts/bar` | Trending sessions where VPOC is moving fast |
| Sigma panic | `SIGMA_EXP_MAX=1.25` | Bars where volatility just expanded ≥25% vs 5 bars ago |
| ATR slope | `ATR_SLOPE_MAX=0.10` | Bars where 10-bar ATR is expanding (trend developing) |
| VWAP slope | `VWAP_SLOPE_THRESH=0.3` | Entry direction must match or be neutral to VWAP slope |
| Volume confirmation | `MIN_VOL_REL=0.80` | Bar volume < 80% of 5-bar average |
| Bid-ask spread | `MAX_SPREAD=1.0 pt` | Wide spreads (thin tape) — live only |
| Lunch skip | 120–195 tod_mins (11:30–12:45 ET) | Thin midday tape |
| Power Hour cutoff | 330 tod_mins (3:00 PM ET) | No new entries in the last hour |
| Trade count cap | `MAX_TRADES=11` | Hard daily entry limit |
| Gutter win | `GUTTER_GOAL=$1,500` | Stop new entries after daily profit target hit |
| Gutter loss | `GUTTER_DD=$1,900` | Stop new entries after daily loss cap hit |
| Direction cooldown | `DIR_COOLDOWN_BARS=8` | Block same-direction re-entry for 8 bars after a stop-out |
| Cumulative delta | `CUM_DELTA_FADE_MAX=0.12` | Skip if order flow strongly opposes the fade direction |
| High-impact calendar | `HIGH_IMPACT_DATES` | FOMC/CPI/NFP days: `"reduce"` raises z-thresh +0.5, `"skip"` blocks all trades |

### Regime Adjustments

- **VWAP crossings** (last 20 bars): ≤1 crossings → trending regime, z-thresh multiplied by 1.3
- **Session type** (classified at bar 45): TRENDING / RANGING / NEUTRAL via VPOC migration and range heuristics
- **VIX proxy**: fetched from TopStepX VX futures at RTH open; EXTREME (>30) / HIGH (>22) / NORMAL
- **Previous session levels**: prior day VWAP, VPOC, close loaded at RTH open for confluence tightening (`PREV_LEVEL_Z_BONUS=-0.10` z-thresh reduction when near those levels)

### Stop & Exit Management

| Mechanism | Parameter | Behavior |
|---|---|---|
| ATR stop | `ATR_STOP_RATIO=0.80` | Stop = 0.80 × 20-bar ATR, clamped 4–12 ticks |
| Breakeven | `BREAKEVEN_TICKS=6` | Stop moves to entry price once position is 6 ticks in profit |
| Trailing stop | `TRAIL_ACTIVATE=8`, `TRAIL_DISTANCE=4` | Trailing begins after 8 ticks of run-up; trails 4 ticks behind peak |
| Time stop | `TIME_STOP_MINS=90` | Force-close any position open longer than 90 bars |
| Early time stop | 45 bars (half of 90) | Close early if still losing (< −4 ticks) |
| Hard time cap | 180 bars | Safety valve — force-close regardless |
| Partial scale-out | `SCALE_OUT_ENABLED=True` | With 2 contracts: exit 1 at VWAP, trail the other; SL moves to breakeven |
| Gutter win exit | `GUTTER_GOAL=$1,500` | Close open position the moment daily P/L would reach $1,500 |
| Gutter loss exit | `GUTTER_DD=$1,900` | Close open position the moment daily P/L would breach −$1,900 |

### Server-Side Brackets

SL and TP are submitted as bracket orders to the broker at entry. The bot monitors and provides a backup exit if the server bracket fails to fire within 2 ticks of the level. VWAP TP drift is synced to the server: if VWAP moves ≥3 ticks from the entry anchor, the TP bracket is repriced.

After entry, the bot queries the server for the actual fill price and rebases the internal EP/SL/TP to the broker's average fill.

---

## Architecture

### Rust Bot (primary)

```
TopStepX API (HTTPS)           TopStepX SignalR WebSocket
        │                               │
        │  Auth / History / Orders      │  GatewayTrade / GatewayQuote / User hub
        ▼                               ▼
              rust/src/main.rs  (Tokio async runtime)
              │
              ├── feed/websocket.rs     SignalR WebSocket loop (bar + tick events)
              ├── feed/user_hub.rs      Account position / order fill stream
              │
              ├── market/session.rs     Session (bars, VWAP, VPOC, vol profile,
              │                         cum delta, ATR, std history)
              ├── market/regime.rs      Regime state (VIX proxy, crossings,
              │                         session type, prev levels, calendar)
              │
              ├── strategy/bot.rs       VwapReclaim + FirstPullback strategies
              ├── strategy/order_manager.rs  Order placement / bracket mgmt
              ├── strategy/backtest.rs  Offline replay (mirrors live filter stack)
              │
              ├── api/                  Auth, contracts, orders, history, trades
              │
              └── web/server.rs         Axum HTTP + WebSocket server :8080
                    ├── GET /           Bloomberg-style live dashboard
                    ├── WS  /ws         100 ms push (full state snapshot)
                    ├── GET /api/state  REST snapshot (JSON)
                    ├── GET /api/day/<date>  Historical session replay
                    ├── GET /api/sessions   Date list
                    ├── POST /api/toggle_gw  Toggle gutter win
                    └── POST /api/toggle_gl  Toggle gutter loss
```

Concurrent Tokio tasks: SignalR feed, User hub, Strategy bot, Order manager, Web server, Stats worker (60 s), Session saver (30 s).

### Python Tools (optimization / simulation)

```
combine_live.py   Reference implementation + backtest engine used by optimizers
optimize_es.py    ES grid-search optimizer (7 params × 4–6 values, Monte Carlo scored)
optimize_nq.py    NQ optimizer
sim_combine.py    TopStepX Combine pass-rate Monte Carlo simulator (1,000 sims)
tick_backtest.py  Tick-level SL/TP back-tester against es_ticks/*.csv
```

---

## File Reference

### Rust (`rust/src/`)

| File | Purpose |
|---|---|
| `main.rs` | Entry point — startup menu, auth, account/contract discovery, task spawning |
| `config.rs` | All strategy parameters (single source of truth) |
| `types.rs` | Shared types: `Bar`, `TradeRecord`, `AppState`, `BotSnapshot`, `RegimeState`, `Position` |
| `feed/websocket.rs` | SignalR WebSocket loop, bar builder, tick recorder |
| `feed/user_hub.rs` | Account stream — position sync, fill events |
| `market/session.rs` | `Session` struct: VWAP, VPOC, vol profile, ATR, cum delta, disk persistence |
| `market/regime.rs` | VIX proxy fetch, calendar check, prev session level loader |
| `strategy/bot.rs` | `BotState`: entry/exit decisions, trailing stop, breakeven, time stop, gutter controls |
| `strategy/order_manager.rs` | Bracket order placement, SL/TP repricing, fill reconciliation |
| `strategy/backtest.rs` | Bar-level backtest replay: mirrors live filter stack exactly |
| `api/auth.rs` | TopStepX key-based auth, token refresh |
| `api/history.rs` | 35-day RTH bar backfill |
| `api/orders.rs` | Order placement, cancellation, modification |
| `api/positions.rs` | Position query |
| `api/trades.rs` | Closed trade history fetch |
| `web/server.rs` | Axum server + WebSocket push + Bloomberg-style dashboard HTML/JS |
| `backtest_runner.rs` | CLI `backtest` subcommand (offline, no auth) |
| `backtest_server.rs` | CLI `backtest-server` subcommand (local HTTP GUI) |
| `startup_menu.rs` | Interactive instrument and account selection menu |

### Python

| File | Purpose |
|---|---|
| `combine_live.py` | Reference bot — 3,846 lines. Live feed, Session, VWAPBot, backtest engine, HTTP dashboard |
| `optimize_es.py` | ES grid-search optimizer |
| `optimize_nq.py` | NQ optimizer |
| `optimize_params.py` | General optimizer (ES + SPY proxy sessions) |
| `backtest_gc.py` | Gold (GC) backtester |
| `tick_backtest.py` | Tick-level backtester against raw `es_ticks/*.csv` |
| `sim_combine.py` | TopStepX Combine pass-rate Monte Carlo simulator |
| `sortino_3d_model.py` | 3D Sortino surface over z-thresh / stop-ratio parameter space |
| `fetch_nq.py` | Fetches NQ RTH 1-min bars into `nq_sessions/` |
| `fetch_gc.py` | Fetches GC RTH 1-min bars into `gc_sessions/` |

### Data

| Directory | Contents |
|---|---|
| `es_sessions/` | JSON per RTH session for ES |
| `nq_sessions/` | JSON per RTH session for NQ |
| `gc_sessions/` | JSON per RTH session for Gold |
| `spy_proxy_sessions/` | SPY proxy sessions used to pad ES training data |
| `es_ticks/` | Raw tick CSVs for ES (T/Q rows) |
| `optimization_results_es.csv` | Grid search output for ES |
| `optimization_results_nq.csv` | Grid search output for NQ |

---

## Setup

### Prerequisites

- Rust (stable, 1.75+): `rustup update stable`
- TopStepX account with API access enabled
- `.env` file in the project root

### Configure Credentials

```
PROJECT_X_USERNAME=your@email.com
PROJECT_X_API_KEY=your_api_key_here
```

### Build

```bash
cd rust
cargo build --release
```

---

## Running the Live Bot

```bash
cd rust
cargo run --release
```

On first run (interactive TTY), a menu prompts for:
1. **Instrument**: ES (VWAP Reclaim) or NQ (First Pullback)
2. **Account**: auto-selects the practice account, or prompts if multiple are available

Skip the menu with flags:

```bash
cargo run --release -- --es   # ES VWAP Reclaim
cargo run --release -- --nq   # NQ First Pullback
```

**Startup sequence:**
1. Authenticates with TopStepX
2. Backfills the last 35 RTH days of 1-min bars (skips days already cached)
3. Loads previous session VWAP/VPOC/close into regime state
4. Checks the economic calendar
5. Fetches VIX proxy from VX futures
6. Starts the Axum web server on port 8080
7. Connects to SignalR WebSocket and begins streaming
8. Opens `http://localhost:8080` in the default browser

**Dry-run / bot disable** — set in `rust/src/config.rs`:

```rust
pub const DRY_RUN: bool = false;   // true = log orders, don't send
pub const BOT_ACTIVE: bool = true; // false = data collection only
```

---

## Dashboard

Open `http://localhost:8080` while the bot is running. State is pushed over WebSocket every 100 ms.

### Layout

**Fixed header** — live bid / ask / spread, VWAP, Z-score, VPOC, phase badge (PRE / RTH / POST), GW/GL toggle buttons.

**Left column (main chart area):**
- Session timeline bar — 9:30→16:00 ET with open-range (red), main (green), close-avoid (amber) zones
- Interactive candlestick chart (scroll to zoom, drag to pan, hover for OHLCVZ tooltip)
  - VPOC, VPOC60, VWAP, HMA overlay lines with right-axis labels
  - Trade windows: green (TP zone), red (SL zone) shaded rectangles; dashed SL/TP lines; MAX and EXIT vertical markers
  - Live session guide lines (open / high / low / bid / ask / last)
  - 20%-width volume profile overlay
  - Bottom sub-panel: volume bars + volatility σ (amber) + Z-score (purple) + P&L equity curve
- Bottom tray (3 cells): tape mini-chart + VP sidebar | volume profile table | recent bars table

**Right column (data panels):**
- P&L card: daily / total, trade count, win streak (last 10 trades as colored squares)
- Confluence scorecard: 8 factors (Z-Score, VWAP Regime, VIX, Calendar, Session, Trades Left, MLL Buffer, Time Window) with GO / WAIT / HALT verdict
- Z-Score gauge: horizontal −3σ to +3σ bar with entry zone highlighted
- Position card: direction, EP, SL, TP, ticks in profit, bars open
- Account card: balance, MLL floor, headroom
- Regime card: VIX level, VWAP crossings, session type, calendar event, prev session levels
- Open orders table
- Trade history table

**Toggle controls** (POST endpoints also callable from the header buttons):
- `POST /api/toggle_gw` — enable/disable gutter win exit
- `POST /api/toggle_gl` — enable/disable gutter loss exit

---

## Backtesting

### CLI (no auth, offline)

```bash
cd rust
cargo run -- backtest es_sessions/2026-04-15.json
```

Prints per-trade results and daily summary to stdout.

### Web GUI

```bash
cd rust
cargo run -- backtest-server
```

Starts a local HTTP server with a browser GUI for replaying and comparing sessions. Open `http://localhost:8081`.

### How it works

`strategy/backtest.rs` mirrors the live filter stack exactly — same VPOC stability check, sigma panic, ATR slope, VWAP slope, cumulative delta, time-of-day filters, trailing stop, breakeven, scale-out. Entries fire on bar-close (no tick-level simulation).

For tick-precision SL/TP back-testing:

```bash
python tick_backtest.py es_ticks/2026-04-15.csv
```

---

## Optimization

### ES

```bash
python optimize_es.py
```

Sweeps the following parameter grid:

| Parameter | Values |
|---|---|
| `contracts` | 1, 2 |
| `z_thresh` | 1.2, 1.5, 1.75, 2.0, 2.25, 2.5 |
| `atr_stop_ratio` | 0.35, 0.50, 0.65, 0.80 |
| `trail_activate` | 8, 12, 16, 20 ticks |
| `trail_distance` | 4, 6, 8, 10 ticks |
| `breakeven_ticks` | 4, 6, 8, 10 ticks |
| `vwap_slope_thresh` | 0.15, 0.30, 0.50, 1.0 |

**Scoring**: 1,000 Monte Carlo simulations per candidate. Each sim randomly draws sessions (with replacement) and runs until the $3K profit target, $2K MLL breach, or 45-session timeout. Score = pass rate × Sortino ratio.

**Walk-forward split**: 65% training / 35% held-out validation. Only candidates that pass both folds are reported.

Results → `optimization_results_es.csv`.

### NQ

```bash
python optimize_nq.py
```

Same structure with NQ-specific constants (tick value $5.00, wider stop defaults).

---

## Monte Carlo Pass Simulator

```bash
python sim_combine.py
```

Uses all sessions in `es_sessions/` and `spy_proxy_sessions/`. Runs 1,000 simulations of a 45-session combine attempt. Reports: pass %, blow %, timeout %, median / 5th-percentile days to pass.

---

## Key Parameters

All parameters live in `rust/src/config.rs` (single source of truth for the Rust bot; `combine_live.py` lines 39–114 for the Python tools).

### Trading

| Parameter | Default | Description |
|---|---|---|
| `BOT_ACTIVE` | `true` | Master switch — false = data collection only |
| `DRY_RUN` | `false` | Log orders without sending them |
| `TARGET_MODE` | `"vwap"` | Target for TP: `"vwap"` or `"volume"` (VPOC) |
| `Z_THRESH` | `1.2` | Minimum z-score to enter (σ from VWAP) |
| `CONTRACTS` | `1` | Contracts per trade |
| `MAX_TRADES` | `11` | Max entries per RTH session |

### Risk

| Parameter | Default | Description |
|---|---|---|
| `ATR_STOP_RATIO` | `0.80` | Stop = this fraction × 20-bar ATR |
| `MIN_STOP_TICKS` | `4` | Floor for ATR stop |
| `MAX_STOP_TICKS` | `12` | Ceiling for ATR stop |
| `BREAKEVEN_TICKS` | `6` | Ticks of run-up before stop moves to entry |
| `TRAIL_ACTIVATE` | `8` | Ticks of run-up before trailing activates |
| `TRAIL_DISTANCE` | `4` | Trailing stop distance from peak (ticks) |
| `TIME_STOP_MINS` | `90` | Max bars a position may stay open |
| `GUTTER_GOAL` | `$1,500` | Daily profit target — stops new entries |
| `GUTTER_DD` | `$1,900` | Daily loss cap — stops new entries |
| `TRAILING_MLL_DISTANCE` | `$2,000` | TopStepX MLL distance from peak EOD balance |
| `ACCOUNT_START_BALANCE` | `$50,000` | Starting balance for MLL floor calculation |
| `PROFIT_TARGET` | `$3,000` | TopStepX combine profit target |

### Filters

| Parameter | Default | Description |
|---|---|---|
| `VWAP_SLOPE_THRESH` | `0.3` | Max VWAP slope in opposing direction |
| `MIN_VOL_REL` | `0.80` | Min bar volume as fraction of 5-bar average |
| `MAX_SPREAD` | `1.0` | Max bid-ask spread in points |
| `CUM_DELTA_FADE_MAX` | `0.12` | Max order flow imbalance when fading |
| `DIR_COOLDOWN_BARS` | `8` | Post-stop-out cooldown for same direction |
| `SESSION_TYPE_BARS` | `45` | Bars before session type is classified |
| `HIGH_IMPACT_BEHAVIOR` | `"reduce"` | `"skip"` = no trades on FOMC/CPI/NFP; `"reduce"` = z-thresh +0.5 |

### Contract IDs (update on quarterly roll — Mar/Jun/Sep/Dec)

| Instrument | Config key | Current |
|---|---|---|
| ES E-mini S&P 500 | `ES_CONTRACT` | `CON.F.US.EP.M26` |
| NQ E-mini NASDAQ | `NQ_CONTRACT` | `CON.F.US.ENQ.M26` |

---

## TopStepX Combine Rules

The bot is tuned for the **$50K TopStepX Combine**:

| Rule | Value |
|---|---|
| Profit Target | $3,000 |
| Trailing Max Loss Limit (MLL) | $2,000 below peak EOD balance |
| Max evaluation window | 45 trading days |

**MLL mechanics**: The trailing floor is `peak_eod_balance − $2,000`. The peak updates only at the end of each RTH session (not intraday). The floor can only move up, never down.

---

## Data Directories

Session JSON schema:

```json
{
  "date": "2026-04-15",
  "bars": [{"ts": "...", "o": 5250.25, "h": 5251.50, "l": 5249.75, "c": 5250.75, "v": 342,
            "vwap": 5249.83, "vwap_std": 2.14, "vpoc": 5250.25, "vpoc60": 5250.00,
            "atr": 1.25, "z": 0.42, "cum_delta": 1423}],
  "vol_profile": {"5250.25": 1847.3, ...},
  "tape": [...]
}
```

Tick CSV row format:

```
2026-04-15 09:30:01.234,T,5240.25,12,BUY,,
2026-04-15 09:30:01.456,Q,,,,5240.00,5240.25
```

Columns: `timestamp, type (T=trade / Q=quote), price, volume, side, bid, ask`

---

## Instruments

| Instrument | Strategy | Session Dir | Tick Size | Tick Value |
|---|---|---|---|---|
| ES (E-mini S&P 500) | VWAP Reclaim | `es_sessions/` | 0.25 | $12.50 |
| NQ (E-mini NASDAQ) | First Pullback | `nq_sessions/` | 0.25 | $5.00 |
| GC (Gold) | — (backtest only) | `gc_sessions/` | 0.10 | $10.00 |
