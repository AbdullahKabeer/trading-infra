#![allow(dead_code)]
pub const API: &str = "https://api.topstepx.com/api";
pub const HUB: &str = "https://rtc.topstepx.com/hubs/market";
pub const USER_HUB: &str = "https://rtc.topstepx.com/hubs/user";
pub const CONTRACT: &str = "CON.F.US.EP.U26";
pub const NQ_CONTRACT: &str = "CON.F.US.ENQ.U26";
pub const NQ_TICK_VALUE: f64 = 5.0;
pub const VX_CONTRACT: &str = "CON.F.US.VX.M26";
pub const DATA_DIR: &str = "es_sessions";
pub const TICK_DIR: &str = "es_ticks";
pub const DAYS_BACK: u32 = 35;

// Strategy
pub const BOT_ACTIVE: bool = true;
pub const DRY_RUN: bool = false;
pub const TARGET_MODE: &str = "vwap"; // "vwap" or "volume"
pub const Z_THRESH: f64 = 1.2;
pub const CONTRACTS: u32 = 1;
pub const MAX_TRADES: u32 = 11;
pub const MAX_HOURLY_LOSS: f64 = 200.0;
pub const HOURLY_GUARD_TYPE: &str = "DD"; // "PL" or "DD"
pub const ATR_STOP_RATIO: f64 = 0.80;
pub const MIN_STOP_TICKS: i32 = 4;
pub const MAX_STOP_TICKS: i32 = 12;
pub const TRAIL_ACTIVATE: f64 = 8.0;
pub const TRAIL_DISTANCE: f64 = 4.0;
pub const BREAKEVEN_TICKS: f64 = 6.0;
pub const EXIT_MIN_TICKS: f64 = 6.0;
pub const TIME_STOP_MINS: i64 = 90;
pub const TICK_SIZE: f64 = 0.25;
pub const TICK_VALUE: f64 = 12.50;
pub const GUTTER_GOAL: f64 = 1500.0;
pub const GUTTER_DD: f64 = 1900.0;
pub const TRAILING_MLL_DISTANCE: f64 = 2000.0;
pub const ACCOUNT_START_BALANCE: f64 = 50000.0;
pub const PROFIT_TARGET: f64 = 3000.0;
pub const STATS_LOOKBACK_DAYS: usize = 0;
pub const COMMISSION_RT: f64 = 2.80;
pub const SERVER_BRACKETS_PRIMARY: bool = true;
pub const ENTRY_REBASE_ENABLED: bool = true;

// Session/regime filters
pub const LUNCH_SKIP_START: i64 = 120; // tod_mins (11:30 ET)
pub const LUNCH_SKIP_END: i64 = 195;   // tod_mins (12:45 ET)
pub const POWER_HOUR_START: i64 = 330; // tod_mins (15:00 ET)
pub const VWAP_SLOPE_THRESH: f64 = 0.3;
pub const MIN_VOL_REL: f64 = 0.8;
pub const MAX_SPREAD: f64 = 1.0;
pub const VWAP_CROSS_TRENDING: u32 = 1;
pub const VWAP_CROSS_BALANCED: u32 = 3;
pub const SCALE_OUT_ENABLED: bool = true;
pub const CUM_DELTA_FILTER: bool = true;
pub const CUM_DELTA_FADE_MAX: f64 = 0.12;
pub const SESSION_TYPE_BARS: usize = 45;
pub const SESSION_TREND_Z_MULT: f64 = 1.25;
pub const DIR_COOLDOWN_BARS: i64 = 8;
pub const PREV_LEVEL_TICKS: f64 = 3.0;
pub const PREV_LEVEL_Z_BONUS: f64 = 0.10;
pub const HIGH_IMPACT_BEHAVIOR: &str = "reduce";

pub fn high_impact_dates() -> std::collections::HashMap<&'static str, &'static str> {
    let mut m = std::collections::HashMap::new();
    // FOMC 2026
    for d in &["2026-01-28","2026-03-18","2026-05-06","2026-06-17",
               "2026-07-29","2026-09-16","2026-10-28","2026-12-09"] {
        m.insert(*d, "FOMC");
    }
    // CPI 2026
    for d in &["2026-01-14","2026-02-11","2026-03-11","2026-04-10",
               "2026-05-13","2026-06-10","2026-07-14","2026-08-12",
               "2026-09-11","2026-10-13","2026-11-12","2026-12-10"] {
        m.insert(*d, "CPI");
    }
    // NFP 2026
    for d in &["2026-01-09","2026-02-06","2026-03-06","2026-04-03",
               "2026-05-01","2026-06-05","2026-07-02","2026-08-07",
               "2026-09-04","2026-10-02","2026-11-06","2026-12-04"] {
        m.insert(*d, "NFP");
    }
    m
}

pub const WEB_PORT: u16 = 8080;
pub const TOKEN_MAX_AGE_SECS: u64 = 12 * 3600;
pub const SYNC_INTERVAL_SECS: u64 = 5;
pub const SAVE_INTERVAL_SECS: u64 = 30;
pub const STATS_INTERVAL_SECS: u64 = 60;
pub const MC_SIMULATIONS: u32 = 10_000;
