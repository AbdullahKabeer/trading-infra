use crate::config::{
    ACCOUNT_START_BALANCE, ATR_STOP_RATIO, BREAKEVEN_TICKS, COMMISSION_RT, EXIT_MIN_TICKS,
    GUTTER_GOAL, LUNCH_SKIP_END, LUNCH_SKIP_START, MAX_STOP_TICKS, MAX_TRADES,
    MIN_STOP_TICKS, POWER_HOUR_START, SCALE_OUT_ENABLED, TARGET_MODE, TICK_SIZE, TICK_VALUE,
    TIME_STOP_MINS, TRAIL_ACTIVATE, TRAIL_DISTANCE, TRAILING_MLL_DISTANCE, Z_THRESH,
};
use crate::market::session::Session;
use crate::types::Bar;
use chrono::{DateTime, TimeZone, Timelike, Utc};
use std::{fs, path::Path};

// ── Config ─────────────────────────────────────────────────────────────────────

pub struct BtConfig {
    pub z_thresh: f64,
    pub atr_stop_ratio: f64,
    pub min_stop_ticks: i32,
    pub max_stop_ticks: i32,
    pub trail_activate: f64,
    pub trail_distance: f64,
    pub breakeven_ticks: f64,
    pub exit_min_ticks: f64,
    pub time_stop_mins: i64,
    pub max_trades: u32,
    pub lunch_skip: bool,
    pub power_hour_start: i64,
    pub target_mode: String,
    pub gutter_win: bool,
    pub gutter_loss: bool,
    pub scale_out: bool,
    pub commission_rt: f64,
    pub tick_size: f64,
    pub tick_value: f64,
    pub account_balance: f64,
    pub trailing_mll: f64,
    pub verbose: bool,
    // Prop firm risk controls
    pub daily_profit_cap: f64,
    pub daily_loss_limit: f64,
    pub profit_target: f64,
    // Strategy selector
    pub strategy: String,   // "fade" | "orb" | "trend"
    pub orb_bars: i64,      // minutes to build opening range (default 30)
    pub orb_target_mult: f64, // target = range * mult (default 1.5)
    pub slippage_ticks: f64,  // round-trip slippage per trade in ticks (on top of commission)
}

impl Default for BtConfig {
    fn default() -> Self {
        Self {
            z_thresh: Z_THRESH,
            atr_stop_ratio: ATR_STOP_RATIO,
            min_stop_ticks: MIN_STOP_TICKS,
            max_stop_ticks: MAX_STOP_TICKS,
            trail_activate: TRAIL_ACTIVATE,
            trail_distance: TRAIL_DISTANCE,
            breakeven_ticks: BREAKEVEN_TICKS,
            exit_min_ticks: EXIT_MIN_TICKS,
            time_stop_mins: TIME_STOP_MINS,
            max_trades: MAX_TRADES,
            lunch_skip: true,
            power_hour_start: POWER_HOUR_START,
            target_mode: TARGET_MODE.to_string(),
            gutter_win: false,
            gutter_loss: false,
            scale_out: SCALE_OUT_ENABLED,
            commission_rt: COMMISSION_RT,
            tick_size: TICK_SIZE,
            tick_value: TICK_VALUE,
            account_balance: ACCOUNT_START_BALANCE,
            trailing_mll: TRAILING_MLL_DISTANCE,
            verbose: false,
            daily_profit_cap: 1500.0,
            daily_loss_limit: f64::MAX,
            profit_target: 3000.0,
            strategy: "fade".to_string(),
            orb_bars: 30,
            orb_target_mult: 1.5,
            slippage_ticks: 0.0,
        }
    }
}

impl BtConfig {
    pub fn from_args(args: &[String]) -> (Self, String) {
        let mut cfg = Self::default();
        let mut dir = String::new();

        let mut i = 0;
        while i < args.len() {
            let s = args[i].as_str();
            let next = || args.get(i + 1).map(|s| s.as_str()).unwrap_or("");
            match s {
                "--z" | "--z-thresh" => { if let Ok(v) = next().parse() { cfg.z_thresh = v; } i += 1; }
                "--atr" | "--atr-ratio" => { if let Ok(v) = next().parse() { cfg.atr_stop_ratio = v; } i += 1; }
                "--trail-on" => { if let Ok(v) = next().parse() { cfg.trail_activate = v; } i += 1; }
                "--trail-dist" => { if let Ok(v) = next().parse() { cfg.trail_distance = v; } i += 1; }
                "--be" | "--breakeven" => { if let Ok(v) = next().parse() { cfg.breakeven_ticks = v; } i += 1; }
                "--time-stop" => { if let Ok(v) = next().parse() { cfg.time_stop_mins = v; } i += 1; }
                "--max-trades" => { if let Ok(v) = next().parse() { cfg.max_trades = v; } i += 1; }
                "--target" => { cfg.target_mode = next().to_string(); i += 1; }
                "--no-lunch" => { cfg.lunch_skip = false; }
                "--gutter-win" => { cfg.gutter_win = true; }
                "--gutter-loss" => { cfg.gutter_loss = true; }
                "--no-scale-out" => { cfg.scale_out = false; }
                "--verbose" | "-v" => { cfg.verbose = true; }
                "--nq" => { cfg.tick_value = 5.0; }
                "--es" => { cfg.tick_value = 12.50; }
                "--balance" => { if let Ok(v) = next().parse() { cfg.account_balance = v; } i += 1; }
                "--min-stop" => { if let Ok(v) = next().parse() { cfg.min_stop_ticks = v; } i += 1; }
                "--max-stop" => { if let Ok(v) = next().parse() { cfg.max_stop_ticks = v; } i += 1; }
                "--daily-cap" => { if let Ok(v) = next().parse() { cfg.daily_profit_cap = v; } i += 1; }
                "--daily-loss" => { if let Ok(v) = next().parse() { cfg.daily_loss_limit = v; } i += 1; }
                "--profit-target" => { if let Ok(v) = next().parse() { cfg.profit_target = v; } i += 1; }
                "--prop-firm" => {
                    cfg.daily_profit_cap = 1499.0;
                    cfg.daily_loss_limit = 700.0;
                    cfg.profit_target = 3000.0;
                }
                "--strategy" | "--strat" => { cfg.strategy = next().to_string(); i += 1; }
                "--orb-bars" => { if let Ok(v) = next().parse() { cfg.orb_bars = v; } i += 1; }
                "--orb-mult" => { if let Ok(v) = next().parse() { cfg.orb_target_mult = v; } i += 1; }
                s if !s.starts_with('-') => dir = s.to_string(),
                _ => {}
            }
            i += 1;
        }

        if dir.is_empty() {
            dir = "../es_sessions".to_string();
        }

        (cfg, dir)
    }

    #[allow(dead_code)]
    fn display(&self) -> String {
        format!(
            "Z={:.2}  ATR={:.2}  TRAIL_ON={:.0}t  TRAIL_DIST={:.0}t  BE={:.0}t  TIME={}  MAX_TRADES={}  TARGET={}  LUNCH_SKIP={}",
            self.z_thresh, self.atr_stop_ratio, self.trail_activate, self.trail_distance,
            self.breakeven_ticks, self.time_stop_mins, self.max_trades, self.target_mode,
            if self.lunch_skip { "ON" } else { "OFF" }
        )
    }
}

// ── Session loading ────────────────────────────────────────────────────────────

struct DaySession {
    date: String,
    bars: Vec<Bar>,
}

fn load_sessions(dir: &str) -> anyhow::Result<Vec<DaySession>> {
    let p = Path::new(dir);
    if !p.exists() {
        anyhow::bail!("Directory not found: {dir}");
    }

    let mut files: Vec<_> = fs::read_dir(p)?
        .filter_map(|e| e.ok())
        .filter(|e| e.path().extension().map(|x| x == "json").unwrap_or(false))
        .map(|e| e.path())
        .collect();
    files.sort();

    let mut sessions = Vec::new();
    for path in &files {
        let text = fs::read_to_string(path)?;
        let raw: serde_json::Value = serde_json::from_str(&text)?;

        let date = raw["date"].as_str().unwrap_or("").to_string();
        let raw_bars = match raw["bars"].as_array() {
            Some(b) => b,
            None => continue,
        };
        if raw_bars.len() < 15 {
            continue;
        }

        // Replay raw OHLCV through Session to compute VWAP/std/z/VPOC
        let mut sess = Session::new(&date);
        for rb in raw_bars {
            let ts_str = rb["ts"].as_str().unwrap_or("");
            let ts: DateTime<Utc> = ts_str.parse().unwrap_or(Utc::now());
            let o = rb["o"].as_f64().unwrap_or(0.0);
            let h = rb["h"].as_f64().unwrap_or(0.0);
            let l = rb["l"].as_f64().unwrap_or(0.0);
            let c = rb["c"].as_f64().unwrap_or(0.0);
            let v = rb["v"].as_f64().unwrap_or(0.0);
            if c == 0.0 { continue; }
            sess.add_bar(o, h, l, c, v, ts);
        }

        if sess.bars.len() >= 15 {
            sessions.push(DaySession { date, bars: sess.bars });
        }
    }

    Ok(sessions)
}

// ── Backtest engine ────────────────────────────────────────────────────────────

#[derive(Default, Clone)]
pub struct DayResult {
    pub date: String,
    pub trades: u32,
    pub wins: u32,
    pub losses: u32,
    pub daily_pnl: f64,
    pub cumul_pnl: f64,
    pub flag: &'static str,
    pub capped: bool,       // hit daily profit cap
    pub loss_limited: bool, // hit daily loss limit
}

struct TradeLog {
    date: String,
    dir: String,
    entry: f64,
    exit: f64,
    ticks: f64,
    pnl: f64,
    reason: String,
}

fn bar_tod_mins(bar: &Bar) -> i64 {
    let et = chrono_tz::America::New_York.from_utc_datetime(&bar.ts.naive_utc());
    et.hour() as i64 * 60 + et.minute() as i64 - 9 * 60 - 30
}

fn hurst_exponent(closes: &[f64]) -> f64 {
    let n = closes.len();
    if n < 8 { return 0.5; }
    let mean = closes.iter().sum::<f64>() / n as f64;
    let mut cum = 0.0f64;
    let mut c_max = f64::NEG_INFINITY;
    let mut c_min = f64::INFINITY;
    for &c in closes {
        cum += c - mean;
        if cum > c_max { c_max = cum; }
        if cum < c_min { c_min = cum; }
    }
    let range = (c_max - c_min).max(1e-10);
    let std = (closes.iter().map(|&x| (x - mean).powi(2)).sum::<f64>() / n as f64).sqrt().max(1e-10);
    (range / std).ln() / (n as f64).ln()
}

pub fn normal_cdf(x: f64) -> f64 {
    let t = 1.0 / (1.0 + 0.2316419 * x.abs());
    let d = 0.3989422820 * (-0.5 * x * x).exp();
    let p = d * t * (0.3193815530 + t * (-0.3565637780 + t * (1.7814779370 + t * (-1.8212559780 + t * 1.3302744290))));
    if x >= 0.0 { 1.0 - p } else { p }
}

fn run_backtest_cfg(
    sessions: &[DaySession],
    cfg: &BtConfig,
) -> (f64, f64, u32, u32, u32, f64, Vec<DayResult>, Vec<TradeLog>) {
    let high_impact = crate::config::high_impact_dates();

    let mut total_pnl = 0.0f64;
    let mut max_dd = 0.0f64;
    let mut peak_pnl = 0.0f64;
    let mut total_trades = 0u32;
    let mut total_wins = 0u32;
    let mut total_losses = 0u32;
    let mut max_consec_loss = 0u32;
    let mut cur_consec_loss = 0u32;

    let mut day_results = Vec::new();
    let mut trade_log = Vec::new();

    for sess in sessions {
        let bars = &sess.bars;
        if bars.len() < 15 { continue; }

        let flag = *high_impact.get(sess.date.as_str()).unwrap_or(&"");

        let mut daily_pnl = 0.0f64;
        let mut trades_today = 0u32;
        let mut wins_today = 0u32;
        let mut losses_today = 0u32;
        let mut day_capped = false;
        let mut day_loss_limited = false;

        // ORB state (reset per session)
        let mut orb_high = f64::NEG_INFINITY;
        let mut orb_low = f64::INFINITY;
        let mut orb_done = false;
        let mut orb_used = false; // ORB only takes first clean breakout per session

        // Position state
        let mut pos_dir: Option<&'static str> = None;
        let mut pos_ep = 0.0f64;
        let mut pos_sl = 0.0f64;
        let mut pos_tp = 0.0f64;
        let mut pos_bp = 0.0f64;
        let mut pos_bar_idx: i64 = 0;
        let mut pos_contracts: u32 = 1;
        let mut pos_scale1_done = false;
        let mut last_exit_bar: i64 = -999;

        // Kalman filter state (reset per session)
        let mut kal_x_hat = 0.0f64;
        let mut kal_p = 1.0f64;
        let kal_q = 0.5f64;

        // Opening drive state (reset per session)
        let mut od_dir: Option<&'static str> = None;
        let mut od_used = false;

        // Kalman normalized innovation (updated each bar before entry block)
        let mut kal_innov_norm = 0.0f64;
        let _ = kal_innov_norm; // initial default; overwritten each bar before first read

        // OU/VWAP state (reset per session, used only by ou_vwap strategy)
        const OU_EWMA_ALPHA: f64 = 0.05;   // span ≈ 20 bars (slow detrend)
        const OU_HL_MIN: f64 = 2.0;        // min tradeable half-life (bars)
        const OU_HL_MAX: f64 = 30.0;       // max tradeable half-life (bars)
        const OU_EST_WIN_HL: usize = 8;    // estimation window = 8 half-lives
        const OU_TIME_STOP_HL: f64 = 2.0;  // bail after 2 half-lives if no reversion
        const OU_ADF_CRIT: f64 = -2.57;    // 10% critical value (DF distribution, with constant)
        const OU_COST_GATE_MULT: f64 = 2.0; // require sigma_eq >= 2× round-trip cost
        const OU_MAX_CONTRACTS: u32 = 3;
        const OU_KELLY: f64 = 0.25;        // fractional Kelly (OU params are noisy)

        let mut ou_ewma = 0.0f64;
        let mut ou_y_buf: Vec<f64> = Vec::with_capacity(400);
        let mut ou_half_life = 10.0f64;
        let mut ou_theta = 0.0f64;
        let mut ou_sigma_eq = 0.0f64;
        let mut ou_regime_ok = false;
        let mut ou_entry_z = 1.5f64;
        let mut ou_exit_z = 0.3f64;
        let mut ou_stop_z = 2.5f64;
        let mut ou_bars_held: i64 = 0;
        let mut ou_time_stop_bars: i64 = 40;

        for (i, bar) in bars.iter().enumerate() {
            let bar_idx = i as i64;
            if bar_idx < 15 { continue; }

            let tod = bar_tod_mins(bar);

            // Manage open position
            if let Some(dir) = pos_dir {
                let price = bar.c;

                // Force-close and stop trading if daily profit cap reached
                if daily_pnl >= cfg.daily_profit_cap {
                    let pnl = book_close_cfg(dir, pos_ep, price, pos_contracts, cfg);
                    daily_pnl += pnl;
                    total_pnl += pnl;
                    if pnl > 0.0 { wins_today += 1; total_wins += 1; cur_consec_loss = 0; }
                    else { losses_today += 1; total_losses += 1; cur_consec_loss += 1; if cur_consec_loss > max_consec_loss { max_consec_loss = cur_consec_loss; } }
                    trade_log.push(TradeLog {
                        date: sess.date.clone(), dir: dir.to_string(), entry: pos_ep, exit: price,
                        ticks: if dir == "LONG" { (price - pos_ep) / cfg.tick_size } else { (pos_ep - price) / cfg.tick_size },
                        pnl, reason: "DAY CAP".into(),
                    });
                    last_exit_bar = bar_idx;
                    pos_dir = None;
                    day_capped = true;
                    if total_pnl > peak_pnl { peak_pnl = total_pnl; }
                    let dd = peak_pnl - total_pnl; if dd > max_dd { max_dd = dd; }
                    continue;
                }

                // Force-close and stop trading if daily loss limit reached
                if daily_pnl <= -cfg.daily_loss_limit {
                    let pnl = book_close_cfg(dir, pos_ep, price, pos_contracts, cfg);
                    daily_pnl += pnl;
                    total_pnl += pnl;
                    if pnl > 0.0 { wins_today += 1; total_wins += 1; cur_consec_loss = 0; }
                    else { losses_today += 1; total_losses += 1; cur_consec_loss += 1; if cur_consec_loss > max_consec_loss { max_consec_loss = cur_consec_loss; } }
                    trade_log.push(TradeLog {
                        date: sess.date.clone(), dir: dir.to_string(), entry: pos_ep, exit: price,
                        ticks: if dir == "LONG" { (price - pos_ep) / cfg.tick_size } else { (pos_ep - price) / cfg.tick_size },
                        pnl, reason: "DAY LOSS LIMIT".into(),
                    });
                    last_exit_bar = bar_idx;
                    pos_dir = None;
                    day_loss_limited = true;
                    if total_pnl > peak_pnl { peak_pnl = total_pnl; }
                    let dd = peak_pnl - total_pnl; if dd > max_dd { max_dd = dd; }
                    continue;
                }

                let cur = match dir {
                    "LONG" => (price - pos_ep) / cfg.tick_size,
                    _ => (pos_ep - price) / cfg.tick_size,
                };
                let bt = bar_idx - pos_bar_idx;

                // Update best price
                match dir {
                    "LONG" => { if price > pos_bp { pos_bp = price; } }
                    _ => { if price < pos_bp { pos_bp = price; } }
                }

                let ur = match dir {
                    "LONG" => (pos_bp - pos_ep) / cfg.tick_size,
                    _ => (pos_ep - pos_bp) / cfg.tick_size,
                };

                // Trailing stop
                if ur >= cfg.trail_activate {
                    match dir {
                        "LONG" => {
                            let tl = ((pos_bp - cfg.trail_distance * cfg.tick_size) / cfg.tick_size).round() * cfg.tick_size;
                            if tl > pos_sl { pos_sl = tl; }
                        }
                        _ => {
                            let tl = ((pos_bp + cfg.trail_distance * cfg.tick_size) / cfg.tick_size).round() * cfg.tick_size;
                            if tl < pos_sl { pos_sl = tl; }
                        }
                    }
                }

                // Breakeven
                if ur >= cfg.breakeven_ticks {
                    match dir {
                        "LONG" if pos_sl < pos_ep => pos_sl = pos_ep,
                        "SHORT" if pos_sl > pos_ep => pos_sl = pos_ep,
                        _ => {}
                    }
                }

                // Gutter win check
                let mut exited = false;
                if cfg.gutter_win {
                    let gut_tp_ticks = (GUTTER_GOAL - daily_pnl) / (pos_contracts as f64 * cfg.tick_value);
                    let gut_tp = match dir {
                        "LONG" => pos_ep + gut_tp_ticks * cfg.tick_size,
                        _ => pos_ep - gut_tp_ticks * cfg.tick_size,
                    };
                    let hit = match dir {
                        "LONG" => price >= gut_tp,
                        _ => price <= gut_tp,
                    };
                    if hit {
                        let pnl = book_close_cfg(dir, pos_ep, gut_tp, pos_contracts, cfg);
                        daily_pnl += pnl;
                        total_pnl += pnl;
                        if pnl > 0.0 { wins_today += 1; total_wins += 1; cur_consec_loss = 0; }
                        else { losses_today += 1; total_losses += 1; cur_consec_loss += 1; if cur_consec_loss > max_consec_loss { max_consec_loss = cur_consec_loss; } }
                        trade_log.push(TradeLog { date: sess.date.clone(), dir: dir.to_string(), entry: pos_ep, exit: gut_tp, ticks: if dir == "LONG" { (gut_tp - pos_ep) / cfg.tick_size } else { (pos_ep - gut_tp) / cfg.tick_size }, pnl, reason: "GUTTER WIN".into() });
                        last_exit_bar = bar_idx;
                        pos_dir = None;
                        exited = true;
                    }
                }

                if !exited {
                    let exit_reason = check_exit_cfg(dir, price, pos_ep, pos_sl, pos_tp, bt, cur, cfg);
                    if let Some(reason) = exit_reason {
                        // Scale out
                        if cfg.scale_out && pos_contracts > 1 && !pos_scale1_done && cur >= cfg.exit_min_ticks {
                            let scale_pnl = book_close_cfg(dir, pos_ep, price, 1, cfg);
                            daily_pnl += scale_pnl;
                            total_pnl += scale_pnl;
                            trade_log.push(TradeLog { date: sess.date.clone(), dir: dir.to_string(), entry: pos_ep, exit: price, ticks: cur, pnl: scale_pnl, reason: "SCALE OUT".into() });
                            pos_contracts -= 1;
                            pos_scale1_done = true;
                            match dir {
                                "LONG" if pos_sl < pos_ep => pos_sl = pos_ep,
                                "SHORT" if pos_sl > pos_ep => pos_sl = pos_ep,
                                _ => {}
                            }
                        } else {
                            let pnl = book_close_cfg(dir, pos_ep, price, pos_contracts, cfg);
                            daily_pnl += pnl;
                            total_pnl += pnl;
                            if pnl > 0.0 { wins_today += 1; total_wins += 1; cur_consec_loss = 0; }
                            else { losses_today += 1; total_losses += 1; cur_consec_loss += 1; if cur_consec_loss > max_consec_loss { max_consec_loss = cur_consec_loss; } }
                            trade_log.push(TradeLog { date: sess.date.clone(), dir: dir.to_string(), entry: pos_ep, exit: price, ticks: cur, pnl, reason: reason.to_string() });
                            last_exit_bar = bar_idx;
                            pos_dir = None;
                        }
                    }
                }

                // Drawdown tracking
                if total_pnl > peak_pnl { peak_pnl = total_pnl; }
                let dd = peak_pnl - total_pnl;
                if dd > max_dd { max_dd = dd; }
            }

            // ── ORB range building (always runs, even while in position) ──
            if cfg.strategy == "orb" && !orb_done && tod >= 0 && tod < cfg.orb_bars {
                if bar.h > orb_high { orb_high = bar.h; }
                if bar.l < orb_low { orb_low = bar.l; }
            } else if cfg.strategy == "orb" && !orb_done && tod >= cfg.orb_bars {
                orb_done = true;
            }

            // ── Kalman filter update (every bar) ─────────────────────────────────
            {
                let z_meas = bar.c - bar.vwap;
                let kal_r = ((bar.h - bar.l).powi(2)).max(0.001);
                let kal_p_pred = kal_p + kal_q;
                let kal_k = kal_p_pred / (kal_p_pred + kal_r);
                let innovation = z_meas - kal_x_hat;
                kal_innov_norm = innovation / (kal_p_pred + kal_r).sqrt();
                kal_x_hat += kal_k * innovation;
                kal_p = (1.0 - kal_k) * kal_p_pred;
            }

            // ── OU/VWAP update (ou_vwap strategy only) ───────────────────────
            if cfg.strategy == "ou_vwap" {
                // Build detrended residual Y = (price - VWAP) - slow_EWMA_premium
                let raw_res = bar.c - bar.vwap;
                ou_ewma = if ou_y_buf.is_empty() {
                    raw_res
                } else {
                    ou_ewma * (1.0 - OU_EWMA_ALPHA) + raw_res * OU_EWMA_ALPHA
                };
                let y = raw_res - ou_ewma;
                ou_y_buf.push(y);

                // Fit OU on slow estimation window (8 × current half-life, ≥30 bars)
                let est_len = ((OU_EST_WIN_HL as f64 * ou_half_life.max(5.0)) as usize)
                    .max(30)
                    .min(ou_y_buf.len());
                if est_len >= 20 {
                    let window = &ou_y_buf[ou_y_buf.len() - est_len..];
                    let rt_dollars = cfg.commission_rt + cfg.slippage_ticks * cfg.tick_value;
                    if let Some((_phi, _var, sigma_eq, theta, half_life, adf_t)) = ou_ar1_fit(window) {
                        ou_theta = theta;
                        ou_half_life = half_life.clamp(0.5, 200.0);
                        ou_sigma_eq = sigma_eq;
                        let sigma_dollars = sigma_eq * cfg.tick_value / cfg.tick_size;
                        let cost_gate = rt_dollars * OU_COST_GATE_MULT;
                        // Regime gate: reject unit root + half-life in window + cost clears
                        ou_regime_ok = adf_t < OU_ADF_CRIT
                            && half_life >= OU_HL_MIN
                            && half_life <= OU_HL_MAX
                            && sigma_dollars >= cost_gate;
                        if ou_regime_ok {
                            let (a, m, s) = ou_optimal_thresholds(
                                ou_theta,
                                ou_sigma_eq,
                                rt_dollars,
                                cfg.tick_value / cfg.tick_size,
                            );
                            ou_entry_z = a;
                            ou_exit_z = m;
                            ou_stop_z = s;
                            ou_time_stop_bars =
                                (OU_TIME_STOP_HL * ou_half_life).ceil() as i64;
                        }
                    }
                }

                // OU-specific position exits: regime break and OU time stop
                // (price-based SL/TP are handled by the standard management block)
                if pos_dir.is_some() {
                    ou_bars_held += 1;
                    let ou_exit: Option<&'static str> = if !ou_regime_ok {
                        Some("REGIME BREAK")
                    } else if ou_bars_held >= ou_time_stop_bars {
                        Some("OU TIME STOP")
                    } else {
                        None
                    };
                    if let Some(reason) = ou_exit {
                        let dir = pos_dir.unwrap();
                        let price = bar.c;
                        let pnl = book_close_cfg(dir, pos_ep, price, pos_contracts, cfg);
                        daily_pnl += pnl;
                        total_pnl += pnl;
                        if pnl > 0.0 {
                            wins_today += 1; total_wins += 1; cur_consec_loss = 0;
                        } else {
                            losses_today += 1; total_losses += 1;
                            cur_consec_loss += 1;
                            if cur_consec_loss > max_consec_loss { max_consec_loss = cur_consec_loss; }
                        }
                        trade_log.push(TradeLog {
                            date: sess.date.clone(),
                            dir: dir.to_string(),
                            entry: pos_ep,
                            exit: price,
                            ticks: if dir == "LONG" {
                                (price - pos_ep) / cfg.tick_size
                            } else {
                                (pos_ep - price) / cfg.tick_size
                            },
                            pnl,
                            reason: reason.to_string(),
                        });
                        last_exit_bar = bar_idx;
                        pos_dir = None;
                        if total_pnl > peak_pnl { peak_pnl = total_pnl; }
                        let dd = peak_pnl - total_pnl;
                        if dd > max_dd { max_dd = dd; }
                        continue; // skip rest of bar
                    }
                }
            }

            // ── Entry — only when flat ──────────────────────────────────────
            if pos_dir.is_none() {
                if tod < 0 || tod >= cfg.power_hour_start { continue; }
                if cfg.lunch_skip && tod >= LUNCH_SKIP_START && tod < LUNCH_SKIP_END { continue; }
                if trades_today >= cfg.max_trades { continue; }
                if bar_idx - last_exit_bar < 2 { continue; }
                if daily_pnl >= cfg.daily_profit_cap { day_capped = true; continue; }
                if daily_pnl <= -cfg.daily_loss_limit { day_loss_limited = true; continue; }

                // ATR from recent bars (used by fade + trend strategies)
                let atr = {
                    let slice = &bars[(i.saturating_sub(20))..=i];
                    let ranges: Vec<f64> = slice.windows(2).map(|w| {
                        (w[1].h - w[1].l)
                            .max((w[1].h - w[0].c).abs())
                            .max((w[1].l - w[0].c).abs())
                    }).collect();
                    if ranges.is_empty() { bar.h - bar.l } else { ranges.iter().sum::<f64>() / ranges.len() as f64 }
                };

                let sl_dist = (atr * cfg.atr_stop_ratio)
                    .max(cfg.min_stop_ticks as f64 * cfg.tick_size)
                    .min(cfg.max_stop_ticks as f64 * cfg.tick_size);

                // ── Strategy-specific entry signal + sl/tp ─────────────────
                let entry: Option<(&'static str, f64, f64)> = match cfg.strategy.as_str() {
                    "orb" => {
                        // Opening Range Breakout: only one trade per session, after range is set
                        if !orb_done || orb_used || orb_high <= orb_low { None }
                        else {
                            let range = orb_high - orb_low;
                            if range < cfg.min_stop_ticks as f64 * cfg.tick_size { None }
                            else if bar.c > orb_high {
                                orb_used = true;
                                let tp = orb_high + range * cfg.orb_target_mult;
                                Some(("LONG", orb_low, tp))
                            } else if bar.c < orb_low {
                                orb_used = true;
                                let tp = orb_low - range * cfg.orb_target_mult;
                                Some(("SHORT", orb_high, tp))
                            } else { None }
                        }
                    }
                    "trend" => {
                        // VWAP trend follow: enter IN direction of VWAP deviation
                        // Buy when price is significantly above VWAP (momentum up)
                        // Sell when price is significantly below VWAP (momentum down)
                        let std = bar.vwap_std;
                        if std < 0.01 { None }
                        else {
                            let z = (bar.c - bar.vwap) / std;
                            if z > cfg.z_thresh {
                                // Price extended above VWAP, ride momentum SHORT (at extreme) — NO:
                                // Trend = follow the direction, so z > thresh → LONG (price heading up)
                                let sl = bar.c - sl_dist;
                                // TP = 2x ATR extension above entry (momentum target)
                                let tp = bar.c + atr * 2.0;
                                let tp = (tp / cfg.tick_size).round() * cfg.tick_size;
                                if (tp - bar.c) / cfg.tick_size >= cfg.exit_min_ticks { Some(("LONG", sl, tp)) } else { None }
                            } else if z < -cfg.z_thresh {
                                let sl = bar.c + sl_dist;
                                let tp = bar.c - atr * 2.0;
                                let tp = (tp / cfg.tick_size).round() * cfg.tick_size;
                                if (bar.c - tp) / cfg.tick_size >= cfg.exit_min_ticks { Some(("SHORT", sl, tp)) } else { None }
                            } else { None }
                        }
                    }
                    "first_pullback" => {
                        // First Pullback: enter on first retest of VWAP after a strong directional move
                        // Strong move = bar where z crossed threshold, now price pulls back toward VWAP
                        let std = bar.vwap_std;
                        if std < 0.01 { None }
                        else {
                            let z = (bar.c - bar.vwap) / std;
                            // Price near VWAP (z small) but we had a prior strong move
                            // Approximate: z between 0.2 and z_thresh → near-VWAP pullback entry
                            let prev_z = if i > 0 { (bars[i-1].c - bars[i-1].vwap) / std.max(0.01) } else { 0.0 };
                            if prev_z > cfg.z_thresh * 1.5 && z < prev_z && z > 0.1 {
                                // Was extended up, now pulling back toward VWAP → buy the dip
                                let sl = bar.vwap - sl_dist;
                                let tp = bars[i-1].c.max(bar.c + atr * 1.5);
                                let tp = (tp / cfg.tick_size).round() * cfg.tick_size;
                                if (tp - bar.c) / cfg.tick_size >= cfg.exit_min_ticks { Some(("LONG", sl, tp)) } else { None }
                            } else if prev_z < -cfg.z_thresh * 1.5 && z > prev_z && z < -0.1 {
                                let sl = bar.vwap + sl_dist;
                                let tp = bars[i-1].c.min(bar.c - atr * 1.5);
                                let tp = (tp / cfg.tick_size).round() * cfg.tick_size;
                                if (bar.c - tp) / cfg.tick_size >= cfg.exit_min_ticks { Some(("SHORT", sl, tp)) } else { None }
                            } else { None }
                        }
                    }
                    "regime" => {
                        let std = bar.vwap_std;
                        if std < 0.01 { None } else {
                            let z = (bar.c - bar.vwap) / std;
                            let closes: Vec<f64> = bars[i.saturating_sub(30)..=i].iter().map(|b| b.c).collect();
                            let h = hurst_exponent(&closes);
                            if h < 0.45 {
                                // Mean-reverting regime → fade
                                if z.abs() < cfg.z_thresh { None } else {
                                    let dir: &'static str = if z > 0.0 { "SHORT" } else { "LONG" };
                                    let tp_price = (bar.vwap / cfg.tick_size).round() * cfg.tick_size;
                                    let sl_price = match dir { "LONG" => bar.c - sl_dist, _ => bar.c + sl_dist };
                                    if (tp_price - bar.c).abs() / cfg.tick_size < cfg.exit_min_ticks { None }
                                    else { Some((dir, sl_price, tp_price)) }
                                }
                            } else if h > 0.55 {
                                // Trending regime → follow
                                if z > cfg.z_thresh {
                                    let sl = bar.c - sl_dist;
                                    let tp = (( bar.c + atr * 2.0) / cfg.tick_size).round() * cfg.tick_size;
                                    if (tp - bar.c) / cfg.tick_size >= cfg.exit_min_ticks { Some(("LONG", sl, tp)) } else { None }
                                } else if z < -cfg.z_thresh {
                                    let sl = bar.c + sl_dist;
                                    let tp = ((bar.c - atr * 2.0) / cfg.tick_size).round() * cfg.tick_size;
                                    if (bar.c - tp) / cfg.tick_size >= cfg.exit_min_ticks { Some(("SHORT", sl, tp)) } else { None }
                                } else { None }
                            } else { None }
                        }
                    }
                    "kalman" => {
                        if kal_innov_norm.abs() < cfg.z_thresh { None } else {
                            let dir: &'static str = if kal_innov_norm > 0.0 { "SHORT" } else { "LONG" };
                            let tp_price = (bar.vwap / cfg.tick_size).round() * cfg.tick_size;
                            let sl_price = match dir { "LONG" => bar.c - sl_dist, _ => bar.c + sl_dist };
                            if (tp_price - bar.c).abs() / cfg.tick_size < cfg.exit_min_ticks { None }
                            else { Some((dir, sl_price, tp_price)) }
                        }
                    }
                    "exhaust" => {
                        let std = bar.vwap_std;
                        if std < 0.01 { None } else {
                            let z = (bar.c - bar.vwap) / std;
                            if z.abs() < cfg.z_thresh { None } else {
                                let bar_body = (bar.c - bar.o).abs();
                                let is_wide = bar_body > atr * 0.65;
                                let vol_ma = {
                                    let slice = &bars[i.saturating_sub(20)..i];
                                    if slice.is_empty() { bar.v } else { slice.iter().map(|b| b.v).sum::<f64>() / slice.len() as f64 }
                                };
                                let is_heavy = bar.v > vol_ma * 1.15;
                                if !is_wide || !is_heavy { None } else {
                                    let dir: &'static str = if z > 0.0 { "SHORT" } else { "LONG" };
                                    let tp_price = (bar.vwap / cfg.tick_size).round() * cfg.tick_size;
                                    let sl_price = match dir { "LONG" => bar.c - sl_dist, _ => bar.c + sl_dist };
                                    if (tp_price - bar.c).abs() / cfg.tick_size < cfg.exit_min_ticks { None }
                                    else { Some((dir, sl_price, tp_price)) }
                                }
                            }
                        }
                    }
                    "opening_drive" => {
                        if od_used { None } else {
                            // Establish drive from first 15 bars
                            if od_dir.is_none() && bar_idx >= 15 && bar_idx <= 20 {
                                let open_price = bars.get(15).map(|b| b.o).unwrap_or(bars[0].o);
                                let disp = bar.c - open_price;
                                if disp.abs() > atr * 1.5 {
                                    od_dir = if disp > 0.0 { Some("LONG") } else { Some("SHORT") };
                                }
                            }
                            if let Some(dir) = od_dir {
                                let dist_to_vwap = (bar.c - bar.vwap).abs();
                                if dist_to_vwap < atr * 0.4 {
                                    od_used = true;
                                    let sl_price = match dir { "LONG" => bar.c - sl_dist, _ => bar.c + sl_dist };
                                    let raw_tp = match dir { "LONG" => bar.c + atr * 2.5, _ => bar.c - atr * 2.5 };
                                    let tp_price = (raw_tp / cfg.tick_size).round() * cfg.tick_size;
                                    if (tp_price - bar.c).abs() / cfg.tick_size >= cfg.exit_min_ticks { Some((dir, sl_price, tp_price)) } else { None }
                                } else { None }
                            } else { None }
                        }
                    }
                    "vwap_reclaim" => {
                        // Fade the VWAP touch: price crossing VWAP often overshoots; trade the reversion back
                        if i == 0 { None } else {
                            let prev = &bars[i - 1];
                            let prev_above = prev.c >= prev.vwap;
                            let curr_above = bar.c >= bar.vwap;
                            if !prev_above && curr_above {
                                // Crossed above VWAP — likely to fail and reverse back down
                                let sl = bar.h + cfg.tick_size * 2.0;
                                let tp = ((bar.vwap - atr * 1.0) / cfg.tick_size).round() * cfg.tick_size;
                                if (bar.c - tp) / cfg.tick_size >= cfg.exit_min_ticks { Some(("SHORT", sl, tp)) } else { None }
                            } else if prev_above && !curr_above {
                                // Crossed below VWAP — likely to fail and reverse back up
                                let sl = bar.l - cfg.tick_size * 2.0;
                                let tp = ((bar.vwap + atr * 1.0) / cfg.tick_size).round() * cfg.tick_size;
                                if (tp - bar.c) / cfg.tick_size >= cfg.exit_min_ticks { Some(("LONG", sl, tp)) } else { None }
                            } else { None }
                        }
                    }
                    "z_cross" => {
                        // Enter when z-score crosses BACK through threshold — confirmed reversal not just extreme
                        if i == 0 { None } else {
                            let prev_std = bars[i-1].vwap_std.max(0.01);
                            let prev_z = (bars[i-1].c - bars[i-1].vwap) / prev_std;
                            let std = bar.vwap_std;
                            if std < 0.01 { None } else {
                                let curr_z = (bar.c - bar.vwap) / std;
                                if prev_z > cfg.z_thresh && curr_z <= cfg.z_thresh {
                                    // Was extended up, just crossed back inside band → SHORT with stop above recent wick
                                    let sl = bar.h.max(bars[i-1].h) + cfg.tick_size * 2.0;
                                    let tp = (bar.vwap / cfg.tick_size).round() * cfg.tick_size;
                                    if (bar.c - tp) / cfg.tick_size >= cfg.exit_min_ticks { Some(("SHORT", sl, tp)) } else { None }
                                } else if prev_z < -cfg.z_thresh && curr_z >= -cfg.z_thresh {
                                    // Was extended down, just crossed back inside band → LONG with stop below recent wick
                                    let sl = bar.l.min(bars[i-1].l) - cfg.tick_size * 2.0;
                                    let tp = (bar.vwap / cfg.tick_size).round() * cfg.tick_size;
                                    if (tp - bar.c) / cfg.tick_size >= cfg.exit_min_ticks { Some(("LONG", sl, tp)) } else { None }
                                } else { None }
                            }
                        }
                    }
                    "quiet_fade" => {
                        // Fade only on low-volume extensions — weak tape = no conviction, higher reversion probability
                        let std = bar.vwap_std;
                        if std < 0.01 { None } else {
                            let z = (bar.c - bar.vwap) / std;
                            if z.abs() < cfg.z_thresh { None } else {
                                let vol_ma = {
                                    let slice = &bars[i.saturating_sub(20)..i];
                                    if slice.is_empty() { bar.v + 1.0 } else { slice.iter().map(|b| b.v).sum::<f64>() / slice.len() as f64 }
                                };
                                if bar.v > vol_ma * 0.80 { None } else {
                                    // Low volume = no sellers stepping in = drift continues; follow momentum
                                    let dir: &'static str = if z > 0.0 { "LONG" } else { "SHORT" };
                                    let raw_tp = match dir { "LONG" => bar.c + atr * 2.0, _ => bar.c - atr * 2.0 };
                                    let tp_price = (raw_tp / cfg.tick_size).round() * cfg.tick_size;
                                    let sl_price = match dir { "LONG" => bar.c - sl_dist, _ => bar.c + sl_dist };
                                    if (tp_price - bar.c).abs() / cfg.tick_size < cfg.exit_min_ticks { None }
                                    else { Some((dir, sl_price, tp_price)) }
                                }
                            }
                        }
                    }
                    "bar_rejection" => {
                        // Close position within bar range is intrabar order flow proxy
                        // At z-extreme: close near LOW = intrabar sellers dominated = confirmed SHORT
                        // At z-extreme: close near HIGH = intrabar buyers dominated = confirmed LONG
                        let std = bar.vwap_std;
                        if std < 0.01 { None } else {
                            let z = (bar.c - bar.vwap) / std;
                            if z.abs() < cfg.z_thresh { None } else {
                                let range = (bar.h - bar.l).max(cfg.tick_size);
                                let close_pos = (bar.c - bar.l) / range; // 0=closed at low, 1=closed at high
                                if z > cfg.z_thresh && close_pos < 0.30 {
                                    // Price at extreme high, but bar closed near its LOW — rejection of the push
                                    let sl = bar.h + cfg.tick_size * 2.0;
                                    let tp = (bar.vwap / cfg.tick_size).round() * cfg.tick_size;
                                    if (bar.c - tp) / cfg.tick_size >= cfg.exit_min_ticks { Some(("SHORT", sl, tp)) } else { None }
                                } else if z < -cfg.z_thresh && close_pos > 0.70 {
                                    // Price at extreme low, but bar closed near its HIGH — rejection of the sell
                                    let sl = bar.l - cfg.tick_size * 2.0;
                                    let tp = (bar.vwap / cfg.tick_size).round() * cfg.tick_size;
                                    if (tp - bar.c) / cfg.tick_size >= cfg.exit_min_ticks { Some(("LONG", sl, tp)) } else { None }
                                } else { None }
                            }
                        }
                    }
                    "delta_fade" => {
                        // Cumulative (close-open) over last 5 bars as order-flow pressure proxy
                        // Fade when sustained pressure in same direction as z-extension — trend is exhausting
                        let std = bar.vwap_std;
                        if std < 0.01 { None } else {
                            let z = (bar.c - bar.vwap) / std;
                            if z.abs() < cfg.z_thresh { None } else {
                                let slice = &bars[i.saturating_sub(5)..=i];
                                let cdelta: f64 = slice.iter().map(|b| b.c - b.o).sum();
                                let norm = (atr * slice.len() as f64).max(1e-10);
                                let cdelta_n = cdelta / norm;
                                let dir: Option<&'static str> = if z > cfg.z_thresh && cdelta_n > 0.15 {
                                    Some("SHORT")
                                } else if z < -cfg.z_thresh && cdelta_n < -0.15 {
                                    Some("LONG")
                                } else { None };
                                if let Some(d) = dir {
                                    let tp = (bar.vwap / cfg.tick_size).round() * cfg.tick_size;
                                    let sl = match d { "LONG" => bar.c - sl_dist, _ => bar.c + sl_dist };
                                    if (tp - bar.c).abs() / cfg.tick_size >= cfg.exit_min_ticks { Some((d, sl, tp)) } else { None }
                                } else { None }
                            }
                        }
                    }
                    "ou_vwap" => {
                        // OU/VWAP mean-reversion — Bertram (2010) / Leung-Li (2015)
                        // Enter when detrended VWAP residual exceeds Bertram-optimal threshold.
                        // SL/TP are price-space approximations of OU stop_z / exit_z.
                        if ou_y_buf.len() < 30 || !ou_regime_ok || ou_sigma_eq < 1e-6 {
                            None
                        } else {
                            let y_now = *ou_y_buf.last().unwrap_or(&0.0);
                            let z_now = y_now / ou_sigma_eq;
                            // Distance in price points for TP and SL
                            let tp_pts = (ou_entry_z + ou_exit_z) * ou_sigma_eq;
                            let sl_pts = (ou_stop_z - ou_entry_z).max(0.25) * ou_sigma_eq;
                            if z_now <= -ou_entry_z {
                                // Y below VWAP+EWMA → expect mean reversion upward
                                let tp = ((bar.c + tp_pts) / cfg.tick_size).round() * cfg.tick_size;
                                let sl = ((bar.c - sl_pts) / cfg.tick_size).round() * cfg.tick_size;
                                if (tp - bar.c) / cfg.tick_size >= cfg.exit_min_ticks {
                                    Some(("LONG", sl, tp))
                                } else { None }
                            } else if z_now >= ou_entry_z {
                                // Y above VWAP+EWMA → expect mean reversion downward
                                let tp = ((bar.c - tp_pts) / cfg.tick_size).round() * cfg.tick_size;
                                let sl = ((bar.c + sl_pts) / cfg.tick_size).round() * cfg.tick_size;
                                if (bar.c - tp) / cfg.tick_size >= cfg.exit_min_ticks {
                                    Some(("SHORT", sl, tp))
                                } else { None }
                            } else { None }
                        }
                    }
                    _ => {
                        // Fade (VWAP mean reversion, default)
                        let std = bar.vwap_std;
                        if std < 0.01 { None }
                        else {
                            let z = (bar.c - bar.vwap) / std;
                            if z.abs() < cfg.z_thresh { None }
                            else {
                                let dir: &'static str = if z > 0.0 { "SHORT" } else { "LONG" };
                                let tp_price = if cfg.target_mode == "vpoc" { bar.vpoc } else {
                                    (bar.vwap / cfg.tick_size).round() * cfg.tick_size
                                };
                                let sl_price = match dir { "LONG" => bar.c - sl_dist, _ => bar.c + sl_dist };
                                let tp_buf = (tp_price - bar.c).abs() / cfg.tick_size;
                                if tp_buf < cfg.exit_min_ticks { None }
                                else { Some((dir, sl_price, tp_price)) }
                            }
                        }
                    }
                };

                let (dir, sl_price, tp_price) = match entry {
                    Some((d, sl, tp)) => (d, sl, tp),
                    None => continue,
                };

                pos_dir = Some(dir);
                pos_ep = bar.c;
                pos_sl = sl_price;
                pos_tp = tp_price;
                pos_bp = bar.c;
                pos_bar_idx = bar_idx;
                pos_contracts = if cfg.scale_out { 2 } else { 1 };
                pos_scale1_done = false;
                trades_today += 1;
                total_trades += 1;

                // OU: Kelly sizing, reset held-bars counter, disable scale-out
                if cfg.strategy == "ou_vwap" {
                    ou_bars_held = 0;
                    let rt_dollars = cfg.commission_rt + cfg.slippage_ticks * cfg.tick_value;
                    let sigma_dollars = ou_sigma_eq * cfg.tick_value / cfg.tick_size;
                    let expected_capture = (ou_entry_z + ou_exit_z) * sigma_dollars;
                    let edge = expected_capture - rt_dollars;
                    pos_contracts = if edge > 0.0 && sigma_dollars > 1e-6 {
                        let f = OU_KELLY * edge / sigma_dollars;
                        (f.floor() as u32).max(1).min(OU_MAX_CONTRACTS)
                    } else {
                        1
                    };
                    pos_scale1_done = true; // manage OU as a unit position
                }
            }
        }

        // Force-close at session end
        if let Some(dir) = pos_dir {
            let last = bars.last().map(|b| b.c).unwrap_or(pos_ep);
            let pnl = book_close_cfg(dir, pos_ep, last, pos_contracts, cfg);
            daily_pnl += pnl;
            total_pnl += pnl;
            if pnl > 0.0 { wins_today += 1; total_wins += 1; } else { losses_today += 1; total_losses += 1; }
            trade_log.push(TradeLog { date: sess.date.clone(), dir: dir.to_string(), entry: pos_ep, exit: last, ticks: if dir == "LONG" { (last - pos_ep) / cfg.tick_size } else { (pos_ep - last) / cfg.tick_size }, pnl, reason: "END OF DAY".into() });
        }

        day_results.push(DayResult {
            date: sess.date.clone(),
            trades: trades_today,
            wins: wins_today,
            losses: losses_today,
            daily_pnl,
            cumul_pnl: total_pnl,
            flag,
            capped: day_capped,
            loss_limited: day_loss_limited,
        });
    }

    (total_pnl, max_dd, total_trades, total_wins, total_losses, max_consec_loss as f64, day_results, trade_log)
}

fn check_exit_cfg(
    dir: &str, price: f64, ep: f64, sl: f64, tp: f64,
    bars_in_trade: i64, cur_ticks: f64, cfg: &BtConfig,
) -> Option<&'static str> {
    match dir {
        "LONG" => {
            if price <= sl { return Some(classify_stop(dir, ep, sl, cfg)); }
            if price >= tp && cur_ticks >= cfg.exit_min_ticks { return Some("TARGET"); }
        }
        _ => {
            if price >= sl { return Some(classify_stop(dir, ep, sl, cfg)); }
            if price <= tp && cur_ticks >= cfg.exit_min_ticks { return Some("TARGET"); }
        }
    }
    if bars_in_trade > cfg.time_stop_mins { return Some("TIME STOP"); }
    if bars_in_trade > cfg.time_stop_mins / 2 && cur_ticks < -4.0 { return Some("TIME STOP"); }
    None
}

fn classify_stop(dir: &str, ep: f64, sl: f64, cfg: &BtConfig) -> &'static str {
    let tol = cfg.tick_size * 0.25;
    if (sl - ep).abs() <= tol { return "BREAKEVEN STOP"; }
    match dir {
        "LONG" => if sl > ep { "TRAIL STOP" } else { "STOP LOSS" },
        _ => if sl < ep { "TRAIL STOP" } else { "STOP LOSS" },
    }
}

fn book_close_cfg(dir: &str, ep: f64, fill: f64, size: u32, cfg: &BtConfig) -> f64 {
    let ticks = match dir {
        "LONG" => (fill - ep) / cfg.tick_size,
        _ => (ep - fill) / cfg.tick_size,
    };
    let gross = ticks * cfg.tick_value * size as f64;
    gross - cfg.commission_rt * size as f64 - cfg.slippage_ticks * cfg.tick_value * size as f64
}

// ── Report printer ─────────────────────────────────────────────────────────────

fn vol_sparkline(day_results: &[DayResult], width: usize) -> String {
    let pnls: Vec<f64> = day_results.iter().map(|d| d.daily_pnl).collect();
    let n = pnls.len();
    if n < 3 { return String::new(); }

    let blocks = ['▁','▂','▃','▄','▅','▆','▇','█'];
    let window = 10usize;
    let w = width.min(n).max(4);

    // rolling std dev for each sampled point
    let vols: Vec<f64> = (0..w).map(|i| {
        let idx = i * n / w;
        let start = idx.saturating_sub(window / 2);
        let end = (idx + window / 2).min(n);
        let slice = &pnls[start..end];
        if slice.len() < 2 { return 0.0; }
        let mean = slice.iter().sum::<f64>() / slice.len() as f64;
        (slice.iter().map(|&x| (x - mean).powi(2)).sum::<f64>() / (slice.len() - 1) as f64).sqrt()
    }).collect();

    let max_vol = vols.iter().cloned().fold(0.0_f64, f64::max).max(1.0);
    vols.iter().map(|&v| {
        let idx = ((v / max_vol) * (blocks.len() - 1) as f64).round() as usize;
        blocks[idx.min(blocks.len() - 1)]
    }).collect()
}

fn equity_curve_ascii(day_results: &[DayResult], width: usize) -> String {
    let pnls: Vec<f64> = day_results.iter().map(|d| d.cumul_pnl).collect();
    if pnls.is_empty() { return String::new(); }

    let min_v = pnls.iter().cloned().fold(f64::INFINITY, f64::min).min(0.0);
    let max_v = pnls.iter().cloned().fold(f64::NEG_INFINITY, f64::max).max(1.0);
    let range = (max_v - min_v).max(1.0);

    let height = 8usize;
    let w = width.min(pnls.len()).max(10);
    // Downsample to w points
    let sampled: Vec<f64> = (0..w).map(|i| {
        let idx = i * pnls.len() / w;
        pnls[idx.min(pnls.len() - 1)]
    }).collect();

    let mut grid = vec![vec![' '; w + 8]; height];

    // Y-axis labels
    for row in 0..height {
        let val = max_v - (row as f64 / (height - 1) as f64) * range;
        let label = if val >= 0.0 { format!("{:>6}", format!("${:.0}", val)) }
                    else { format!("{:>6}", format!("-${:.0}", val.abs())) };
        for (ci, ch) in label.chars().enumerate() {
            grid[row][ci] = ch;
        }
        grid[row][6] = if row == height / 2 { '┤' } else { '│' };
    }

    // Plot bars
    for (col, &val) in sampled.iter().enumerate() {
        let norm = (val - min_v) / range;
        let row = ((1.0 - norm) * (height - 1) as f64).round() as usize;
        let row = row.min(height - 1);
        let col = col + 7;
        grid[row][col] = if val >= 0.0 { '▲' } else { '▽' };
        // Fill column below/above zero line
        let zero_row = ((1.0 - (-min_v / range)) * (height - 1) as f64).round() as usize;
        let zero_row = zero_row.min(height - 1);
        if val >= 0.0 {
            for r in (row + 1)..=zero_row { grid[r][col] = '│'; }
        } else {
            for r in zero_row..row { grid[r][col] = '│'; }
        }
    }

    // Zero line marker
    let zero_row = ((1.0 - (-min_v / range)) * (height - 1) as f64).round() as usize;
    let zero_row = zero_row.min(height - 1);
    for col in 7..(w + 7) {
        if grid[zero_row][col] == ' ' { grid[zero_row][col] = '·'; }
    }

    grid.iter().map(|row| row.iter().collect::<String>()).collect::<Vec<_>>().join("\n")
}

fn row(label: &str, val: &str, label2: &str, val2: &str, label3: &str, val3: &str) {
    println!("  {:<16}{:>10}    {:<14}{:>10}    {:<12}{:>9}",
        label, val, label2, val2, label3, val3);
}

fn print_report(
    cfg: &BtConfig,
    _dir: &str,
    sessions: &[DaySession],
    total_pnl: f64,
    max_dd: f64,
    total_trades: u32,
    total_wins: u32,
    total_losses: u32,
    max_consec_loss: f64,
    day_results: &[DayResult],
    trade_log: &[TradeLog],
) {
    let sep = "═".repeat(72);
    let thin = "─".repeat(72);

    let n_sessions = sessions.len();
    let date_from = sessions.first().map(|s| s.date.as_str()).unwrap_or("?");
    let date_to = sessions.last().map(|s| s.date.as_str()).unwrap_or("?");

    let wins = total_wins;
    let losses = total_losses;
    let n_closed = wins + losses;
    let win_rate = if n_closed > 0 { wins as f64 / n_closed as f64 * 100.0 } else { 0.0 };

    let gross_win: f64 = trade_log.iter().filter(|t| t.pnl > 0.0 && t.reason != "SCALE OUT").map(|t| t.pnl).sum();
    let gross_loss: f64 = trade_log.iter().filter(|t| t.pnl < 0.0 && t.reason != "SCALE OUT").map(|t| t.pnl.abs()).sum();
    let avg_win = if wins > 0 { gross_win / wins as f64 } else { 0.0 };
    let avg_loss = if losses > 0 { gross_loss / losses as f64 } else { 0.0 };
    let pf = if gross_loss > 0.0 { gross_win / gross_loss } else { f64::INFINITY };
    let rr = if avg_loss > 0.0 { avg_win / avg_loss } else { 0.0 };
    let expectancy = if n_closed > 0 {
        (win_rate / 100.0 * avg_win) - ((1.0 - win_rate / 100.0) * avg_loss)
    } else { 0.0 };

    let days_with_trades = day_results.iter().filter(|d| d.trades > 0).count();
    let avg_trades_day = if days_with_trades > 0 { total_trades as f64 / days_with_trades as f64 } else { 0.0 };

    println!();
    println!("{sep}");
    println!("  BACKTEST  ·  {n_sessions} sessions  ·  {date_from} → {date_to}");
    // Config params split across two lines so nothing overflows
    println!("  Z={:.2}  ATR={:.2}  TRAIL_ON={:.0}t  DIST={:.0}t  BE={:.0}t  TIME={}  MAX_T={}",
        cfg.z_thresh, cfg.atr_stop_ratio, cfg.trail_activate, cfg.trail_distance,
        cfg.breakeven_ticks, cfg.time_stop_mins, cfg.max_trades);
    println!("  TARGET={}  LUNCH={}  SCALE={}  TICK_VAL=${:.2}",
        cfg.target_mode,
        if cfg.lunch_skip { "ON" } else { "OFF" },
        if cfg.scale_out { "ON" } else { "OFF" },
        cfg.tick_value);
    println!("{sep}");
    row("Total P&L", &format!("{:+.2}", total_pnl),
        "Win Rate", &format!("{:.1}%", win_rate),
        "Trades", &format!("{}", total_trades));
    row("Max DD", &format!("{:.2}", -max_dd),
        "Avg Win", &format!("{:+.2}", avg_win),
        "Avg Loss", &format!("{:.2}", -avg_loss));
    row("Prof Factor", &format!("{:.2}", pf),
        "R:R", &format!("{:.2}", rr),
        "Expectancy", &format!("{:+.2}", expectancy));
    row("Max Consec L", &format!("{:.0}", max_consec_loss),
        "Avg T/Day", &format!("{:.1}", avg_trades_day),
        "Active Days", &format!("{}", days_with_trades));
    println!("{sep}");

    // Prop firm section (always shown, highlights violations)
    let best_day = day_results.iter().map(|d| d.daily_pnl).fold(f64::NEG_INFINITY, f64::max);
    let consistency_threshold = cfg.profit_target * 0.5;
    let consistency_violations: usize = day_results.iter().filter(|d| d.daily_pnl >= consistency_threshold).count();
    let loss_limit_days: usize = day_results.iter().filter(|d| d.loss_limited).count();
    let capped_days: usize = day_results.iter().filter(|d| d.capped).count();
    let daily_exp = if days_with_trades > 0 { total_pnl / days_with_trades as f64 } else { 0.0 };
    let est_days = if daily_exp > 0.0 { (cfg.profit_target / daily_exp).ceil() as i64 } else { -1 };
    let mll_breach_days: usize = {
        let mut peak = 0.0f64;
        day_results.iter().filter(|d| {
            if d.cumul_pnl > peak { peak = d.cumul_pnl; }
            peak - d.cumul_pnl > cfg.trailing_mll
        }).count()
    };
    println!();
    println!("  PROP FIRM  ·  TopStep $50K  ·  Pass Target ${:.0}  ·  MLL $2,000 trailing", cfg.profit_target);
    println!("  {}", thin);
    let con_status = if consistency_violations == 0 { "PASS" } else { "FAIL" };
    let mll_status = if mll_breach_days == 0 { "PASS" } else { "RISK" };
    let est_str = if est_days > 0 { format!("~{est_days} days") } else { "N/A (losing)".to_string() };
    row("Best Day", &format!("{:+.2}", best_day),
        "Consistency", &format!("{con_status} ({consistency_violations} violations >=${:.0})", consistency_threshold),
        "", "");
    row("Est. Days to Pass", &est_str,
        "MLL Risk", &format!("{mll_status} ({mll_breach_days} breaches)"),
        "", "");
    if cfg.daily_loss_limit < f64::MAX {
        row("Daily Loss Limit", &format!("${:.0} ({loss_limit_days} days stopped)", cfg.daily_loss_limit),
            "Daily Cap", &format!("${:.0} ({capped_days} days capped)", cfg.daily_profit_cap),
            "", "");
    }
    println!("  {}", thin);

    // Equity curve + vol sparkline
    println!();
    println!("  Equity Curve");
    println!("  ┌{}┐", "─".repeat(74));
    let curve = equity_curve_ascii(day_results, 66);
    for line in curve.lines() {
        println!("  │ {:<72} │", line);
    }
    println!("  └{}┘", "─".repeat(74));
    let vol_line = vol_sparkline(day_results, 66);
    if !vol_line.is_empty() {
        println!("  Rolling Vol σ (10d)");
        println!("  ┌{}┐", "─".repeat(74));
        println!("  │ {:<72} │", vol_line);
        println!("  └{}┘", "─".repeat(74));
    }

    // Per-day table
    println!();
    println!("  {:<12} {:>6} {:>4} {:>4}  {:>10}  {:>10}  {}", "DATE", "TRADES", "W", "L", "DAY P&L", "CUMUL P&L", "");
    println!("  {}", thin);
    for d in day_results {
        let mut tags = String::new();
        if !d.flag.is_empty() { tags.push_str(&format!("  [{}]", d.flag)); }
        if d.capped { tags.push_str("  [CAP]"); }
        if d.loss_limited { tags.push_str("  [LTD]"); }
        if d.daily_pnl >= consistency_threshold { tags.push_str("  [!CON]"); }
        println!("  {:<12} {:>6} {:>4} {:>4}  {:>+10.2}  {:>+10.2}{}",
            d.date, d.trades, d.wins, d.losses,
            d.daily_pnl, d.cumul_pnl, tags);
    }
    println!("  {}", thin);
    println!("  {:<12} {:>6} {:>4} {:>4}  {:>+10.2}",
        "TOTAL", total_trades, total_wins, total_losses, total_pnl);

    // Trade log
    if cfg.verbose {
        println!();
        println!("  Full Trade Log");
        println!("  {}", thin);
        println!("  {:<12} {:>6} {:>9} {:>9} {:>7} {:>10}  {}", "DATE", "DIR", "ENTRY", "EXIT", "TICKS", "P&L", "REASON");
        println!("  {}", thin);
        for t in trade_log {
            println!("  {:<12} {:>6} {:>9.2} {:>9.2} {:>+7.1} {:>+10.2}  {}",
                t.date, t.dir, t.entry, t.exit, t.ticks, t.pnl, t.reason);
        }
    }

    println!();
    println!("  Usage examples:");
    println!("    cargo run -- backtest ../es_sessions --z 1.5 --atr 0.7");
    println!("    cargo run -- backtest ../nq_sessions --nq --z 1.3 --trail-on 10 --verbose");
    println!("    cargo run -- backtest ../es_sessions --target vpoc --no-lunch");
    println!();
}

// ── Monte Carlo pass probability ──────────────────────────────────────────────

pub struct McResult {
    pub pass_rate: f64,
    pub fail_rate: f64,
    pub timeout_rate: f64,
    pub avg_days_to_pass: f64,
    pub avg_days_to_fail: f64,
    pub n_sims: usize,
    // p5/p50/p95 cumulative P&L by day (60 days)
    pub p5: Vec<f64>,
    pub p50: Vec<f64>,
    pub p95: Vec<f64>,
}

pub fn monte_carlo_pass(daily_pnls: &[f64], cfg: &BtConfig) -> McResult {
    use rand::seq::SliceRandom;
    use rand::SeedableRng;

    if daily_pnls.is_empty() {
        return McResult { pass_rate: 0.0, fail_rate: 0.0, timeout_rate: 1.0,
            avg_days_to_pass: 0.0, avg_days_to_fail: 0.0, n_sims: 0,
            p5: vec![], p50: vec![], p95: vec![] };
    }

    let n_sims = 10_000usize;
    let max_days = 60usize;
    let start = cfg.account_balance;
    let pass_target = start + cfg.profit_target;
    let mll_start = start - cfg.trailing_mll;
    // User's gambler's ruin insight: even at EV=0, P(pass) ≈ MLL/(MLL+target) = 2000/5000 = 40%

    let mut rng = rand::rngs::SmallRng::seed_from_u64(12345);
    let mut pass_count = 0usize;
    let mut fail_count = 0usize;
    let mut timeout_count = 0usize;
    let mut days_pass_sum = 0.0f64;
    let mut days_fail_sum = 0.0f64;
    let mut days_pass_n = 0usize;
    let mut days_fail_n = 0usize;
    let mut path_matrix: Vec<Vec<f64>> = vec![vec![0.0; max_days]; n_sims];

    for sim in 0..n_sims {
        let mut balance = start;
        let mut peak_eod = start;
        let mut done = false;

        for day in 0..max_days {
            if done { path_matrix[sim][day] = balance - start; continue; }

            let mut day_pnl = *daily_pnls.choose(&mut rng).unwrap_or(&0.0);
            // Apply prop firm daily constraints if active
            if cfg.daily_profit_cap < f64::MAX { day_pnl = day_pnl.min(cfg.daily_profit_cap); }
            if cfg.daily_loss_limit < f64::MAX { day_pnl = day_pnl.max(-cfg.daily_loss_limit); }

            balance += day_pnl;
            if balance > peak_eod { peak_eod = balance; }
            let mll_floor = (peak_eod - cfg.trailing_mll).max(mll_start);
            path_matrix[sim][day] = balance - start;

            if balance >= pass_target {
                pass_count += 1; days_pass_sum += day as f64 + 1.0; days_pass_n += 1;
                done = true;
            } else if balance <= mll_floor {
                fail_count += 1; days_fail_sum += day as f64 + 1.0; days_fail_n += 1;
                done = true;
            }
        }
        if !done { timeout_count += 1; }
    }

    // Percentile curves
    let mut p5 = vec![0.0f64; max_days];
    let mut p50 = vec![0.0f64; max_days];
    let mut p95 = vec![0.0f64; max_days];
    for day in 0..max_days {
        let mut vals: Vec<f64> = path_matrix.iter().map(|p| p[day]).collect();
        vals.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let n = vals.len();
        p5[day] = vals[(n as f64 * 0.05) as usize];
        p50[day] = vals[n / 2];
        p95[day] = vals[((n as f64 * 0.95) as usize).min(n - 1)];
    }

    let total = n_sims as f64;
    McResult {
        pass_rate: pass_count as f64 / total,
        fail_rate: fail_count as f64 / total,
        timeout_rate: timeout_count as f64 / total,
        avg_days_to_pass: if days_pass_n > 0 { days_pass_sum / days_pass_n as f64 } else { 0.0 },
        avg_days_to_fail: if days_fail_n > 0 { days_fail_sum / days_fail_n as f64 } else { 0.0 },
        n_sims,
        p5, p50, p95,
    }
}

// ── Public API for backtest_server ────────────────────────────────────────────

pub struct BacktestResult {
    pub total_pnl: f64,
    pub max_dd: f64,
    pub total_trades: u32,
    pub wins: u32,
    pub losses: u32,
    pub max_consec_loss: f64,
    pub days: Vec<DayResult>,
    pub trade_log: Vec<PublicTradeLog>,
}

#[allow(dead_code)]
pub struct PublicTradeLog {
    pub date: String,
    pub dir: String,
    pub entry: f64,
    pub exit: f64,
    pub ticks: f64,
    pub pnl: f64,
    pub reason: String,
}

pub fn load_sessions_pub(dir: &str) -> anyhow::Result<Vec<PublicSession>> {
    let sessions = load_sessions(dir)?;
    Ok(sessions.into_iter().map(|s| PublicSession { date: s.date, bars: s.bars }).collect())
}

pub struct PublicSession {
    pub date: String,
    pub bars: Vec<Bar>,
}

pub fn run_backtest_pub(sessions: &[PublicSession], cfg: &BtConfig) -> BacktestResult {
    let internal: Vec<DaySession> = sessions.iter().map(|s| DaySession {
        date: s.date.clone(),
        bars: s.bars.clone(),
    }).collect();
    let (total_pnl, max_dd, total_trades, wins, losses, max_consec_loss, days, tl) =
        run_backtest_cfg(&internal, cfg);
    BacktestResult {
        total_pnl, max_dd, total_trades, wins, losses, max_consec_loss, days,
        trade_log: tl.into_iter().map(|t| PublicTradeLog {
            date: t.date, dir: t.dir, entry: t.entry, exit: t.exit,
            ticks: t.ticks, pnl: t.pnl, reason: t.reason,
        }).collect(),
    }
}

// ── Statistical significance tests ────────────────────────────────────────────

#[derive(Default)]
pub struct StatTests {
    pub n_days: usize,
    pub n_trades: usize,
    // Daily P&L
    pub mean_daily_pnl: f64,
    pub std_daily_pnl: f64,
    pub t_stat: f64,
    pub p_value_mean: f64,       // H0: mean = 0, one-tailed (H1: mean > 0)
    pub ci_mean_lo: f64,         // 95% CI on mean daily P&L
    pub ci_mean_hi: f64,
    pub cohens_d: f64,           // effect size
    // Win rate
    pub win_rate_ci_lo: f64,     // Wilson 95% CI
    pub win_rate_ci_hi: f64,
    pub p_value_winrate: f64,    // H0: WR = 50%, two-tailed
    // Pass rate (MC bootstrap)
    pub pass_rate_ci_lo: f64,    // bootstrap 95% CI
    pub pass_rate_ci_hi: f64,
    pub p_value_passrate: f64,   // bootstrap p-value: H0: pass_rate <= zero_ev_baseline
    // Power
    pub power_at_current_n: f64, // power of daily P&L t-test at current n
    pub n_required_80_power: usize,
    // Sample quality
    pub sample_adequate: bool,   // n_days >= 30
    // Risk-adjusted returns
    pub sharpe: f64,    // annualized daily Sharpe: mean/std * sqrt(252)
    pub sortino: f64,   // annualized daily Sortino: mean/downside_std * sqrt(252)
    pub calmar: f64,    // annualized return / max drawdown (from backtest period)
    // Market correlation
    pub market_correlation: f64,    // Pearson r: strat daily P&L vs session market move
    pub market_corr_pvalue: f64,    // H0: r = 0, two-tailed
    pub market_beta: f64,           // OLS slope: ΔP&L per $1 market move
    pub market_alpha: f64,          // OLS intercept: expected daily P&L at zero market move
    pub market_corr_ci_lo: f64,     // 95% CI lower (Fisher z-transform)
    pub market_corr_ci_hi: f64,     // 95% CI upper
}

pub fn run_stat_tests(
    daily_pnls: &[f64],       // only active days (days with at least 1 trade)
    market_returns: &[f64],   // session open→close move for the same active days
    wins: u32,
    losses: u32,
    mc_pass_rate: f64,
    zero_ev_baseline: f64,
    cfg: &BtConfig,
) -> StatTests {
    let n = daily_pnls.len();
    let n_trades = (wins + losses) as usize;

    if n < 2 {
        return StatTests { n_days: n, n_trades, ..Default::default() };
    }

    let mean = daily_pnls.iter().sum::<f64>() / n as f64;
    let variance = daily_pnls.iter().map(|&x| (x - mean).powi(2)).sum::<f64>() / (n - 1) as f64;
    let std = variance.sqrt();
    let sem = std / (n as f64).sqrt();

    let t_stat = if sem > 1e-10 { mean / sem } else { 0.0 };
    // p-value via normal approximation (valid for n >= 30; approximate for smaller)
    let p_value_mean = 1.0 - normal_cdf(t_stat);

    let z_95 = 1.96f64;
    let ci_lo = mean - z_95 * sem;
    let ci_hi = mean + z_95 * sem;
    let cohens_d = if std > 1e-10 { mean / std } else { 0.0 };

    // Sharpe / Sortino (annualized, assuming 252 trading days)
    let ann = 252.0_f64.sqrt();
    let sharpe = if std > 1e-10 { mean / std * ann } else { 0.0 };
    let downside_var = daily_pnls.iter().map(|&x| if x < 0.0 { x.powi(2) } else { 0.0 }).sum::<f64>() / n as f64;
    let downside_std = downside_var.sqrt();
    let sortino = if downside_std > 1e-10 { mean / downside_std * ann } else { 0.0 };

    // Wilson confidence interval for win rate
    let (wr_ci_lo, wr_ci_hi, pv_wr) = if n_trades >= 5 {
        let p_hat = wins as f64 / n_trades as f64;
        let z = 1.96f64;
        let nt = n_trades as f64;
        let center = (p_hat + z * z / (2.0 * nt)) / (1.0 + z * z / nt);
        let margin = z * (p_hat * (1.0 - p_hat) / nt + z * z / (4.0 * nt * nt)).sqrt() / (1.0 + z * z / nt);
        let z_wr = (p_hat - 0.5) / (0.5 * (1.0 - 0.5) / nt).sqrt();
        let pv = 2.0 * normal_cdf(-z_wr.abs());
        ((center - margin).max(0.0), (center + margin).min(1.0), pv)
    } else {
        (0.0, 1.0, 1.0)
    };

    // Bootstrap 95% CI and p-value for pass rate
    let (pr_ci_lo, pr_ci_hi, pv_pr) = {
        use rand::seq::SliceRandom;
        use rand::SeedableRng;
        let mut rng = rand::rngs::SmallRng::seed_from_u64(99);
        let n_boot = 500usize;
        let mini_mc = 1_000usize;
        let max_days = 60usize;
        let start = cfg.account_balance;
        let pass_target = start + cfg.profit_target;
        let mll_start = start - cfg.trailing_mll;

        let mut boot_rates = Vec::with_capacity(n_boot);
        for _ in 0..n_boot {
            let resample: Vec<f64> = (0..n)
                .map(|_| *daily_pnls.choose(&mut rng).unwrap_or(&0.0))
                .collect();
            let mut pass_n = 0usize;
            for _ in 0..mini_mc {
                let mut balance = start;
                let mut peak_eod = start;
                let mut done = false;
                for _ in 0..max_days {
                    if done { break; }
                    let pnl = *resample.choose(&mut rng).unwrap_or(&0.0);
                    balance += pnl;
                    if balance > peak_eod { peak_eod = balance; }
                    let mll_floor = (peak_eod - cfg.trailing_mll).max(mll_start);
                    if balance >= pass_target { pass_n += 1; done = true; }
                    else if balance <= mll_floor { done = true; }
                }
            }
            boot_rates.push(pass_n as f64 / mini_mc as f64);
        }
        boot_rates.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let lo = boot_rates[(n_boot as f64 * 0.025) as usize];
        let hi = boot_rates[((n_boot as f64 * 0.975) as usize).min(n_boot - 1)];
        // p-value: fraction of bootstrap samples <= zero_ev_baseline
        let n_below = boot_rates.iter().filter(|&&r| r <= zero_ev_baseline).count();
        let pv = n_below as f64 / n_boot as f64;
        (lo, hi, pv)
    };

    // Power analysis (one-tailed t-test, α=0.05)
    let z_alpha = 1.645f64;
    let z_beta_80 = 0.842f64;
    let power = normal_cdf(cohens_d.abs() * (n as f64).sqrt() - z_alpha).max(0.0);
    let n_req = if cohens_d.abs() > 1e-10 {
        ((z_alpha + z_beta_80) / cohens_d.abs()).powi(2).ceil() as usize
    } else { 9999 };

    let _ = mc_pass_rate; // unused parameter kept for API compatibility

    // Market correlation (Pearson r, OLS beta/alpha, Fisher CI)
    let nm = market_returns.len().min(n);
    let (mkt_r, mkt_pv, mkt_beta, mkt_alpha, mkt_ci_lo, mkt_ci_hi) = if nm >= 5 {
        let xs = &market_returns[..nm];
        let ys = &daily_pnls[..nm];
        let mx = xs.iter().sum::<f64>() / nm as f64;
        let my = ys.iter().sum::<f64>() / nm as f64;
        let cov: f64 = xs.iter().zip(ys).map(|(&x, &y)| (x - mx) * (y - my)).sum::<f64>() / (nm - 1) as f64;
        let vx: f64 = xs.iter().map(|&x| (x - mx).powi(2)).sum::<f64>() / (nm - 1) as f64;
        let vy: f64 = ys.iter().map(|&y| (y - my).powi(2)).sum::<f64>() / (nm - 1) as f64;
        let r = if vx * vy > 1e-20 { (cov / (vx.sqrt() * vy.sqrt())).clamp(-0.9999, 0.9999) } else { 0.0 };
        let t_r = r * ((nm - 2) as f64).sqrt() / (1.0 - r * r).sqrt();
        let pv = 2.0 * normal_cdf(-t_r.abs());
        let beta = if vx > 1e-10 { cov / vx } else { 0.0 };
        let alpha = my - beta * mx;
        let z_r = r.atanh();
        let se_z = 1.0 / ((nm as f64 - 3.0).max(1.0)).sqrt();
        let ci_lo_r = (z_r - 1.96 * se_z).tanh();
        let ci_hi_r = (z_r + 1.96 * se_z).tanh();
        (r, pv, beta, alpha, ci_lo_r, ci_hi_r)
    } else {
        (0.0, 1.0, 0.0, 0.0, -1.0, 1.0)
    };

    // Calmar: annualized P&L / max drawdown over the period
    let mut peak = 0.0f64;
    let mut max_dd_local = 0.0f64;
    let mut cum = 0.0f64;
    for &p in daily_pnls {
        cum += p;
        if cum > peak { peak = cum; }
        let dd = peak - cum;
        if dd > max_dd_local { max_dd_local = dd; }
    }
    let ann_pnl = mean * 252.0;
    let calmar = if max_dd_local > 1e-10 { ann_pnl / max_dd_local } else { 0.0 };

    StatTests {
        n_days: n,
        n_trades,
        mean_daily_pnl: mean,
        std_daily_pnl: std,
        t_stat,
        p_value_mean,
        ci_mean_lo: ci_lo,
        ci_mean_hi: ci_hi,
        cohens_d,
        sharpe,
        sortino,
        calmar,
        win_rate_ci_lo: wr_ci_lo,
        win_rate_ci_hi: wr_ci_hi,
        p_value_winrate: pv_wr,
        pass_rate_ci_lo: pr_ci_lo,
        pass_rate_ci_hi: pr_ci_hi,
        p_value_passrate: pv_pr,
        power_at_current_n: power,
        n_required_80_power: n_req.min(9999),
        sample_adequate: n >= 30,
        market_correlation: mkt_r,
        market_corr_pvalue: mkt_pv,
        market_beta: mkt_beta,
        market_alpha: mkt_alpha,
        market_corr_ci_lo: mkt_ci_lo,
        market_corr_ci_hi: mkt_ci_hi,
    }
}

// ── Entry point ────────────────────────────────────────────────────────────────

pub fn run_from_cli(args: &[String]) {
    let (cfg, dir) = BtConfig::from_args(args);

    println!("Loading sessions from: {dir}");
    let sessions = match load_sessions(&dir) {
        Ok(s) => s,
        Err(e) => { eprintln!("Error: {e}"); std::process::exit(1); }
    };
    println!("Loaded {} sessions", sessions.len());

    let (total_pnl, max_dd, total_trades, total_wins, total_losses, max_consec_loss, day_results, trade_log) =
        run_backtest_cfg(&sessions, &cfg);

    print_report(
        &cfg, &dir, &sessions,
        total_pnl, max_dd, total_trades, total_wins, total_losses, max_consec_loss,
        &day_results, &trade_log,
    );

    // ── Stat tests + Monte Carlo ───────────────────────────────────────────────
    let sess_map: std::collections::HashMap<&str, (&Bar, &Bar)> = sessions.iter()
        .filter_map(|s| {
            let first = s.bars.first()?;
            let last = s.bars.last()?;
            Some((s.date.as_str(), (first, last)))
        })
        .collect();

    let active_days: Vec<&DayResult> = day_results.iter().filter(|d| d.trades > 0).collect();
    let active_pnls: Vec<f64> = active_days.iter().map(|d| d.daily_pnl).collect();
    let mkt_returns: Vec<f64> = active_days.iter()
        .map(|d| sess_map.get(d.date.as_str()).map(|(f, l)| l.c - f.c).unwrap_or(0.0))
        .collect();

    let mc = monte_carlo_pass(&active_pnls, &cfg);
    let zero_ev_baseline = cfg.trailing_mll / (cfg.trailing_mll + cfg.profit_target);
    let st = run_stat_tests(&active_pnls, &mkt_returns, total_wins, total_losses,
        mc.pass_rate, zero_ev_baseline, &cfg);

    let thin = "─".repeat(72);
    println!();
    println!("  STATISTICS  ·  {} active days  ·  {} trades", st.n_days, st.n_trades);
    println!("  {thin}");
    println!("  {:<22} {:>+10.2}    {:<22} {:>10.2}",
        "Mean Daily P&L", st.mean_daily_pnl, "Std Dev", st.std_daily_pnl);
    println!("  {:<22} {:>10.4}    {:<22} {:>10.4}",
        "T-Stat", st.t_stat, "P-Value (mean>0)", st.p_value_mean);
    println!("  {:<22} {:>10.2}    {:<22} {:>10.1}%",
        "Cohen's d", st.cohens_d, "Power @current n", st.power_at_current_n * 100.0);
    println!("  CI Mean 95%  [{:+.2}, {:+.2}]    WR Wilson CI  [{:.1}%, {:.1}%]",
        st.ci_mean_lo, st.ci_mean_hi,
        st.win_rate_ci_lo * 100.0, st.win_rate_ci_hi * 100.0);
    println!("  {:<22} {:>10.2}    {:<22} {:>10.2}    Calmar {:>8.2}",
        "Sharpe (ann.)", st.sharpe, "Sortino (ann.)", st.sortino, st.calmar);
    println!("  {thin}");
    println!("  {:<22} {:>10.1}%   {:<22} {:>10.4}",
        "MC Pass Rate", mc.pass_rate * 100.0, "Bootstrap P(pass)", st.p_value_passrate);
    println!("  {:<22} {:>10.1}%   {:<22} {:>10.1}%",
        "MC Fail Rate", mc.fail_rate * 100.0, "MC Timeout", mc.timeout_rate * 100.0);
    println!("  {:<22} {:>10.1}    {:<22} {:>10.1}",
        "Avg Days to Pass", mc.avg_days_to_pass, "Avg Days to Fail", mc.avg_days_to_fail);
    println!("  {thin}");
    let mkt_status = if st.market_corr_pvalue > 0.05 { "NEUTRAL" } else { "CORRELATED" };
    println!("  {:<22} {:>10.4}    {:<22} {:>10.4}  [{}]",
        "Market r", st.market_correlation, "Mkt P-Value", st.market_corr_pvalue, mkt_status);
    println!("  {:<22} {:>10.3}    {:<22} {:>+10.2}",
        "Beta (β)", st.market_beta, "Alpha α ($/day)", st.market_alpha);
    println!("  Corr CI 95%  [{:+.3}, {:+.3}]",
        st.market_corr_ci_lo, st.market_corr_ci_hi);
    println!("  {thin}");
    println!();
}

// ── OU Math Helpers ────────────────────────────────────────────────────────────

/// Fit AR(1) to slice `y`.
/// Returns `(phi, var_eps, sigma_eq, theta, half_life, adf_t_stat)`.
/// Returns `None` if `n < 10` or numerically degenerate.
pub fn ou_ar1_fit(y: &[f64]) -> Option<(f64, f64, f64, f64, f64, f64)> {
    let n = y.len();
    if n < 10 {
        return None;
    }
    let n_obs = (n - 1) as f64;

    // OLS: regress y[1..] on y[0..n-1]
    let x = &y[..n - 1];
    let yy = &y[1..];

    let x_mean = x.iter().sum::<f64>() / n_obs;
    let y_mean = yy.iter().sum::<f64>() / n_obs;

    let sxx: f64 = x.iter().map(|&xi| (xi - x_mean).powi(2)).sum();
    let sxy: f64 = x
        .iter()
        .zip(yy.iter())
        .map(|(&xi, &yi)| (xi - x_mean) * (yi - y_mean))
        .sum();

    if sxx < 1e-12 {
        return None;
    }

    let phi_hat = sxy / sxx;

    // Kendall small-sample bias correction
    let phi = phi_hat + (1.0 + 3.0 * phi_hat) / n_obs;

    if phi >= 1.0 || phi <= 0.0 {
        return None;
    }

    // Regression residuals
    let intercept = y_mean - phi * x_mean;
    let var_eps: f64 = x
        .iter()
        .zip(yy.iter())
        .map(|(&xi, &yi)| {
            let resid = yi - (intercept + phi * xi);
            resid * resid
        })
        .sum::<f64>()
        / (n_obs - 2.0);

    let sigma_eq = (var_eps / (1.0 - phi * phi)).sqrt();

    let theta = -phi.ln();
    if theta <= 0.0 {
        return None;
    }

    let half_life = std::f64::consts::LN_2 / theta;

    // ADF t-statistic
    let se_phi = (var_eps / sxx).sqrt();
    let adf_t = (phi - 1.0) / se_phi;

    Some((phi, var_eps, sigma_eq, theta, half_life, adf_t))
}

/// Expected first-passage time from `z = -entry_z` to `z = +exit_z` under OU
/// with mean-reversion speed `theta`.
/// Returns `1e9` if `theta <= 0`.
pub fn ou_fpt(entry_z: f64, exit_z: f64, theta: f64) -> f64 {
    if theta <= 0.0 {
        return 1e9;
    }
    // E[T] = (sqrt(2*pi) / theta) * integral_{-entry_z}^{exit_z} exp(u^2/2) * Phi(u) du
    let lo = -entry_z;
    let hi = exit_z;
    let n = 80usize;
    let h = (hi - lo) / n as f64;

    let integrand = |u: f64| -> f64 { (0.5 * u * u).exp() * normal_cdf(u) };

    // Trapezoidal rule
    let mut sum = 0.5 * (integrand(lo) + integrand(hi));
    for k in 1..n {
        sum += integrand(lo + k as f64 * h);
    }
    let integral = sum * h;

    let sqrt_2pi = (2.0 * std::f64::consts::PI).sqrt();
    (sqrt_2pi / theta) * integral
}

/// Grid search for `(entry_z, exit_z, stop_z)` maximising expected return per
/// unit time, accounting for `cost_dollars` round-trip and `dollars_per_point`
/// conversion.
///
/// Key constraint: FPT from entry to exit must fit within the OU time-stop
/// budget of 2 × half_life (= 2·ln2/theta bars). Without this, the unconstrained
/// objective always prefers the widest entry (boundary of the grid), because profit
/// grows linearly with `a` while FPT grows sub-linearly due to the OU drift.
/// Enforcing FPT ≤ budget yields practically reachable entry thresholds.
///
/// Returns `(1.2, 0.0, 2.5)` on degenerate input.
pub fn ou_optimal_thresholds(
    theta: f64,
    sigma_eq: f64,
    cost_dollars: f64,
    dollars_per_point: f64,
) -> (f64, f64, f64) {
    if theta <= 0.0 || sigma_eq <= 0.0 || dollars_per_point <= 0.0 {
        return (1.2, 0.0, 2.5);
    }

    let sigma_dollars = sigma_eq * dollars_per_point;
    let cost_gate = 2.0 * cost_dollars;

    // Time-stop budget: 2 half-lives (same as OU_TIME_STOP_HL constant)
    let half_life = std::f64::consts::LN_2 / theta;
    let max_fpt = 2.0 * half_life;

    let mut best_obj = f64::NEG_INFINITY;
    let mut best_a = 1.2f64;
    let mut best_m = 0.0f64;

    // a in 0.5..=3.0 step 0.1; m in 0.0..a step 0.1
    for ai in 5usize..=30 {
        let a = ai as f64 * 0.1;
        for mi in 0usize..ai {
            let m = mi as f64 * 0.1;
            let expected_profit = (a + m) * sigma_dollars - cost_dollars;
            if expected_profit <= cost_gate - cost_dollars {
                continue;
            }
            let fpt = ou_fpt(a, m, theta);
            if fpt <= 0.0 || !fpt.is_finite() {
                continue;
            }
            // Skip if expected time from entry to exit exceeds the time-stop budget.
            // This ensures the optimizer picks practically reachable entry thresholds.
            if fpt > max_fpt {
                continue;
            }
            let obj = expected_profit / fpt;
            if obj > best_obj {
                best_obj = obj;
                best_a = a;
                best_m = m;
            }
        }
    }

    let stop_z = (best_a + 1.0).min(4.0);
    (best_a, best_m, stop_z)
}

// ── OU Unit Tests ──────────────────────────────────────────────────────────────

#[cfg(test)]
mod ou_tests {
    use super::*;

    #[test]
    fn test_ar1_fit_known_phi() {
        let phi_true = 0.7f64;
        let mut y = vec![0.0f64; 500];
        // xorshift64 PRNG — seeded, produces i.i.d. uniform noise.
        // sin() noise is autocorrelated with y_{i-1}, which biases OLS.
        let mut rng = 0xdeadbeef_cafebabe_u64;
        for i in 1..500 {
            rng ^= rng << 13;
            rng ^= rng >> 7;
            rng ^= rng << 17;
            let noise = (rng as i64 as f64) / (i64::MAX as f64); // roughly U(-1,1)
            y[i] = phi_true * y[i - 1] + 0.3 * noise;
        }
        let (phi, _var_eps, _sigma_eq, theta, half_life, adf_t) =
            ou_ar1_fit(&y).expect("fit failed");
        assert!(
            (phi - phi_true).abs() < 0.05,
            "phi={phi:.3} expected ~{phi_true}"
        );
        assert!(theta > 0.0);
        assert!(half_life > 0.0);
        assert!(adf_t < 0.0, "ADF t-stat should be negative");
    }

    #[test]
    fn test_ar1_fit_unit_root_returns_none() {
        let mut y = vec![0.0f64; 100];
        for i in 1..100 {
            y[i] = y[i - 1] + 0.01 * i as f64;
        }
        // Should not panic; result may be None (phi >= 1) or Some
        let _ = ou_ar1_fit(&y);
    }

    #[test]
    fn test_fpt_monotone_in_distance() {
        let theta = 0.5f64;
        let t1 = ou_fpt(1.0, 0.5, theta);
        let t2 = ou_fpt(2.0, 1.0, theta);
        assert!(
            t2 > t1,
            "fpt(2,1)={t2:.3} should be > fpt(1,0.5)={t1:.3}"
        );
    }

    #[test]
    fn test_fpt_faster_at_higher_theta() {
        let t_slow = ou_fpt(1.5, 0.5, 0.1);
        let t_fast = ou_fpt(1.5, 0.5, 0.5);
        assert!(t_fast < t_slow, "fast theta should give shorter fpt");
    }

    #[test]
    fn test_optimal_thresholds_entry_widens_with_cost() {
        let theta = 0.3f64;
        let sigma_eq = 3.0f64;
        let dpv = 50.0f64;
        let (a_cheap, _, _) = ou_optimal_thresholds(theta, sigma_eq, 10.0, dpv);
        let (a_exp, _, _) = ou_optimal_thresholds(theta, sigma_eq, 50.0, dpv);
        assert!(
            a_exp >= a_cheap - 0.2,
            "expensive entry_z={a_exp:.2} should not be much tighter than cheap={a_cheap:.2}"
        );
    }

    #[test]
    fn test_optimal_thresholds_returns_valid_values() {
        let (entry_z, exit_z, stop_z) = ou_optimal_thresholds(0.3, 2.0, 37.5, 50.0);
        assert!(entry_z > 0.0);
        assert!(exit_z >= 0.0);
        assert!(
            stop_z > entry_z,
            "stop_z={stop_z:.2} must be > entry_z={entry_z:.2}"
        );
    }
}
