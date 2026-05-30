use rand::seq::SliceRandom;
use rand::SeedableRng;

use crate::config::{
    ACCOUNT_START_BALANCE, ATR_STOP_RATIO, BREAKEVEN_TICKS, COMMISSION_RT, EXIT_MIN_TICKS,
    GUTTER_DD, GUTTER_GOAL, LUNCH_SKIP_END, LUNCH_SKIP_START, MAX_STOP_TICKS, MAX_TRADES,
    MC_SIMULATIONS, MIN_STOP_TICKS, POWER_HOUR_START, PROFIT_TARGET, SCALE_OUT_ENABLED,
    TARGET_MODE, TICK_SIZE, TICK_VALUE, TIME_STOP_MINS, TRAIL_ACTIVATE, TRAIL_DISTANCE,
    TRAILING_MLL_DISTANCE, Z_THRESH,
};
use crate::types::{BacktestStats, Bar, MonteCarloCurve, TradeRecord};
use crate::strategy::bot::BotState;

pub fn run_backtest(
    bars: &[Bar],
    gutter_win: bool,
    gutter_loss: bool,
) -> (serde_json::Value, Vec<TradeRecord>) {
    let mut trades: Vec<TradeRecord> = Vec::new();
    let mut total_pnl = 0.0f64;
    let mut daily_pnl = 0.0f64;
    let mut trades_today: u32 = 0;
    let mut peak_pnl = 0.0f64;
    let mut max_dd = 0.0f64;
    let mut peak_eod_balance = ACCOUNT_START_BALANCE;

    // Position state
    let mut pos_dir: Option<&'static str> = None; // "LONG" or "SHORT"
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

        if bar_idx < 15 {
            continue;
        }

        // Compute time of day from bar timestamp
        let tod_mins = bar_ts_to_tod_mins(&bar.ts);

        // Manage open position
        if let Some(dir) = pos_dir {
            let price = bar.c;
            let cur = match dir {
                "LONG" => (price - pos_ep) / TICK_SIZE,
                _ => (pos_ep - price) / TICK_SIZE,
            };
            let bt = bar_idx - pos_bar_idx;

            // Update best price
            match dir {
                "LONG" => {
                    if price > pos_bp {
                        pos_bp = price;
                    }
                }
                _ => {
                    if price < pos_bp {
                        pos_bp = price;
                    }
                }
            }

            let ur = match dir {
                "LONG" => (pos_bp - pos_ep) / TICK_SIZE,
                _ => (pos_ep - pos_bp) / TICK_SIZE,
            };

            // Trailing stop update
            if ur >= TRAIL_ACTIVATE {
                match dir {
                    "LONG" => {
                        let tl = ((pos_bp - TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE).round()
                            * TICK_SIZE;
                        if tl > pos_sl {
                            pos_sl = tl;
                        }
                    }
                    _ => {
                        let tl = ((pos_bp + TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE).round()
                            * TICK_SIZE;
                        if tl < pos_sl {
                            pos_sl = tl;
                        }
                    }
                }
            }

            // Breakeven
            if ur >= BREAKEVEN_TICKS {
                match dir {
                    "LONG" if pos_sl < pos_ep => pos_sl = pos_ep,
                    "SHORT" if pos_sl > pos_ep => pos_sl = pos_ep,
                    _ => {}
                }
            }

            // Gutter win
            let mut exited = false;
            if gutter_win && daily_pnl < GUTTER_GOAL {
                let account_bal = ACCOUNT_START_BALANCE + total_pnl;
                let mll_floor = peak_eod_balance - TRAILING_MLL_DISTANCE;
                let eff_dd = if gutter_loss {
                    let mll_room = account_bal - mll_floor;
                    if mll_room < GUTTER_DD {
                        (mll_room - 100.0).max(0.0)
                    } else {
                        GUTTER_DD
                    }
                } else {
                    f64::MAX
                };
                let _ = eff_dd;

                let gut_tp_ticks = (GUTTER_GOAL - daily_pnl) / (pos_contracts as f64 * TICK_VALUE);
                let gut_tp = match dir {
                    "LONG" => pos_ep + gut_tp_ticks * TICK_SIZE,
                    _ => pos_ep - gut_tp_ticks * TICK_SIZE,
                };
                let hit = match dir {
                    "LONG" => price >= gut_tp,
                    _ => price <= gut_tp,
                };
                if hit {
                    let fill = gut_tp;
                    let (pnl, _) = book_close(dir, pos_ep, fill, pos_contracts);
                    total_pnl += pnl;
                    daily_pnl += pnl;
                    trades.push(TradeRecord {
                        action: "EXIT".into(),
                        dir: dir.to_string(),
                        price: fill,
                        pnl: pnl - COMMISSION_RT * pos_contracts as f64,
                        bar_idx,
                        reason: "GUTTER WIN".into(),
                        size: pos_contracts,
                    });
                    last_exit_bar = bar_idx;
                    pos_dir = None;
                    exited = true;
                }
            }

            // Check SL/TP/time stop (bar close approximation)
            if !exited {
                let exit_reason = check_exit(dir, price, pos_ep, pos_sl, pos_tp, bt, cur);
                if let Some(reason) = exit_reason {
                    // Scale out before full exit if applicable
                    if SCALE_OUT_ENABLED && pos_contracts > 1 && !pos_scale1_done && cur >= EXIT_MIN_TICKS {
                        let (pnl, _) = book_close(dir, pos_ep, price, 1);
                        total_pnl += pnl;
                        daily_pnl += pnl;
                        trades.push(TradeRecord {
                            action: "EXIT".into(),
                            dir: dir.to_string(),
                            price,
                            pnl: pnl - COMMISSION_RT,
                            bar_idx,
                            reason: "SCALE OUT".into(),
                            size: 1,
                        });
                        pos_contracts -= 1;
                        pos_scale1_done = true;
                        // Move SL to breakeven after scale
                        match dir {
                            "LONG" if pos_sl < pos_ep => pos_sl = pos_ep,
                            "SHORT" if pos_sl > pos_ep => pos_sl = pos_ep,
                            _ => {}
                        }
                    } else {
                        let (pnl, _) = book_close(dir, pos_ep, price, pos_contracts);
                        total_pnl += pnl;
                        daily_pnl += pnl;
                        let net = pnl - COMMISSION_RT * pos_contracts as f64;
                        trades.push(TradeRecord {
                            action: "EXIT".into(),
                            dir: dir.to_string(),
                            price,
                            pnl: net,
                            bar_idx,
                            reason: reason.to_string(),
                            size: pos_contracts,
                        });
                        last_exit_bar = bar_idx;
                        pos_dir = None;
                    }
                }
            }

            // Update drawdown
            let cur_eq = total_pnl;
            if cur_eq > peak_pnl {
                peak_pnl = cur_eq;
            }
            let dd = peak_pnl - cur_eq;
            if dd > max_dd {
                max_dd = dd;
            }
        }

        // Entry signal (only if flat)
        if pos_dir.is_none() {
            if tod_mins < 0 || tod_mins >= POWER_HOUR_START {
                continue;
            }
            if tod_mins >= LUNCH_SKIP_START && tod_mins < LUNCH_SKIP_END {
                continue;
            }
            if trades_today >= MAX_TRADES {
                continue;
            }
            if bar_idx - last_exit_bar < 2 {
                continue; // brief cooldown after exit
            }

            let std = bar.vwap_std;
            if std < 0.01 {
                continue;
            }
            let z = (bar.c - bar.vwap) / std;
            if z.abs() < Z_THRESH {
                continue;
            }

            let dir: &'static str = if z > 0.0 { "SHORT" } else { "LONG" };

            // ATR from bar or approximate
            let atr = if i >= 14 {
                let slice = &bars[(i.saturating_sub(20))..=i];
                let ranges: Vec<f64> = slice
                    .windows(2)
                    .map(|w| {
                        let tr = (w[1].h - w[1].l)
                            .max((w[1].h - w[0].c).abs())
                            .max((w[1].l - w[0].c).abs());
                        tr
                    })
                    .collect();
                if ranges.is_empty() {
                    bar.h - bar.l
                } else {
                    ranges.iter().sum::<f64>() / ranges.len() as f64
                }
            } else {
                bar.h - bar.l
            };

            let sl_dist =
                (atr * ATR_STOP_RATIO).max(MIN_STOP_TICKS as f64 * TICK_SIZE).min(MAX_STOP_TICKS as f64 * TICK_SIZE);

            let (sl_price, tp_price) = match dir {
                "LONG" => {
                    let sl = bar.c - sl_dist;
                    let tp = if TARGET_MODE == "vwap" {
                        (bar.vwap / TICK_SIZE).round() * TICK_SIZE
                    } else {
                        bar.vpoc
                    };
                    (sl, tp)
                }
                _ => {
                    let sl = bar.c + sl_dist;
                    let tp = if TARGET_MODE == "vwap" {
                        (bar.vwap / TICK_SIZE).round() * TICK_SIZE
                    } else {
                        bar.vpoc
                    };
                    (sl, tp)
                }
            };

            let tp_buffer_ticks = (tp_price - bar.c).abs() / TICK_SIZE;
            if tp_buffer_ticks < EXIT_MIN_TICKS {
                continue;
            }

            // Enter
            pos_dir = Some(dir);
            pos_ep = bar.c;
            pos_sl = sl_price;
            pos_tp = tp_price;
            pos_bp = bar.c;
            pos_bar_idx = bar_idx;
            pos_contracts = 1;
            pos_scale1_done = false;
            trades_today += 1;

            trades.push(TradeRecord {
                action: "ENTER".into(),
                dir: dir.to_string(),
                price: bar.c,
                pnl: 0.0,
                bar_idx,
                reason: String::new(),
                size: 1,
            });
        }
    }

    // Force-close any open position at end of data
    if let Some(dir) = pos_dir {
        let last_price = bars.last().map(|b| b.c).unwrap_or(0.0);
        let bar_idx = bars.len() as i64 - 1;
        let (pnl, _) = book_close(dir, pos_ep, last_price, pos_contracts);
        total_pnl += pnl;
        trades.push(TradeRecord {
            action: "EXIT".into(),
            dir: dir.to_string(),
            price: last_price,
            pnl: pnl - COMMISSION_RT * pos_contracts as f64,
            bar_idx,
            reason: "END OF DATA".into(),
            size: pos_contracts,
        });
    }

    let account_balance = ACCOUNT_START_BALANCE + total_pnl;
    let mll_floor = peak_eod_balance - TRAILING_MLL_DISTANCE;

    let snapshot = serde_json::json!({
        "total_pnl": total_pnl,
        "daily_pnl": daily_pnl,
        "max_dd": max_dd,
        "trades_today": trades_today,
        "account_balance": account_balance,
        "mll_floor": mll_floor,
        "mll_remaining": account_balance - mll_floor,
        "peak_eod_balance": peak_eod_balance,
        "gutter_win": gutter_win,
        "gutter_loss": gutter_loss,
    });

    (snapshot, trades)
}

fn check_exit(
    dir: &str,
    price: f64,
    ep: f64,
    sl: f64,
    tp: f64,
    bars_in_trade: i64,
    cur_ticks: f64,
) -> Option<&'static str> {
    match dir {
        "LONG" => {
            if price <= sl {
                return Some(classify_stop_str("LONG", ep, sl));
            }
            if price >= tp && cur_ticks >= EXIT_MIN_TICKS {
                return Some("TARGET");
            }
        }
        _ => {
            if price >= sl {
                return Some(classify_stop_str("SHORT", ep, sl));
            }
            if price <= tp && cur_ticks >= EXIT_MIN_TICKS {
                return Some("TARGET");
            }
        }
    }

    if bars_in_trade > TIME_STOP_MINS {
        return Some("TIME STOP");
    }
    if bars_in_trade > TIME_STOP_MINS / 2 && cur_ticks < -4.0 {
        return Some("TIME STOP");
    }

    None
}

fn classify_stop_str(dir: &str, ep: f64, sl: f64) -> &'static str {
    let tol = TICK_SIZE * 0.25;
    if (sl - ep).abs() <= tol {
        return "BREAKEVEN STOP";
    }
    match dir {
        "LONG" => {
            if sl > ep {
                "TRAIL STOP"
            } else {
                "STOP LOSS"
            }
        }
        _ => {
            if sl < ep {
                "TRAIL STOP"
            } else {
                "STOP LOSS"
            }
        }
    }
}

fn book_close(dir: &str, ep: f64, fill: f64, size: u32) -> (f64, f64) {
    let ticks = match dir {
        "LONG" => (fill - ep) / TICK_SIZE,
        _ => (ep - fill) / TICK_SIZE,
    };
    let gross = ticks * TICK_VALUE * size as f64;
    let net = gross - COMMISSION_RT * size as f64;
    (gross, net)
}

fn bar_ts_to_tod_mins(ts: &chrono::DateTime<chrono::Utc>) -> i64 {
    use chrono::TimeZone;
    let et = chrono_tz::America::New_York.from_utc_datetime(&ts.naive_utc());
    et.format("%H").to_string().parse::<i64>().unwrap_or(0) * 60
        + et.format("%M").to_string().parse::<i64>().unwrap_or(0)
        - 9 * 60 - 30 // offset from RTH open (09:30 ET)
}

pub fn build_stats(
    trades: &[TradeRecord],
    max_trades: u32,
    bot_state: &BotState,
) -> BacktestStats {
    let exit_trades: Vec<&TradeRecord> = trades
        .iter()
        .filter(|t| t.action == "EXIT" && t.reason != "SCALE OUT")
        .collect();

    if exit_trades.is_empty() {
        return BacktestStats {
            ready: false,
            max_trades_day: max_trades,
            simulations: MC_SIMULATIONS,
            start_balance: ACCOUNT_START_BALANCE,
            pass_balance: ACCOUNT_START_BALANCE + PROFIT_TARGET,
            trailing_mll: TRAILING_MLL_DISTANCE,
            profit_target: PROFIT_TARGET,
            account_balance: bot_state.account_balance(),
            mll_floor: bot_state.mll_floor(),
            mll_remaining: bot_state.mll_remaining(),
            peak_eod_balance: bot_state.peak_eod_balance,
            ..Default::default()
        };
    }

    let wins: Vec<f64> = exit_trades.iter().filter(|t| t.pnl > 0.0).map(|t| t.pnl).collect();
    let losses: Vec<f64> = exit_trades.iter().filter(|t| t.pnl < 0.0).map(|t| t.pnl).collect();
    let flats: usize = exit_trades.iter().filter(|t| t.pnl == 0.0).count();

    let n_wins = wins.len();
    let n_losses = losses.len();
    let n_total = exit_trades.len();
    let n_nonflat = n_wins + n_losses;

    let avg_win = if n_wins > 0 { wins.iter().sum::<f64>() / n_wins as f64 } else { 0.0 };
    let avg_loss = if n_losses > 0 { losses.iter().sum::<f64>() / n_losses as f64 } else { 0.0 };
    let win_rate = if n_nonflat > 0 { n_wins as f64 / n_nonflat as f64 } else { 0.0 };
    let win_rate_all = if n_total > 0 { n_wins as f64 / n_total as f64 } else { 0.0 };

    let gross_profit: f64 = wins.iter().sum();
    let gross_loss: f64 = losses.iter().map(|x| x.abs()).sum();
    let profit_factor = if gross_loss > 0.0 { gross_profit / gross_loss } else { f64::INFINITY };

    let rr = if avg_loss != 0.0 { avg_win / avg_loss.abs() } else { 0.0 };
    let expectancy = win_rate * avg_win + (1.0 - win_rate) * avg_loss;

    // Group into daily P&L for Monte Carlo
    let daily_pnls = group_daily_pnl(exit_trades.as_slice());
    let sample_days = daily_pnls.len();

    // Monte Carlo simulation
    let (pass_rate, blow_rate, timeout_rate, avg_days_to_pass, avg_days_to_blow, x_axis, paths_preview, curve) =
        if sample_days > 0 {
            monte_carlo(
                &daily_pnls,
                bot_state.account_balance(),
                bot_state.mll_floor(),
                bot_state.peak_eod_balance,
                max_trades,
            )
        } else {
            (0.0, 0.0, 0.0, 0.0, 0.0, vec![], vec![], MonteCarloCurve::default())
        };

    BacktestStats {
        ready: true,
        trades: n_total,
        wins: n_wins,
        losses: n_losses,
        flats,
        win_rate,
        win_rate_nonflat: win_rate,
        win_rate_all,
        avg_win,
        avg_loss,
        rr,
        expectancy,
        profit_factor,
        pass_rate,
        blow_rate,
        timeout_rate,
        avg_days_to_pass,
        avg_days_to_blow,
        simulations: MC_SIMULATIONS,
        sample_days,
        lookback_days: sample_days,
        max_days: 60,
        max_trades_day: max_trades,
        start_balance: ACCOUNT_START_BALANCE,
        pass_balance: ACCOUNT_START_BALANCE + PROFIT_TARGET,
        trailing_mll: TRAILING_MLL_DISTANCE,
        profit_target: PROFIT_TARGET,
        mll_floor: bot_state.mll_floor(),
        account_balance: bot_state.account_balance(),
        mll_remaining: bot_state.mll_remaining(),
        peak_eod_balance: bot_state.peak_eod_balance,
        x_axis,
        paths_preview,
        curve,
    }
}

fn group_daily_pnl(trades: &[&TradeRecord]) -> Vec<f64> {
    // Group by simple bucket: every MAX_TRADES exits = 1 simulated day
    // In practice, we use each trade's P&L and group N trades per simulated day
    let max_t = MAX_TRADES as usize;
    if trades.is_empty() {
        return vec![];
    }
    trades
        .chunks(max_t.max(1))
        .map(|chunk| chunk.iter().map(|t| t.pnl).sum())
        .collect()
}

fn monte_carlo(
    daily_pnls: &[f64],
    start_balance: f64,
    mll_floor: f64,
    peak_eod_balance: f64,
    max_trades: u32,
) -> (f64, f64, f64, f64, f64, Vec<i32>, Vec<Vec<f64>>, MonteCarloCurve) {
    let pass_target = start_balance + PROFIT_TARGET;
    let max_days = 60usize;
    let n_sims = MC_SIMULATIONS as usize;
    let _ = max_trades;

    let mut rng = rand::rngs::SmallRng::seed_from_u64(42);
    let mut pass_count = 0usize;
    let mut blow_count = 0usize;
    let mut timeout_count = 0usize;
    let mut days_to_pass_sum = 0.0f64;
    let mut days_to_blow_sum = 0.0f64;
    let mut days_to_pass_n = 0usize;
    let mut days_to_blow_n = 0usize;

    // Collect all final balances per day for percentile curves
    let mut all_paths: Vec<Vec<f64>> = Vec::with_capacity(n_sims.min(50));
    let mut path_matrix: Vec<Vec<f64>> = vec![vec![0.0f64; max_days]; n_sims];

    for sim in 0..n_sims {
        let mut balance = start_balance;
        let mut peak_bal = peak_eod_balance;
        let mut done = false;
        let mut outcome_day = max_days;

        for day in 0..max_days {
            if done {
                path_matrix[sim][day] = balance;
                continue;
            }

            // Sample a random daily pnl from historical distribution
            let day_pnl = *daily_pnls.choose(&mut rng).unwrap_or(&0.0);
            balance += day_pnl;

            // Update trailing MLL
            if balance > peak_bal {
                peak_bal = balance;
            }
            let cur_mll_floor = (peak_bal - TRAILING_MLL_DISTANCE).max(mll_floor);

            path_matrix[sim][day] = balance;

            if balance >= pass_target {
                pass_count += 1;
                days_to_pass_sum += day as f64 + 1.0;
                days_to_pass_n += 1;
                done = true;
                outcome_day = day;
            } else if balance <= cur_mll_floor {
                blow_count += 1;
                days_to_blow_sum += day as f64 + 1.0;
                days_to_blow_n += 1;
                done = true;
                outcome_day = day;
            }
        }

        if !done {
            timeout_count += 1;
        }

        if sim < 50 {
            let path: Vec<f64> = path_matrix[sim][..=outcome_day.min(max_days - 1)]
                .iter()
                .map(|b| b - start_balance)
                .collect();
            all_paths.push(path);
        }
    }

    let total = n_sims as f64;
    let pass_rate = pass_count as f64 / total;
    let blow_rate = blow_count as f64 / total;
    let timeout_rate = timeout_count as f64 / total;
    let avg_days_to_pass = if days_to_pass_n > 0 {
        days_to_pass_sum / days_to_pass_n as f64
    } else {
        0.0
    };
    let avg_days_to_blow = if days_to_blow_n > 0 {
        days_to_blow_sum / days_to_blow_n as f64
    } else {
        0.0
    };

    let x_axis: Vec<i32> = (0..max_days as i32).collect();

    // Build percentile curves
    let mut p5 = vec![0.0f64; max_days];
    let mut p50 = vec![0.0f64; max_days];
    let mut p95 = vec![0.0f64; max_days];

    for day in 0..max_days {
        let mut day_vals: Vec<f64> = path_matrix.iter().map(|p| p[day] - start_balance).collect();
        day_vals.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let n = day_vals.len();
        p5[day] = day_vals[(n as f64 * 0.05) as usize].min(day_vals[n - 1]);
        p50[day] = day_vals[n / 2];
        p95[day] = day_vals[((n as f64 * 0.95) as usize).min(n - 1)];
    }

    let curve = MonteCarloCurve { p5, p50, p95 };

    (
        pass_rate,
        blow_rate,
        timeout_rate,
        avg_days_to_pass,
        avg_days_to_blow,
        x_axis,
        all_paths,
        curve,
    )
}
