# EvalPass

Automated VWAP mean-reversion trading bot targeting the **TopStepX Combine** ($50K account, $3K profit target, $2K trailing max loss limit). Trades ES E-mini S&P 500 futures live via the TopStepX SignalR API with a full backtest/optimization pipeline for NQ, GC, and ES.

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

The target (`TARGET_MODE`) is either:
- `"vwap"` — the live session VWAP (drifts with price over time)
- `"volume"` — the session VPOC (volume point of control)

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

`SERVER_BRACKETS_PRIMARY=True` — SL and TP are submitted as bracket orders to the broker at entry. The client monitors and provides a backup exit if the server bracket fails to fire within 2 ticks of the level. The client also syncs VWAP TP drift to the server: if VWAP moves ≥3 ticks from the entry anchor, the TP bracket is repriced.

`ENTRY_REBASE_ENABLED=True` — After entry, the bot queries the server for the actual fill price and rebases the internal EP/SL/TP to the broker's average fill.

---

## Architecture

```
TopStepX API (HTTPS)       TopStepX SignalR WebSocket
        │                           │
        │ Auth / History / Orders   │ GatewayTrade / GatewayQuote
        ▼                           ▼
  combine_live.py ───────────────────────────────────
  │                                                  │
  │  Session (bars, VWAP, VPOC, vol profile,         │
  │           cumulative delta, ATR, std history)    │
  │                                                  │
  │  REGIME dict (VIX proxy, VWAP crossings,         │
  │               session type, prev levels,         │
  │               calendar event)                    │
  │                                                  │
  │  VWAPBot (async, httpx)                          │
  │    ├── setup()         → account + contract ID   │
  │    ├── check_logic()   → entry / exit decisions  │
  │    ├── place_order()   → market + brackets       │
  │    ├── modify_order()  → move SL/TP              │
  │    ├── sync_from_server() → reconcile position   │
  │    └── cancel_all_bracket_orders()               │
  │                                                  │
  │  TickRecorder → es_ticks/YYYY-MM-DD.csv         │
  │  Session.save() → es_sessions/YYYY-MM-DD.json   │
  │                                                  │
  │  HTTPServer :8080                                │
  │    ├── GET /           → dashboard HTML          │
  │    ├── GET /api/state  → live JSON snapshot      │
  │    ├── GET /api/day/<date> → historical session  │
  │    ├── GET /api/sessions → date list             │
  │    ├── POST /api/toggle_gw → toggle gutter win   │
  │    └── POST /api/toggle_gl → toggle gutter loss  │
  │                                                  │
  │  Background threads                              │
  │    ├── saver (30s)    → session + tick flush     │
  │    └── bg_stats (60s) → Monte Carlo cache        │
  └──────────────────────────────────────────────────
```

The entry point (`asyncio.run(main())`) runs three concurrent tasks:
1. `stream(token)` — SignalR WebSocket loop (auto-reconnects, re-negotiates on drop)
2. `serve(8080)` — HTTP dashboard in a daemon thread
3. Background saver + stats threads

---

## File Reference

| File | Purpose |
|---|---|
| `combine_live.py` | **Main bot** — 3,846 lines. Live feed, Session class, VWAPBot, backtest engine, HTTP dashboard, all configuration |
| `optimize_es.py` | ES grid-search optimizer. Sweeps 7 parameters × 4–6 values each using the same filter stack as the live bot. Scores by Monte Carlo pass-rate + Sortino. Includes walk-forward (65/35 train/val split) and leave-one-out cross-validation |
| `optimize_nq.py` | NQ optimizer (same structure, NQ-specific tick values and constraint defaults) |
| `optimize_params.py` | General optimizer, loads from both `es_sessions/` and `spy_proxy_sessions/` |
| `backtest_gc.py` | Gold (GC) backtester — standalone, uses `gc_sessions/` |
| `tick_backtest.py` | Tick-level backtester — ingests raw `es_ticks/*.csv` for precise SL/TP simulation |
| `sim_combine.py` | TopStepX Combine pass-rate Monte Carlo simulator — 1,000 simulations over all historical sessions, produces pass % and equity curve stats |
| `sim_test.py` | Quick sim smoke test |
| `sortino_3d_model.py` | 3D Sortino surface over z-thresh and stop-ratio parameter space |
| `fetch_nq.py` | Fetches NQ RTH 1-min bars from TopStepX API into `nq_sessions/` (handles Z25/H26/M26 front-month rollover) |
| `fetch_gc.py` | Fetches GC RTH 1-min bars into `gc_sessions/` |
| `fetch_swagger.py` | Minimal API explorer |
| `find_nq_symbol.py` | Identifies the correct NQ contract ID on TopStepX for a given date |
| `list_contracts.py` | Lists all available contracts on the account |
| `test_bracket_order.py` | Integration test for bracket order placement and cancellation |
| `time_test.py` | Timestamp parsing sanity check |
| `strat/strat2.py` | Earlier SMC/FVG strategy prototype (MES, MGC, MNQ on 4H bars via Polygon) |

### Data

| Directory | Contents |
|---|---|
| `es_sessions/` | JSON per RTH session for ES (bars + VWAP/VPOC state) |
| `nq_sessions/` | JSON per RTH session for NQ |
| `gc_sessions/` | JSON per RTH session for Gold |
| `spy_proxy_sessions/` | SPY proxy sessions used to pad ES training data |
| `es_ticks/` | Raw tick CSVs for ES (T/Q rows: timestamp, type, price, volume, side, bid, ask) |
| `optimization_results_es.csv` | Grid search output for ES |
| `optimization_results_nq.csv` | Grid search output for NQ |
| `optimization_results.csv` | General grid search output |

---

## Setup

### Prerequisites

- Python 3.11+ (project uses CPython 3.14 per `__pycache__` names)
- TopStepX account with API access enabled
- `.venv` already present in the project root

### Install Dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install httpx websockets pytz python-dotenv pandas
```

### Configure Credentials

Create `.env` in the project root:

```
PROJECT_X_USERNAME=your@email.com
PROJECT_X_API_KEY=your_api_key_here
```

The bot authenticates at startup via `POST /api/Auth/loginKey`. The token is auto-refreshed every 12 hours.

---

## Running the Live Bot

```bash
source .venv/bin/activate
python combine_live.py
```

**Startup sequence:**
1. Authenticates with TopStepX
2. Backfills the last 35 RTH days of 1-min ES bars (skips days already cached)
3. Sets up `VWAPBot` — discovers account ID and contract ID
4. Loads previous session VWAP/VPOC/close into `REGIME`
5. Checks the economic calendar for today
6. Fetches VIX proxy from VX futures
7. Seeds the stats cache (Monte Carlo pass estimate)
8. Starts HTTP dashboard on port 8080
9. Connects to the SignalR WebSocket and begins streaming

**Dry-run mode** (no real orders placed):

```python
# In combine_live.py, line 41:
DRY_RUN = False   # change to True
```

**Bot disabled** (stream and collect data only):

```python
BOT_ACTIVE = False  # line 39
```

---

## Dashboard

Open `http://localhost:8080` while the bot is running.

The dashboard polls `/api/state` every second and shows:

- Live bid / ask / last price
- Session VWAP, VWAP σ, z-score, VPOC
- Open position: direction, entry price, current SL/TP, unrealized P/L, bars open
- Daily P/L, total P/L, trade count
- Account balance, MLL floor, MLL remaining headroom
- Monte Carlo combine pass probability (updated every 60 seconds)
- Full trade history for the session
- Regime panel: VIX level, VWAP crossing count, session type, calendar event, previous session levels
- Historical day viewer: click any date to replay that session's backtest

**Toggle controls** (POST endpoints):
- `POST /api/toggle_gw` — enable/disable gutter win exit
- `POST /api/toggle_gl` — enable/disable gutter loss exit

---

## Backtesting

The backtester lives inside `combine_live.py:run_backtest()` and is called automatically for every session viewed in the dashboard. It mirrors the live filter stack exactly (same VPOC stability check, sigma panic, ATR slope, VWAP slope, cumulative delta, time-of-day filters, trailing stop, breakeven, scale-out).

**Key difference vs live:** the backtest fires entries on bar-close (no tick-level simulation). For tick-precision SL/TP back-testing, use:

```bash
python tick_backtest.py es_ticks/2026-03-27.csv
```

The tick backtester ingests `es_ticks/*.csv` (raw T/Q records written by `TickRecorder` in the live bot) and replays every trade/quote tick to check exact SL and TP levels.

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

**Scoring**: `N_MC_SIMS=1000` Monte Carlo simulations per candidate. Each sim randomly selects sessions (with replacement) and runs them until the $3K profit target, $2K MLL breach, or 45-session timeout. Score = pass rate × Sortino ratio.

**Walk-forward split**: sessions are split 65% training / 35% held-out validation. Only candidates that pass both folds are reported.

**Leave-one-out cross-validation** detects overfitting on the session dataset.

Results are written to `optimization_results_es.csv`.

### NQ

```bash
python optimize_nq.py
```

Same structure with NQ-specific constants (tick value $5.00, larger stop/trail defaults).

---

## Monte Carlo Pass Simulator

```bash
python sim_combine.py
```

Uses all sessions in `es_sessions/` and `spy_proxy_sessions/`. Runs 1,000 simulations of a 45-session combine attempt. Each simulation:

1. Randomly draws sessions (with replacement)
2. Backtests each session using live bot parameters
3. Tracks cumulative P/L, peak equity, and trailing MLL floor (`peak - $2,000`)
4. Outcome: **PASSED** ($3K+ reached), **BLOWN** (MLL breached), or **TIMEOUT** (45 sessions elapsed)

Reports: pass %, blow %, timeout %, median days to pass, 5th percentile days to pass.

---

## Key Parameters

All parameters are defined at the top of `combine_live.py` (lines 39–114) and serve as the single source of truth for both the live bot and all backtest/optimizer scripts.

### Trading

| Parameter | Default | Description |
|---|---|---|
| `BOT_ACTIVE` | `True` | Master switch — False = data collection only |
| `DRY_RUN` | `False` | Log orders without sending them |
| `TARGET_MODE` | `"vwap"` | Target for TP: `"vwap"` or `"volume"` (VPOC) |
| `Z_THRESH` | `1.2` | Minimum z-score to enter (σ from VWAP) |
| `CONTRACTS` | `1` | Contracts per trade |
| `MAX_TRADES` | `11` | Max entries per RTH session |

### Risk

| Parameter | Default | Description |
|---|---|---|
| `ATR_STOP_RATIO` | `0.80` | Stop = this fraction × 20-bar ATR |
| `MIN_STOP_TICKS` | `4` | Floor for ATR stop calculation |
| `MAX_STOP_TICKS` | `12` | Ceiling for ATR stop calculation |
| `BREAKEVEN_TICKS` | `6` | Ticks of run-up before stop moves to entry |
| `TRAIL_ACTIVATE` | `8` | Ticks of run-up before trailing activates |
| `TRAIL_DISTANCE` | `4` | Trailing stop distance from peak price (ticks) |
| `EXIT_MIN_TICKS` | `6` | Minimum ticks of profit to take target |
| `TIME_STOP_MINS` | `90` | Max bars a position may stay open |
| `MAX_HOURLY_LOSS` | `200` | Lockdown trigger: loss within any rolling 60-min window |
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
| `CUM_DELTA_FADE_MAX` | `0.12` | Max order flow imbalance (delta/volume) when fading |
| `DIR_COOLDOWN_BARS` | `8` | Post-stop-out cooldown for same direction |
| `SESSION_TYPE_BARS` | `45` | Bars before session type is classified |
| `VWAP_CROSS_TRENDING` | `1` | Crossings ≤ this = trending (raise z-thresh) |
| `VWAP_CROSS_BALANCED` | `3` | Crossings ≥ this = balanced (normal z-thresh) |
| `PREV_LEVEL_TICKS` | `3` | Ticks from prior session VWAP/VPOC to count as confluence |
| `HIGH_IMPACT_BEHAVIOR` | `"reduce"` | `"skip"` = no trades on FOMC/CPI/NFP; `"reduce"` = z-thresh +0.5 |

### Contract

| Parameter | Default | Description |
|---|---|---|
| `CONTRACT` | `CON.F.US.EP.M26` | TopStepX ES E-mini contract ID (update on roll) |
| `TICK_SIZE` | `0.25` | ES tick size in points |
| `TICK_VALUE` | `$12.50` | ES tick value in dollars |
| `COMMISSION_RT` | `$2.80` | Round-turn commission per contract |

---

## TopStepX Combine Rules

The bot is tuned for the **$50K TopStepX Combine**:

| Rule | Value |
|---|---|
| Profit Target | $3,000 |
| Trailing Max Loss Limit (MLL) | $2,000 below peak EOD balance |
| Max evaluation window | 45 trading days |

**MLL mechanics**: The trailing floor is `peak_eod_balance - $2,000`. The peak updates only at the end of each RTH session (not intraday). The floor can only move up, never down. The bot tracks this internally via `VWAPBot.peak_eod_balance` and `VWAPBot.mll_floor` and displays remaining headroom on the dashboard.

---

## Data Directories

Session JSON files follow this schema:

```json
{
  "date": "2026-04-15",
  "bars": [{"ts": "...", "o": 5250.25, "h": 5251.50, "l": 5249.75, "c": 5250.75, "v": 342}],
  "cur_bar": {"ts": "...", "o": ..., "h": ..., "l": ..., "c": ..., "v": ...},
  "vol_profile": {"5250.25": 1847.3, ...},
  "tape": [...],
  "cum_pv": ..., "cum_v": ..., "cum_p2v": ..., "cum_delta": ...,
  "vwap": 5249.83, "vwap_std": 2.14,
  "open": 5240.0, "high": 5265.5, "low": 5237.25,
  "trade_count": 18432, "total_volume": 184923, "tick_count": 52841,
  "bid": 5251.0, "ask": 5251.25, "last": 5251.0
}
```

Tick CSVs use this row format:

```
2026-04-15 09:30:01.234,T,5240.25,12,BUY,,
2026-04-15 09:30:01.456,Q,,,,5240.00,5240.25
```

Columns: `timestamp, type (T=trade/Q=quote), price, volume, side, bid, ask`

---

## Instruments

| Instrument | Session Dir | Optimizer | Tick Size | Tick Value | Key Differences |
|---|---|---|---|---|---|
| ES (E-mini S&P 500) | `es_sessions/` | `optimize_es.py` | 0.25 | $12.50 | Primary instrument, live bot |
| NQ (E-mini NASDAQ) | `nq_sessions/` | `optimize_nq.py` | 0.25 | $5.00 | Wider stops (24 ticks max vs 12 for ES) |
| GC (Gold) | `gc_sessions/` | — (`backtest_gc.py`) | 0.10 | $10.00 | Standalone backtester, tighter daily loss cap |

The live bot (`combine_live.py`) currently runs **ES only** (`CONTRACT = "CON.F.US.EP.M26"`). Update the contract ID at each quarterly expiration (Mar/Jun/Sep/Dec).
