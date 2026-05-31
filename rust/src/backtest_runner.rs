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
    pub daily_profit_cap: f64,  // stop new entries when daily P&L >= this
    pub daily_loss_limit: f64,  // stop new entries when daily P&L <= -this
    pub profit_target: f64,     // combine pass target (for consistency check)
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
            daily_profit_cap: f64::MAX,
            daily_loss_limit: f64::MAX,
            profit_target: 3000.0,
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
                    // TopStep $50K defaults: cap below consistency threshold, protect MLL
                    cfg.daily_profit_cap = 1499.0;
                    cfg.daily_loss_limit = 700.0;
                    cfg.profit_target = 3000.0;
                }
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

        for (i, bar) in bars.iter().enumerate() {
            let bar_idx = i as i64;
            if bar_idx < 15 { continue; }

            let tod = bar_tod_mins(bar);

            // Manage open position
            if let Some(dir) = pos_dir {
                let price = bar.c;
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

            // Entry — only when flat
            if pos_dir.is_none() {
                if tod < 0 || tod >= cfg.power_hour_start { continue; }
                if cfg.lunch_skip && tod >= LUNCH_SKIP_START && tod < LUNCH_SKIP_END { continue; }
                if trades_today >= cfg.max_trades { continue; }
                if bar_idx - last_exit_bar < 2 { continue; }
                // Prop firm risk guards
                if daily_pnl >= cfg.daily_profit_cap { day_capped = true; continue; }
                if daily_pnl <= -cfg.daily_loss_limit { day_loss_limited = true; continue; }

                let std = bar.vwap_std;
                if std < 0.01 { continue; }
                let z = (bar.c - bar.vwap) / std;
                if z.abs() < cfg.z_thresh { continue; }

                let dir: &'static str = if z > 0.0 { "SHORT" } else { "LONG" };

                // ATR from recent bars
                let atr = if i >= 14 {
                    let slice = &bars[(i.saturating_sub(20))..=i];
                    let ranges: Vec<f64> = slice.windows(2).map(|w| {
                        (w[1].h - w[1].l)
                            .max((w[1].h - w[0].c).abs())
                            .max((w[1].l - w[0].c).abs())
                    }).collect();
                    if ranges.is_empty() { bar.h - bar.l } else { ranges.iter().sum::<f64>() / ranges.len() as f64 }
                } else {
                    bar.h - bar.l
                };

                let sl_dist = (atr * cfg.atr_stop_ratio)
                    .max(cfg.min_stop_ticks as f64 * cfg.tick_size)
                    .min(cfg.max_stop_ticks as f64 * cfg.tick_size);

                let tp_price = if cfg.target_mode == "vpoc" { bar.vpoc } else {
                    (bar.vwap / cfg.tick_size).round() * cfg.tick_size
                };

                let sl_price = match dir {
                    "LONG" => bar.c - sl_dist,
                    _ => bar.c + sl_dist,
                };

                let tp_buf = (tp_price - bar.c).abs() / cfg.tick_size;
                if tp_buf < cfg.exit_min_ticks { continue; }

                pos_dir = Some(dir);
                pos_ep = bar.c;
                pos_sl = sl_price;
                pos_tp = tp_price;
                pos_bp = bar.c;
                pos_bar_idx = bar_idx;
                pos_contracts = 1;
                pos_scale1_done = false;
                trades_today += 1;
                total_trades += 1;
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
    gross - cfg.commission_rt * size as f64
}

// ── Report printer ─────────────────────────────────────────────────────────────

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

    // Equity curve
    println!();
    println!("  Equity Curve");
    println!("  ┌{}┐", "─".repeat(74));
    let curve = equity_curve_ascii(day_results, 66);
    for line in curve.lines() {
        println!("  │ {:<72} │", line);
    }
    println!("  └{}┘", "─".repeat(74));

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
}
