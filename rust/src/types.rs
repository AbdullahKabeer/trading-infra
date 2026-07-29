use chrono::{DateTime, Utc};
use ordered_float::OrderedFloat;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, VecDeque};
use uuid::Uuid;

// ── Market data ───────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Bar {
    pub ts: DateTime<Utc>,
    pub o: f64,
    pub h: f64,
    pub l: f64,
    pub c: f64,
    pub v: f64,
    #[serde(default)]
    pub vwap: f64,
    #[serde(default)]
    pub vwap_std: f64,
    #[serde(default)]
    pub z: f64,
    #[serde(default)]
    pub vpoc: f64,
    #[serde(default)]
    pub vpoc60: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Trade {
    pub time: String,
    pub price: f64,
    pub vol: f64,
    pub side: String, // "BUY" | "SELL" | ""
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Quote {
    pub time: String,
    pub bid: f64,
    pub ask: f64,
    pub spread: f64,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct LiveQuote {
    pub bid: f64,
    pub ask: f64,
    pub last: f64,
}

/// One level in the Depth-of-Market (DOM) book.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct DomLevel {
    pub price: f64,
    pub bid_vol: f64,
    pub ask_vol: f64,
}

// ── Session ───────────────────────────────────────────────────────────────────

#[allow(dead_code)]
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Session {
    pub date: String,
    pub bars: Vec<Bar>,
    pub cur_bar: Option<CurBar>,
    pub vol_profile: BTreeMap<OrderedFloat<f64>, f64>,
    pub vwap: f64,
    pub vwap_std: f64,
    pub vpoc: f64,
    pub cum_pv: f64,
    pub cum_v: f64,
    pub cum_p2v: f64,
    pub cum_delta: f64,
    pub vwap_history: Vec<f64>,
    pub atr_history: Vec<f64>,
    pub std_history: Vec<f64>,
    pub vpoc_history: Vec<f64>,
    pub bar_ranges: Vec<f64>,
    pub prev_close: Option<f64>,
    pub tape: VecDeque<Trade>,
    pub quotes: VecDeque<Quote>,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub last: f64,
    pub bid: f64,
    pub ask: f64,
    pub trade_count: u64,
    pub total_volume: f64,
    pub tick_count: u64,
    pub bar_just_closed: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CurBar {
    pub ts: String,
    pub o: f64,
    pub h: f64,
    pub l: f64,
    pub c: f64,
    pub v: f64,
}

// ── Bot / Position ─────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Direction {
    Long,
    Short,
}

impl Direction {
    pub fn as_str(&self) -> &'static str {
        match self {
            Direction::Long => "LONG",
            Direction::Short => "SHORT",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Position {
    pub uuid: Uuid,
    pub dir: Direction,
    pub ep: f64,
    pub sl: f64,
    pub tp: f64,
    pub bp: f64, // best price seen
    pub bar_idx: i64,
    pub contracts: u32,
    pub contracts_remaining: u32,
    pub fill_synced: bool,
    pub entry_vwap: f64,
    pub tp_buffer_ticks: f64,
    pub scale1_done: bool,
    pub created_at_secs: u64, // Unix seconds for age checks
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TradeRecord {
    pub action: String,
    pub dir: String,
    pub price: f64,
    pub pnl: f64,
    pub bar_idx: i64,
    pub reason: String,
    pub size: u32,
    pub sl: f64,
    pub tp: f64,
}

/// Read-only snapshot written to AppState for TUI/Web display
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct BotSnapshot {
    pub account_name: Option<String>,
    pub total_pnl: f64,
    pub daily_pnl: f64,
    pub open_pnl: f64,
    pub max_dd: f64,
    pub trades_today: u32,
    pub pos: Option<PositionSnapshot>,
    pub trade_history: Vec<TradeRecord>,
    pub active_orders: Vec<serde_json::Value>,
    pub account_balance: f64,
    pub mll_floor: f64,
    pub mll_remaining: f64,
    pub peak_eod_balance: f64,
    pub gutter_win: bool,
    pub gutter_loss: bool,
    pub equity_curve: Vec<(f64, f64)>, // (secs_offset, cumulative_pnl)
    pub z_history: Vec<f64>,
    pub delta_history: Vec<f64>,
    pub atr_sparkline: Vec<f64>,
    pub hourly_pnl: f64,
    pub is_locked_down: bool,
    pub strategy: String,
    // State machine & execution quality
    pub bot_state: String,       // "HUNTING" | "ENTERING" | "MANAGING_LONG" | "TRAILING_LONG" | "COOLDOWN" | "HALTED"
    pub avg_slippage_ticks: f64, // positive = got filled better than signal; negative = paid more
    pub pos_age_secs: u64,       // seconds since current position was opened
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PositionSnapshot {
    pub uuid: Uuid,
    pub dir: String,
    pub ep: f64,
    pub sl: f64,
    pub tp: f64,
    pub contracts_remaining: u32,
    pub bar_idx: i64,
}

// ── Regime ────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct RegimeState {
    pub vix_proxy: Option<f64>,
    pub vix_status: Option<String>,
    pub vwap_crossings: u32,
    pub vwap_regime: String,
    pub calendar_event: Option<String>,
    pub high_impact_behavior: String,
    pub session_type: String,
    pub session_type_bar: i64,
    pub prev_vwap: Option<f64>,
    pub prev_vpoc: Option<f64>,
    pub prev_close: Option<f64>,
}

// ── Shared app state (written by actors, read by TUI/web) ─────────────────────

#[derive(Debug, Serialize)]
pub struct AppState {
    pub session: Option<crate::market::session::Session>,
    pub bot: BotSnapshot,
    pub regime: RegimeState,
    pub connected: bool,
    pub live: LiveQuote,
    pub log: VecDeque<String>,
    pub target_mode: String,
    pub time_stop_mins: i64,
    pub rth: bool,
    // Runtime control flags — written by TUI, read by bot/order_manager
    pub bot_enabled: bool,
    pub dry_run: bool,
    // Historical bars from prior sessions (loaded at startup, oldest-first)
    pub historical_bars: Vec<Bar>,
    // Live DOM from market hub GatewayDepth (bid levels and ask levels merged, keyed by price)
    pub dom: Vec<DomLevel>,
    // Real-time account balance pushed by user hub GatewayUserAccount
    pub account_balance_live: Option<f64>,
    /// Unix seconds of last broker message received (used for feed health indicator)
    pub last_feed_secs: u64,
}

impl Default for AppState {
    fn default() -> Self {
        Self {
            session: None,
            bot: BotSnapshot::default(),
            regime: RegimeState::default(),
            connected: false,
            live: LiveQuote::default(),
            log: VecDeque::new(),
            target_mode: "vwap".to_string(),
            time_stop_mins: crate::config::TIME_STOP_MINS,
            rth: false,
            bot_enabled: crate::config::BOT_ACTIVE,
            dry_run: crate::config::DRY_RUN,
            historical_bars: Vec::new(),
            dom: Vec::new(),
            account_balance_live: None,
            last_feed_secs: 0,
        }
    }
}

// ── Channel messages ──────────────────────────────────────────────────────────

#[allow(dead_code)]
#[derive(Debug, Clone)]
pub struct BarEvent {
    pub bar_idx: i64,
    pub price: f64,
    pub vwap: f64,
    pub vpoc: f64,
    pub vpoc60: f64,
    pub std: f64,
    pub vol: f64,
    pub bar_range: f64,
    pub tod_mins: i64,
    pub vwap_dist: f64,
    pub vpoc_dist: f64,
    pub vwap_slope: f64,
    pub vwap_vpoc_dist: f64,
    pub mom_5: f64,
    pub velocity_10: f64,
    pub vol_rel: f64,
    pub rng_rel: f64,
    pub cum_vol: f64,
    pub vol_60: f64,
    pub poc_mig: f64,
    pub sess_pct: f64,
    pub body_size: f64,
    pub wick_low: f64,
    pub wick_high: f64,
    pub sigma_exp: f64,
    pub prev_h: f64,
    pub prev_l: f64,
    pub atr_slope: f64,
    pub atr: f64,
    pub spread: f64,
    pub cum_delta_pct: f64,
    pub stable: bool,
    pub rth: bool,
}

#[derive(Debug, Clone)]
#[allow(dead_code)]
pub struct TickEvent {
    pub price: f64,
    pub vwap: f64,
    pub vpoc: f64,
    pub std: f64,
    pub bar_idx: i64,
    pub rth: bool,
    pub tod_mins: i64,
    pub spread: f64,
}

/// Commands from Strategy → OrderManager
#[allow(dead_code)]
#[derive(Debug, Clone)]
pub enum TradeCommand {
    Enter {
        dir: Direction,
        sl_price: f64,
        tp_price: f64,
        entry_price: f64,
        entry_vwap: f64,
        tp_buffer_ticks: f64,
        bar_idx: i64,
        atr: f64,
    },
    Exit {
        pos_uuid: Uuid,
        reason: String,
        price: f64,
        bar_idx: i64,
    },
    ModifyStop {
        pos_uuid: Uuid,
        new_stop: f64,
    },
    ModifyTp {
        pos_uuid: Uuid,
        new_tp: f64,
    },
    CancelAllBrackets {
        dir: Direction,
    },
    ScaleOut {
        pos_uuid: Uuid,
        price: f64,
        bar_idx: i64,
    },
    ToggleGutterWin,
    ToggleGutterLoss,
}

/// Fills from OrderManager → Strategy
#[derive(Debug, Clone)]
pub enum FillEvent {
    Entered {
        pos_uuid: Uuid,
        fill_price: f64,
        dir: Direction,
        sl: f64,
        tp: f64,
        bar_idx: i64,
        entry_vwap: f64,
        tp_buffer_ticks: f64,
    },
    Closed {
        pos_uuid: Uuid,
        fill_price: f64,
        reason: String,
        bar_idx: i64,
        size: u32,
    },
    ExternalClose {
        fill_price: f64,
        reason: String,
        bar_idx: i64,
        size: u32,
    },
    StopModified {
        pos_uuid: Uuid,
        new_stop: f64,
    },
    TpModified {
        pos_uuid: Uuid,
        new_tp: f64,
    },
    ScaledOut {
        pos_uuid: Uuid,
        fill_price: f64,
        contracts_sold: u32,
        bar_idx: i64,
    },
    ActiveOrders(Vec<serde_json::Value>),
    Error(String),
    /// Broker-authoritative daily P&L rebase (fired when flat, every ~60s)
    Rebase { pnl: f64, trades: u32 },
}

// ── Backtest ──────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct BacktestStats {
    pub ready: bool,
    pub trades: usize,
    pub wins: usize,
    pub losses: usize,
    pub flats: usize,
    pub win_rate: f64,
    pub win_rate_nonflat: f64,
    pub win_rate_all: f64,
    pub avg_win: f64,
    pub avg_loss: f64,
    pub rr: f64,
    pub expectancy: f64,
    pub profit_factor: f64,
    pub pass_rate: f64,
    pub blow_rate: f64,
    pub timeout_rate: f64,
    pub avg_days_to_pass: f64,
    pub avg_days_to_blow: f64,
    pub simulations: u32,
    pub sample_days: usize,
    pub lookback_days: usize,
    pub max_days: u32,
    pub max_trades_day: u32,
    pub start_balance: f64,
    pub pass_balance: f64,
    pub trailing_mll: f64,
    pub profit_target: f64,
    pub mll_floor: f64,
    pub account_balance: f64,
    pub mll_remaining: f64,
    pub peak_eod_balance: f64,
    pub x_axis: Vec<i32>,
    pub paths_preview: Vec<Vec<f64>>,
    pub curve: MonteCarloCurve,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct MonteCarloCurve {
    pub p5: Vec<f64>,
    pub p50: Vec<f64>,
    pub p95: Vec<f64>,
}

// ── Live strategy selection ────────────────────────────────────────────────────

#[derive(Clone, Debug, PartialEq)]
pub enum LiveStrategy {
    VwapReclaim,   // ES — fade VWAP crossovers
    FirstPullback, // NQ — enter on pullback toward VWAP from extension
}

impl LiveStrategy {
    pub fn name(&self) -> &'static str {
        match self {
            LiveStrategy::VwapReclaim   => "VWAP Reclaim",
            LiveStrategy::FirstPullback => "First Pullback",
        }
    }
}

#[derive(Clone, Debug)]
pub struct InstrumentCfg {
    pub tick_size: f64,
    pub tick_value: f64,
    pub daily_profit_cap: f64,
    pub contract: &'static str,
}

impl InstrumentCfg {
    pub fn es() -> Self {
        Self {
            tick_size: crate::config::TICK_SIZE,
            tick_value: crate::config::TICK_VALUE,
            daily_profit_cap: 1499.0,
            contract: crate::config::CONTRACT,
        }
    }
    pub fn nq() -> Self {
        Self {
            tick_size: crate::config::TICK_SIZE,
            tick_value: crate::config::NQ_TICK_VALUE,
            daily_profit_cap: 1499.0,
            contract: crate::config::NQ_CONTRACT,
        }
    }
}

// ── Server-side position snapshot ─────────────────────────────────────────────

#[allow(dead_code)]
#[derive(Debug, Clone)]
pub enum ServerPositionState {
    Flat,
    Open { size: u32, avg_price: Option<f64>, side: Option<serde_json::Value> },
    Unknown, // API error
}
