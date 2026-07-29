use std::sync::Arc;
use tokio::sync::{mpsc, RwLock};
use eframe::egui::{self, Color32, FontId, Pos2, Rect, RichText, Rounding, Stroke, Vec2};
use egui_plot::{Bar as PlotBar, BarChart, HLine, Line, Plot, PlotPoints};
use uuid::Uuid;

use crate::config::{ACCOUNT_START_BALANCE, CONTRACT, PROFIT_TARGET, TICK_SIZE, TRAILING_MLL_DISTANCE};
use crate::types::{AppState, TradeCommand};

// ── Palette ───────────────────────────────────────────────────────────────────
const BG:      Color32 = Color32::from_rgb(14,  14,  20);
const PANEL:   Color32 = Color32::from_rgb(20,  20,  28);
const PANEL2:  Color32 = Color32::from_rgb(24,  24,  34);
const BORDER:  Color32 = Color32::from_rgb(50,  50,  70);
const DIM:     Color32 = Color32::from_rgb(90,  90, 110);
const MID:     Color32 = Color32::from_rgb(155,155, 175);
const GREEN:   Color32 = Color32::from_rgb(72,  210, 120);
const RED:     Color32 = Color32::from_rgb(220,  72,  72);
const YELLOW:  Color32 = Color32::from_rgb(220, 180,  60);
const BLUE:    Color32 = Color32::from_rgb(80,  140, 220);
const CYAN:    Color32 = Color32::from_rgb(72,  200, 210);
const MAGENTA: Color32 = Color32::from_rgb(180, 100, 220);
const ORANGE:  Color32 = Color32::from_rgb(220, 140,  60);
const WHITE:   Color32 = Color32::WHITE;

// ── Snapshot ──────────────────────────────────────────────────────────────────

#[derive(Clone, Default)]
struct BarSnap {
    ts: String, date: String,
    o: f64, h: f64, l: f64, c: f64,
    vwap: f64, v: f64,
    forming: bool,
}

#[derive(Clone, Default)]
pub struct Snap {
    date: String, time_et: String,
    bar_secs_left: u64, rth_mins_left: i64,
    connected: bool, rth: bool,
    bot_enabled: bool, dry_run: bool, is_locked_down: bool,
    last: f64, bid: f64, ask: f64, spread: f64,
    vwap: f64, vwap_std: f64, z_score: f64, vpoc: f64,
    atr: f64, atr_slope: f64,
    cum_delta: f64, sess_high: f64, sess_low: f64, total_volume: f64,
    vpoc_stable: bool,
    total_pnl: f64, daily_pnl: f64, hourly_pnl: f64, open_pnl: f64,
    max_dd: f64, trades_today: u32,
    account_balance: f64, mll_remaining: f64,
    gutter_win: bool, gutter_loss: bool,
    pos_dir: Option<String>, pos_ep: f64, pos_sl: f64, pos_tp: f64,
    pos_contracts: u32, #[allow(dead_code)] pos_bars_held: i64, pos_uuid: Option<Uuid>,
    regime_type: String, vwap_regime: String,
    vix_status: Option<String>, calendar_event: Option<String>,
    streak: i32, profit_factor: f64,
    win_count: usize, loss_count: usize, win_rate: f64,
    avg_win: f64, avg_loss: f64, expectancy: f64,
    bars: Vec<BarSnap>,
    vol_profile: Vec<(f64, f64)>,
    equity_curve: Vec<[f64; 2]>,
    z_history: Vec<f64>,
    delta_history: Vec<f64>,
    tape: Vec<(String, f64, f64, String)>,
    trade_history: Vec<(String, String, f64, f64, String)>,
    // (bar_index_within_visible_bars, price, "ENTER"/"EXIT", dir "LONG"/"SHORT", pnl)
    trade_markers: Vec<(i64, f64, String, String, f64)>,
    active_orders: Vec<String>,
    log: Vec<String>,
    strategy_name: String,
    total_bars: i64, // total bars in session so far (to convert bar_idx → offset)
    // System health
    feed_age_secs: u64,
    // State machine
    bot_state: String,
    // Execution quality
    avg_slippage_ticks: f64,
    pos_age_secs: u64,
    // Risk
    mll_loss_pct: f32, // 0.0 = no loss, 1.0 = at MLL floor
    // DOM ladder: (price, bid_vol, ask_vol) sorted price desc
    dom: Vec<(f64, f64, f64)>,
    // Previous session reference levels
    prev_close_lvl: Option<f64>,
    prev_vwap_lvl:  Option<f64>,
    prev_vpoc_lvl:  Option<f64>,
}

pub fn build_snap(state: &AppState) -> Snap {
    use chrono::TimeZone;
    let now_et = chrono_tz::America::New_York
        .from_utc_datetime(&chrono::Utc::now().naive_utc());
    let time_et  = now_et.format("%H:%M:%S ET").to_string();
    let date     = now_et.format("%Y-%m-%d").to_string();
    let tod_mins = now_et.format("%H").to_string().parse::<i64>().unwrap_or(0) * 60
        + now_et.format("%M").to_string().parse::<i64>().unwrap_or(0);
    let rth_mins_left   = (crate::config::POWER_HOUR_START - tod_mins).max(0);
    let bar_secs_left   = (60 - chrono::Utc::now().timestamp() % 60) as u64 % 60;

    let sess = state.session.as_ref();

    let bars: Vec<BarSnap> = {
        let mut v: Vec<BarSnap> = state.historical_bars.iter().map(|b| BarSnap {
            ts: b.ts.format("%H:%M").to_string(),
            date: b.ts.format("%m/%d").to_string(),
            o: b.o, h: b.h, l: b.l, c: b.c, vwap: b.vwap, v: b.v, forming: false,
        }).collect();
        if let Some(s) = sess {
            for b in &s.bars {
                v.push(BarSnap {
                    ts: b.ts.format("%H:%M").to_string(),
                    date: b.ts.format("%m/%d").to_string(),
                    o: b.o, h: b.h, l: b.l, c: b.c, vwap: b.vwap, v: b.v, forming: false,
                });
            }
            if let Some(ref cb) = s.cur_bar {
                v.push(BarSnap {
                    ts: cb.ts.get(11..16).unwrap_or("--:--").to_string(),
                    date: date.clone(),
                    o: cb.o, h: cb.h, l: cb.l, c: cb.c,
                    vwap: s.vwap, v: cb.v, forming: true,
                });
            }
        }
        v
    };

    let vol_profile: Vec<(f64, f64)> = sess.map(|s| {
        s.vol_profile.iter().map(|(k, v)| (k.0, *v)).collect()
    }).unwrap_or_default();

    let tape = sess.map(|s| s.tape.iter().rev().take(30).map(|t| {
        (t.time.clone(), t.price, t.vol, t.side.clone())
    }).collect()).unwrap_or_default();

    let trade_history = state.bot.trade_history.iter().rev().take(15).map(|t| {
        (t.action.clone(), t.dir.clone(), t.price, t.pnl, t.reason.clone())
    }).collect();

    let trade_markers: Vec<(i64, f64, String, String, f64)> = state.bot.trade_history.iter()
        .filter(|t| t.action == "ENTER" || t.action == "EXIT" || t.action == "SCALE")
        .map(|t| (t.bar_idx, t.price, t.action.clone(), t.dir.clone(), t.pnl))
        .collect();

    let total_bars = sess.map(|s| s.bars.len() as i64).unwrap_or(0)
        + state.historical_bars.len() as i64;

    let active_orders = state.bot.active_orders.iter().take(8).map(|o| {
        format!("{} {} @ {} [{:?}]",
            o["side"], o["type"],
            o["stopPrice"].as_f64().unwrap_or(0.0), o["status"])
    }).collect();

    let log = state.log.iter().rev().take(10).cloned().collect();

    let (pos_dir, pos_uuid, pos_ep, pos_sl, pos_tp, pos_contracts, pos_bars_held) =
        if let Some(ref p) = state.bot.pos {
            let bh = sess.map(|s| s.bars.len() as i64 - p.bar_idx).unwrap_or(0);
            (Some(p.dir.clone()), Some(p.uuid), p.ep, p.sl, p.tp, p.contracts_remaining, bh)
        } else { (None, None, 0.0, 0.0, 0.0, 0, 0) };

    let atr         = sess.map(|s| s.current_atr()).unwrap_or(0.0);
    let atr_slope   = sess.map(|s| s.get_atr_slope(10)).unwrap_or(0.0);
    let cum_delta   = sess.map(|s| s.cum_delta).unwrap_or(0.0);
    let sess_high   = sess.map(|s| s.high).unwrap_or(0.0);
    let sess_low    = sess.map(|s| s.low).unwrap_or(0.0);
    let total_volume = sess.map(|s| s.total_volume).unwrap_or(0.0);
    let vpoc_stable  = sess.map(|s| s.is_session_stable()).unwrap_or(false);
    let spread       = sess.map(|s| if s.ask > 0.0 && s.bid > 0.0 { s.ask - s.bid } else { 0.0 }).unwrap_or(0.0);

    // Performance metrics
    let hist = &state.bot.trade_history;
    let wins: Vec<f64>   = hist.iter().filter(|t| t.pnl > 0.0).map(|t| t.pnl).collect();
    let losses: Vec<f64> = hist.iter().filter(|t| t.pnl < 0.0).map(|t| t.pnl.abs()).collect();
    let win_count   = wins.len();
    let loss_count  = losses.len();
    let total_trades = win_count + loss_count;
    let win_rate    = if total_trades > 0 { win_count as f64 / total_trades as f64 } else { 0.0 };
    let avg_win     = if win_count > 0 { wins.iter().sum::<f64>() / win_count as f64 } else { 0.0 };
    let avg_loss    = if loss_count > 0 { losses.iter().sum::<f64>() / loss_count as f64 } else { 0.0 };
    let expectancy  = win_rate * avg_win - (1.0 - win_rate) * avg_loss;
    let pf_wins: f64  = wins.iter().sum();
    let pf_losses: f64 = losses.iter().sum();
    let profit_factor = if pf_losses > 0.0 { pf_wins / pf_losses } else if pf_wins > 0.0 { f64::INFINITY } else { 1.0 };

    let streak = {
        if hist.is_empty() { 0i32 } else {
            let ls = if hist.last().unwrap().pnl > 0.0 { 1i32 } else { -1i32 };
            let mut n = 0i32;
            for t in hist.iter().rev() {
                let s = if t.pnl > 0.0 { 1 } else { -1 };
                if s == ls { n += 1; } else { break; }
            }
            n * ls
        }
    };

    // Equity curve: index → cumulative pnl
    let equity_curve: Vec<[f64; 2]> = {
        let mut cum = ACCOUNT_START_BALANCE;
        hist.iter().enumerate().map(|(i, t)| {
            cum += t.pnl;
            [i as f64, cum]
        }).collect()
    };

    // Z-history and delta from bot snapshot
    let z_history    = state.bot.z_history.clone();
    let delta_history = state.bot.delta_history.clone();

    let feed_age_secs = {
        let now = chrono::Utc::now().timestamp() as u64;
        if state.last_feed_secs > 0 { now.saturating_sub(state.last_feed_secs) } else { 999 }
    };

    let mll_loss_pct = {
        let mll_room = TRAILING_MLL_DISTANCE;
        let remaining = state.bot.mll_remaining;
        ((mll_room - remaining.max(0.0)) / mll_room).clamp(0.0, 1.0) as f32
    };

    // DOM ladder sorted price descending (asks above, bids below mid)
    let dom = {
        let mut v: Vec<(f64, f64, f64)> = state.dom.iter()
            .map(|l| (l.price, l.bid_vol, l.ask_vol))
            .collect();
        v.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
        v
    };

    let prev_close_lvl = state.regime.prev_close;
    let prev_vwap_lvl  = state.regime.prev_vwap;
    let prev_vpoc_lvl  = state.regime.prev_vpoc;

    Snap {
        date, time_et, bar_secs_left, rth_mins_left,
        connected: state.connected, rth: state.rth,
        bot_enabled: state.bot_enabled, dry_run: state.dry_run,
        is_locked_down: state.bot.is_locked_down,
        last: state.live.last, bid: state.live.bid, ask: state.live.ask, spread,
        vwap: sess.map(|s| s.vwap).unwrap_or(0.0),
        vwap_std: sess.map(|s| s.vwap_std).unwrap_or(0.0),
        z_score: sess.map(|s| s.z_score()).unwrap_or(0.0),
        vpoc: sess.map(|s| s.vpoc).unwrap_or(0.0),
        atr, atr_slope, cum_delta, sess_high, sess_low, total_volume, vpoc_stable,
        total_pnl: state.bot.total_pnl,
        daily_pnl: state.bot.daily_pnl,
        hourly_pnl: state.bot.hourly_pnl,
        open_pnl: state.bot.open_pnl,
        max_dd: state.bot.max_dd,
        trades_today: state.bot.trades_today,
        account_balance: state.bot.account_balance,
        mll_remaining: state.bot.mll_remaining,
        gutter_win: state.bot.gutter_win,
        gutter_loss: state.bot.gutter_loss,
        pos_dir, pos_uuid, pos_ep, pos_sl, pos_tp, pos_contracts, pos_bars_held,
        regime_type: state.regime.session_type.clone(),
        vwap_regime: state.regime.vwap_regime.clone(),
        vix_status: state.regime.vix_status.clone(),
        calendar_event: state.regime.calendar_event.clone(),
        streak, profit_factor, win_count, loss_count, win_rate,
        avg_win, avg_loss, expectancy,
        bars, vol_profile, equity_curve, z_history, delta_history,
        tape, trade_history, trade_markers, active_orders, log,
        strategy_name: state.bot.strategy.clone(),
        total_bars,
        feed_age_secs,
        bot_state: state.bot.bot_state.clone(),
        avg_slippage_ticks: state.bot.avg_slippage_ticks,
        pos_age_secs: state.bot.pos_age_secs,
        mll_loss_pct,
        dom,
        prev_close_lvl,
        prev_vwap_lvl,
        prev_vpoc_lvl,
    }
}

// ── Chart view enum ───────────────────────────────────────────────────────────

#[derive(PartialEq, Clone, Copy)]
enum BottomChart { Equity, ZScore, Delta, Volume }

// ── App ───────────────────────────────────────────────────────────────────────

pub struct BotApp {
    state:            Arc<RwLock<AppState>>,
    cmd_tx:           mpsc::Sender<TradeCommand>,
    snap:             Snap,
    chart_zoom:       usize,
    chart_offset:     usize,
    chart_drag_accum: f32,
    // Y-axis manual control
    y_scale:          f64,   // >1 = more price range visible, <1 = zoomed in
    y_pan:            f64,   // price offset (positive = shift view upward)
    // Box-select zoom (right-drag)
    box_sel_start:    Option<egui::Pos2>,
    bottom_chart:     BottomChart,
    log_msgs:         Vec<String>,
    show_drawer:      bool,
}

impl BotApp {
    pub fn new(state: Arc<RwLock<AppState>>, cmd_tx: mpsc::Sender<TradeCommand>) -> Self {
        Self {
            state, cmd_tx, snap: Snap::default(),
            chart_zoom: 1, chart_offset: 0,
            chart_drag_accum: 0.0,
            y_scale: 1.0, y_pan: 0.0,
            box_sel_start: None,
            bottom_chart: BottomChart::Equity,
            log_msgs: Vec::new(),
            show_drawer: false,
        }
    }

    fn send_cmd(&self, cmd: TradeCommand) {
        let tx = self.cmd_tx.clone();
        tokio::spawn(async move { let _ = tx.send(cmd).await; });
    }
}

impl eframe::App for BotApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        if let Ok(st) = self.state.try_read() {
            self.snap = build_snap(&st);
        }
        ctx.request_repaint_after(std::time::Duration::from_millis(200));

        // Dark theme
        let mut vis = egui::Visuals::dark();
        vis.panel_fill       = BG;
        vis.window_fill      = BG;
        vis.extreme_bg_color = PANEL;
        vis.faint_bg_color   = PANEL2;
        vis.override_text_color = Some(MID);
        vis.widgets.noninteractive.bg_fill = PANEL;
        vis.widgets.inactive.bg_fill       = PANEL2;
        ctx.set_visuals(vis);

        // Arrow keys for pan; T = tape drawer; R = reset view
        let (toggle_drawer, reset_view) = ctx.input(|i| {
            if i.key_pressed(egui::Key::ArrowLeft)  { self.chart_offset = self.chart_offset.saturating_add(20); }
            if i.key_pressed(egui::Key::ArrowRight) { self.chart_offset = self.chart_offset.saturating_sub(20); }
            if i.key_pressed(egui::Key::Num0)       { self.chart_zoom = 1; self.chart_offset = 0; }
            (i.key_pressed(egui::Key::T), i.key_pressed(egui::Key::R))
        });
        if toggle_drawer { self.show_drawer = !self.show_drawer; }
        if reset_view { self.y_scale = 1.0; self.y_pan = 0.0; self.chart_zoom = 1; self.chart_offset = 0; }

        // ── Layout ────────────────────────────────────────────────────────────
        egui::TopBottomPanel::top("titlebar")
            .exact_height(28.0)
            .frame(egui::Frame::none().fill(Color32::from_rgb(10, 10, 16)))
            .show(ctx, |ui| self.draw_titlebar(ui));

        // Collapsible tape/log drawer (T key)
        let drawer_h = if self.show_drawer { 180.0 } else { 22.0 };
        egui::TopBottomPanel::bottom("bottom")
            .exact_height(drawer_h)
            .frame(panel_frame_style())
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    let lbl = if self.show_drawer { "▼ TAPE / LOG" } else { "▲ TAPE / LOG" };
                    if ui.add(egui::Button::new(RichText::new(lbl).color(DIM).size(10.0))
                        .fill(Color32::TRANSPARENT)
                        .stroke(Stroke::NONE)).clicked()
                    {
                        self.show_drawer = !self.show_drawer;
                    }
                    ui.label(RichText::new("  (T)").color(Color32::from_rgb(50,50,70)).size(9.0));
                });
                if self.show_drawer {
                    self.draw_bottom_panel(ui);
                }
            });

        egui::SidePanel::left("left")
            .exact_width(210.0)
            .frame(panel_frame_style())
            .show(ctx, |ui| self.draw_left_panel(ui));

        egui::SidePanel::right("right")
            .exact_width(240.0)
            .frame(panel_frame_style())
            .show(ctx, |ui| self.draw_right_panel(ui));

        egui::CentralPanel::default()
            .frame(egui::Frame::none().fill(BG))
            .show(ctx, |ui| self.draw_central(ui));
    }
}

// ── Title bar ─────────────────────────────────────────────────────────────────

impl BotApp {
    fn draw_titlebar(&self, ui: &mut egui::Ui) {
        let s = &self.snap;
        ui.horizontal_centered(|ui| {
            ui.add_space(8.0);
            ui.label(RichText::new(CONTRACT).color(WHITE).strong().monospace().size(13.0));
            sep(ui);
            let (dot, dc) = if s.connected { ("● LIVE", GREEN) } else { ("○ OFFLINE", RED) };
            ui.label(RichText::new(dot).color(dc).strong().size(12.0));
            sep(ui);
            ui.label(RichText::new(if s.rth { "RTH" } else { "ETH" }).color(CYAN).size(12.0));
            sep(ui);
            ui.label(RichText::new(&s.date).color(DIM).size(11.0));
            ui.label(RichText::new(&s.time_et).color(MID).size(11.0));
            sep(ui);
            let (bl, bc) = if !s.bot_enabled { ("BOT OFF", RED) }
                else if s.is_locked_down { ("LOCKED", ORANGE) }
                else { ("BOT ON", GREEN) };
            ui.label(RichText::new(bl).color(bc).strong().size(12.0));
            if s.dry_run {
                sep(ui);
                ui.label(RichText::new("DRY RUN").color(YELLOW).strong().size(12.0));
            }
            sep(ui);
            ui.label(RichText::new(format!("bar {:02}s", s.bar_secs_left)).color(DIM).size(10.0));
            sep(ui);
            // Feed health indicator
            let (feed_lbl, feed_col) = if s.feed_age_secs < 3 {
                (format!("● FEED {:0.0}s", s.feed_age_secs), GREEN)
            } else if s.feed_age_secs < 10 {
                (format!("◑ FEED {:0}s", s.feed_age_secs), YELLOW)
            } else {
                (format!("○ FEED {:0}s", s.feed_age_secs), RED)
            };
            ui.label(RichText::new(&feed_lbl).color(feed_col).strong().size(11.0));
            sep(ui);
            // Bot state heartbeat
            let (state_lbl, state_col) = match s.bot_state.as_str() {
                s if s.starts_with("MANAGING") => (s, CYAN),
                s if s.starts_with("TRAILING") => (s, GREEN),
                "ENTERING" => ("ENTERING", YELLOW),
                "COOLDOWN" => ("COOLDOWN", ORANGE),
                "HALTED"   => ("HALTED",   RED),
                _          => ("HUNTING",  DIM),
            };
            ui.label(RichText::new(state_lbl).color(state_col).strong().size(11.0));
        });
    }

    // ── Left panel: price + bot controls ─────────────────────────────────────

    fn draw_left_panel(&mut self, ui: &mut egui::Ui) {
        let s = self.snap.clone();
        egui::ScrollArea::vertical().show(ui, |ui| {
            ui.add_space(4.0);

            // Big last price
            ui.horizontal(|ui| {
                ui.label(RichText::new(format!("{:.2}", s.last))
                    .color(WHITE).strong().monospace().size(26.0));
                let chg = s.last - s.vwap;
                let cc = if chg >= 0.0 { GREEN } else { RED };
                ui.label(RichText::new(format!("{:+.2}", chg)).color(cc).size(12.0));
            });

            // DOM ladder
            if !s.dom.is_empty() {
                ui.add_space(2.0);
                // Split into asks (above mid) and bids (below mid)
                let mid = s.last;
                // asks: prices above mid, lowest 5 (closest to spread), shown high→low
                let asks: Vec<_> = {
                    let mut v: Vec<_> = s.dom.iter().filter(|(p,_,_)| *p > mid).collect();
                    v.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
                    v.truncate(5);
                    v.into_iter().rev().collect()
                };
                // bids: prices at/below mid, highest 5 (closest to spread), shown high→low
                let bids: Vec<_> = s.dom.iter().filter(|(p,_,_)| *p <= mid).take(5).collect();
                let max_vol = s.dom.iter()
                    .map(|(_, b, a)| b.max(*a))
                    .fold(0.01f64, f64::max);

                for (price, _bid, ask) in &asks {
                    let frac = (ask / max_vol).clamp(0.0, 1.0) as f32;
                    ui.horizontal(|ui| {
                        ui.label(RichText::new(format!("{:>8.2}", price)).color(RED).monospace().size(9.5));
                        let bar = "▓".repeat((frac * 8.0) as usize);
                        ui.label(RichText::new(format!("{:<8}", bar)).color(Color32::from_rgb(160,60,60)).monospace().size(9.5));
                        ui.label(RichText::new(format!("{:>5.0}", ask)).color(DIM).monospace().size(9.5));
                    });
                }
                // Mid separator
                ui.horizontal(|ui| {
                    ui.label(RichText::new(format!("── {:.2} ──", s.last)).color(Color32::from_rgb(60,60,90)).monospace().size(9.0));
                });
                for (price, bid, _ask) in &bids {
                    let frac = (bid / max_vol).clamp(0.0, 1.0) as f32;
                    ui.horizontal(|ui| {
                        ui.label(RichText::new(format!("{:>8.2}", price)).color(GREEN).monospace().size(9.5));
                        let bar = "▓".repeat((frac * 8.0) as usize);
                        ui.label(RichText::new(format!("{:<8}", bar)).color(Color32::from_rgb(40,120,70)).monospace().size(9.5));
                        ui.label(RichText::new(format!("{:>5.0}", bid)).color(DIM).monospace().size(9.5));
                    });
                }
                ui.add_space(2.0);
            }

            ui.add_space(2.0);
            section(ui, "MARKET");

            // MICROSTRUCTURE group
            data_subhead(ui, "MICROSTRUCTURE");
            row2(ui, "Bid",  &format!("{:.2}", s.bid),  GREEN,
                      "Ask",  &format!("{:.2}", s.ask),  RED);
            let dc = if s.cum_delta >= 0.0 { GREEN } else { RED };
            row2(ui, "Δ",    &format!("{:+.0}", s.cum_delta), dc,
                      "Spd",  &format!("{:.2}", s.spread), DIM);
            row2(ui, "Vol",  &vol_fmt(s.total_volume), DIM,
                      "H/L", &format!("{:.0}/{:.0}", s.sess_high, s.sess_low), MID);

            // VOLATILITY group
            data_subhead(ui, "VOLATILITY");
            row2(ui, "VWAP", &format!("{:.2}", s.vwap), BLUE,
                      "σ",    &format!("{:.2}", s.vwap_std), MID);
            let zc = if s.z_score.abs() > 1.5 { if s.z_score > 0.0 { RED } else { GREEN } }
                     else if s.z_score.abs() > 0.8 { YELLOW } else { MID };
            row2(ui, "Z",    &format!("{:+.2}", s.z_score), zc,
                      "VPOC", &format!("{:.2}", s.vpoc), CYAN);
            let ad = if s.atr_slope > 0.01 { "↑" } else if s.atr_slope < -0.01 { "↓" } else { "→" };
            let ac = if s.atr_slope > 0.01 { ORANGE } else { CYAN };
            row2(ui, "ATR",  &format!("{:.2}{ad}", s.atr), ac,
                      "RTH",  &format!("{}m", s.rth_mins_left), DIM);

            ui.add_space(6.0);
            section(ui, "CONTROLS");

            // Bot toggle
            let (bl2, bc2) = if s.bot_enabled && !s.is_locked_down { ("● BOT ON", GREEN) }
                else if s.is_locked_down { ("◆ LOCKED", ORANGE) }
                else { ("○ BOT OFF", RED) };
            if big_btn(ui, bl2, bc2) {
                let st = self.state.clone();
                tokio::spawn(async move { st.write().await.bot_enabled ^= true; });
            }

            let (dl, dc2) = if s.dry_run { ("● DRY RUN ON", YELLOW) } else { ("○ DRY RUN", DIM) };
            if big_btn(ui, dl, dc2) {
                let st = self.state.clone();
                tokio::spawn(async move { st.write().await.dry_run ^= true; });
            }

            let (gwl, gwc) = if s.gutter_win { ("● GUTTER WIN", GREEN) } else { ("○ GUTTER WIN", DIM) };
            if big_btn(ui, gwl, gwc) {
                let st = self.state.clone();
                tokio::spawn(async move { st.write().await.bot.gutter_win ^= true; });
            }

            let (gll, glc) = if s.gutter_loss { ("● GUTTER LOSS", GREEN) } else { ("○ GUTTER LOSS", DIM) };
            if big_btn(ui, gll, glc) {
                let st = self.state.clone();
                tokio::spawn(async move { st.write().await.bot.gutter_loss ^= true; });
            }

            ui.add_space(6.0);
            section(ui, "CHART");
            ui.horizontal(|ui| {
                if ui.button("−").clicked() { self.chart_zoom = (self.chart_zoom + 1).min(16); }
                ui.label(RichText::new(format!(" {}× ", self.chart_zoom)).color(MID).monospace());
                if ui.button("+").clicked() && self.chart_zoom > 1 { self.chart_zoom -= 1; }
                ui.add_space(4.0);
                if ui.button("↺ reset").clicked() { self.chart_zoom = 1; self.chart_offset = 0; }
            });
            ui.horizontal(|ui| {
                if ui.button("◀ pan").clicked() { self.chart_offset = self.chart_offset.saturating_add(20); }
                if ui.button("pan ▶").clicked() { self.chart_offset = self.chart_offset.saturating_sub(20); }
                ui.label(RichText::new(if self.chart_offset > 0 { format!("-{}", self.chart_offset) } else { "live".into() }).color(DIM).size(10.0));
            });

            if s.active_orders.is_empty() { return; }
            ui.add_space(6.0);
            section(ui, "ORDERS");
            for o in &s.active_orders {
                ui.label(RichText::new(o.as_str()).color(YELLOW).size(10.0).monospace());
            }
        });
    }

    // ── Right panel: execution-focused ───────────────────────────────────────

    fn draw_right_panel(&self, ui: &mut egui::Ui) {
        let s = &self.snap;
        egui::ScrollArea::vertical().show(ui, |ui| {
            ui.add_space(4.0);

            // ── KILL SWITCH — always visible, always first ────────────────────
            let kill_btn = egui::Button::new(
                RichText::new("⚡ FLATTEN & HALT").color(WHITE).size(12.0).strong().monospace())
                .fill(Color32::from_rgb(120, 20, 20))
                .stroke(Stroke::new(1.5, Color32::from_rgb(220, 60, 60)))
                .min_size(Vec2::new(ui.available_width(), 30.0));
            if ui.add(kill_btn).clicked() {
                if let Some(uuid) = s.pos_uuid {
                    self.send_cmd(TradeCommand::Exit {
                        pos_uuid: uuid, reason: "KILL SWITCH".into(),
                        price: s.last, bar_idx: -1,
                    });
                }
                let st = self.state.clone();
                tokio::spawn(async move {
                    let mut w = st.write().await;
                    w.bot_enabled = false;
                });
            }
            ui.add_space(4.0);

            // ── POSITION ─────────────────────────────────────────────────────
            section(ui, "POSITION");
            if let Some(ref dir) = s.pos_dir {
                let dc = if dir == "LONG" { GREEN } else { RED };
                ui.horizontal(|ui| {
                    ui.label(RichText::new(dir.as_str()).color(dc).strong().size(16.0));
                    ui.label(RichText::new(format!(" ×{}", s.pos_contracts)).color(MID));
                    // Position age
                    let age = if s.pos_age_secs >= 60 {
                        format!("  {}m{:02}s", s.pos_age_secs / 60, s.pos_age_secs % 60)
                    } else {
                        format!("  {}s", s.pos_age_secs)
                    };
                    let age_col = if s.pos_age_secs > 1200 { ORANGE } else { DIM };
                    ui.label(RichText::new(&age).color(age_col).size(10.0));
                });
                let sl_t = ((s.last - s.pos_sl).abs() / TICK_SIZE).round() as i64;
                let tp_t = ((s.pos_tp - s.last).abs() / TICK_SIZE).round() as i64;
                row2(ui, "Entry",  &format!("{:.2}", s.pos_ep), WHITE,
                          "Open",  &format!("${:+.2}", s.open_pnl), if s.open_pnl >= 0.0 { GREEN } else { RED });
                row2(ui, "SL",     &format!("{:.2} ({}t)", s.pos_sl, sl_t), RED,
                          "TP",    &format!("{:.2} ({}t)", s.pos_tp, tp_t), GREEN);
            } else {
                ui.horizontal(|ui| {
                    ui.label(RichText::new("FLAT").color(DIM).strong().size(14.0));
                    ui.add_space(8.0);
                    let sc = if s.streak > 0 { GREEN } else if s.streak < 0 { RED } else { DIM };
                    let ss = if s.streak == 0 { "—".into() }
                             else if s.streak > 0 { format!("{}W ↑", s.streak) }
                             else { format!("{}L ↓", s.streak.abs()) };
                    ui.label(RichText::new(ss).color(sc).strong().size(13.0));
                });
                row2(ui, "Trades",  &format!("{}", s.trades_today), MID,
                          "Today",  &format!("${:+.2}", s.daily_pnl), if s.daily_pnl >= 0.0 { GREEN } else { RED });
            }

            // ── P&L ──────────────────────────────────────────────────────────
            ui.add_space(6.0);
            section(ui, "P&L");
            row2(ui, "Daily",  &format!("${:+.2}", s.daily_pnl),  if s.daily_pnl >= 0.0 { GREEN } else { RED },
                      "Hour",  &format!("${:+.2}", s.hourly_pnl), if s.hourly_pnl >= 0.0 { GREEN } else { RED });
            row2(ui, "Total",  &format!("${:+.2}", s.total_pnl),  if s.total_pnl >= 0.0 { GREEN } else { RED },
                      "Bal",   &format!("${:.0}", s.account_balance), if s.account_balance >= ACCOUNT_START_BALANCE { GREEN } else { RED });
            let mc = if s.mll_remaining > 500.0 { GREEN } else if s.mll_remaining > 200.0 { YELLOW } else { RED };
            row2(ui, "MLL",    &format!("${:.0}", s.mll_remaining), mc,
                      "MaxDD", &format!("${:.0}", s.max_dd), RED);

            // Profit target progress bar (blue → green)
            let progress = ((s.account_balance - ACCOUNT_START_BALANCE) / PROFIT_TARGET).clamp(0.0, 1.0) as f32;
            let dist = (ACCOUNT_START_BALANCE + PROFIT_TARGET - s.account_balance).max(0.0);
            ui.label(RichText::new(format!("Pass target: -${:.0}  ({:.0}%)", dist, progress * 100.0)).color(DIM).size(10.0));
            let bar_col = Color32::from_rgb(
                ((1.0 - progress) * 80.0 + progress * 72.0) as u8,
                ((1.0 - progress) * 140.0 + progress * 210.0) as u8,
                ((1.0 - progress) * 220.0 + progress * 120.0) as u8,
            );
            ui.add(egui::ProgressBar::new(progress).fill(bar_col).desired_height(8.0));

            // Daily loss heat gauge (green → red as MLL is consumed)
            ui.add_space(2.0);
            let mll_lbl = format!("Daily loss risk: {:.0}%", s.mll_loss_pct * 100.0);
            let mll_gauge_col = Color32::from_rgb(
                (s.mll_loss_pct * 220.0) as u8,
                ((1.0 - s.mll_loss_pct) * 160.0) as u8,
                30,
            );
            ui.label(RichText::new(mll_lbl).color(if s.mll_loss_pct > 0.7 { RED } else { DIM }).size(10.0));
            ui.add(egui::ProgressBar::new(s.mll_loss_pct).fill(mll_gauge_col).desired_height(6.0));

            ui.add_space(6.0);
            section(ui, "PERFORMANCE");
            let wc = if s.win_rate >= 0.55 { GREEN } else if s.win_rate >= 0.45 { YELLOW } else { RED };
            row2(ui, "W/L",    &format!("{}/{}", s.win_count, s.loss_count), MID,
                      "Win%",  &format!("{:.0}%", s.win_rate * 100.0), wc);
            let pc = if s.profit_factor >= 1.5 { GREEN } else if s.profit_factor >= 1.0 { YELLOW } else { RED };
            let ps = if s.profit_factor == f64::INFINITY { "∞".into() } else { format!("{:.2}", s.profit_factor) };
            row2(ui, "AvgW",   &format!("${:.0}", s.avg_win),   GREEN,
                      "AvgL",  &format!("${:.0}", s.avg_loss),  RED);
            row2(ui, "PF",     &ps, pc,
                      "Exp",   &format!("${:.0}", s.expectancy), if s.expectancy >= 0.0 { GREEN } else { RED });
            // Slippage: negative = paid more than signal price (bad)
            let sc = if s.avg_slippage_ticks >= 0.0 { GREEN } else if s.avg_slippage_ticks > -1.0 { YELLOW } else { RED };
            let slip_str = if s.avg_slippage_ticks == 0.0 && s.win_count + s.loss_count == 0 {
                "—".to_string()
            } else {
                format!("{:+.1}t", s.avg_slippage_ticks)
            };
            row2(ui, "Slip",   &slip_str, sc,
                      "Trds",  &format!("{}", s.trades_today), MID);

            ui.add_space(6.0);
            section(ui, &format!("STRATEGY — {}", s.strategy_name));
            match s.strategy_name.as_str() {
                "VWAP Reclaim" => {
                    ui.label(RichText::new("ES · Fade VWAP crossovers").color(CYAN).size(10.0));
                    ui.add_space(2.0);
                    ui.label(RichText::new("LONG trigger:").color(GREEN).size(10.0).strong());
                    ui.label(RichText::new("  Price crosses BELOW VWAP on a 1-min bar.\n  SL = prev bar low − 2t\n  TP = VWAP + ATR").color(MID).size(10.0));
                    ui.add_space(2.0);
                    ui.label(RichText::new("SHORT trigger:").color(RED).size(10.0).strong());
                    ui.label(RichText::new("  Price crosses ABOVE VWAP on a 1-min bar.\n  SL = prev bar high + 2t\n  TP = VWAP − ATR").color(MID).size(10.0));
                    ui.add_space(2.0);
                    ui.label(RichText::new("Filters: 9:30–3:00 ET, skip 11:30–12:45,\nTP gap ≥ 6t, no entry if ATR stops invalid.").color(DIM).size(9.0));
                }
                "First Pullback" => {
                    ui.label(RichText::new("NQ · Enter pullback from VWAP extension").color(CYAN).size(10.0));
                    ui.add_space(2.0);
                    ui.label(RichText::new("LONG trigger:").color(GREEN).size(10.0).strong());
                    ui.label(RichText::new("  Z-score was > 1.8, now falling back\n  toward VWAP (still > 0.1).\n  SL = VWAP − ATR×0.8\n  TP = prev close or price + ATR×1.5").color(MID).size(10.0));
                    ui.add_space(2.0);
                    ui.label(RichText::new("SHORT trigger:").color(RED).size(10.0).strong());
                    ui.label(RichText::new("  Z-score was < −1.8, now rising back\n  toward VWAP (still < −0.1).\n  SL = VWAP + ATR×0.8\n  TP = prev close or price − ATR×1.5").color(MID).size(10.0));
                    ui.add_space(2.0);
                    ui.label(RichText::new("Filters: 9:30–3:00 ET, skip 11:30–12:45,\nTP gap ≥ 6t, direction cooldown after SL.").color(DIM).size(9.0));
                }
                _ => {}
            }
            ui.add_space(2.0);
            ui.label(RichText::new(format!("No-trade zones: lunch 11:30–12:45,\nmax {} trades/day, daily cap $1,499.", crate::config::MAX_TRADES)).color(DIM).size(9.0));

            ui.add_space(6.0);
            section(ui, "REGIME");
            let rc = match s.regime_type.as_str() { "TRENDING" => YELLOW, "RANGING" => BLUE, _ => MID };
            row2(ui, "Session", &s.regime_type, rc,
                      "VWAP",  &s.vwap_regime, CYAN);
            let vc = match s.vix_status.as_deref() { Some("EXTREME") => RED, Some("HIGH") => YELLOW, _ => GREEN };
            row2(ui, "VIX",    s.vix_status.as_deref().unwrap_or("N/A"), vc,
                      "VPOC",  if s.vpoc_stable { "stable" } else { "migrating" }, if s.vpoc_stable { GREEN } else { ORANGE });
            let rtc = if s.rth_mins_left < 30 { ORANGE } else { MID };
            row2(ui, "RTH",    &format!("{} min", s.rth_mins_left), rtc,
                      "Vol",   &vol_fmt(s.total_volume), DIM);
            if let Some(ref ev) = s.calendar_event {
                ui.label(RichText::new(format!("Event: {ev}")).color(MAGENTA).size(10.0));
            }

        });
    }

    // ── Central: candle chart + sub-charts ────────────────────────────────────

    fn draw_central(&mut self, ui: &mut egui::Ui) {
        let total_h = ui.available_height();
        let candle_h = (total_h * 0.62).max(100.0);
        let sub_h    = (total_h - candle_h - 32.0).max(80.0);

        // Candle chart (handles all interaction internally)
        ui.allocate_ui(Vec2::new(ui.available_width(), candle_h), |ui| {
            self.draw_candle_chart(ui);
        });

        // Sub-chart selector tabs
        ui.horizontal(|ui| {
            ui.add_space(4.0);
            for (label, view) in [("Equity", BottomChart::Equity), ("Z-Score", BottomChart::ZScore), ("Delta", BottomChart::Delta), ("Volume", BottomChart::Volume)] {
                let active = self.bottom_chart == view;
                let col = if active { WHITE } else { DIM };
                let btn = egui::Button::new(RichText::new(label).color(col).size(11.0))
                    .fill(if active { Color32::from_rgb(40,40,60) } else { PANEL })
                    .stroke(Stroke::new(1.0, if active { BORDER } else { Color32::TRANSPARENT }));
                if ui.add(btn).clicked() { self.bottom_chart = view; }
            }
        });

        // Sub-chart
        let _sub_rect = ui.allocate_ui(Vec2::new(ui.available_width(), sub_h), |ui| {
            match self.bottom_chart {
                BottomChart::Equity => self.draw_equity_chart(ui, sub_h),
                BottomChart::ZScore => self.draw_zscore_chart(ui, sub_h),
                BottomChart::Delta  => self.draw_delta_chart(ui, sub_h),
                BottomChart::Volume => self.draw_volume_bars(ui, sub_h),
            }
        });

    }

    // ── Candle chart ──────────────────────────────────────────────────────────

    fn draw_candle_chart(&mut self, ui: &mut egui::Ui) {
        let zoom    = self.chart_zoom.max(1);
        let offset  = self.chart_offset;
        let s       = &self.snap;
        let y_scale = self.y_scale;
        let y_pan        = self.y_pan;
        let box_sel_start = self.box_sel_start;

        // Output vars — written inside Frame::show, applied to self after
        let mut out_pan_dx:          f32                = 0.0;
        let mut out_y_pan_delta:     f64                = 0.0;
        let mut out_y_scale_factor:  f64                = 1.0;
        let mut out_x_scroll:        f32                = 0.0;
        let mut out_sec_down:        bool               = false;
        let mut out_sec_pos: Option<egui::Pos2>         = None;
        let mut out_y_reset:         bool               = false;
        let mut out_new_zoom:        Option<usize>      = None;
        let mut out_new_offset:      Option<usize>      = None;
        let mut out_new_y_scale:     Option<f64>        = None;
        let mut out_new_y_pan:       Option<f64>        = None;

        egui::Frame::none().fill(PANEL).stroke(Stroke::new(1.0, BORDER))
            .inner_margin(egui::Margin::same(0.0))
            .show(ui, |ui| {
                let rect = ui.available_rect_before_wrap();
                if rect.width() < 10.0 || rect.height() < 10.0 {
                    ui.allocate_rect(rect, egui::Sense::hover());
                    return;
                }
                let painter = ui.painter_at(rect);
                let w = rect.width();
                // Height split: candle area | gap | volume strip | time axis (18px)
                let h_total  = rect.height() - 18.0;
                let h_vol    = (h_total * 0.18).max(24.0);
                let h_candle = h_total - h_vol - 4.0;
                let chart_y0 = rect.min.y;
                let chart_y1 = chart_y0 + h_candle;
                let vol_y0   = chart_y1 + 4.0;
                let vol_y1   = chart_y0 + h_total;
                // Y-axis strip geometry (right 50 px) — defined early so all hlines can clip to it
                let y_strip_x = rect.max.x - 50.0;
                let y_strip = Rect::from_min_max(
                    Pos2::new(y_strip_x, rect.min.y),
                    Pos2::new(rect.max.x, vol_y1));
                painter.rect_filled(y_strip, Rounding::ZERO, Color32::from_rgb(18, 18, 26));
                painter.vline(y_strip_x, rect.min.y..=vol_y1, Stroke::new(0.5, BORDER));

                // Slice
                let end = s.bars.len().saturating_sub(offset);
                let raw = &s.bars[..end];

                // Aggregate
                struct Agg { o:f64,h:f64,l:f64,c:f64,vwap:f64,v:f64,date:String,ts:String,forming:bool }
                let n_fit = (w / 5.0) as usize; // ~5px per candle at 1× zoom
                let zoom_n = n_fit * zoom;
                let from = raw.len().saturating_sub(zoom_n);
                let raw = &raw[from..];
                let agged: Vec<Agg> = raw.chunks(zoom).map(|c| {
                    let vs: f64 = c.iter().map(|b| b.v).sum();
                    let vw = if vs > 0.0 { c.iter().map(|b| b.vwap * b.v).sum::<f64>() / vs }
                              else { c.last().map(|b| b.vwap).unwrap_or(0.0) };
                    Agg {
                        o: c.first().map(|b| b.o).unwrap_or(0.0),
                        c: c.last().map(|b| b.c).unwrap_or(0.0),
                        h: c.iter().map(|b| b.h).fold(f64::NEG_INFINITY, f64::max),
                        l: c.iter().map(|b| b.l).fold(f64::INFINITY, f64::min),
                        vwap: vw, v: vs,
                        date: c.last().map(|b| b.date.clone()).unwrap_or_default(),
                        ts:   c.last().map(|b| b.ts.clone()).unwrap_or_default(),
                        forming: c.last().map(|b| b.forming).unwrap_or(false),
                    }
                }).collect();

                if agged.is_empty() {
                    painter.text(rect.center(), egui::Align2::CENTER_CENTER,
                        "waiting for bars...", FontId::proportional(13.0), DIM);
                    ui.allocate_rect(rect, egui::Sense::hover());
                    return;
                }

                // Price range: candle-driven, then apply y_scale (zoom) and y_pan (shift)
                let max_h = agged.iter().map(|b| b.h).fold(f64::NEG_INFINITY, f64::max);
                let min_l = agged.iter().map(|b| b.l).fold(f64::INFINITY, f64::min);
                let auto_pad    = (max_h - min_l).max(0.5) * 0.06;
                let auto_center = (max_h + min_l) / 2.0 + y_pan;
                let half        = ((max_h - min_l) / 2.0 + auto_pad) * y_scale;
                let hi    = auto_center + half;
                let lo    = auto_center - half;
                let range = (hi - lo).max(0.01);

                let p2y = |p: f64| -> f32 {
                    (chart_y0 + (1.0 - (p - lo) / range) as f32 * h_candle).clamp(chart_y0, chart_y1)
                };

                // VWAP ±1σ and ±2σ bands (shaded)
                if s.vwap > 0.0 && s.vwap_std > 0.0 {
                    for (mult, alpha) in [(2.0f64, 18u8), (1.0, 28u8)] {
                        let y_hi = p2y(s.vwap + s.vwap_std * mult);
                        let y_lo = p2y(s.vwap - s.vwap_std * mult);
                        let band = Rect::from_min_max(
                            Pos2::new(rect.min.x, y_hi),
                            Pos2::new(y_strip_x, y_lo),
                        );
                        painter.rect_filled(band, Rounding::ZERO,
                            Color32::from_rgba_unmultiplied(80, 140, 220, alpha));
                    }
                }

                // Previous session reference levels (very faint — drawn first, behind everything)
                for (price_opt, label, rgba) in [
                    (s.prev_close_lvl, "PC",   [220u8, 180u8,  60u8, 45u8]),
                    (s.prev_vwap_lvl,  "PVWAP", [80u8, 140u8, 220u8, 45u8]),
                    (s.prev_vpoc_lvl,  "PPOC",  [72u8, 200u8, 210u8, 45u8]),
                ] {
                    if let Some(price) = price_opt {
                        if price >= lo && price <= hi {
                            let y = p2y(price);
                            painter.hline(rect.min.x..=y_strip_x, y,
                                Stroke::new(0.75, Color32::from_rgba_unmultiplied(rgba[0], rgba[1], rgba[2], rgba[3])));
                            painter.text(
                                Pos2::new(rect.min.x + 4.0, y - 2.0),
                                egui::Align2::LEFT_BOTTOM,
                                format!("{} {:.2}", label, price),
                                FontId::monospace(7.5),
                                Color32::from_rgba_unmultiplied(rgba[0], rgba[1], rgba[2], 110));
                        }
                    }
                }

                // Session H/L lines
                if s.sess_high > 0.0 {
                    let yh = p2y(s.sess_high);
                    let yl = p2y(s.sess_low);
                    painter.hline(rect.min.x..=y_strip_x, yh, Stroke::new(1.0, Color32::from_rgba_unmultiplied(220,180,60,60)));
                    painter.hline(rect.min.x..=y_strip_x, yl, Stroke::new(1.0, Color32::from_rgba_unmultiplied(80,140,220,60)));
                }

                // Position levels: EP (entry), SL, TP
                if s.pos_dir.is_some() {
                    let _is_long = s.pos_dir.as_deref() == Some("LONG");
                    for (price, col, label) in [
                        (s.pos_ep, YELLOW, "EP"),
                        (s.pos_sl, RED,    "SL"),
                        (s.pos_tp, GREEN,  "TP"),
                    ] {
                        if price > 0.0 && price >= lo && price <= hi {
                            let y = p2y(price);
                            // Dashed line across chart
                            let mut x = rect.min.x;
                            while x < y_strip_x - 4.0 {
                                painter.hline(x..=(x + 6.0).min(y_strip_x), y,
                                    Stroke::new(if label == "EP" { 1.5 } else { 1.0 }, col.linear_multiply(0.8)));
                                x += 10.0;
                            }
                            // Right-edge label chip
                            let chip_w = 48.0f32;
                            let chip_x = rect.max.x - chip_w - 2.0;
                            painter.rect_filled(
                                Rect::from_min_max(
                                    Pos2::new(chip_x, y - 7.0),
                                    Pos2::new(rect.max.x - 2.0, y + 7.0)),
                                Rounding::same(2.0),
                                col.linear_multiply(0.25));
                            painter.text(
                                Pos2::new(rect.max.x - 4.0, y),
                                egui::Align2::RIGHT_CENTER,
                                format!("{} {:.2}", label, price),
                                FontId::monospace(8.0), col);
                        }
                    }
                    // Distance ticks label near current price
                    if s.pos_sl > 0.0 && s.pos_tp > 0.0 && s.last > 0.0 {
                        let sl_t = ((s.last - s.pos_sl).abs() / TICK_SIZE).round() as i64;
                        let tp_t = ((s.pos_tp - s.last).abs() / TICK_SIZE).round() as i64;
                        let y = p2y(s.last);
                        painter.text(
                            Pos2::new(rect.min.x + 6.0, y - 10.0),
                            egui::Align2::LEFT_BOTTOM,
                            format!("TP {}t  SL {}t", tp_t, sl_t),
                            FontId::monospace(8.0), MID);
                    }
                }

                // Candles
                let cw = w / agged.len().max(1) as f32;
                let body_w = (cw * 0.48).max(1.5).min(16.0);
                let mut vwap_pts: Vec<Pos2> = Vec::new();

                for (i, bar) in agged.iter().enumerate() {
                    let cx = rect.min.x + (i as f32 + 0.5) * cw;
                    let bull = bar.c >= bar.o;
                    let (bc, wc) = if bar.forming {
                        if bull { (Color32::from_rgb(45,155,80), Color32::from_rgb(22,80,42)) }
                        else    { (Color32::from_rgb(160,50,50), Color32::from_rgb(85,25,25)) }
                    } else {
                        if bull { (Color32::from_rgb(72,214,112), Color32::from_rgb(36,110,58)) }
                        else    { (Color32::from_rgb(228,72,72),  Color32::from_rgb(120,36,36)) }
                    };

                    // Wick
                    painter.line_segment(
                        [Pos2::new(cx, p2y(bar.h)), Pos2::new(cx, p2y(bar.l))],
                        Stroke::new(1.0, wc));
                    // Body: dark outline first, colored fill on top
                    let by0 = p2y(bar.o.max(bar.c));
                    let by1 = p2y(bar.o.min(bar.c));
                    let body_rect = Rect::from_min_max(
                        Pos2::new(cx - body_w/2.0, by0),
                        Pos2::new(cx + body_w/2.0, (by1 + 1.5).max(by0 + 1.5)));
                    let outline = Rect::from_min_max(
                        Pos2::new(body_rect.min.x - 0.5, body_rect.min.y - 0.5),
                        Pos2::new(body_rect.max.x + 0.5, body_rect.max.y + 0.5));
                    painter.rect_filled(outline, Rounding::ZERO, Color32::from_rgb(8, 8, 14));
                    painter.rect_filled(body_rect, Rounding::ZERO, bc);

                    // VWAP accumulate
                    vwap_pts.push(if bar.vwap >= lo && bar.vwap <= hi {
                        Pos2::new(cx, p2y(bar.vwap))
                    } else {
                        Pos2::new(cx, f32::NAN)
                    });
                }

                // VWAP line
                draw_segmented_line(&painter, &vwap_pts, Stroke::new(1.5, CYAN));

                // VPOC dashed line
                if s.vpoc > 0.0 && s.vpoc >= lo && s.vpoc <= hi {
                    let y = p2y(s.vpoc);
                    let mut x = rect.min.x;
                    while x < rect.max.x - 4.0 {
                        painter.hline(x..=(x + 5.0).min(rect.max.x), y,
                            Stroke::new(1.0, Color32::from_rgba_unmultiplied(72, 200, 210, 130)));
                        x += 9.0;
                    }
                    painter.text(
                        Pos2::new(rect.min.x + 4.0, y - 2.0),
                        egui::Align2::LEFT_BOTTOM,
                        format!("POC {:.2}", s.vpoc),
                        FontId::monospace(8.0),
                        Color32::from_rgba_unmultiplied(72, 200, 210, 180));
                }

                // Volume profile histogram (right-anchored semi-transparent bars)
                if !s.vol_profile.is_empty() {
                    let max_bar_px = (w * 0.12).min(90.0); // up to 12% of chart width
                    let vp_max = s.vol_profile.iter()
                        .filter(|(p, _)| *p >= lo && *p <= hi)
                        .map(|(_, v)| *v)
                        .fold(0.01f64, f64::max);
                    // bar height = one tick in screen pixels, clamped so bars don't bleed together
                    let tick_px = ((TICK_SIZE / range) as f32 * h_candle).clamp(1.5, 14.0);
                    for (price, vol) in &s.vol_profile {
                        if *price < lo || *price > hi { continue; }
                        let y   = p2y(*price);
                        let bw  = (vol / vp_max * max_bar_px as f64) as f32;
                        let is_vpoc = (price - s.vpoc).abs() < 0.001;
                        let col = if is_vpoc {
                            Color32::from_rgba_unmultiplied(72, 200, 210, 90)
                        } else {
                            Color32::from_rgba_unmultiplied(80, 140, 220, 38)
                        };
                        painter.rect_filled(
                            Rect::from_min_max(
                                Pos2::new(y_strip_x - bw, y - tick_px / 2.0),
                                Pos2::new(y_strip_x,       y + tick_px / 2.0)),
                            Rounding::ZERO, col);
                    }
                }

                // Trade markers — entries (▲/▼) and exits (×)
                let visible_start_bar = s.total_bars - raw.len() as i64;
                for (bar_idx, price, action, dir, pnl) in &s.trade_markers {
                    let bar_pos = bar_idx - visible_start_bar;
                    if bar_pos < 0 || bar_pos >= raw.len() as i64 { continue; }
                    // Map to aggregated bar index
                    let agg_pos = (bar_pos as usize) / zoom;
                    if agg_pos >= agged.len() { continue; }
                    if *price < lo || *price > hi { continue; }
                    let cx = rect.min.x + (agg_pos as f32 + 0.5) * cw;
                    let py = p2y(*price);
                    let is_long = dir == "LONG";
                    match action.as_str() {
                        "ENTER" => {
                            // Triangle pointing in trade direction
                            let col = if is_long { GREEN } else { RED };
                            let (tip_y, base_y) = if is_long {
                                (py - 10.0, py)
                            } else {
                                (py + 10.0, py)
                            };
                            painter.add(egui::Shape::convex_polygon(
                                vec![
                                    Pos2::new(cx,        tip_y),
                                    Pos2::new(cx - 5.0,  base_y),
                                    Pos2::new(cx + 5.0,  base_y),
                                ],
                                col, Stroke::NONE));
                        }
                        "EXIT" | "SCALE" => {
                            // X marker, green if profit, red if loss
                            let col = if *pnl >= 0.0 { GREEN } else { RED };
                            let sz = 4.0f32;
                            painter.line_segment([Pos2::new(cx-sz, py-sz), Pos2::new(cx+sz, py+sz)], Stroke::new(1.5, col));
                            painter.line_segment([Pos2::new(cx+sz, py-sz), Pos2::new(cx-sz, py+sz)], Stroke::new(1.5, col));
                        }
                        _ => {}
                    }
                }

                // Live price extension
                let last_price = agged.last().map(|b| b.c).unwrap_or(0.0);
                let is_live = offset == 0 && agged.last().map(|b| b.forming).unwrap_or(false);
                if is_live && last_price >= lo && last_price <= hi {
                    let y = p2y(last_price);
                    let from_x = rect.min.x + agged.len() as f32 * cw;
                    if from_x < y_strip_x {
                        let mut x = from_x;
                        while x < y_strip_x {
                            painter.hline(x..=(x+3.0).min(y_strip_x), y,
                                Stroke::new(1.0, Color32::from_rgb(70,70,110)));
                            x += 6.0;
                        }
                    }
                }

                // Price scale — gridlines stop at y_strip_x; labels live inside the strip
                for &frac in &[0.0f64, 0.25, 0.5, 0.75, 1.0] {
                    let price = lo + range * frac;
                    let y = p2y(price);
                    painter.hline(rect.min.x..=y_strip_x, y,
                        Stroke::new(0.5, Color32::from_rgba_unmultiplied(60,60,80,80)));
                    painter.text(Pos2::new(rect.max.x - 2.0, y),
                        egui::Align2::RIGHT_CENTER,
                        format!("{:.2}", price), FontId::monospace(9.0),
                        if frac == 0.5 { MID } else { DIM });
                }
                // Current price label
                if is_live || last_price > 0.0 {
                    let y = p2y(last_price);
                    painter.rect_filled(
                        Rect::from_min_max(Pos2::new(rect.max.x - 58.0, y - 7.0), Pos2::new(rect.max.x, y + 7.0)),
                        Rounding::same(2.0), Color32::from_rgb(40, 60, 90));
                    painter.text(Pos2::new(rect.max.x - 4.0, y),
                        egui::Align2::RIGHT_CENTER,
                        format!("{:.2} ◄", last_price), FontId::monospace(9.0), WHITE);
                }

                // Time axis
                let label_step = (agged.len() / 10).max(1);
                let mut prev_date = String::new();
                for i in (0..agged.len()).step_by(label_step) {
                    let bar = &agged[i];
                    let x = rect.min.x + (i as f32 + 0.5) * cw;
                    let (lbl, lc) = if bar.date != prev_date {
                        prev_date.clone_from(&bar.date);
                        (bar.date.as_str(), CYAN)
                    } else { (bar.ts.as_str(), DIM) };
                    painter.text(Pos2::new(x, vol_y1 + 2.0), egui::Align2::CENTER_TOP,
                        lbl, FontId::monospace(9.0), lc);
                }

                // Volume bars (bottom strip, x-aligned with candles)
                {
                    let max_v = agged.iter().map(|b| b.v).fold(0.01f64, f64::max);
                    painter.hline(rect.min.x..=rect.max.x, vol_y0 - 1.0,
                        Stroke::new(0.5, Color32::from_rgba_unmultiplied(60, 60, 80, 100)));
                    for (i, bar) in agged.iter().enumerate() {
                        let cx = rect.min.x + (i as f32 + 0.5) * cw;
                        let vfrac = (bar.v / max_v).clamp(0.0, 1.0) as f32;
                        let vbar_h = (vfrac * h_vol).max(1.0);
                        let vc = if bar.c >= bar.o {
                            Color32::from_rgba_unmultiplied(72, 214, 112, 80)
                        } else {
                            Color32::from_rgba_unmultiplied(228, 72, 72, 80)
                        };
                        painter.rect_filled(
                            Rect::from_min_max(
                                Pos2::new(cx - body_w / 2.0, vol_y1 - vbar_h),
                                Pos2::new(cx + body_w / 2.0, vol_y1)),
                            Rounding::ZERO, vc);
                    }
                }

                // ── Interaction zones (y_strip already defined + painted above) ─
                let chart_area = Rect::from_min_max(rect.min, Pos2::new(y_strip_x, rect.max.y));
                let y_resp  = ui.allocate_rect(y_strip,    egui::Sense::click_and_drag());
                let response = ui.allocate_rect(chart_area, egui::Sense::click_and_drag());

                // Y-axis: drag up/down = pan, scroll = scale, dbl-click = reset
                if y_resp.hovered() {
                    ui.ctx().set_cursor_icon(egui::CursorIcon::ResizeVertical);
                }
                // drag_delta().y: negative = dragged up, positive = down
                // drag up = want to see HIGHER prices → positive y_pan
                let y_drag_px  = y_resp.drag_delta().y;
                let y_pan_delta = -(y_drag_px as f64) / h_candle as f64 * range;

                // Scroll on Y strip scales Y: scroll up = zoom in (less range)
                let y_scroll = ui.ctx().input(|i| {
                    if i.pointer.hover_pos().map(|p| y_strip.contains(p)).unwrap_or(false) {
                        i.smooth_scroll_delta.y
                    } else { 0.0 }
                });
                let y_scale_factor = if y_scroll > 0.0 { 0.92f64 } else if y_scroll < 0.0 { 1.08 } else { 1.0 };

                if y_resp.double_clicked() {
                    out_y_reset = true;
                }

                // 2. Main chart area — left drag = pan X, right drag = box select, scroll = zoom X
                // Pan X
                let pan_dx = response.drag_delta().x;

                // Scroll to zoom X
                let x_scroll = ui.ctx().input(|i| {
                    if i.pointer.hover_pos().map(|p| chart_area.contains(p)).unwrap_or(false) {
                        i.smooth_scroll_delta.y
                    } else { 0.0 }
                });

                // Box select via secondary (right) button drag
                let sec_down = ui.ctx().input(|i|
                    i.pointer.button_down(egui::PointerButton::Secondary)
                    && i.pointer.hover_pos().map(|p| chart_area.contains(p)).unwrap_or(false)
                );
                let sec_pos = ui.ctx().input(|i| i.pointer.hover_pos());

                // Draw active box selection / compute zoom on release
                if sec_down {
                    if let Some(start) = box_sel_start {
                        if let Some(cur) = sec_pos {
                            let sel = Rect::from_two_pos(start, cur);
                            painter.rect_filled(sel, Rounding::ZERO,
                                Color32::from_rgba_unmultiplied(220, 200, 60, 25));
                            painter.rect_stroke(sel, Rounding::ZERO,
                                Stroke::new(1.0, Color32::from_rgba_unmultiplied(220, 200, 60, 180)));
                            let p_top = hi - (sel.min.y - chart_y0) as f64 / h_candle as f64 * range;
                            let p_bot = hi - (sel.max.y - chart_y0) as f64 / h_candle as f64 * range;
                            painter.text(Pos2::new(sel.min.x + 2.0, sel.min.y + 2.0),
                                egui::Align2::LEFT_TOP,
                                format!("{:.2}", p_top), FontId::monospace(8.5),
                                Color32::from_rgba_unmultiplied(220, 200, 60, 200));
                            painter.text(Pos2::new(sel.min.x + 2.0, sel.max.y - 2.0),
                                egui::Align2::LEFT_BOTTOM,
                                format!("{:.2}", p_bot), FontId::monospace(8.5),
                                Color32::from_rgba_unmultiplied(220, 200, 60, 200));
                        }
                    }
                } else if let Some(start) = box_sel_start {
                    // Box just released — zoom to selection
                    if let Some(end_pos) = sec_pos {
                        let sel = Rect::from_two_pos(start, end_pos);
                        if sel.width() > 10.0 && sel.height() > 10.0 {
                            // X zoom: scale so the selected agg-candles fill the screen
                            let sel_agg = ((sel.width() / cw).max(1.0) as usize).max(1);
                            let new_zoom = ((sel_agg * zoom) / agged.len().max(1)).max(1);
                            out_new_zoom = Some(new_zoom);
                            // Offset: raw bars to the right of the selection's right edge
                            let right_agg_i = ((sel.max.x - rect.min.x) / cw).max(0.0).round() as usize;
                            let agg_from_right = agged.len().saturating_sub(right_agg_i);
                            out_new_offset = Some(offset + agg_from_right * zoom);
                            // Y zoom: fit the selected price range
                            let p_top = hi - (sel.min.y.max(chart_y0) - chart_y0) as f64 / h_candle as f64 * range;
                            let p_bot = hi - (sel.max.y.min(chart_y1) - chart_y0) as f64 / h_candle as f64 * range;
                            let sel_range = (p_top - p_bot).abs().max(0.01);
                            let auto_range = ((max_h - min_l).max(0.5) * 1.12).max(0.01);
                            out_new_y_scale = Some((y_scale * sel_range / auto_range).clamp(0.05, 20.0));
                            out_new_y_pan   = Some((p_top + p_bot) / 2.0 - (max_h + min_l) / 2.0);
                        }
                    }
                }

                // 3. Crosshair + OHLCV hover (main chart area only)
                if response.hovered() {
                    if let Some(hover_pos) = response.hover_pos() {
                        let bar_i = ((hover_pos.x - rect.min.x) / cw).max(0.0) as usize;
                        if bar_i < agged.len() {
                            let hbar = &agged[bar_i];
                            let cross_x = rect.min.x + (bar_i as f32 + 0.5) * cw;
                            painter.vline(cross_x, rect.min.y..=vol_y1,
                                Stroke::new(1.0, Color32::from_rgba_unmultiplied(220, 220, 255, 45)));
                            if hover_pos.y >= chart_y0 && hover_pos.y <= chart_y1 {
                                painter.hline(rect.min.x..=y_strip.min.x, hover_pos.y,
                                    Stroke::new(1.0, Color32::from_rgba_unmultiplied(220, 220, 255, 45)));
                                let hp = hi - (hover_pos.y - chart_y0) as f64 / h_candle as f64 * range;
                                // Price on Y axis strip
                                let ly = hover_pos.y.clamp(y_strip.min.y + 7.0, y_strip.max.y - 7.0);
                                painter.rect_filled(
                                    Rect::from_min_max(
                                        Pos2::new(y_strip.min.x, ly - 7.0),
                                        Pos2::new(y_strip.max.x, ly + 7.0)),
                                    Rounding::same(2.0), Color32::from_rgb(50, 80, 130));
                                painter.text(
                                    Pos2::new(y_strip.max.x - 3.0, ly),
                                    egui::Align2::RIGHT_CENTER,
                                    format!("{:.2}", hp), FontId::monospace(9.0), WHITE);
                            }
                            let ohlcv = format!("{}  O:{:.2}  H:{:.2}  L:{:.2}  C:{:.2}  V:{:.0}",
                                hbar.ts, hbar.o, hbar.h, hbar.l, hbar.c, hbar.v);
                            let box_right = (rect.min.x + 4.0 + 370.0).min(y_strip.min.x - 4.0);
                            painter.rect_filled(
                                Rect::from_min_max(
                                    Pos2::new(rect.min.x + 4.0, rect.min.y + 2.0),
                                    Pos2::new(box_right, rect.min.y + 15.0)),
                                Rounding::same(2.0),
                                Color32::from_rgba_unmultiplied(14, 14, 20, 210));
                            painter.text(Pos2::new(rect.min.x + 6.0, rect.min.y + 4.0),
                                egui::Align2::LEFT_TOP, &ohlcv, FontId::monospace(9.5), WHITE);
                        }
                    }
                } else {
                    // Normal title when not hovering
                    let y_hint = if y_scale != 1.0 || y_pan != 0.0 { "  [Y custom — R=reset]" } else { "" };
                    let title = format!("CANDLES  {}{}{}{}{}",
                        raw.len(),
                        if zoom > 1 { format!("  {}×", zoom) } else { String::new() },
                        if offset > 0 { format!("  −{offset}") } else { String::new() },
                        if is_live { "  ● live" } else { "" },
                        y_hint);
                    painter.text(Pos2::new(rect.min.x + 6.0, rect.min.y + 4.0),
                        egui::Align2::LEFT_TOP, &title, FontId::proportional(10.0), MID);
                }

                // Help hint (bottom-left of chart)
                painter.text(
                    Pos2::new(rect.min.x + 4.0, chart_y1 - 3.0),
                    egui::Align2::LEFT_BOTTOM,
                    "drag=pan  right-drag=zoom  scroll=X  Y-axis=pan/scale  R=reset",
                    FontId::proportional(8.5),
                    Color32::from_rgba_unmultiplied(90, 90, 110, 140));

                out_pan_dx         = pan_dx;
                out_y_pan_delta    = y_pan_delta;
                out_y_scale_factor = y_scale_factor;
                out_x_scroll       = x_scroll;
                out_sec_down       = sec_down;
                out_sec_pos        = sec_pos;
            });

        // ── Apply interaction results to self ─────────────────────────────────
        // Pan X: accumulate sub-bar fractional deltas
        self.chart_drag_accum += out_pan_dx / (8.0f32 * zoom as f32).max(1.0);
        let whole = self.chart_drag_accum as i32;
        if whole != 0 {
            self.chart_drag_accum -= whole as f32;
            if whole > 0 { self.chart_offset += whole as usize; }
            else { self.chart_offset = self.chart_offset.saturating_sub((-whole) as usize); }
        }
        // Zoom X (scroll on chart area)
        if out_x_scroll > 0.0 { self.chart_zoom = (self.chart_zoom + 1).min(64); }
        else if out_x_scroll < 0.0 && self.chart_zoom > 1 { self.chart_zoom -= 1; }
        // Pan/scale Y
        self.y_pan += out_y_pan_delta;
        if out_y_scale_factor != 1.0 {
            self.y_scale = (self.y_scale * out_y_scale_factor).clamp(0.05, 20.0);
        }
        // Box select state
        if out_sec_down {
            if self.box_sel_start.is_none() { self.box_sel_start = out_sec_pos; }
        } else {
            self.box_sel_start = None;
        }
        // Box-select zoom (computed inside closure with full geometry)
        if let Some(nz) = out_new_zoom    { self.chart_zoom   = nz; }
        if let Some(no) = out_new_offset  { self.chart_offset = no; }
        if let Some(ns) = out_new_y_scale { self.y_scale      = ns; }
        if let Some(np) = out_new_y_pan   { self.y_pan        = np; }
        // Y-axis double-click reset
        if out_y_reset { self.y_scale = 1.0; self.y_pan = 0.0; }
    }

    // ── Sub-charts ────────────────────────────────────────────────────────────

    fn draw_equity_chart(&self, ui: &mut egui::Ui, h: f32) {
        let s = &self.snap;
        let pts: Vec<[f64;2]> = s.equity_curve.clone();
        let baseline = ACCOUNT_START_BALANCE;
        sub_chart_frame(ui, "EQUITY CURVE", h, |ui| {
            if pts.len() < 2 {
                ui.centered_and_justified(|ui| { ui.label(RichText::new("no trades yet").color(DIM)); });
                return;
            }
            Plot::new("equity")
                .height(h - 28.0)
                .show_axes([false, true])
                .allow_zoom(false).allow_drag(false).allow_scroll(false)
                .show(ui, |plot_ui| {
                    let col = if pts.last().map(|p| p[1]).unwrap_or(baseline) >= baseline { GREEN } else { RED };
                    plot_ui.line(Line::new(PlotPoints::from(pts)).color(col).width(1.5));
                    plot_ui.hline(HLine::new(baseline).color(DIM).width(1.0));
                });
        });
    }

    fn draw_zscore_chart(&self, ui: &mut egui::Ui, h: f32) {
        let s = &self.snap;
        let pts: Vec<[f64;2]> = s.z_history.iter().enumerate()
            .map(|(i, &z)| [i as f64, z]).collect();
        sub_chart_frame(ui, "Z-SCORE", h, |ui| {
            if pts.len() < 2 {
                ui.centered_and_justified(|ui| { ui.label(RichText::new("no z-history").color(DIM)); });
                return;
            }
            Plot::new("zscore")
                .height(h - 28.0)
                .show_axes([false, true])
                .allow_zoom(false).allow_drag(false).allow_scroll(false)
                .show(ui, |plot_ui| {
                    plot_ui.line(Line::new(PlotPoints::from(pts)).color(CYAN).width(1.5));
                    plot_ui.hline(HLine::new(1.5).color(RED).width(1.0).style(egui_plot::LineStyle::Dashed { length: 6.0 }));
                    plot_ui.hline(HLine::new(-1.5).color(GREEN).width(1.0).style(egui_plot::LineStyle::Dashed { length: 6.0 }));
                    plot_ui.hline(HLine::new(0.0).color(DIM).width(0.5));
                });
        });
    }

    fn draw_delta_chart(&self, ui: &mut egui::Ui, h: f32) {
        let s = &self.snap;
        let pts: Vec<[f64;2]> = s.delta_history.iter().enumerate()
            .map(|(i, &d)| [i as f64, d]).collect();
        sub_chart_frame(ui, "CUMULATIVE DELTA", h, |ui| {
            if pts.len() < 2 {
                ui.centered_and_justified(|ui| { ui.label(RichText::new("no delta history").color(DIM)); });
                return;
            }
            Plot::new("delta")
                .height(h - 28.0)
                .show_axes([false, true])
                .allow_zoom(false).allow_drag(false).allow_scroll(false)
                .show(ui, |plot_ui| {
                    let col = if pts.last().map(|p| p[1]).unwrap_or(0.0) >= 0.0 { GREEN } else { RED };
                    plot_ui.line(Line::new(PlotPoints::from(pts)).color(col).width(1.5));
                    plot_ui.hline(HLine::new(0.0).color(DIM).width(0.5));
                });
        });
    }

    fn draw_volume_bars(&self, ui: &mut egui::Ui, h: f32) {
        let s = &self.snap;
        // Take last 100 bars
        let bars: Vec<Bar> = s.bars.iter().rev().take(100).rev().cloned()
            .collect::<Vec<_>>();
        sub_chart_frame(ui, "VOLUME", h, |ui| {
            if bars.is_empty() {
                ui.centered_and_justified(|ui| { ui.label(RichText::new("no bars").color(DIM)); });
                return;
            }
            let vol_bars: Vec<egui_plot::Bar> = bars.iter().enumerate().map(|(i, b)| {
                let col = if b.c >= b.o { GREEN } else { RED };
                PlotBar::new(i as f64, b.v).fill(col).width(0.8)
            }).collect();
            Plot::new("volume")
                .height(h - 28.0)
                .show_axes([false, true])
                .allow_zoom(false).allow_drag(false).allow_scroll(false)
                .show(ui, |plot_ui| {
                    plot_ui.bar_chart(BarChart::new(vol_bars));
                });
        });
    }

    // ── Bottom panel: tape + fills + log ─────────────────────────────────────

    fn draw_bottom_panel(&mut self, ui: &mut egui::Ui) {
        ui.columns(3, |cols| {
            // Tape
            cols[0].label(RichText::new("TAPE").color(MID).strong().size(10.0));
            cols[0].separator();
            egui::ScrollArea::vertical().id_salt("tape").max_height(140.0).show(&mut cols[0], |ui| {
                for (time, price, vol, side) in &self.snap.tape {
                    let (col, sym) = match side.as_str() { "BUY" => (GREEN, "▲"), "SELL" => (RED, "▼"), _ => (DIM, "·") };
                    ui.horizontal(|ui| {
                        ui.label(RichText::new(time.as_str()).color(DIM).monospace().size(9.0));
                        ui.label(RichText::new(format!("{:>9.2}", price)).color(WHITE).monospace().size(9.0));
                        ui.label(RichText::new(format!("{:>6.0}", vol)).color(DIM).monospace().size(9.0));
                        ui.label(RichText::new(sym).color(col).size(9.0));
                    });
                }
            });

            // Fills
            cols[1].label(RichText::new("FILLS").color(MID).strong().size(10.0));
            cols[1].separator();
            egui::ScrollArea::vertical().id_salt("fills").max_height(140.0).show(&mut cols[1], |ui| {
                for (_action, dir, price, pnl, reason) in &self.snap.trade_history {
                    let pc = if *pnl >= 0.0 { GREEN } else { RED };
                    let dc = match dir.as_str() { "LONG" => GREEN, "SHORT" => RED, _ => MID };
                    ui.horizontal(|ui| {
                        ui.label(RichText::new(dir.as_str()).color(dc).monospace().size(9.0));
                        ui.label(RichText::new(format!("{:.2}", price)).color(MID).monospace().size(9.0));
                        ui.label(RichText::new(format!("${:+.2}", pnl)).color(pc).monospace().size(9.0));
                        ui.label(RichText::new(reason.as_str()).color(DIM).size(9.0));
                    });
                }
            });

            // Log
            cols[2].label(RichText::new("LOG").color(MID).strong().size(10.0));
            cols[2].separator();
            let mut all_log = self.log_msgs.clone();
            all_log.extend(self.snap.log.clone());
            all_log.truncate(12);
            egui::ScrollArea::vertical().id_salt("log").max_height(140.0).show(&mut cols[2], |ui| {
                for msg in &all_log {
                    ui.label(RichText::new(msg.as_str()).color(DIM).size(9.0));
                }
            });
        });
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

// egui uses BarSnap alias in draw_volume_bars; resolve to our BarSnap
type Bar = BarSnap;

fn panel_frame_style() -> egui::Frame {
    egui::Frame::none()
        .fill(BG)
        .inner_margin(egui::Margin::same(6.0))
        .stroke(Stroke::new(0.0, Color32::TRANSPARENT))
}

fn sub_chart_frame(ui: &mut egui::Ui, title: &str, _h: f32, content: impl FnOnce(&mut egui::Ui)) {
    egui::Frame::none().fill(PANEL).stroke(Stroke::new(1.0, BORDER))
        .inner_margin(egui::Margin::same(4.0))
        .show(ui, |ui| {
            ui.label(RichText::new(title).color(MID).strong().size(10.0));
            content(ui);
        });
}

fn section(ui: &mut egui::Ui, title: &str) {
    ui.label(RichText::new(title).color(MID).strong().size(10.5));
    ui.separator();
}

fn data_subhead(ui: &mut egui::Ui, title: &str) {
    ui.add_space(3.0);
    ui.label(RichText::new(title).color(Color32::from_rgb(58, 58, 78)).size(8.0).monospace());
    ui.add(egui::Separator::default().spacing(1.0));
}

fn row2(ui: &mut egui::Ui, l1: &str, v1: &str, c1: Color32, l2: &str, v2: &str, c2: Color32) {
    ui.horizontal(|ui| {
        ui.label(RichText::new(format!("{:<6}", l1)).color(DIM).monospace().size(10.5));
        ui.label(RichText::new(v1).color(c1).monospace().size(10.5));
        ui.add_space(6.0);
        ui.label(RichText::new(format!("{:<6}", l2)).color(DIM).monospace().size(10.5));
        ui.label(RichText::new(v2).color(c2).monospace().size(10.5));
    });
}

fn big_btn(ui: &mut egui::Ui, label: &str, col: Color32) -> bool {
    let btn = egui::Button::new(RichText::new(label).color(col).size(11.0).monospace())
        .fill(Color32::from_rgb(24, 24, 36))
        .stroke(Stroke::new(1.0, col.linear_multiply(0.4)))
        .min_size(Vec2::new(ui.available_width(), 24.0));
    ui.add(btn).clicked()
}

fn sep(ui: &mut egui::Ui) {
    ui.add(egui::Separator::default().vertical().spacing(8.0));
}

fn vol_fmt(v: f64) -> String {
    if v >= 1_000_000.0 { format!("{:.1}M", v / 1_000_000.0) }
    else if v >= 1_000.0 { format!("{:.0}K", v / 1_000.0) }
    else { format!("{:.0}", v) }
}

fn draw_segmented_line(painter: &egui::Painter, pts: &[Pos2], stroke: Stroke) {
    let mut seg: Vec<Pos2> = Vec::new();
    for p in pts {
        if p.y.is_nan() {
            if seg.len() >= 2 { painter.add(egui::Shape::line(seg.clone(), stroke)); }
            seg.clear();
        } else {
            seg.push(*p);
        }
    }
    if seg.len() >= 2 { painter.add(egui::Shape::line(seg, stroke)); }
}

// ── Entry point ───────────────────────────────────────────────────────────────

pub fn run(state: Arc<RwLock<AppState>>, cmd_tx: mpsc::Sender<TradeCommand>) -> anyhow::Result<()> {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_title(format!("ES Bot — {CONTRACT}"))
            .with_inner_size([1440.0, 920.0])
            .with_min_inner_size([960.0, 640.0])
            .with_app_id("es-bot"),
        ..Default::default()
    };
    eframe::run_native("ES Bot", options,
        Box::new(move |_cc| Ok(Box::new(BotApp::new(state, cmd_tx)))))
        .map_err(|e| anyhow::anyhow!("{e}"))?;
    Ok(())
}
