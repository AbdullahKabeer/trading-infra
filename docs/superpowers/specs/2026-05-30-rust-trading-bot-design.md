# Rust Trading Bot — Design Spec
**Date:** 2026-05-30
**Scope:** Rewrite `combine_live.py` in Rust (`rust/` subdirectory). Same strategy, same API, correct concurrency, TUI + web dashboard.

---

## 1. Context

`combine_live.py` is a 3,846-line ES E-mini VWAP mean-reversion trading bot for TopStepX (ProjectX API). It:
- Authenticates with `POST /Auth/loginKey`
- Backfills 35 days of RTH 1-min bars from `POST /History/retrieveBars`
- Streams live trades and quotes via SignalR WebSocket (`wss://rtc.topstepx.com/hubs/market`)
- Computes VWAP, VPOC, z-score, ATR, cumulative delta locally from ticks
- Places/manages orders via `POST /Order/place|cancel|modify`
- Serves an HTML/JS dashboard on port 8080

Known Python bugs being fixed:
- Threading races: `pos` cleared by one thread while another reads it
- Ghost positions: API error → false "flat" detection → new position opens in reverse
- Token expiry: no proper `Instant`-based check
- Double-close: two code paths both sending market orders on the same close
- Volume accumulator: integer division losing fractional volume

---

## 2. Architecture

### Actor Model (Tokio)

```
main.rs
  → auth
  → spawn: MarketFeed | Strategy | OrderManager
  → spawn: TUI task
  → spawn: Web server (Axum, port 8080)
  → wait for shutdown signal
```

**Data flow (one-directional):**
```
MarketFeed ──BarEvent/TickEvent──► Strategy ──TradeCommand──► OrderManager
                                              ◄──FillEvent────────────────
All actors ──write──► StateStore (Arc<RwLock<AppState>>)
TUI / Web  ──read──► StateStore
```

### Channel contracts

| Channel | Type | Producer | Consumer |
|---------|------|----------|----------|
| `bar_tx` | `broadcast` | MarketFeed | Strategy, StateStore |
| `tick_tx` | `broadcast` | MarketFeed | Strategy (intra-bar stop watch) |
| `cmd_tx` | `mpsc` | Strategy | OrderManager |
| `fill_tx` | `mpsc` | OrderManager | Strategy |

`StateStore` (`Arc<RwLock<AppState>>`) is written by actors and read by TUI/Web. No actor reads another's private state directly.

---

## 3. Data Model

### AppState (shared read-only view)
```rust
struct AppState {
    session: Session,
    bot: BotSnapshot,         // snapshot for display — not used for trading decisions
    regime: RegimeState,
    connected: bool,
    live: LiveQuote,
    log: VecDeque<String>,    // last 20 bot messages
}
```

### Session (market data, computed locally)
```rust
struct Session {
    date: NaiveDate,
    bars: Vec<Bar>,                         // completed 1-min bars
    cur_bar: Option<Bar>,                   // forming bar
    vol_profile: BTreeMap<OrderedFloat<f64>, f64>,
    vwap: f64, vwap_std: f64, vpoc: f64,
    cum_pv: f64, cum_v: f64, cum_p2v: f64, // VWAP accumulators
    cum_delta: f64,                          // buy_vol - sell_vol
    vwap_history: Vec<f64>,                  // per bar, for crossing count
    atr_history: Vec<f64>,                   // developing ATR per bar
    tape: VecDeque<Trade>,                   // last 500 ticks
}
```

### Bar
```rust
struct Bar {
    ts: DateTime<Utc>,
    o: f64, h: f64, l: f64, c: f64, v: f64,
    vwap: f64, vwap_std: f64, z: f64,
    vpoc: f64, vpoc60: f64,
}
```

### BotState (private to Strategy actor)
```rust
struct BotState {
    pos: Option<Position>,
    daily_pnl: f64, total_pnl: f64,
    trade_history: Vec<TradeRecord>,
    pnl_history: VecDeque<(Instant, f64)>,    // pruned to 2h
    equity_history: VecDeque<(Instant, f64)>,
    peak_pnl: f64, max_dd: f64,
    peak_eod_balance: f64,
    trades_today: u32,
    lockdown_until: Option<Instant>,
    last_exit_bar: i64,
    cooldown_long_until: i64,
    cooldown_short_until: i64,
    entry_pending: bool,
}
```

### Position
```rust
struct Position {
    uuid: Uuid,
    dir: Direction,            // Long | Short
    ep: f64,                   // entry price (rebased to actual fill from API)
    sl: f64, tp: f64, bp: f64, // stop, target, best price
    bar_idx: i64,
    contracts: u32,
    contracts_remaining: u32,
    fill_synced: bool,         // true once ep rebased from API fill
    entry_vwap: f64,
    tp_buffer_ticks: f64,
    scale1_done: bool,
}
```

---

## 4. Component Specs

### 4.1 MarketFeed (`feed/websocket.rs`)
- Connects to `wss://rtc.topstepx.com/hubs/market` using SignalR handshake
- Subscribes to `SubscribeContractQuotes` and `SubscribeContractTrades`
- On `GatewayTrade`: parses price/volume/side, calls `session.add_trade()`, sends `TickEvent`; when bar closes, sends `BarEvent`
- On `GatewayQuote`: parses bid/ask, sends `QuoteEvent`
- Reconnect loop: on disconnect, re-negotiate, re-subscribe, re-backfill missed bars
- Token refresh: if token age > 12h, re-auth before reconnect

### 4.2 Strategy (`strategy/bot.rs`)
- Receives `BarEvent` (entry decisions) and `TickEvent` (stop/target watching)
- Entry logic: fired only on `BarEvent` (bar close). Checks z-score, regime filters, session filters, spread, volume, cooldown
- Exit logic on `TickEvent`: SL/TP/trailing stop/breakeven updates, time stop, gutter win/loss
- Sends `TradeCommand` to OrderManager; waits for `FillEvent` before updating position state
- Never mutates position based solely on local inference — fill price always from API

### 4.3 OrderManager (`api/orders.rs`)
- Receives `TradeCommand`: ENTER, EXIT, MODIFY_STOP, MODIFY_TP, CANCEL_ALL_BRACKETS
- For every ENTER/EXIT: first calls `GET /Position/searchOpen` to verify server state
  - If API error → drop the command, log, return (never assume flat)
  - If server already flat and we're exiting → book as external close, no market order
- Places market orders with bracket SL/TP attached
- Polls active orders every 5s to detect external bracket fills (sync_from_server equivalent)
- On fill detected → sends `FillEvent` with actual fill price to Strategy
- Manages token refresh (12h expiry, `Instant`-based)

### 4.4 TUI (`tui/app.rs`)
Layout (ratatui):
```
┌─ Header: symbol | date | time | connection status ─────────────────┐
├─ Row 1 ────────────────────────────────────────────────────────────┤
│  PRICE (3 cols)  │  POSITION MANAGER  │  REGIME   │  P&L SUMMARY  │
├─ Row 2 ────────────────────────────────────────────────────────────┤
│  SPARKLINES: bars/VWAP | z-score | equity curve | cum-delta | ATR │
├─ Row 3 ────────────────────────────────────────────────────────────┤
│  TAPE (left)  │  VOLUME PROFILE (right)                            │
├─ Row 4 ────────────────────────────────────────────────────────────┤
│  ACTIVE ORDERS  │  FILL HISTORY                                    │
├─ LOG ──────────────────────────────────────────────────────────────┤
└────────────────────────────────────────────────────────────────────┘
```
Sparklines (row 2): last 60 bar closes (with VWAP line overlay), z-score last 60 bars, equity curve (last 2h trades), cumulative delta per bar, ATR per bar. All in compact 8-row ratatui `Sparkline` widgets.
Key bindings: `q` quit, `b` toggle bot on/off, `d` dry-run toggle.

### 4.5 Web Server (`web/server.rs`)
Axum routes:
- `GET /` → embedded HTML (same frontend JS as Python, minimal changes)
- `GET /api/data` → JSON snapshot of `AppState`
- `GET /api/day/:date` → load saved session JSON + run backtest
- `GET /api/sessions` → list saved session dates
- `POST /api/toggle_gw` / `POST /api/toggle_gl` → toggle gutter win/loss flags

JSON schema matches Python exactly so the existing JS frontend works without changes.

### 4.6 Backtest (`strategy/backtest.rs`)
- `run_backtest(bars: &[Bar]) -> (BotState, Vec<TradeRecord>)` — pure function, no side effects
- Used by web server for `/api/day/:date` and by background Monte Carlo stats
- Monte Carlo: 10,000 simulations sampling with replacement from historical trade P&Ls

---

## 5. Key Bug Fixes vs Python

| Python bug | Rust fix |
|-----------|----------|
| `pos` mutated from HTTP thread and async loop simultaneously | `pos` lives only in Strategy actor; HTTP/TUI get read-only snapshots |
| Ghost position: API error → false flat → reverse entry | OrderManager: if `searchOpen` returns error, abort command entirely |
| Double-close: two code paths send market orders | UUID gate: Strategy checks `pos.uuid` before sending ExitCmd; OrderManager verifies flat before executing |
| Token expiry: Python uses wall-clock `datetime.now()` with no monotonic check | `Instant::now()` with 12h duration check |
| Integer volume division loses fractional volume | `f64` throughout; `BTreeMap<OrderedFloat<f64>, f64>` |
| `_bg_stats_cache` computed in background thread but read in HTTP with no guarantee of type coherence | Tokio `Mutex<Option<Stats>>` around cache; HTTP handler awaits lock |

---

## 6. File Structure

```
rust/
├── Cargo.toml
└── src/
    ├── main.rs
    ├── config.rs              # BOT_ACTIVE, Z_THRESH, TICK_SIZE, etc.
    ├── types.rs               # Bar, Trade, Position, TradeRecord, Direction, etc.
    ├── api/
    │   ├── mod.rs
    │   ├── auth.rs
    │   ├── history.rs
    │   ├── orders.rs
    │   └── positions.rs
    ├── feed/
    │   └── websocket.rs
    ├── market/
    │   ├── session.rs
    │   └── regime.rs
    ├── strategy/
    │   ├── bot.rs
    │   └── backtest.rs
    ├── tui/
    │   └── app.rs
    └── web/
        └── server.rs
```

---

## 7. Crate Dependencies

```toml
tokio = { version = "1", features = ["full"] }
tokio-tungstenite = { version = "0.23", features = ["native-tls"] }
reqwest = { version = "0.12", features = ["json"] }
axum = "0.7"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
chrono = { version = "0.4", features = ["serde"] }
ratatui = "0.28"
crossterm = "0.28"
anyhow = "1"
tracing = "0.1"
tracing-subscriber = "0.3"
dotenvy = "0.15"
uuid = { version = "1", features = ["v4"] }
ordered-float = "4"
```

---

## 8. Non-Goals

- No ML/model inference in this build
- No multi-instrument support (ES only)
- No order book depth (L2) — bid/ask from quotes only
- No persistence beyond JSON session files (same as Python)
