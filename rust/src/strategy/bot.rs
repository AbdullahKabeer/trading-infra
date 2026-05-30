use std::collections::VecDeque;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::sync::{broadcast, mpsc};
use uuid::Uuid;

use crate::config::*;
use crate::types::*;

pub struct BotState {
    pub pos: Option<Position>,
    pub daily_pnl: f64,
    pub total_pnl: f64,
    pub trade_history: Vec<TradeRecord>,
    pub pnl_history: VecDeque<(std::time::Instant, f64)>,
    pub equity_history: VecDeque<(std::time::Instant, f64)>,
    pub peak_pnl: f64,
    pub max_dd: f64,
    pub peak_eod_balance: f64,
    pub trades_today: u32,
    pub lockdown_until: Option<std::time::Instant>,
    pub last_exit_bar: i64,
    pub cooldown_long_until: i64,
    pub cooldown_short_until: i64,
    pub entry_pending: bool,
    pub gutter_win: bool,
    pub gutter_loss: bool,
    pub account_name: Option<String>,
    pub account_id: Option<i64>,
    pub contract_id: Option<String>,
    pub open_pnl: f64,
}

impl BotState {
    pub fn new() -> Self {
        Self {
            pos: None,
            daily_pnl: 0.0, total_pnl: 0.0,
            trade_history: vec![],
            pnl_history: VecDeque::new(),
            equity_history: VecDeque::new(),
            peak_pnl: 0.0, max_dd: 0.0,
            peak_eod_balance: ACCOUNT_START_BALANCE,
            trades_today: 0,
            lockdown_until: None,
            last_exit_bar: -999,
            cooldown_long_until: -999,
            cooldown_short_until: -999,
            entry_pending: false,
            gutter_win: true, gutter_loss: true,
            account_name: None, account_id: None, contract_id: None,
            open_pnl: 0.0,
        }
    }

    pub fn account_balance(&self) -> f64 { ACCOUNT_START_BALANCE + self.total_pnl }
    pub fn account_balance_with_open(&self) -> f64 { self.account_balance() + self.open_pnl }
    pub fn mll_floor(&self) -> f64 { self.peak_eod_balance - TRAILING_MLL_DISTANCE }
    pub fn mll_remaining(&self) -> f64 { self.account_balance_with_open() - self.mll_floor() }

    pub fn snapshot(&self, price: f64) -> BotSnapshot {
        let open_pnl = if let Some(ref pos) = self.pos {
            let ticks = match pos.dir {
                Direction::Long => (price - pos.ep) / TICK_SIZE,
                Direction::Short => (pos.ep - price) / TICK_SIZE,
            };
            ticks * TICK_VALUE * pos.contracts_remaining as f64
        } else { 0.0 };

        BotSnapshot {
            account_name: self.account_name.clone(),
            total_pnl: self.total_pnl,
            daily_pnl: self.daily_pnl,
            open_pnl,
            max_dd: self.max_dd,
            trades_today: self.trades_today,
            pos: self.pos.as_ref().map(|p| PositionSnapshot {
                dir: p.dir.as_str().to_string(),
                ep: p.ep, sl: p.sl, tp: p.tp,
                contracts_remaining: p.contracts_remaining,
            }),
            trade_history: self.trade_history.iter().rev().take(50).cloned().collect(),
            active_orders: vec![],
            account_balance: self.account_balance_with_open(),
            mll_floor: self.mll_floor(),
            mll_remaining: self.mll_remaining(),
            peak_eod_balance: self.peak_eod_balance,
            gutter_win: self.gutter_win,
            gutter_loss: self.gutter_loss,
            equity_curve: self.equity_history.iter()
                .map(|(t, v)| (t.elapsed().as_secs_f64(), *v))
                .collect(),
            z_history: vec![],
            delta_history: vec![],
            atr_sparkline: vec![],
        }
    }

    fn prune_history(&mut self) {
        let cutoff = std::time::Instant::now() - Duration::from_secs(7200);
        self.pnl_history.retain(|(t, _)| *t > cutoff);
        self.equity_history.retain(|(t, _)| *t > cutoff);
    }

    fn hourly_stats(&self) -> (f64, f64) {
        let now = std::time::Instant::now();
        let cutoff = now - Duration::from_secs(3600);
        let hourly_pnl: f64 = self.pnl_history.iter()
            .filter(|(t, _)| *t > cutoff).map(|(_, p)| p).sum();
        let h_eq: Vec<f64> = self.equity_history.iter()
            .filter(|(t, _)| *t > cutoff).map(|(_, e)| *e).collect();
        let h_dd = if h_eq.is_empty() { 0.0 } else {
            h_eq.iter().cloned().fold(f64::NEG_INFINITY, f64::max) - self.total_pnl
        };
        (hourly_pnl, h_dd)
    }

    fn check_lockdown(&mut self, hourly_pnl: f64, h_dd: f64) {
        let trigger = if HOURLY_GUARD_TYPE == "PL" { hourly_pnl } else { -h_dd };
        if trigger <= -MAX_HOURLY_LOSS {
            self.lockdown_until = Some(std::time::Instant::now() + Duration::from_secs(3600));
            tracing::warn!("LOCKDOWN: hourly guard hit — trading disabled for 60 mins");
        }
    }

    pub fn book_close(&mut self, price: f64, reason: &str, bar_idx: i64, size: u32, dir: &str) {
        let normalized = normalize_exit_reason(reason);
        let pos_ep = self.pos.as_ref().map(|p| p.ep).unwrap_or(price);
        let ticks = if dir == "LONG" { (price - pos_ep) / TICK_SIZE } else { (pos_ep - price) / TICK_SIZE };
        let gross_pnl = ticks * TICK_VALUE * size as f64;
        let net_pnl = gross_pnl - COMMISSION_RT * size as f64;

        // Direction cooldown
        if matches!(normalized.as_str(), "STOP LOSS" | "TRAIL STOP") {
            match dir {
                "LONG" => self.cooldown_long_until = bar_idx + DIR_COOLDOWN_BARS,
                _ => self.cooldown_short_until = bar_idx + DIR_COOLDOWN_BARS,
            }
        }

        self.total_pnl += gross_pnl;
        self.daily_pnl += gross_pnl;
        self.trade_history.push(TradeRecord {
            action: "EXIT".to_string(), dir: dir.to_string(),
            price, pnl: net_pnl, bar_idx, reason: normalized.clone(), size,
        });

        let now = std::time::Instant::now();
        self.pnl_history.push_back((now, net_pnl));
        self.equity_history.push_back((now, self.total_pnl));
        self.prune_history();

        let (hourly_pnl, h_dd) = self.hourly_stats();
        self.check_lockdown(hourly_pnl, h_dd);
        self.last_exit_bar = bar_idx;
        self.pos = None;

        tracing::info!(
            "CLOSED {dir} @ {price:.2} ({normalized}) | Net: ${net_pnl:.2} | Total: ${:.2}",
            self.total_pnl
        );
    }

    pub fn reset_for_new_day(&mut self) {
        self.trades_today = 0;
        self.daily_pnl = 0.0;
        let balance = self.account_balance();
        if balance > self.peak_eod_balance {
            self.peak_eod_balance = balance;
        }
    }
}

// ── Main bot loop ──────────────────────────────────────────────────────────────

pub async fn run(
    mut bar_rx: broadcast::Receiver<BarEvent>,
    mut tick_rx: broadcast::Receiver<TickEvent>,
    cmd_tx: mpsc::Sender<TradeCommand>,
    mut fill_rx: mpsc::Receiver<FillEvent>,
    state: std::sync::Arc<tokio::sync::RwLock<AppState>>,
) {
    let mut bot = BotState::new();

    loop {
        tokio::select! {
            Ok(bar) = bar_rx.recv() => {
                // Process any pending fills first
                while let Ok(fill) = fill_rx.try_recv() {
                    handle_fill(&mut bot, fill).await;
                }
                handle_bar_event(&mut bot, bar, &cmd_tx, &state).await;
                // Publish snapshot
                let price = state.read().await.live.last;
                let snap = bot.snapshot(price);
                state.write().await.bot = snap;
            }
            Ok(tick) = tick_rx.recv() => {
                // Process any pending fills first
                while let Ok(fill) = fill_rx.try_recv() {
                    handle_fill(&mut bot, fill).await;
                }
                handle_tick_event(&mut bot, tick, &cmd_tx).await;
                // Update open P&L in snapshot
                let price = state.read().await.live.last;
                let snap = bot.snapshot(price);
                state.write().await.bot = snap;
            }
            Some(fill) = fill_rx.recv() => {
                handle_fill(&mut bot, fill).await;
            }
        }
    }
}

async fn handle_fill(bot: &mut BotState, fill: FillEvent) {
    match fill {
        FillEvent::Entered { pos_uuid, fill_price, dir, sl, tp, bar_idx, entry_vwap, tp_buffer_ticks } => {
            if bot.pos.is_some() {
                tracing::warn!("Got Entered fill but already have position — ignoring");
                return;
            }
            let now_secs = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
            bot.pos = Some(Position {
                uuid: pos_uuid, dir, ep: fill_price, sl, tp, bp: fill_price,
                bar_idx, contracts: CONTRACTS, contracts_remaining: CONTRACTS,
                fill_synced: true, entry_vwap, tp_buffer_ticks, scale1_done: false,
                created_at_secs: now_secs,
            });
            bot.trades_today += 1;
            bot.entry_pending = false;
            bot.trade_history.push(TradeRecord {
                action: "ENTER".to_string(), dir: dir.as_str().to_string(),
                price: fill_price, pnl: 0.0, bar_idx, reason: String::new(), size: CONTRACTS,
            });
            tracing::info!("ENTERED {dir:?} @ {fill_price:.2} SL={sl:.2} TP={tp:.2}");
        }
        FillEvent::Closed { pos_uuid, fill_price, reason, bar_idx, size } => {
            if bot.pos.as_ref().map(|p| p.uuid) != Some(pos_uuid) { return; }
            let dir = bot.pos.as_ref().map(|p| p.dir.as_str().to_string()).unwrap_or_default();
            bot.book_close(fill_price, &reason, bar_idx, size, &dir);
        }
        FillEvent::ExternalClose { fill_price, reason, bar_idx, size } => {
            if let Some(ref pos) = bot.pos {
                let dir = pos.dir.as_str().to_string();
                bot.book_close(fill_price, &reason, bar_idx, size, &dir);
            }
        }
        FillEvent::StopModified { pos_uuid, new_stop } => {
            if let Some(ref mut pos) = bot.pos {
                if pos.uuid == pos_uuid { pos.sl = new_stop; }
            }
        }
        FillEvent::TpModified { pos_uuid, new_tp } => {
            if let Some(ref mut pos) = bot.pos {
                if pos.uuid == pos_uuid { pos.tp = new_tp; }
            }
        }
        FillEvent::ScaledOut { pos_uuid, fill_price, contracts_sold, bar_idx } => {
            if let Some(ref mut pos) = bot.pos {
                if pos.uuid == pos_uuid {
                    let ticks = (fill_price - pos.ep) / TICK_SIZE;
                    let gross = ticks * TICK_VALUE * contracts_sold as f64;
                    let net = gross - COMMISSION_RT * contracts_sold as f64;
                    bot.total_pnl += gross; bot.daily_pnl += gross;
                    pos.contracts_remaining -= contracts_sold;
                    pos.scale1_done = true;
                    if pos.sl < pos.ep { pos.sl = pos.ep; } // move to BE on scale-out
                    bot.trade_history.push(TradeRecord {
                        action: "EXIT".to_string(), dir: pos.dir.as_str().to_string(),
                        price: fill_price, pnl: net, bar_idx, reason: "SCALE OUT".to_string(),
                        size: contracts_sold,
                    });
                    tracing::info!("SCALE OUT {contracts_sold} @ {fill_price:.2} | Net: ${net:.2}");
                }
            }
        }
        FillEvent::ActiveOrders(orders) => {
            // Stored in AppState by order manager — nothing to do here
        }
        FillEvent::Error(e) => {
            tracing::warn!("OrderManager error: {e}");
        }
    }
}

async fn handle_bar_event(
    bot: &mut BotState,
    bar: BarEvent,
    cmd_tx: &mpsc::Sender<TradeCommand>,
    state: &std::sync::Arc<tokio::sync::RwLock<AppState>>,
) {
    if !BOT_ACTIVE || !bar.rth { return; }
    if bar.bar_idx < 15 || bar.std < 0.01 { return; }

    // Update regime crossings
    {
        let mut st = state.write().await;
        let (prices, vwaps, atr, bars_clone) = if let Some(s) = st.session.as_ref() {
            let p: Vec<f64> = s.bars.iter().rev().take(20).map(|b| b.c).collect();
            let v: Vec<f64> = s.bars.iter().rev().take(20).map(|b| b.vwap).collect();
            let a = s.current_atr();
            let bc = s.bars.clone();
            (p, v, a, bc)
        } else {
            (vec![], vec![], 0.0, vec![])
        };
        if !prices.is_empty() {
            st.regime.update_vwap_crossings(&vwaps, &prices, 20);
            st.regime.classify_session(&bars_clone, bar.bar_idx as usize, atr);
        }
    }

    // Lockdown check
    if let Some(until) = bot.lockdown_until {
        if std::time::Instant::now() < until { return; }
        bot.lockdown_until = None;
    }

    // Session filters
    if bar.tod_mins < 0 || bar.tod_mins >= POWER_HOUR_START { return; }
    if bar.tod_mins >= LUNCH_SKIP_START && bar.tod_mins < LUNCH_SKIP_END { return; }
    if bot.trades_today >= MAX_TRADES { return; }
    if bot.entry_pending { return; }
    if bot.pos.is_some() { return; }

    // Compute effective z threshold
    let regime = state.read().await.regime.clone();
    let mut z_thresh = Z_THRESH;
    if regime.vwap_crossings <= VWAP_CROSS_TRENDING { z_thresh *= SESSION_TREND_Z_MULT; }
    if regime.calendar_event.is_some() && HIGH_IMPACT_BEHAVIOR == "reduce" { z_thresh += 0.5; }
    if let (Some(prev_vwap), Some(prev_vpoc)) = (regime.prev_vwap, regime.prev_vpoc) {
        let near_prev = (bar.price - prev_vwap).abs() / TICK_SIZE <= PREV_LEVEL_TICKS
            || (bar.price - prev_vpoc).abs() / TICK_SIZE <= PREV_LEVEL_TICKS;
        if near_prev { z_thresh -= PREV_LEVEL_Z_BONUS; }
    }
    if regime.session_type == "TRENDING" { z_thresh *= SESSION_TREND_Z_MULT; }

    let z = if bar.std > 0.01 { (bar.price - bar.vwap) / bar.std } else { 0.0 };
    let abs_z = z.abs();
    if abs_z < z_thresh { return; }
    if bar.spread > MAX_SPREAD { return; }
    if bar.vol_rel < MIN_VOL_REL { return; }
    if bar.vwap_slope.abs() > VWAP_SLOPE_THRESH && bar.vwap_slope.signum() == z.signum() { return; }
    if bar.sigma_exp > 2.5 { return; } // volatility panic filter

    // Cumulative delta filter
    if CUM_DELTA_FILTER && bar.cum_delta_pct.abs() > CUM_DELTA_FADE_MAX {
        // Fading into a strong directional delta — skip
        if (bar.cum_delta_pct > 0.0 && z < 0.0) || (bar.cum_delta_pct < 0.0 && z > 0.0) {
            return;
        }
    }

    // Direction cooldown
    let dir = if z > 0.0 { Direction::Short } else { Direction::Long }; // mean-reversion: fade the z
    if dir == Direction::Long && bar.bar_idx <= bot.cooldown_long_until { return; }
    if dir == Direction::Short && bar.bar_idx <= bot.cooldown_short_until { return; }

    // Gutter daily loss guard
    let effective_dd = if bot.gutter_loss {
        let mll_room = bot.account_balance() - bot.mll_floor();
        if mll_room < GUTTER_DD { (mll_room - 100.0).max(0.0) } else { GUTTER_DD }
    } else { f64::MAX };
    if bot.daily_pnl <= -effective_dd { return; }

    // ATR-based stop sizing
    let atr = bar.atr.max(bar.std * 0.5);
    let sl_dist = (atr * ATR_STOP_RATIO).max(MIN_STOP_TICKS as f64 * TICK_SIZE);
    let sl_dist = sl_dist.min(MAX_STOP_TICKS as f64 * TICK_SIZE);
    let sl_ticks = (sl_dist / TICK_SIZE).round() as i32;

    let (sl_price, tp_price) = match dir {
        Direction::Long => {
            let sl = bar.price - sl_ticks as f64 * TICK_SIZE;
            let target = if TARGET_MODE == "vwap" {
                (bar.vwap / TICK_SIZE).round() * TICK_SIZE
            } else { bar.vpoc };
            (sl, target)
        }
        Direction::Short => {
            let sl = bar.price + sl_ticks as f64 * TICK_SIZE;
            let target = if TARGET_MODE == "vwap" {
                (bar.vwap / TICK_SIZE).round() * TICK_SIZE
            } else { bar.vpoc };
            (sl, target)
        }
    };

    let tp_buffer_ticks = (tp_price - bar.price).abs() / TICK_SIZE;
    if tp_buffer_ticks < EXIT_MIN_TICKS { return; }

    bot.entry_pending = true;
    tracing::info!(
        "SIGNAL {dir:?} | z={z:.2} z_thresh={z_thresh:.2} | SL={sl_price:.2} TP={tp_price:.2}"
    );

    let _ = cmd_tx.send(TradeCommand::Enter {
        dir, sl_price, tp_price, entry_price: bar.price,
        entry_vwap: bar.vwap, tp_buffer_ticks,
        bar_idx: bar.bar_idx, atr,
    }).await;
}

async fn handle_tick_event(
    bot: &mut BotState,
    tick: TickEvent,
    cmd_tx: &mpsc::Sender<TradeCommand>,
) {
    if !tick.rth {
        if bot.pos.is_some() {
            let _ = cmd_tx.send(TradeCommand::Exit {
                pos_uuid: bot.pos.as_ref().unwrap().uuid,
                reason: "END OF RTH".to_string(),
                price: tick.price,
                bar_idx: tick.bar_idx,
            }).await;
        }
        return;
    }

    // Update open PnL and drawdown tracking
    if let Some(ref pos) = bot.pos {
        let ticks = match pos.dir {
            Direction::Long => (tick.price - pos.ep) / TICK_SIZE,
            Direction::Short => (pos.ep - tick.price) / TICK_SIZE,
        };
        let epnl = ticks * TICK_VALUE * pos.contracts_remaining as f64;
        bot.open_pnl = epnl;
        let cur_eq = bot.total_pnl + epnl;
        if cur_eq > bot.peak_pnl { bot.peak_pnl = cur_eq; }
        let dd = bot.peak_pnl - cur_eq;
        if dd > bot.max_dd { bot.max_dd = dd; }
    }

    let Some(ref pos) = bot.pos else { return; };
    let pos_uuid = pos.uuid;
    let dir = pos.dir;
    let ep = pos.ep;
    let sl = pos.sl;
    let tp = pos.tp;
    let bp = pos.bp;
    let bt = tick.bar_idx - pos.bar_idx;
    let sz = pos.contracts_remaining;
    let scale1_done = pos.scale1_done;
    let entry_vwap = pos.entry_vwap;
    let tp_buffer_ticks = pos.tp_buffer_ticks;
    let price = tick.price;

    // Update best price
    if let Some(ref mut p) = bot.pos {
        match dir {
            Direction::Long => { if price > p.bp { p.bp = price; } }
            Direction::Short => { if price < p.bp { p.bp = price; } }
        }
    }

    let bp = bot.pos.as_ref().map(|p| p.bp).unwrap_or(ep);
    let ur = match dir {
        Direction::Long => (bp - ep) / TICK_SIZE,
        Direction::Short => (ep - bp) / TICK_SIZE,
    };
    let cur = match dir {
        Direction::Long => (price - ep) / TICK_SIZE,
        Direction::Short => (ep - price) / TICK_SIZE,
    };

    // Gutter logic
    let effective_dd = if bot.gutter_loss {
        let mll_room = bot.account_balance() - bot.mll_floor();
        if mll_room < GUTTER_DD { (mll_room - 100.0).max(0.0) } else { GUTTER_DD }
    } else { f64::MAX };

    match dir {
        Direction::Long => {
            // Gutter win
            if bot.gutter_win && bot.daily_pnl < GUTTER_GOAL {
                let gut_tp = ep + ((GUTTER_GOAL - bot.daily_pnl) / (sz as f64 * TICK_VALUE)) * TICK_SIZE;
                if price >= gut_tp {
                    let _ = cmd_tx.send(TradeCommand::Exit { pos_uuid, reason: "GUTTER WIN".to_string(), price: gut_tp, bar_idx: tick.bar_idx }).await;
                    return;
                }
            }
            // Gutter loss
            if bot.gutter_loss && bot.daily_pnl > -effective_dd {
                let remaining = (bot.daily_pnl + effective_dd) / (sz as f64 * TICK_VALUE);
                if remaining > 0.0 {
                    let gut_sl = ep - remaining * TICK_SIZE;
                    if price <= gut_sl {
                        let _ = cmd_tx.send(TradeCommand::Exit { pos_uuid, reason: "GUTTER LOSS".to_string(), price: gut_sl, bar_idx: tick.bar_idx }).await;
                        return;
                    }
                }
            }
            // Stop loss (local fallback)
            if !SERVER_BRACKETS_PRIMARY && price <= sl {
                let reason = classify_stop("LONG", ep, sl);
                let _ = cmd_tx.send(TradeCommand::Exit { pos_uuid, reason, price, bar_idx: tick.bar_idx }).await;
                return;
            }
            // Target (local fallback)
            if !SERVER_BRACKETS_PRIMARY && price >= tp && cur >= EXIT_MIN_TICKS {
                let _ = cmd_tx.send(TradeCommand::Exit { pos_uuid, reason: "TARGET".to_string(), price, bar_idx: tick.bar_idx }).await;
                return;
            }
            // Backup: bracket didn't fire
            if SERVER_BRACKETS_PRIMARY {
                let sl_breach = (sl - price) / TICK_SIZE;
                if sl_breach >= 2.0 {
                    let reason = classify_stop("LONG", ep, sl);
                    let _ = cmd_tx.send(TradeCommand::Exit { pos_uuid, reason, price, bar_idx: tick.bar_idx }).await;
                    return;
                }
                if price >= tp + 2.0 * TICK_SIZE && cur >= EXIT_MIN_TICKS {
                    let _ = cmd_tx.send(TradeCommand::Exit { pos_uuid, reason: "TARGET".to_string(), price, bar_idx: tick.bar_idx }).await;
                    return;
                }
            }
            // Scale out
            if SCALE_OUT_ENABLED && sz > 1 && !scale1_done && price >= tp && cur >= EXIT_MIN_TICKS {
                let _ = cmd_tx.send(TradeCommand::ScaleOut { pos_uuid, price, bar_idx: tick.bar_idx }).await;
            }
            // Trailing stop
            if ur >= TRAIL_ACTIVATE {
                let tl = ((bp - TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE).round() * TICK_SIZE;
                if tl > sl {
                    let _ = cmd_tx.send(TradeCommand::ModifyStop { pos_uuid, new_stop: tl }).await;
                }
            }
            // Breakeven
            if ur >= BREAKEVEN_TICKS && sl < ep {
                let _ = cmd_tx.send(TradeCommand::ModifyStop { pos_uuid, new_stop: ep }).await;
            }
            // TP drift update
            if entry_vwap > 0.0 && tp_buffer_ticks > 0.0 {
                let drift_ticks = (tick.vwap - entry_vwap).abs() / TICK_SIZE;
                if drift_ticks >= 3.0 {
                    let new_tp = ((tick.vwap + tp_buffer_ticks * TICK_SIZE) / TICK_SIZE).round() * TICK_SIZE;
                    if (new_tp - tp).abs() >= TICK_SIZE {
                        let _ = cmd_tx.send(TradeCommand::ModifyTp { pos_uuid, new_tp }).await;
                        if let Some(ref mut p) = bot.pos { p.entry_vwap = tick.vwap; }
                    }
                }
            }
            // Time stops
            if bt > TIME_STOP_MINS * 2 {
                let _ = cmd_tx.send(TradeCommand::Exit { pos_uuid, reason: "TIME STOP".to_string(), price, bar_idx: tick.bar_idx }).await;
            } else if bt > TIME_STOP_MINS {
                let _ = cmd_tx.send(TradeCommand::Exit { pos_uuid, reason: "TIME STOP".to_string(), price, bar_idx: tick.bar_idx }).await;
            } else if bt > TIME_STOP_MINS / 2 && cur < -4.0 {
                let _ = cmd_tx.send(TradeCommand::Exit { pos_uuid, reason: "TIME STOP".to_string(), price, bar_idx: tick.bar_idx }).await;
            }
        }
        Direction::Short => {
            // Gutter win
            if bot.gutter_win && bot.daily_pnl < GUTTER_GOAL {
                let gut_tp = ep - ((GUTTER_GOAL - bot.daily_pnl) / (sz as f64 * TICK_VALUE)) * TICK_SIZE;
                if price <= gut_tp {
                    let _ = cmd_tx.send(TradeCommand::Exit { pos_uuid, reason: "GUTTER WIN".to_string(), price: gut_tp, bar_idx: tick.bar_idx }).await;
                    return;
                }
            }
            // Gutter loss
            if bot.gutter_loss && bot.daily_pnl > -effective_dd {
                let remaining = (bot.daily_pnl + effective_dd) / (sz as f64 * TICK_VALUE);
                if remaining > 0.0 {
                    let gut_sl = ep + remaining * TICK_SIZE;
                    if price >= gut_sl {
                        let _ = cmd_tx.send(TradeCommand::Exit { pos_uuid, reason: "GUTTER LOSS".to_string(), price: gut_sl, bar_idx: tick.bar_idx }).await;
                        return;
                    }
                }
            }
            if !SERVER_BRACKETS_PRIMARY && price >= sl {
                let reason = classify_stop("SHORT", ep, sl);
                let _ = cmd_tx.send(TradeCommand::Exit { pos_uuid, reason, price, bar_idx: tick.bar_idx }).await;
                return;
            }
            if !SERVER_BRACKETS_PRIMARY && price <= tp && cur >= EXIT_MIN_TICKS {
                let _ = cmd_tx.send(TradeCommand::Exit { pos_uuid, reason: "TARGET".to_string(), price, bar_idx: tick.bar_idx }).await;
                return;
            }
            if SERVER_BRACKETS_PRIMARY {
                let sl_breach = (price - sl) / TICK_SIZE;
                if sl_breach >= 2.0 {
                    let reason = classify_stop("SHORT", ep, sl);
                    let _ = cmd_tx.send(TradeCommand::Exit { pos_uuid, reason, price, bar_idx: tick.bar_idx }).await;
                    return;
                }
                if price <= tp - 2.0 * TICK_SIZE && cur >= EXIT_MIN_TICKS {
                    let _ = cmd_tx.send(TradeCommand::Exit { pos_uuid, reason: "TARGET".to_string(), price, bar_idx: tick.bar_idx }).await;
                    return;
                }
            }
            if SCALE_OUT_ENABLED && sz > 1 && !scale1_done && price <= tp && cur >= EXIT_MIN_TICKS {
                let _ = cmd_tx.send(TradeCommand::ScaleOut { pos_uuid, price, bar_idx: tick.bar_idx }).await;
            }
            if ur >= TRAIL_ACTIVATE {
                let tl = ((bp + TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE).round() * TICK_SIZE;
                if tl < sl {
                    let _ = cmd_tx.send(TradeCommand::ModifyStop { pos_uuid, new_stop: tl }).await;
                }
            }
            if ur >= BREAKEVEN_TICKS && sl > ep {
                let _ = cmd_tx.send(TradeCommand::ModifyStop { pos_uuid, new_stop: ep }).await;
            }
            if bt > TIME_STOP_MINS * 2 || bt > TIME_STOP_MINS || (bt > TIME_STOP_MINS / 2 && cur < -4.0) {
                let _ = cmd_tx.send(TradeCommand::Exit { pos_uuid, reason: "TIME STOP".to_string(), price, bar_idx: tick.bar_idx }).await;
            }
        }
    }
}

fn classify_stop(dir: &str, ep: f64, sl: f64) -> String {
    let tol = TICK_SIZE * 0.25;
    if (sl - ep).abs() <= tol { return "BREAKEVEN STOP".to_string(); }
    match dir {
        "LONG" => if sl > ep { "TRAIL STOP" } else { "STOP LOSS" },
        _ => if sl < ep { "TRAIL STOP" } else { "STOP LOSS" },
    }.to_string()
}

pub fn normalize_exit_reason(reason: &str) -> String {
    let r = reason.trim().to_uppercase();
    let aliases: &[(&str, &str)] = &[("SL","STOP LOSS"),("TP","TARGET"),("STOP","STOP LOSS"),("SERVER","SERVER FILL")];
    let r = aliases.iter().find(|(k,_)| *k == r).map(|(_,v)| *v).unwrap_or(r.as_str());
    let valid = ["STOP LOSS","TRAIL STOP","BREAKEVEN STOP","TARGET","TIME STOP",
                 "END OF RTH","GUTTER WIN","GUTTER LOSS","SERVER FILL","END OF DATA","UNKNOWN","SCALE OUT"];
    if valid.contains(&r) { r.to_string() } else { "UNKNOWN".to_string() }
}
