use std::sync::Arc;
use std::time::Duration;

use crossterm::{
    event::{self, Event, KeyCode, KeyEventKind},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{
    backend::CrosstermBackend,
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{
        Block, Borders, Cell, List, ListItem, Paragraph, Row, Sparkline, Table,
    },
    Frame, Terminal,
};
use tokio::sync::{watch, RwLock};

use crate::config::CONTRACT;
use crate::types::AppState;

pub async fn run(
    state: Arc<RwLock<AppState>>,
    mut shutdown: watch::Receiver<bool>,
) -> anyhow::Result<()> {
    enable_raw_mode()?;
    let mut stdout = std::io::stdout();
    execute!(stdout, EnterAlternateScreen)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    let result = event_loop(&mut terminal, state, &mut shutdown).await;

    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    terminal.show_cursor()?;

    result
}

async fn event_loop(
    terminal: &mut Terminal<CrosstermBackend<std::io::Stdout>>,
    state: Arc<RwLock<AppState>>,
    shutdown: &mut watch::Receiver<bool>,
) -> anyhow::Result<()> {
    let mut log_msgs: Vec<String> = Vec::new();

    loop {
        // Check shutdown signal
        if *shutdown.borrow() {
            break;
        }

        // Take a snapshot of state (hold lock briefly)
        let snap = state.read().await.clone_snapshot();

        terminal.draw(|f| render(f, &snap, &log_msgs))?;

        // Poll for key events with short timeout
        if event::poll(Duration::from_millis(200))? {
            if let Event::Key(key) = event::read()? {
                if key.kind == KeyEventKind::Press {
                    match key.code {
                        KeyCode::Char('q') | KeyCode::Char('Q') => {
                            break;
                        }
                        KeyCode::Char('b') | KeyCode::Char('B') => {
                            log_msgs.push("Bot toggle (not implemented in TUI)".to_string());
                            if log_msgs.len() > 100 {
                                log_msgs.remove(0);
                            }
                        }
                        KeyCode::Char('d') | KeyCode::Char('D') => {
                            log_msgs.push("Dry run toggle (not implemented in TUI)".to_string());
                            if log_msgs.len() > 100 {
                                log_msgs.remove(0);
                            }
                        }
                        _ => {}
                    }
                }
            }
        }
    }

    Ok(())
}

// ── Snapshot helper ────────────────────────────────────────────────────────────

struct UiSnapshot {
    date: String,
    time_et: String,
    connected: bool,
    rth: bool,
    last: f64,
    bid: f64,
    ask: f64,
    vwap: f64,
    vwap_std: f64,
    z_score: f64,
    vpoc: f64,
    total_pnl: f64,
    daily_pnl: f64,
    open_pnl: f64,
    max_dd: f64,
    trades_today: u32,
    account_balance: f64,
    mll_floor: f64,
    mll_remaining: f64,
    peak_eod_balance: f64,
    gutter_win: bool,
    gutter_loss: bool,
    pos_dir: Option<String>,
    pos_ep: f64,
    pos_sl: f64,
    pos_tp: f64,
    pos_contracts: u32,
    regime_type: String,
    vwap_regime: String,
    vix_status: Option<String>,
    calendar_event: Option<String>,
    bars: Vec<BarUi>,
    tape: Vec<TapeUi>,
    vol_profile: Vec<(f64, f64)>,
    trade_history: Vec<TradeUi>,
    active_orders: Vec<String>,
    log: Vec<String>,
    z_history: Vec<u64>,
    equity_curve: Vec<u64>,
    atr_sparkline: Vec<u64>,
    delta_history: Vec<u64>,
}

struct BarUi {
    ts: String,
    o: f64,
    h: f64,
    l: f64,
    c: f64,
    v: f64,
    vwap: f64,
    z: f64,
}

struct TapeUi {
    time: String,
    price: f64,
    vol: f64,
    side: String,
}

struct TradeUi {
    action: String,
    dir: String,
    price: f64,
    pnl: f64,
    reason: String,
}

trait CloneSnapshot {
    fn clone_snapshot(&self) -> UiSnapshot;
}

impl CloneSnapshot for AppState {
    fn clone_snapshot(&self) -> UiSnapshot {
        use chrono::TimeZone;
        let now_et = chrono_tz::America::New_York
            .from_utc_datetime(&chrono::Utc::now().naive_utc());
        let time_et = now_et.format("%H:%M:%S ET").to_string();
        let date = now_et.format("%Y-%m-%d").to_string();

        let sess = self.session.as_ref();
        let bars: Vec<BarUi> = sess
            .map(|s| {
                s.bars
                    .iter()
                    .rev()
                    .take(20)
                    .map(|b| BarUi {
                        ts: b.ts.format("%H:%M").to_string(),
                        o: b.o,
                        h: b.h,
                        l: b.l,
                        c: b.c,
                        v: b.v,
                        vwap: b.vwap,
                        z: b.z,
                    })
                    .collect()
            })
            .unwrap_or_default();

        let tape: Vec<TapeUi> = sess
            .map(|s| {
                s.tape
                    .iter()
                    .rev()
                    .take(20)
                    .map(|t| TapeUi {
                        time: t.time.clone(),
                        price: t.price,
                        vol: t.vol,
                        side: t.side.clone(),
                    })
                    .collect()
            })
            .unwrap_or_default();

        let vol_profile: Vec<(f64, f64)> = sess
            .map(|s| {
                let mut vp: Vec<(f64, f64)> = s
                    .vol_profile
                    .iter()
                    .map(|(k, v)| (k.0, *v))
                    .collect();
                vp.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
                vp.truncate(15);
                vp
            })
            .unwrap_or_default();

        let trade_history: Vec<TradeUi> = self
            .bot
            .trade_history
            .iter()
            .rev()
            .take(10)
            .map(|t| TradeUi {
                action: t.action.clone(),
                dir: t.dir.clone(),
                price: t.price,
                pnl: t.pnl,
                reason: t.reason.clone(),
            })
            .collect();

        let active_orders: Vec<String> = self
            .bot
            .active_orders
            .iter()
            .take(5)
            .map(|o| {
                format!(
                    "{} {} @ {} [{:?}]",
                    o["side"], o["type"], o["stopPrice"].as_f64().unwrap_or(0.0),
                    o["status"]
                )
            })
            .collect();

        let log: Vec<String> = self.log.iter().rev().take(4).cloned().collect();

        // Scale z_history to 0-100
        let z_history: Vec<u64> = self
            .bot
            .z_history
            .iter()
            .map(|&z| ((z + 3.0) / 6.0 * 100.0).clamp(0.0, 100.0) as u64)
            .collect();

        // Scale equity curve to 0-100
        let eq: Vec<f64> = self.bot.equity_curve.iter().map(|(_, v)| *v).collect();
        let eq_min = eq.iter().cloned().fold(f64::INFINITY, f64::min);
        let eq_max = eq.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        let eq_range = (eq_max - eq_min).max(1.0);
        let equity_curve: Vec<u64> = eq
            .iter()
            .map(|&v| ((v - eq_min) / eq_range * 100.0).clamp(0.0, 100.0) as u64)
            .collect();

        // ATR sparkline
        let atr_max = self
            .bot
            .atr_sparkline
            .iter()
            .cloned()
            .fold(0.01f64, f64::max);
        let atr_sparkline: Vec<u64> = self
            .bot
            .atr_sparkline
            .iter()
            .map(|&v| (v / atr_max * 100.0).clamp(0.0, 100.0) as u64)
            .collect();

        let delta_history: Vec<u64> = self
            .bot
            .delta_history
            .iter()
            .map(|&d| ((d + 1.0) / 2.0 * 100.0).clamp(0.0, 100.0) as u64)
            .collect();

        UiSnapshot {
            date,
            time_et,
            connected: self.connected,
            rth: self.rth,
            last: self.live.last,
            bid: self.live.bid,
            ask: self.live.ask,
            vwap: sess.map(|s| s.vwap).unwrap_or(0.0),
            vwap_std: sess.map(|s| s.vwap_std).unwrap_or(0.0),
            z_score: sess.map(|s| s.z_score()).unwrap_or(0.0),
            vpoc: sess.map(|s| s.vpoc).unwrap_or(0.0),
            total_pnl: self.bot.total_pnl,
            daily_pnl: self.bot.daily_pnl,
            open_pnl: self.bot.open_pnl,
            max_dd: self.bot.max_dd,
            trades_today: self.bot.trades_today,
            account_balance: self.bot.account_balance,
            mll_floor: self.bot.mll_floor,
            mll_remaining: self.bot.mll_remaining,
            peak_eod_balance: self.bot.peak_eod_balance,
            gutter_win: self.bot.gutter_win,
            gutter_loss: self.bot.gutter_loss,
            pos_dir: self.bot.pos.as_ref().map(|p| p.dir.clone()),
            pos_ep: self.bot.pos.as_ref().map(|p| p.ep).unwrap_or(0.0),
            pos_sl: self.bot.pos.as_ref().map(|p| p.sl).unwrap_or(0.0),
            pos_tp: self.bot.pos.as_ref().map(|p| p.tp).unwrap_or(0.0),
            pos_contracts: self.bot.pos.as_ref().map(|p| p.contracts_remaining).unwrap_or(0),
            regime_type: self.regime.session_type.clone(),
            vwap_regime: self.regime.vwap_regime.clone(),
            vix_status: self.regime.vix_status.clone(),
            calendar_event: self.regime.calendar_event.clone(),
            bars,
            tape,
            vol_profile,
            trade_history,
            active_orders,
            log,
            z_history,
            equity_curve,
            atr_sparkline,
            delta_history,
        }
    }
}

// ── Render ─────────────────────────────────────────────────────────────────────

fn render(f: &mut Frame, snap: &UiSnapshot, extra_log: &[String]) {
    let area = f.area();

    // Overall layout: header, row1, row2, row3, row4, log
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),  // header
            Constraint::Length(8),  // row1: price/pos/regime/pnl
            Constraint::Length(5),  // row2: sparklines
            Constraint::Length(10), // row3: tape + vol profile
            Constraint::Length(8),  // row4: orders + fill history
            Constraint::Length(4),  // log
        ])
        .split(area);

    render_header(f, snap, chunks[0]);
    render_row1(f, snap, chunks[1]);
    render_sparklines(f, snap, chunks[2]);
    render_tape_volprofile(f, snap, chunks[3]);
    render_orders_fills(f, snap, extra_log, chunks[4]);
    render_log(f, snap, extra_log, chunks[5]);
}

fn render_header(f: &mut Frame, snap: &UiSnapshot, area: Rect) {
    let status_color = if snap.connected { Color::Green } else { Color::Red };
    let status_str = if snap.connected { "● LIVE" } else { "● OFF" };
    let rth_str = if snap.rth { "RTH" } else { "ETH" };

    let spans = vec![
        Span::styled(
            format!(" {} | {} | {} | ", CONTRACT, snap.date, snap.time_et),
            Style::default().fg(Color::White),
        ),
        Span::styled(status_str, Style::default().fg(status_color).add_modifier(Modifier::BOLD)),
        Span::styled(
            format!("  [{}]  q:quit b:toggle d:dry-run", rth_str),
            Style::default().fg(Color::DarkGray),
        ),
    ];

    let paragraph = Paragraph::new(Line::from(spans))
        .style(Style::default().bg(Color::Black));
    f.render_widget(paragraph, area);
}

fn render_row1(f: &mut Frame, snap: &UiSnapshot, area: Rect) {
    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage(25),
            Constraint::Percentage(25),
            Constraint::Percentage(25),
            Constraint::Percentage(25),
        ])
        .split(area);

    // PRICE panel
    let z_color = if snap.z_score > 1.0 {
        Color::Red
    } else if snap.z_score < -1.0 {
        Color::Green
    } else {
        Color::Yellow
    };
    let price_text = vec![
        Line::from(Span::styled(
            format!(" Last: {:.2}", snap.last),
            Style::default().fg(Color::White).add_modifier(Modifier::BOLD),
        )),
        Line::from(Span::styled(
            format!(" Bid: {:.2}  Ask: {:.2}", snap.bid, snap.ask),
            Style::default().fg(Color::DarkGray),
        )),
        Line::from(Span::styled(
            format!(" VWAP: {:.2}  σ: {:.2}", snap.vwap, snap.vwap_std),
            Style::default().fg(Color::Blue),
        )),
        Line::from(Span::styled(
            format!(" Z: {:.2}", snap.z_score),
            Style::default().fg(z_color),
        )),
        Line::from(Span::styled(
            format!(" VPOC: {:.2}", snap.vpoc),
            Style::default().fg(Color::Cyan),
        )),
    ];
    let price_block = Paragraph::new(price_text)
        .block(Block::default().title(" PRICE ").borders(Borders::ALL));
    f.render_widget(price_block, cols[0]);

    // POSITION panel
    let (pos_text, pos_color) = if let Some(ref dir) = snap.pos_dir {
        let col = if dir == "LONG" { Color::Green } else { Color::Red };
        let text = vec![
            Line::from(Span::styled(
                format!(" {dir} x{}", snap.pos_contracts),
                Style::default().fg(col).add_modifier(Modifier::BOLD),
            )),
            Line::from(Span::styled(
                format!(" EP: {:.2}", snap.pos_ep),
                Style::default().fg(Color::White),
            )),
            Line::from(Span::styled(
                format!(" SL: {:.2}", snap.pos_sl),
                Style::default().fg(Color::Red),
            )),
            Line::from(Span::styled(
                format!(" TP: {:.2}", snap.pos_tp),
                Style::default().fg(Color::Green),
            )),
            Line::from(Span::styled(
                format!(" Open PnL: ${:.2}", snap.open_pnl),
                Style::default().fg(if snap.open_pnl >= 0.0 { Color::Green } else { Color::Red }),
            )),
        ];
        (text, col)
    } else {
        let text = vec![
            Line::from(Span::styled(" FLAT", Style::default().fg(Color::DarkGray))),
            Line::from(Span::raw("")),
            Line::from(Span::styled(
                format!(" Trades: {}", snap.trades_today),
                Style::default().fg(Color::White),
            )),
            Line::from(Span::styled(
                format!(" GW: {}  GL: {}", snap.gutter_win, snap.gutter_loss),
                Style::default().fg(Color::Yellow),
            )),
        ];
        (text, Color::DarkGray)
    };
    let pos_block = Paragraph::new(pos_text)
        .block(
            Block::default()
                .title(" POSITION ")
                .borders(Borders::ALL)
                .border_style(Style::default().fg(pos_color)),
        );
    f.render_widget(pos_block, cols[1]);

    // REGIME panel
    let regime_color = match snap.regime_type.as_str() {
        "TRENDING" => Color::Yellow,
        "RANGING" => Color::Blue,
        _ => Color::White,
    };
    let regime_text = vec![
        Line::from(Span::styled(
            format!(" Session: {}", snap.regime_type),
            Style::default().fg(regime_color),
        )),
        Line::from(Span::styled(
            format!(" VWAP: {}", snap.vwap_regime),
            Style::default().fg(Color::Cyan),
        )),
        Line::from(Span::styled(
            format!(" VIX: {}", snap.vix_status.as_deref().unwrap_or("N/A")),
            Style::default().fg(Color::Yellow),
        )),
        Line::from(Span::styled(
            format!(
                " Event: {}",
                snap.calendar_event.as_deref().unwrap_or("none")
            ),
            Style::default().fg(Color::Magenta),
        )),
    ];
    let regime_block = Paragraph::new(regime_text)
        .block(Block::default().title(" REGIME ").borders(Borders::ALL));
    f.render_widget(regime_block, cols[2]);

    // P&L panel
    let pnl_color = if snap.daily_pnl >= 0.0 { Color::Green } else { Color::Red };
    let pnl_text = vec![
        Line::from(Span::styled(
            format!(" Daily: ${:.2}", snap.daily_pnl),
            Style::default().fg(pnl_color).add_modifier(Modifier::BOLD),
        )),
        Line::from(Span::styled(
            format!(" Total: ${:.2}", snap.total_pnl),
            Style::default().fg(if snap.total_pnl >= 0.0 { Color::Green } else { Color::Red }),
        )),
        Line::from(Span::styled(
            format!(" Balance: ${:.2}", snap.account_balance),
            Style::default().fg(Color::White),
        )),
        Line::from(Span::styled(
            format!(" MLL Floor: ${:.2}", snap.mll_floor),
            Style::default().fg(Color::Yellow),
        )),
        Line::from(Span::styled(
            format!(
                " MLL Remain: ${:.2}",
                snap.mll_remaining
            ),
            Style::default().fg(if snap.mll_remaining > 500.0 { Color::Green } else { Color::Red }),
        )),
        Line::from(Span::styled(
            format!(" MaxDD: ${:.2}", snap.max_dd),
            Style::default().fg(Color::Red),
        )),
    ];
    let pnl_block = Paragraph::new(pnl_text)
        .block(Block::default().title(" P&L ").borders(Borders::ALL));
    f.render_widget(pnl_block, cols[3]);
}

fn render_sparklines(f: &mut Frame, snap: &UiSnapshot, area: Rect) {
    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Percentage(20),
            Constraint::Percentage(20),
            Constraint::Percentage(20),
            Constraint::Percentage(20),
            Constraint::Percentage(20),
        ])
        .split(area);

    let sparkline_data: [(&str, &[u64], Color); 5] = [
        (" Z-Score", &snap.z_history, Color::Cyan),
        (" Equity", &snap.equity_curve, Color::Green),
        (" Cum Delta", &snap.delta_history, Color::Blue),
        (" ATR", &snap.atr_sparkline, Color::Yellow),
        (" Vol", &snap.z_history, Color::Magenta),
    ];

    for (i, (title, data, color)) in sparkline_data.iter().enumerate() {
        let sparkline = Sparkline::default()
            .block(Block::default().title(*title).borders(Borders::ALL))
            .data(*data)
            .style(Style::default().fg(*color));
        f.render_widget(sparkline, cols[i]);
    }
}

fn render_tape_volprofile(f: &mut Frame, snap: &UiSnapshot, area: Rect) {
    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(50), Constraint::Percentage(50)])
        .split(area);

    // TAPE
    let tape_items: Vec<ListItem> = snap
        .tape
        .iter()
        .map(|t| {
            let color = match t.side.as_str() {
                "BUY" => Color::Green,
                "SELL" => Color::Red,
                _ => Color::White,
            };
            ListItem::new(Line::from(Span::styled(
                format!("{} {:>9.2} {:>6.0} {}", t.time, t.price, t.vol, t.side),
                Style::default().fg(color),
            )))
        })
        .collect();

    let tape_list =
        List::new(tape_items).block(Block::default().title(" TAPE ").borders(Borders::ALL));
    f.render_widget(tape_list, cols[0]);

    // VOLUME PROFILE
    let vp_items: Vec<ListItem> = snap
        .vol_profile
        .iter()
        .map(|(price, vol)| {
            ListItem::new(Line::from(Span::styled(
                format!(" {:>9.2}  {:>8.0}", price, vol),
                Style::default().fg(Color::White),
            )))
        })
        .collect();

    let vp_list = List::new(vp_items)
        .block(Block::default().title(" VOL PROFILE ").borders(Borders::ALL));
    f.render_widget(vp_list, cols[1]);
}

fn render_orders_fills(f: &mut Frame, snap: &UiSnapshot, extra_log: &[String], area: Rect) {
    let cols = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(50), Constraint::Percentage(50)])
        .split(area);

    // ACTIVE ORDERS
    let order_items: Vec<ListItem> = if snap.active_orders.is_empty() {
        vec![ListItem::new(Line::from(Span::styled(
            " No active orders",
            Style::default().fg(Color::DarkGray),
        )))]
    } else {
        snap.active_orders
            .iter()
            .map(|o| {
                ListItem::new(Line::from(Span::styled(
                    format!(" {o}"),
                    Style::default().fg(Color::Yellow),
                )))
            })
            .collect()
    };
    let orders_list = List::new(order_items)
        .block(Block::default().title(" ACTIVE ORDERS ").borders(Borders::ALL));
    f.render_widget(orders_list, cols[0]);

    // FILL HISTORY
    let header = Row::new(vec![
        Cell::from("Action"),
        Cell::from("Dir"),
        Cell::from("Price"),
        Cell::from("PnL"),
        Cell::from("Reason"),
    ])
    .style(Style::default().add_modifier(Modifier::BOLD));

    let rows: Vec<Row> = snap
        .trade_history
        .iter()
        .map(|t| {
            let pnl_color = if t.pnl >= 0.0 { Color::Green } else { Color::Red };
            Row::new(vec![
                Cell::from(t.action.clone()),
                Cell::from(t.dir.clone()),
                Cell::from(format!("{:.2}", t.price)),
                Cell::from(Span::styled(
                    format!("${:.2}", t.pnl),
                    Style::default().fg(pnl_color),
                )),
                Cell::from(t.reason.clone()),
            ])
        })
        .collect();

    let table = Table::new(
        rows,
        [
            Constraint::Length(6),
            Constraint::Length(6),
            Constraint::Length(8),
            Constraint::Length(8),
            Constraint::Min(10),
        ],
    )
    .header(header)
    .block(Block::default().title(" FILL HISTORY ").borders(Borders::ALL));
    f.render_widget(table, cols[1]);
}

fn render_log(f: &mut Frame, snap: &UiSnapshot, extra_log: &[String], area: Rect) {
    let mut combined: Vec<String> = extra_log.to_vec();
    combined.extend(snap.log.clone());
    combined.reverse();
    combined.truncate(4);

    let items: Vec<ListItem> = combined
        .iter()
        .map(|l| {
            ListItem::new(Line::from(Span::styled(
                format!(" {l}"),
                Style::default().fg(Color::DarkGray),
            )))
        })
        .collect();

    let log_list =
        List::new(items).block(Block::default().title(" LOG ").borders(Borders::ALL));
    f.render_widget(log_list, area);
}
