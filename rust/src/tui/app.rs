use std::sync::Arc;
use std::time::Duration;

use crossterm::{
    event::{self, Event, KeyCode, KeyEventKind},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{
    backend::CrosstermBackend,
    layout::{Alignment, Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Cell, List, ListItem, Paragraph, Row, Table},
    Frame, Terminal,
};
use tokio::sync::{mpsc, watch, RwLock};
use uuid::Uuid;

use crate::config::{ACCOUNT_START_BALANCE, CONTRACT, PROFIT_TARGET, TICK_SIZE};
use crate::types::{AppState, TradeCommand};

// ── Palette ────────────────────────────────────────────────────────────────────
const BG:       Color = Color::Rgb(18, 18, 24);
const PANEL_BG: Color = Color::Rgb(22, 22, 30);
const BORDER:   Color = Color::Rgb(60, 60, 80);
const DIM:      Color = Color::Rgb(90, 90, 110);
const MID:      Color = Color::Rgb(160, 160, 180);
const CYAN:     Color = Color::Cyan;
const GREEN:    Color = Color::Rgb(80, 200, 120);
const RED:      Color = Color::Rgb(220, 80, 80);
const YELLOW:   Color = Color::Rgb(220, 180, 60);
const BLUE:     Color = Color::Rgb(80, 140, 220);
const MAGENTA:  Color = Color::Rgb(180, 100, 220);
const ORANGE:   Color = Color::Rgb(220, 140, 60);

fn panel(title: &str) -> Block<'_> {
    Block::default()
        .title(Span::styled(format!(" {title} "), Style::default().fg(MID).add_modifier(Modifier::BOLD)))
        .borders(Borders::ALL)
        .border_style(Style::default().fg(BORDER))
        .style(Style::default().bg(PANEL_BG))
}

fn panel_col(title: &str, color: Color) -> Block<'_> {
    Block::default()
        .title(Span::styled(format!(" {title} "), Style::default().fg(color).add_modifier(Modifier::BOLD)))
        .borders(Borders::ALL)
        .border_style(Style::default().fg(color))
        .style(Style::default().bg(PANEL_BG))
}

// ── Entry point ────────────────────────────────────────────────────────────────

pub enum AppMode { Quit, LaunchGui }

pub async fn run(
    state: Arc<RwLock<AppState>>,
    mut shutdown: watch::Receiver<bool>,
    cmd_tx: mpsc::Sender<TradeCommand>,
) -> anyhow::Result<AppMode> {
    enable_raw_mode()?;
    let mut stdout = std::io::stdout();
    execute!(stdout, EnterAlternateScreen)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;
    terminal.hide_cursor()?;

    let mode = event_loop(&mut terminal, state, &mut shutdown, cmd_tx).await?;

    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    terminal.show_cursor()?;
    Ok(mode)
}

async fn event_loop(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    state: Arc<RwLock<AppState>>,
    shutdown: &mut watch::Receiver<bool>,
    cmd_tx: mpsc::Sender<TradeCommand>,
) -> anyhow::Result<AppMode> {
    let mut log_msgs:  Vec<String> = Vec::new();
    let mut show_help  = false;
    let mut chart_zoom = 1usize;
    let mut chart_off  = 0usize;
    let mut mode = AppMode::Quit;

    loop {
        if *shutdown.borrow() { break; }

        let snap = state.read().await.clone_snapshot();
        terminal.draw(|f| render(f, &snap, &log_msgs, show_help, chart_zoom, chart_off))?;

        if event::poll(Duration::from_millis(200))? {
            if let Event::Key(key) = event::read()? {
                if key.kind != KeyEventKind::Press { continue; }

                match key.code {
                    KeyCode::Char('q') | KeyCode::Char('Q') | KeyCode::Esc => {
                        if show_help { show_help = false; } else { break; }
                    }
                    KeyCode::Char('g') | KeyCode::Char('G') => {
                        mode = AppMode::LaunchGui;
                        break;
                    }
                    KeyCode::Char('?') | KeyCode::Char('/') => {
                        show_help = !show_help;
                    }
                    KeyCode::Char('b') | KeyCode::Char('B') => {
                        let mut st = state.write().await;
                        st.bot_enabled = !st.bot_enabled;
                        let on = st.bot_enabled;
                        drop(st);
                        push_log(&mut log_msgs, format!("Bot {}", if on { "ENABLED" } else { "DISABLED" }));
                    }
                    KeyCode::Char('d') | KeyCode::Char('D') => {
                        let mut st = state.write().await;
                        st.dry_run = !st.dry_run;
                        let on = st.dry_run;
                        drop(st);
                        push_log(&mut log_msgs, format!("Dry run {}", if on { "ON — orders simulated" } else { "OFF — live orders" }));
                    }
                    KeyCode::Char('w') | KeyCode::Char('W') => {
                        let mut st = state.write().await;
                        st.bot.gutter_win = !st.bot.gutter_win;
                        let on = st.bot.gutter_win;
                        drop(st);
                        push_log(&mut log_msgs, format!("Gutter Win {}", if on { "ON" } else { "OFF" }));
                    }
                    KeyCode::Char('l') | KeyCode::Char('L') => {
                        let mut st = state.write().await;
                        st.bot.gutter_loss = !st.bot.gutter_loss;
                        let on = st.bot.gutter_loss;
                        drop(st);
                        push_log(&mut log_msgs, format!("Gutter Loss {}", if on { "ON" } else { "OFF" }));
                    }
                    KeyCode::Char('f') | KeyCode::Char('F') => {
                        let (pos_uuid, price) = {
                            let st = state.read().await;
                            (st.bot.pos.as_ref().map(|p| p.uuid), st.live.last)
                        };
                        if let Some(pos_uuid) = pos_uuid {
                            let _ = cmd_tx.send(TradeCommand::Exit {
                                pos_uuid,
                                reason: "MANUAL FLATTEN".to_string(),
                                price,
                                bar_idx: -1,
                            }).await;
                            push_log(&mut log_msgs, "FLATTEN sent".to_string());
                        } else {
                            push_log(&mut log_msgs, "No open position to flatten".to_string());
                        }
                    }
                    // ── Chart controls ────────────────────────────────────────
                    KeyCode::Char('-') | KeyCode::Char('_') => {
                        chart_zoom = (chart_zoom + 1).min(8);
                    }
                    KeyCode::Char('+') | KeyCode::Char('=') => {
                        if chart_zoom > 1 { chart_zoom -= 1; }
                    }
                    KeyCode::Left => {
                        chart_off = chart_off.saturating_add(20);
                    }
                    KeyCode::Right => {
                        chart_off = chart_off.saturating_sub(20);
                    }
                    KeyCode::Char('0') => {
                        chart_zoom = 1;
                        chart_off  = 0;
                        push_log(&mut log_msgs, "Chart reset".to_string());
                    }
                    _ => {}
                }
            }
        }
    }
    Ok(mode)
}

fn push_log(log: &mut Vec<String>, msg: String) {
    log.push(msg);
    if log.len() > 100 { log.remove(0); }
}

// ── Chart aggregation (zoom-out merges N bars → 1 column) ─────────────────────

struct AggBar { o: f64, h: f64, l: f64, c: f64, vwap: f64, forming: bool }

fn agg_bars(chunk: &[BarUi]) -> AggBar {
    let o   = chunk.first().map(|b| b.o).unwrap_or(0.0);
    let c   = chunk.last().map(|b| b.c).unwrap_or(0.0);
    let h   = chunk.iter().map(|b| b.h).fold(f64::NEG_INFINITY, f64::max);
    let l   = chunk.iter().map(|b| b.l).fold(f64::INFINITY,     f64::min);
    let vs: f64 = chunk.iter().map(|b| b.v).sum();
    let vwap = if vs > 0.0 {
        chunk.iter().map(|b| b.vwap * b.v).sum::<f64>() / vs
    } else {
        chunk.iter().map(|b| b.vwap).sum::<f64>() / chunk.len().max(1) as f64
    };
    AggBar { o, h, l, c, vwap, forming: chunk.last().map(|b| b.forming).unwrap_or(false) }
}

// ── Snapshot ───────────────────────────────────────────────────────────────────

struct BarUi { ts: String, date: String, o: f64, h: f64, l: f64, c: f64, vwap: f64, v: f64, forming: bool }

#[allow(dead_code)]
struct UiSnapshot {
    date: String,
    time_et: String,
    rth_mins_left: i64,
    connected: bool,
    rth: bool,
    bot_enabled: bool,
    dry_run: bool,
    is_locked_down: bool,
    last: f64,
    bid: f64,
    ask: f64,
    vwap: f64,
    vwap_std: f64,
    z_score: f64,
    vpoc: f64,
    total_pnl: f64,
    daily_pnl: f64,
    hourly_pnl: f64,
    open_pnl: f64,
    max_dd: f64,
    trades_today: u32,
    account_balance: f64,
    mll_floor: f64,
    mll_remaining: f64,
    gutter_win: bool,
    gutter_loss: bool,
    pos_dir: Option<String>,
    pos_uuid: Option<Uuid>,
    pos_ep: f64,
    pos_sl: f64,
    pos_tp: f64,
    pos_contracts: u32,
    pos_bars_held: i64,
    regime_type: String,
    vwap_regime: String,
    vix_status: Option<String>,
    calendar_event: Option<String>,
    atr: f64,
    atr_slope: f64,
    cum_delta: f64,
    sess_high: f64,
    sess_low: f64,
    total_volume: f64,
    spread: f64,
    vpoc_stable: bool,
    streak: i32,
    profit_factor: f64,
    bar_secs_left: u64,
    bars: Vec<BarUi>,
    chart_vol_profile: Vec<(f64, f64)>, // all levels sorted by price for chart overlay
    tape: Vec<TapeUi>,
    vol_profile: Vec<(f64, f64)>,
    trade_history: Vec<TradeUi>,
    active_orders: Vec<String>,
    log: Vec<String>,
}

struct TapeUi  { time: String, price: f64, vol: f64, side: String }
struct TradeUi { action: String, dir: String, price: f64, pnl: f64, reason: String }

trait CloneSnapshot { fn clone_snapshot(&self) -> UiSnapshot; }

impl CloneSnapshot for AppState {
    fn clone_snapshot(&self) -> UiSnapshot {
        use chrono::TimeZone;
        let now_et = chrono_tz::America::New_York.from_utc_datetime(&chrono::Utc::now().naive_utc());
        let time_et = now_et.format("%H:%M:%S ET").to_string();
        let date    = now_et.format("%Y-%m-%d").to_string();
        let tod_mins = now_et.format("%H").to_string().parse::<i64>().unwrap_or(0) * 60
            + now_et.format("%M").to_string().parse::<i64>().unwrap_or(0);
        let rth_mins_left = (crate::config::POWER_HOUR_START - tod_mins).max(0);

        let sess = self.session.as_ref();

        // Bars: historical (prior sessions) + today's closed bars + forming bar, oldest-first
        let bars: Vec<BarUi> = {
            let mut result: Vec<BarUi> = self.historical_bars.iter().map(|b| BarUi {
                ts: b.ts.format("%H:%M").to_string(),
                date: b.ts.format("%m/%d").to_string(),
                o: b.o, h: b.h, l: b.l, c: b.c, vwap: b.vwap, v: b.v, forming: false,
            }).collect();
            if let Some(s) = sess {
                for b in &s.bars {
                    result.push(BarUi {
                        ts: b.ts.format("%H:%M").to_string(),
                        date: b.ts.format("%m/%d").to_string(),
                        o: b.o, h: b.h, l: b.l, c: b.c, vwap: b.vwap, v: b.v, forming: false,
                    });
                }
                if let Some(ref cb) = s.cur_bar {
                    result.push(BarUi {
                        ts: cb.ts.get(11..16).unwrap_or("--:--").to_string(), // "HH:MM" from "YYYY-MM-DDTHH:MM:00Z"
                        date: date.clone(),
                        o: cb.o, h: cb.h, l: cb.l, c: cb.c,
                        vwap: s.vwap, v: cb.v, forming: true,
                    });
                }
            }
            result
        };

        // Full vol profile sorted by price (for chart overlay bucketing)
        let chart_vol_profile: Vec<(f64, f64)> = sess.map(|s| {
            s.vol_profile.iter().map(|(k, v)| (k.0, *v)).collect()
        }).unwrap_or_default();

        let tape = sess.map(|s| s.tape.iter().rev().take(20).map(|t| TapeUi {
            time: t.time.clone(), price: t.price, vol: t.vol, side: t.side.clone(),
        }).collect()).unwrap_or_default();

        let vol_profile = sess.map(|s| {
            let mut vp: Vec<(f64, f64)> = s.vol_profile.iter().map(|(k, v)| (k.0, *v)).collect();
            vp.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
            vp.truncate(15);
            vp
        }).unwrap_or_default();

        let trade_history = self.bot.trade_history.iter().rev().take(10).map(|t| TradeUi {
            action: t.action.clone(), dir: t.dir.clone(),
            price: t.price, pnl: t.pnl, reason: t.reason.clone(),
        }).collect();

        let active_orders = self.bot.active_orders.iter().take(5).map(|o| {
            format!("{} {} @ {} [{:?}]",
                o["side"], o["type"], o["stopPrice"].as_f64().unwrap_or(0.0), o["status"])
        }).collect();

        let log = self.log.iter().rev().take(5).cloned().collect();

        let (pos_dir, pos_uuid, pos_ep, pos_sl, pos_tp, pos_contracts, pos_bars_held) =
            if let Some(ref p) = self.bot.pos {
                let bars_held = sess.map(|s| s.bars.len() as i64 - p.bar_idx).unwrap_or(0);
                (Some(p.dir.clone()), Some(p.uuid), p.ep, p.sl, p.tp, p.contracts_remaining, bars_held)
            } else { (None, None, 0.0, 0.0, 0.0, 0, 0) };

        let atr         = sess.map(|s| s.current_atr()).unwrap_or(0.0);
        let atr_slope   = sess.map(|s| s.get_atr_slope(10)).unwrap_or(0.0);
        let cum_delta   = sess.map(|s| s.cum_delta).unwrap_or(0.0);
        let sess_high   = sess.map(|s| s.high).unwrap_or(0.0);
        let sess_low    = sess.map(|s| s.low).unwrap_or(0.0);
        let total_volume = sess.map(|s| s.total_volume).unwrap_or(0.0);
        let spread      = sess.map(|s| if s.ask > 0.0 && s.bid > 0.0 { s.ask - s.bid } else { 0.0 }).unwrap_or(0.0);
        let vpoc_stable = sess.map(|s| s.is_session_stable()).unwrap_or(false);
        let bar_secs_left = (60 - chrono::Utc::now().timestamp() % 60) as u64 % 60;

        let streak = {
            let hist = &self.bot.trade_history;
            if hist.is_empty() {
                0i32
            } else {
                let last_pnl = hist.last().unwrap().pnl;
                let last_sign = if last_pnl > 0.0 { 1i32 } else if last_pnl < 0.0 { -1i32 } else { 0i32 };
                let mut count = 0i32;
                for t in hist.iter().rev() {
                    let sign = if t.pnl > 0.0 { 1 } else if t.pnl < 0.0 { -1 } else { 0 };
                    if sign == last_sign { count += 1; } else { break; }
                }
                count * last_sign
            }
        };
        let profit_factor = {
            let wins: f64 = self.bot.trade_history.iter().filter(|t| t.pnl > 0.0).map(|t| t.pnl).sum();
            let losses: f64 = self.bot.trade_history.iter().filter(|t| t.pnl < 0.0).map(|t| t.pnl.abs()).sum();
            if losses > 0.0 { wins / losses } else if wins > 0.0 { f64::INFINITY } else { 1.0 }
        };

        UiSnapshot {
            date, time_et, rth_mins_left,
            connected: self.connected, rth: self.rth,
            bot_enabled: self.bot_enabled, dry_run: self.dry_run,
            is_locked_down: self.bot.is_locked_down,
            last: self.live.last, bid: self.live.bid, ask: self.live.ask,
            vwap: sess.map(|s| s.vwap).unwrap_or(0.0),
            vwap_std: sess.map(|s| s.vwap_std).unwrap_or(0.0),
            z_score: sess.map(|s| s.z_score()).unwrap_or(0.0),
            vpoc: sess.map(|s| s.vpoc).unwrap_or(0.0),
            total_pnl: self.bot.total_pnl,
            daily_pnl: self.bot.daily_pnl,
            hourly_pnl: self.bot.hourly_pnl,
            open_pnl: self.bot.open_pnl,
            max_dd: self.bot.max_dd,
            trades_today: self.bot.trades_today,
            account_balance: self.bot.account_balance,
            mll_floor: self.bot.mll_floor,
            mll_remaining: self.bot.mll_remaining,
            gutter_win: self.bot.gutter_win,
            gutter_loss: self.bot.gutter_loss,
            pos_dir, pos_uuid, pos_ep, pos_sl, pos_tp, pos_contracts, pos_bars_held,
            regime_type: self.regime.session_type.clone(),
            vwap_regime: self.regime.vwap_regime.clone(),
            vix_status: self.regime.vix_status.clone(),
            calendar_event: self.regime.calendar_event.clone(),
            atr, atr_slope, cum_delta, sess_high, sess_low, total_volume, spread,
            vpoc_stable, streak, profit_factor, bar_secs_left,
            bars, chart_vol_profile, tape, vol_profile, trade_history, active_orders, log,
        }
    }
}

// ── Top-level render ───────────────────────────────────────────────────────────

fn render(f: &mut Frame, snap: &UiSnapshot, extra_log: &[String], show_help: bool, chart_zoom: usize, chart_off: usize) {
    let area = f.area();
    f.render_widget(Block::default().style(Style::default().bg(BG)), area);

    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),  // title bar
            Constraint::Length(9),  // price / position / regime / pnl
            Constraint::Min(10),    // candlestick chart (expands with terminal height)
            Constraint::Length(9),  // tape + vol profile
            Constraint::Length(9),  // controls + fill history
            Constraint::Length(4),  // log
        ])
        .split(area);

    render_titlebar(f, snap, rows[0]);
    render_top_panels(f, snap, rows[1]);
    render_candle_chart(f, &snap.bars, snap, rows[2], chart_zoom, chart_off);
    render_tape_volprofile(f, snap, rows[3]);
    render_controls_fills(f, snap, rows[4]);
    render_log(f, snap, extra_log, rows[5]);

    if show_help { render_help(f, area); }
}

// ── Title bar ──────────────────────────────────────────────────────────────────

fn render_titlebar(f: &mut Frame, snap: &UiSnapshot, area: Rect) {
    let (status_label, status_color) =
        if snap.connected { ("● LIVE", GREEN) } else { ("○ OFF ", RED) };
    let rth = if snap.rth { "RTH" } else { "ETH" };
    let bot_color = if !snap.bot_enabled { RED }
        else if snap.is_locked_down { ORANGE }
        else { GREEN };
    let bot_label = if !snap.bot_enabled { "BOT:OFF" }
        else if snap.is_locked_down { "BOT:LKD" }
        else { "BOT:ON " };
    let dry_color = if snap.dry_run { YELLOW } else { DIM };
    let dry_label = if snap.dry_run { "DRY:ON " } else { "DRY:OFF" };
    let mins_str = if snap.rth { format!(" {}m", snap.rth_mins_left) } else { String::new() };

    let line = Line::from(vec![
        Span::styled("  ", Style::default().bg(BG)),
        Span::styled(CONTRACT, Style::default().fg(Color::White).bg(BG).add_modifier(Modifier::BOLD)),
        Span::styled(
            format!("  │  {}  │  {}{}  │  {rth}  │  ", snap.date, snap.time_et, mins_str),
            Style::default().fg(DIM).bg(BG),
        ),
        Span::styled(status_label, Style::default().fg(status_color).bg(BG).add_modifier(Modifier::BOLD)),
        Span::styled("  │  ", Style::default().fg(DIM).bg(BG)),
        Span::styled(bot_label, Style::default().fg(bot_color).bg(BG).add_modifier(Modifier::BOLD)),
        Span::styled("  ", Style::default().bg(BG)),
        Span::styled(dry_label, Style::default().fg(dry_color).bg(BG)),
        Span::styled(
            "  │  b bot  d dry  w gw  l gl  f flat  g gui  -/+ zoom  ←→ pan  0 reset  ? help  q quit",
            Style::default().fg(Color::Rgb(50, 50, 65)).bg(BG),
        ),
    ]);
    f.render_widget(Paragraph::new(line).style(Style::default().bg(BG)), area);
}

// ── Top panels ─────────────────────────────────────────────────────────────────

fn render_top_panels(f: &mut Frame, snap: &UiSnapshot, area: Rect) {
    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage(25), Constraint::Percentage(25),
            Constraint::Percentage(25), Constraint::Percentage(25),
        ])
        .split(area);
    render_price_panel(f, snap, cols[0]);
    render_position_panel(f, snap, cols[1]);
    render_regime_panel(f, snap, cols[2]);
    render_pnl_panel(f, snap, cols[3]);
}

fn render_price_panel(f: &mut Frame, snap: &UiSnapshot, area: Rect) {
    let z = snap.z_score;
    let z_color = if z > 1.5 { RED } else if z < -1.5 { GREEN } else if z.abs() > 0.8 { YELLOW } else { MID };
    let atr_dir = if snap.atr_slope > 0.01 { "↑" } else if snap.atr_slope < -0.01 { "↓" } else { "→" };
    let atr_col = if snap.atr_slope > 0.01 { ORANGE } else if snap.atr_slope < -0.01 { CYAN } else { DIM };
    let delta_col = if snap.cum_delta > 0.0 { GREEN } else if snap.cum_delta < 0.0 { RED } else { DIM };
    let lines = vec![
        Line::from(vec![
            Span::styled("  Last  ", Style::default().fg(DIM)),
            Span::styled(format!("{:.2}", snap.last), Style::default().fg(Color::White).add_modifier(Modifier::BOLD)),
            Span::styled(format!("  bar:{:02}s", snap.bar_secs_left), Style::default().fg(DIM)),
        ]),
        Line::from(vec![
            Span::styled("  Bid   ", Style::default().fg(DIM)),
            Span::styled(format!("{:.2}", snap.bid), Style::default().fg(GREEN)),
            Span::styled("  Ask  ", Style::default().fg(DIM)),
            Span::styled(format!("{:.2}", snap.ask), Style::default().fg(RED)),
        ]),
        Line::from(vec![
            Span::styled("  VWAP  ", Style::default().fg(DIM)),
            Span::styled(format!("{:.2}", snap.vwap), Style::default().fg(BLUE)),
            Span::styled("  σ ", Style::default().fg(DIM)),
            Span::styled(format!("{:.2}", snap.vwap_std), Style::default().fg(MID)),
        ]),
        Line::from(vec![
            Span::styled("  Z     ", Style::default().fg(DIM)),
            Span::styled(format!("{:+.2}", z), Style::default().fg(z_color).add_modifier(Modifier::BOLD)),
        ]),
        Line::from(vec![
            Span::styled("  VPOC  ", Style::default().fg(DIM)),
            Span::styled(format!("{:.2}", snap.vpoc), Style::default().fg(CYAN)),
        ]),
        Line::from(vec![
            Span::styled("  ATR   ", Style::default().fg(DIM)),
            Span::styled(format!("{:.2}", snap.atr), Style::default().fg(MID)),
            Span::styled(format!(" {atr_dir}"), Style::default().fg(atr_col)),
        ]),
        Line::from(vec![
            Span::styled("  Δ     ", Style::default().fg(DIM)),
            Span::styled(format!("{:+.0}", snap.cum_delta), Style::default().fg(delta_col)),
            Span::styled(format!("  H{:.0} L{:.0}", snap.sess_high, snap.sess_low), Style::default().fg(DIM)),
        ]),
    ];
    f.render_widget(Paragraph::new(lines).block(panel("PRICE")), area);
}

fn render_position_panel(f: &mut Frame, snap: &UiSnapshot, area: Rect) {
    if let Some(ref dir) = snap.pos_dir {
        let dir_color = if dir == "LONG" { GREEN } else { RED };
        let open_color = if snap.open_pnl >= 0.0 { GREEN } else { RED };
        let time_color = if snap.pos_bars_held > 60 { ORANGE } else { DIM };
        let sl_ticks = ((snap.last - snap.pos_sl).abs() / TICK_SIZE).round() as i64;
        let tp_ticks = ((snap.pos_tp - snap.last).abs() / TICK_SIZE).round() as i64;
        let lines = vec![
            Line::from(vec![
                Span::styled(format!("  {dir}"), Style::default().fg(dir_color).add_modifier(Modifier::BOLD)),
                Span::styled(format!(" ×{}", snap.pos_contracts), Style::default().fg(MID)),
                Span::styled(format!("  {}m", snap.pos_bars_held), Style::default().fg(time_color)),
            ]),
            Line::from(vec![Span::styled("  EP  ", Style::default().fg(DIM)), Span::styled(format!("{:.2}", snap.pos_ep), Style::default().fg(Color::White))]),
            Line::from(vec![
                Span::styled("  SL  ", Style::default().fg(DIM)),
                Span::styled(format!("{:.2}", snap.pos_sl), Style::default().fg(RED)),
                Span::styled(format!("  {}t", sl_ticks), Style::default().fg(Color::Rgb(130, 60, 60))),
            ]),
            Line::from(vec![
                Span::styled("  TP  ", Style::default().fg(DIM)),
                Span::styled(format!("{:.2}", snap.pos_tp), Style::default().fg(GREEN)),
                Span::styled(format!("  {}t", tp_ticks), Style::default().fg(Color::Rgb(60, 130, 60))),
            ]),
            Line::from(vec![Span::styled("  Opn ", Style::default().fg(DIM)), Span::styled(format!("${:+.2}", snap.open_pnl), Style::default().fg(open_color).add_modifier(Modifier::BOLD))]),
            Line::from(Span::raw("")),
            Line::from(Span::raw("")),
        ];
        f.render_widget(Paragraph::new(lines).block(panel_col("POSITION", dir_color)), area);
    } else {
        let streak_col = if snap.streak > 0 { GREEN } else if snap.streak < 0 { RED } else { DIM };
        let streak_str = if snap.streak == 0 {
            "—".to_string()
        } else if snap.streak > 0 {
            format!("{}W", snap.streak)
        } else {
            format!("{}L", snap.streak.abs())
        };
        let pf_col = if snap.profit_factor >= 1.5 { GREEN } else if snap.profit_factor >= 1.0 { YELLOW } else { RED };
        let pf_str = if snap.profit_factor == f64::INFINITY { "∞".to_string() } else { format!("{:.2}", snap.profit_factor) };
        let lines = vec![
            Line::from(Span::styled("  FLAT", Style::default().fg(DIM).add_modifier(Modifier::BOLD))),
            Line::from(Span::raw("")),
            Line::from(vec![Span::styled("  Trades  ", Style::default().fg(DIM)), Span::styled(format!("{}", snap.trades_today), Style::default().fg(MID))]),
            Line::from(vec![
                Span::styled("  GW ", Style::default().fg(DIM)),
                Span::styled(if snap.gutter_win { "ON " } else { "OFF" }, Style::default().fg(if snap.gutter_win { GREEN } else { DIM })),
                Span::styled("  GL ", Style::default().fg(DIM)),
                Span::styled(if snap.gutter_loss { "ON " } else { "OFF" }, Style::default().fg(if snap.gutter_loss { GREEN } else { DIM })),
            ]),
            Line::from(vec![Span::styled("  Streak  ", Style::default().fg(DIM)), Span::styled(streak_str, Style::default().fg(streak_col).add_modifier(Modifier::BOLD))]),
            Line::from(vec![Span::styled("  PF      ", Style::default().fg(DIM)), Span::styled(pf_str, Style::default().fg(pf_col))]),
            Line::from(Span::raw("")),
        ];
        f.render_widget(Paragraph::new(lines).block(panel("POSITION")), area);
    }
}

fn render_regime_panel(f: &mut Frame, snap: &UiSnapshot, area: Rect) {
    let regime_color = match snap.regime_type.as_str() { "TRENDING" => YELLOW, "RANGING" => BLUE, _ => MID };
    let vix_color = match snap.vix_status.as_deref() { Some("EXTREME") => RED, Some("HIGH") => YELLOW, _ => GREEN };
    let vol_str = if snap.total_volume >= 1_000_000.0 {
        format!("{:.1}M", snap.total_volume / 1_000_000.0)
    } else if snap.total_volume >= 1_000.0 {
        format!("{:.0}K", snap.total_volume / 1_000.0)
    } else {
        format!("{:.0}", snap.total_volume)
    };
    let lines = vec![
        Line::from(vec![Span::styled("  Session  ", Style::default().fg(DIM)), Span::styled(snap.regime_type.clone(), Style::default().fg(regime_color).add_modifier(Modifier::BOLD))]),
        Line::from(vec![Span::styled("  VWAP     ", Style::default().fg(DIM)), Span::styled(snap.vwap_regime.clone(), Style::default().fg(CYAN))]),
        Line::from(vec![Span::styled("  VIX      ", Style::default().fg(DIM)), Span::styled(snap.vix_status.as_deref().unwrap_or("N/A"), Style::default().fg(vix_color))]),
        Line::from(vec![Span::styled("  Event    ", Style::default().fg(DIM)), Span::styled(snap.calendar_event.as_deref().unwrap_or("none"), Style::default().fg(MAGENTA))]),
        Line::from(vec![Span::styled("  RTH Left ", Style::default().fg(DIM)), Span::styled(format!("{} min", snap.rth_mins_left), Style::default().fg(if snap.rth_mins_left < 30 { ORANGE } else { MID }))]),
        Line::from(vec![Span::styled("  VPOC     ", Style::default().fg(DIM)), Span::styled(if snap.vpoc_stable { "stable" } else { "migrating" }, Style::default().fg(if snap.vpoc_stable { GREEN } else { ORANGE }))]),
        Line::from(vec![Span::styled("  Vol      ", Style::default().fg(DIM)), Span::styled(vol_str, Style::default().fg(DIM))]),
    ];
    f.render_widget(Paragraph::new(lines).block(panel("REGIME")), area);
}

fn render_pnl_panel(f: &mut Frame, snap: &UiSnapshot, area: Rect) {
    let daily_color  = if snap.daily_pnl  >= 0.0 { GREEN } else { RED };
    let total_color  = if snap.total_pnl  >= 0.0 { GREEN } else { RED };
    let hourly_color = if snap.hourly_pnl >= 0.0 { GREEN } else { RED };
    let mll_color    = if snap.mll_remaining > 500.0 { GREEN } else if snap.mll_remaining > 200.0 { YELLOW } else { RED };
    let bal_color    = if snap.account_balance >= ACCOUNT_START_BALANCE { GREEN } else { RED };
    let progress     = ((snap.account_balance - ACCOUNT_START_BALANCE) / PROFIT_TARGET).clamp(0.0, 1.0);
    let filled       = (progress * 14.0).round() as usize;
    let bar          = format!("{}{}", "█".repeat(filled), "░".repeat(14 - filled));
    let dist_to_pass = (ACCOUNT_START_BALANCE + PROFIT_TARGET - snap.account_balance).max(0.0);
    let lines = vec![
        Line::from(vec![Span::styled("  Daily  ", Style::default().fg(DIM)), Span::styled(format!("${:+.2}", snap.daily_pnl), Style::default().fg(daily_color).add_modifier(Modifier::BOLD))]),
        Line::from(vec![Span::styled("  Hour   ", Style::default().fg(DIM)), Span::styled(format!("${:+.2}", snap.hourly_pnl), Style::default().fg(hourly_color))]),
        Line::from(vec![Span::styled("  Total  ", Style::default().fg(DIM)), Span::styled(format!("${:+.2}", snap.total_pnl), Style::default().fg(total_color))]),
        Line::from(vec![Span::styled("  Bal    ", Style::default().fg(DIM)), Span::styled(format!("${:.2}", snap.account_balance), Style::default().fg(bal_color))]),
        Line::from(vec![Span::styled("  MLL    ", Style::default().fg(DIM)), Span::styled(format!("${:.0}", snap.mll_remaining), Style::default().fg(mll_color).add_modifier(Modifier::BOLD)), Span::styled("  MaxDD ", Style::default().fg(DIM)), Span::styled(format!("${:.0}", snap.max_dd), Style::default().fg(RED))]),
        Line::from(vec![Span::styled("  Pass   ", Style::default().fg(DIM)), Span::styled(format!("-${:.0}  ", dist_to_pass), Style::default().fg(MID)), Span::styled(bar, Style::default().fg(BLUE)), Span::styled(format!(" {:.0}%", progress * 100.0), Style::default().fg(MID))]),
        Line::from(Span::raw("")),
    ];
    f.render_widget(Paragraph::new(lines).block(panel("P&L")), area);
}

// ── Candlestick chart ──────────────────────────────────────────────────────────

fn render_candle_chart(
    f: &mut Frame,
    bars: &[BarUi],
    snap: &UiSnapshot,
    area: Rect,
    zoom: usize,
    offset: usize,
) {
    // ── Slice + aggregate ─────────────────────────────────────────────────────
    let zoom     = zoom.max(1);
    let end      = bars.len().saturating_sub(offset);
    let scrolled = &bars[..end];

    // Compute inner dims before building the panel (border = 1 each side, -1 for time axis)
    const SCALE_W: usize = 9;
    const PROF_W:  usize = 8;
    let w = area.width.saturating_sub(2) as usize;
    let h = area.height.saturating_sub(3) as usize; // -2 border -1 time axis
    let chart_w = w.saturating_sub(SCALE_W + PROF_W);

    let bars_needed = chart_w * zoom;
    let from  = scrolled.len().saturating_sub(bars_needed);
    let raw   = &scrolled[from..];
    let visible: Vec<AggBar> = raw.chunks(zoom).map(|c| agg_bars(c)).collect();

    let is_live   = offset == 0 && raw.last().map(|b| b.forming).unwrap_or(false);
    let bar_count = raw.len().saturating_sub(if is_live { 1 } else { 0 });

    let title = {
        let mut t = format!("CANDLES  {bar_count} bars");
        if zoom > 1   { t.push_str(&format!("  {zoom}x")); }
        if offset > 0 { t.push_str(&format!("  -{offset}")); }
        t.push_str("  ─VWAP");
        if is_live    { t.push_str("  ● live"); }
        t
    };

    let outer = panel(&title);
    let inner = outer.inner(area);
    f.render_widget(outer, area);

    // Reserve bottom row for time axis
    let axis_area = Rect {
        x: inner.x, y: inner.y + inner.height.saturating_sub(1),
        width: inner.width, height: 1.min(inner.height),
    };
    let chart_area = Rect {
        x: inner.x, y: inner.y,
        width: inner.width, height: inner.height.saturating_sub(1),
    };

    if visible.is_empty() || h < 4 || chart_w < 4 {
        f.render_widget(
            Paragraph::new(Span::styled("  waiting for bars...", Style::default().fg(DIM))),
            chart_area,
        );
        return;
    }

    // ── Price range ───────────────────────────────────────────────────────────
    let max_h = visible.iter().map(|b| b.h).fold(f64::NEG_INFINITY, f64::max);
    let min_l = visible.iter().map(|b| b.l).fold(f64::INFINITY,     f64::min);
    let pad   = (max_h - min_l).max(0.5) * 0.06;
    let high  = max_h + pad;
    let low   = min_l - pad;
    let range = (high - low).max(0.01);

    let p2r = |price: f64| -> usize {
        ((1.0 - (price - low) / range) * (h as f64 - 1.0))
            .round().clamp(0.0, h as f64 - 1.0) as usize
    };

    // ── Grid ──────────────────────────────────────────────────────────────────
    let mut grid: Vec<Vec<(char, Color)>> = vec![vec![(' ', BG); w]; h];

    // ── Candles ───────────────────────────────────────────────────────────────
    for (col, bar) in visible.iter().enumerate() {
        if col >= chart_w { break; }
        let bullish = bar.c >= bar.o;
        // Bodies bright; wicks at ~40% brightness so they read as shadow
        let (body_fg, wick_fg) = if bar.forming {
            (if bullish { Color::Rgb(45, 160, 85)  } else { Color::Rgb(165, 52, 52) },
             if bullish { Color::Rgb(25,  90, 48)  } else { Color::Rgb( 90, 26, 26) })
        } else {
            (if bullish { Color::Rgb(72, 214, 112) } else { Color::Rgb(228, 72, 72) },
             if bullish { Color::Rgb(28, 100, 52)  } else { Color::Rgb(110, 30, 30) })
        };

        let top_wick = p2r(bar.h);
        let bot_wick = p2r(bar.l).min(h - 1);
        let body_top = p2r(bar.o.max(bar.c));
        let body_bot = p2r(bar.o.min(bar.c)).min(h - 1);
        let is_doji  = body_top == body_bot;

        for row in top_wick..=bot_wick {
            if row >= h { break; }
            let (ch, fg) = if is_doji && row == body_top {
                ('─', body_fg)                       // doji: horizontal dash at mid
            } else if !is_doji && row >= body_top && row <= body_bot {
                ('█', body_fg)                       // body: full block
            } else {
                ('╎', wick_fg)                       // wick: thin dashed vertical
            };
            grid[row][col] = (ch, fg);
        }

        // VWAP line (don't overwrite body)
        if bar.vwap >= low && bar.vwap <= high {
            let vr = p2r(bar.vwap);
            if vr < h && grid[vr][col].0 != '█' && grid[vr][col].0 != '─' {
                grid[vr][col] = ('─', CYAN);
            }
        }
    }

    // ── Current-price dotted extension (live mode only) ───────────────────────
    let last_price = visible.last().map(|b| b.c).unwrap_or(0.0);
    if is_live && last_price >= low && last_price <= high {
        let pr       = p2r(last_price);
        let from_col = visible.len().min(chart_w);
        for col in from_col..chart_w {
            if grid[pr][col].0 == ' ' { grid[pr][col] = ('·', Color::Rgb(70, 70, 100)); }
        }
    }

    // ── SL / TP / EP ─────────────────────────────────────────────────────────
    {
        let draw_h = |g: &mut Vec<Vec<(char, Color)>>, price: f64, ch: char, fg: Color| {
            if price < low || price > high { return; }
            let pr = p2r(price);
            for col in 0..chart_w {
                if g[pr][col].0 == ' ' { g[pr][col] = (ch, fg); }
            }
        };
        if snap.pos_dir.is_some() {
            draw_h(&mut grid, snap.pos_sl, '╌', RED);
            draw_h(&mut grid, snap.pos_tp, '╌', GREEN);
            draw_h(&mut grid, snap.pos_ep, '╌', YELLOW);
        }
    }

    // ── Volume profile (right of candles, left of scale) ─────────────────────
    let mut row_vol = vec![0.0f64; h];
    for (price, vol) in &snap.chart_vol_profile {
        if *price >= low && *price <= high {
            let row = p2r(*price);
            if row < h { row_vol[row] += vol; }
        }
    }
    let max_rv = row_vol.iter().cloned().fold(0.01f64, f64::max);
    let vpoc_r = row_vol.iter().enumerate()
        .max_by(|a, b| a.1.partial_cmp(b.1).unwrap_or(std::cmp::Ordering::Equal))
        .map(|(i, _)| i);
    for (row, &vol) in row_vol.iter().enumerate() {
        if vol <= 0.0 { continue; }
        let bar_len = ((vol / max_rv) * PROF_W as f64).round() as usize;
        let is_vpoc = vpoc_r == Some(row);
        for j in 0..bar_len.min(PROF_W) {
            let fg = if is_vpoc { Color::Rgb(55, 175, 215) } else { Color::Rgb(35, 58, 118) };
            grid[row][chart_w + j] = ('█', fg);
        }
    }

    // ── Price scale ───────────────────────────────────────────────────────────
    for &frac in &[0.0f64, 0.25, 0.5, 0.75, 1.0] {
        let price = low + range * frac;
        let row   = p2r(price);
        if row < h {
            let label = format!("{:>7.2}│", price);
            for (j, ch) in label.chars().enumerate() {
                let col = chart_w + PROF_W + j;
                if col < w { grid[row][col] = (ch, if frac == 0.5 { MID } else { DIM }); }
            }
        }
    }
    if last_price >= low && last_price <= high {
        let pr    = p2r(last_price);
        let label = format!("{:>7.2}◄", last_price);
        for (j, ch) in label.chars().enumerate() {
            let col = chart_w + PROF_W + j;
            if col < w { grid[pr][col] = (ch, Color::White); }
        }
    }

    // ── Grid → Lines (run-length encode by color) ─────────────────────────────
    let lines: Vec<Line> = grid.iter().map(|row| {
        let mut spans = Vec::new();
        let mut i = 0;
        while i < row.len() {
            let (ch, fg) = row[i];
            let mut s = String::new();
            s.push(ch);
            let mut j = i + 1;
            while j < row.len() && row[j].1 == fg { s.push(row[j].0); j += 1; }
            spans.push(Span::styled(s, Style::default().fg(fg).bg(BG)));
            i = j;
        }
        Line::from(spans)
    }).collect();

    f.render_widget(Paragraph::new(lines), chart_area);

    // ── Time axis ─────────────────────────────────────────────────────────────
    let mut time_chars: Vec<(char, Color)> = vec![(' ', BG); w];
    let label_step = 20usize;
    let mut prev_date = String::new();

    for col in (0..visible.len().min(chart_w)).step_by(label_step.max(1)) {
        let raw_idx = col * zoom;
        if raw_idx >= raw.len() { continue; }
        let bar = &raw[raw_idx];
        let (label, color) = if bar.date != prev_date {
            prev_date.clone_from(&bar.date);
            (&bar.date, CYAN)
        } else {
            (&bar.ts, DIM)
        };
        let start = col.saturating_sub(label.len() / 2);
        for (j, ch) in label.chars().enumerate() {
            let c = start + j;
            if c < chart_w { time_chars[c] = (ch, color); }
        }
    }

    // Run-length encode time row
    let mut time_spans: Vec<Span> = Vec::new();
    let mut i = 0;
    while i < time_chars.len() {
        let (ch, fg) = time_chars[i];
        let mut s = String::new();
        s.push(ch);
        let mut j = i + 1;
        while j < time_chars.len() && time_chars[j].1 == fg { s.push(time_chars[j].0); j += 1; }
        time_spans.push(Span::styled(s, Style::default().fg(fg).bg(BG)));
        i = j;
    }
    f.render_widget(Paragraph::new(Line::from(time_spans)), axis_area);
}

// ── Tape + Vol Profile ─────────────────────────────────────────────────────────

fn render_tape_volprofile(f: &mut Frame, snap: &UiSnapshot, area: Rect) {
    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(50), Constraint::Percentage(50)])
        .split(area);

    let tape_items: Vec<ListItem> = snap.tape.iter().map(|t| {
        let (color, sym) = match t.side.as_str() { "BUY" => (GREEN, "▲"), "SELL" => (RED, "▼"), _ => (MID, "·") };
        ListItem::new(Line::from(vec![
            Span::styled(format!(" {}", t.time), Style::default().fg(DIM)),
            Span::styled(format!("  {:>9.2}", t.price), Style::default().fg(Color::White)),
            Span::styled(format!("  {:>6.0}", t.vol), Style::default().fg(DIM)),
            Span::styled(format!("  {sym}"), Style::default().fg(color)),
        ]))
    }).collect();
    f.render_widget(List::new(tape_items).block(panel("TAPE")), cols[0]);

    let max_vol = snap.vol_profile.iter().map(|(_, v)| *v).fold(0.01f64, f64::max);
    let vp_items: Vec<ListItem> = snap.vol_profile.iter().map(|(price, vol)| {
        let bar_len = ((vol / max_vol) * 12.0).round() as usize;
        let bar = format!("{}{}", "█".repeat(bar_len), "░".repeat(12 - bar_len));
        ListItem::new(Line::from(vec![
            Span::styled(format!(" {:>9.2}  ", price), Style::default().fg(MID)),
            Span::styled(bar, Style::default().fg(BLUE)),
            Span::styled(format!("  {:>7.0}", vol), Style::default().fg(DIM)),
        ]))
    }).collect();
    f.render_widget(List::new(vp_items).block(panel("VOL PROFILE")), cols[1]);
}

// ── Controls + Fill History ────────────────────────────────────────────────────

fn render_controls_fills(f: &mut Frame, snap: &UiSnapshot, area: Rect) {
    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Length(28), Constraint::Min(20)])
        .split(area);
    render_controls_panel(f, snap, cols[0]);
    render_fills_panel(f, snap, cols[1]);
}

fn flag_span<'a>(on: bool) -> Span<'a> {
    if on { Span::styled("● ON ", Style::default().fg(GREEN)) }
    else  { Span::styled("○ OFF", Style::default().fg(DIM)) }
}

fn render_controls_panel(f: &mut Frame, snap: &UiSnapshot, area: Rect) {
    let outer = panel("CONTROLS");
    let inner = outer.inner(area);
    f.render_widget(outer, area);

    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1), Constraint::Length(1), Constraint::Length(1),
            Constraint::Length(1), Constraint::Length(1), Constraint::Length(1),
            Constraint::Min(1),
        ])
        .split(inner);

    let flag_line = |key: &'static str, label: &'static str, on: bool| -> Paragraph<'static> {
        Paragraph::new(Line::from(vec![
            Span::styled(format!("  {key}  "), Style::default().fg(Color::Rgb(50, 50, 65))),
            flag_span(on),
            Span::styled(format!("  {label}"), Style::default().fg(if on { Color::White } else { DIM })),
        ]))
    };

    let bot_on = snap.bot_enabled && !snap.is_locked_down;
    let bot_label = if snap.is_locked_down { "BOT LOCKED" } else { "BOT" };
    f.render_widget(flag_line("b", bot_label, bot_on), rows[0]);
    f.render_widget(flag_line("d", "DRY RUN",     snap.dry_run),     rows[1]);
    f.render_widget(flag_line("w", "GUTTER WIN",  snap.gutter_win),  rows[2]);
    f.render_widget(flag_line("l", "GUTTER LOSS", snap.gutter_loss), rows[3]);

    let flat_color = if snap.pos_dir.is_some() { ORANGE } else { DIM };
    f.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled("  f  ", Style::default().fg(Color::Rgb(50, 50, 65))),
            Span::styled("flatten pos", Style::default().fg(flat_color)),
        ])),
        rows[4],
    );

    let sep = "─".repeat(rows[5].width as usize);
    f.render_widget(
        Paragraph::new(Span::styled(sep, Style::default().fg(Color::Rgb(40, 40, 55)))),
        rows[5],
    );

    let order_items: Vec<ListItem> = if snap.active_orders.is_empty() {
        vec![ListItem::new(Span::styled("  no active orders", Style::default().fg(DIM)))]
    } else {
        snap.active_orders.iter()
            .map(|o| ListItem::new(Span::styled(format!("  {o}"), Style::default().fg(YELLOW))))
            .collect()
    };
    f.render_widget(List::new(order_items), rows[6]);
}

fn render_fills_panel(f: &mut Frame, snap: &UiSnapshot, area: Rect) {
    let header = Row::new(vec![
        Cell::from(Span::styled("Action", Style::default().fg(MID).add_modifier(Modifier::BOLD))),
        Cell::from(Span::styled("Dir",    Style::default().fg(MID).add_modifier(Modifier::BOLD))),
        Cell::from(Span::styled("Price",  Style::default().fg(MID).add_modifier(Modifier::BOLD))),
        Cell::from(Span::styled("PnL",    Style::default().fg(MID).add_modifier(Modifier::BOLD))),
        Cell::from(Span::styled("Reason", Style::default().fg(MID).add_modifier(Modifier::BOLD))),
    ]).style(Style::default().bg(PANEL_BG));

    let fill_rows: Vec<Row> = snap.trade_history.iter().map(|t| {
        let pnl_color = if t.pnl >= 0.0 { GREEN } else { RED };
        let dir_color = match t.dir.as_str() { "LONG" => GREEN, "SHORT" => RED, _ => MID };
        Row::new(vec![
            Cell::from(Span::styled(t.action.clone(), Style::default().fg(DIM))),
            Cell::from(Span::styled(t.dir.clone(),    Style::default().fg(dir_color))),
            Cell::from(Span::styled(format!("{:.2}", t.price), Style::default().fg(MID))),
            Cell::from(Span::styled(format!("${:+.2}", t.pnl), Style::default().fg(pnl_color).add_modifier(Modifier::BOLD))),
            Cell::from(Span::styled(t.reason.clone(), Style::default().fg(DIM))),
        ])
    }).collect();

    let table = Table::new(fill_rows, [
        Constraint::Length(7), Constraint::Length(6), Constraint::Length(9),
        Constraint::Length(10), Constraint::Min(8),
    ])
    .header(header)
    .block(panel("FILL HISTORY"));
    f.render_widget(table, area);
}

// ── Log ────────────────────────────────────────────────────────────────────────

fn render_log(f: &mut Frame, snap: &UiSnapshot, extra_log: &[String], area: Rect) {
    let mut lines: Vec<String> = extra_log.to_vec();
    lines.extend(snap.log.clone());
    lines.truncate(6);
    let items: Vec<ListItem> = lines.iter()
        .map(|l| ListItem::new(Span::styled(format!("  {l}"), Style::default().fg(DIM))))
        .collect();
    f.render_widget(List::new(items).block(panel("LOG")), area);
}

// ── Help overlay ───────────────────────────────────────────────────────────────

fn render_help(f: &mut Frame, screen: Rect) {
    f.render_widget(Block::default().style(Style::default().bg(Color::Rgb(10, 10, 16))), screen);

    let popup_w: u16 = 58;
    let popup_h: u16 = 20;
    let popup = centered_rect(popup_w, popup_h, screen);

    let outer = Block::default()
        .title(Span::styled("  Keyboard Shortcuts  ", Style::default().fg(CYAN).add_modifier(Modifier::BOLD)))
        .title_alignment(Alignment::Center)
        .borders(Borders::ALL)
        .border_style(Style::default().fg(BORDER))
        .style(Style::default().bg(PANEL_BG));
    let inner = outer.inner(popup);
    f.render_widget(outer, popup);

    let entries: &[(&str, &str, Color)] = &[
        ("g",     "Open GUI window",                   CYAN),
        ("b",     "Toggle bot ON / OFF",              GREEN),
        ("d",     "Toggle dry run (simulate orders)", YELLOW),
        ("w",     "Toggle gutter win protection",     CYAN),
        ("l",     "Toggle gutter loss protection",    CYAN),
        ("f",     "Flatten current position now",     ORANGE),
        ("",      "",                                  DIM),
        ("q/Esc", "Quit terminal (Esc closes help)",  RED),
        ("?",     "Show / hide this help",             MID),
        ("",      "",                                  DIM),
        ("",      "BOT:OFF  — new entries blocked",   DIM),
        ("",      "BOT:LKD  — hourly guard triggered",DIM),
        ("",      "DRY:ON   — orders not sent live",  DIM),
        ("",      "GW:OFF   — gutter win disabled",   DIM),
        ("",      "GL:OFF   — gutter loss disabled",  DIM),
        ("",      "",                                  DIM),
        ("",      "Chart:  ─ VWAP  ╌ SL/TP/EP  ● live", CYAN),
        ("-/+",   "Zoom out / zoom in  (max 8x)",      MID),
        ("← →",  "Pan left / right (20 bars)",         MID),
        ("0",     "Reset chart to live view",           MID),
        ("",      "Press ? or Esc to close",            Color::Rgb(60, 60, 80)),
    ];

    let constraints: Vec<Constraint> = entries.iter().map(|_| Constraint::Length(1)).collect();
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .margin(1)
        .constraints(constraints)
        .split(inner);

    for (i, (key, desc, color)) in entries.iter().enumerate() {
        if i >= rows.len() { break; }
        let line = if key.is_empty() {
            Line::from(Span::styled(format!("   {desc}"), Style::default().fg(*color)))
        } else {
            Line::from(vec![
                Span::styled(format!("  {:6}  ", key), Style::default().fg(CYAN).add_modifier(Modifier::BOLD)),
                Span::styled(*desc, Style::default().fg(*color)),
            ])
        };
        f.render_widget(Paragraph::new(line), rows[i]);
    }
}

fn centered_rect(w: u16, h: u16, area: Rect) -> Rect {
    Rect {
        x: area.x + area.width.saturating_sub(w) / 2,
        y: area.y + area.height.saturating_sub(h) / 2,
        width: w.min(area.width),
        height: h.min(area.height),
    }
}
