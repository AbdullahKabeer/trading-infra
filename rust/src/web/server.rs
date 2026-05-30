use std::sync::Arc;

use axum::{
    extract::{Path, State},
    http::StatusCode,
    response::{Html, IntoResponse, Json},
    routing::{get, post},
    Router,
};
use tokio::sync::{Mutex, RwLock};

use crate::config::MAX_TRADES;
use crate::market::session::Session;
use crate::strategy::backtest::run_backtest;
use crate::types::{AppState, BacktestStats};

// ── Embedded HTML dashboard ────────────────────────────────────────────────────

const INDEX_HTML: &str = r#"<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ES Bot Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0a0a0f; color: #cdd3da; font-family: 'Courier New', monospace; font-size: 12px; }
  .header { background: #111318; border-bottom: 1px solid #2a2d35; padding: 6px 12px; display: flex; align-items: center; gap: 16px; position: sticky; top: 0; z-index: 10; }
  .header h1 { color: #4fc3f7; font-size: 14px; font-weight: bold; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #f44336; display: inline-block; margin-right: 4px; }
  .dot.live { background: #4caf50; animation: pulse 1.5s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .badge { background: #1e2330; border: 1px solid #2a2d35; border-radius: 4px; padding: 2px 8px; font-size: 11px; }
  .grid { display: grid; gap: 8px; padding: 8px; }
  .grid-4 { grid-template-columns: repeat(4, 1fr); }
  .grid-3 { grid-template-columns: repeat(3, 1fr); }
  .grid-2 { grid-template-columns: 1fr 1fr; }
  .card { background: #111318; border: 1px solid #1e2330; border-radius: 6px; padding: 10px; }
  .card-title { font-size: 10px; color: #5c6370; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; border-bottom: 1px solid #1e2330; padding-bottom: 4px; }
  .big-num { font-size: 22px; font-weight: bold; }
  .green { color: #4caf50; }
  .red { color: #f44336; }
  .yellow { color: #ffb74d; }
  .blue { color: #4fc3f7; }
  .cyan { color: #26c6da; }
  .dim { color: #5c6370; }
  .metric { display: flex; justify-content: space-between; padding: 2px 0; border-bottom: 1px solid #1a1d26; }
  .metric:last-child { border-bottom: none; }
  table { width: 100%; border-collapse: collapse; }
  th { color: #5c6370; text-align: left; padding: 4px 6px; border-bottom: 1px solid #1e2330; font-size: 10px; text-transform: uppercase; }
  td { padding: 3px 6px; border-bottom: 1px solid #141720; }
  tr:hover td { background: #161a23; }
  .tag { display: inline-block; border-radius: 3px; padding: 1px 5px; font-size: 10px; }
  .tag-buy { background: #1b3a1b; color: #4caf50; }
  .tag-sell { background: #3a1b1b; color: #f44336; }
  .tag-flat { background: #1e2330; color: #5c6370; }
  button { background: #1e2330; border: 1px solid #2a2d35; color: #cdd3da; border-radius: 4px; padding: 4px 12px; cursor: pointer; font-family: inherit; font-size: 11px; }
  button:hover { background: #252b3a; }
  #error-bar { display: none; background: #3a1b1b; border: 1px solid #f44336; color: #f44336; padding: 6px 12px; border-radius: 4px; margin: 4px 8px; }
  .sessions-bar { display: flex; gap: 6px; flex-wrap: wrap; padding: 4px 0; }
  .sess-btn { font-size: 10px; padding: 2px 8px; }
  .pos-panel { border: 1px solid #2a2d35; border-radius: 4px; padding: 8px; background: #161a23; }
</style>
</head>
<body>
<div class="header">
  <h1>ES BOT</h1>
  <span><span id="dot" class="dot"></span><span id="status">CONNECTING</span></span>
  <span class="badge" id="sym">CON.F.US.EP.M26</span>
  <span class="badge" id="date-badge">--</span>
  <span class="badge" id="time-badge">--</span>
  <span class="badge" id="rth-badge">--</span>
  <span style="margin-left:auto;display:flex;gap:6px;">
    <button onclick="toggleGW()">Toggle GW</button>
    <button onclick="toggleGL()">Toggle GL</button>
  </span>
</div>

<div id="error-bar"></div>

<div class="grid grid-4" style="margin-top:4px">
  <div class="card">
    <div class="card-title">Live Price</div>
    <div class="big-num" id="last">--</div>
    <div style="margin-top:6px">
      <div class="metric"><span class="dim">Bid</span><span id="bid">--</span></div>
      <div class="metric"><span class="dim">Ask</span><span id="ask">--</span></div>
      <div class="metric"><span class="dim">VWAP</span><span id="vwap" class="blue">--</span></div>
      <div class="metric"><span class="dim">σ</span><span id="vwap-std">--</span></div>
      <div class="metric"><span class="dim">Z-Score</span><span id="z-score">--</span></div>
      <div class="metric"><span class="dim">VPOC</span><span id="vpoc" class="cyan">--</span></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Position</div>
    <div id="pos-panel" class="pos-panel dim" style="font-size:13px">FLAT</div>
  </div>

  <div class="card">
    <div class="card-title">P&amp;L</div>
    <div class="big-num" id="daily-pnl">--</div>
    <div style="margin-top:6px">
      <div class="metric"><span class="dim">Total P&L</span><span id="total-pnl">--</span></div>
      <div class="metric"><span class="dim">Open P&L</span><span id="open-pnl">--</span></div>
      <div class="metric"><span class="dim">Max DD</span><span id="max-dd" class="red">--</span></div>
      <div class="metric"><span class="dim">Balance</span><span id="balance">--</span></div>
      <div class="metric"><span class="dim">MLL Floor</span><span id="mll-floor" class="yellow">--</span></div>
      <div class="metric"><span class="dim">MLL Rem</span><span id="mll-rem">--</span></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Session Stats</div>
    <div class="metric"><span class="dim">Trades</span><span id="trades">--</span></div>
    <div class="metric"><span class="dim">Max Trades</span><span id="max-trades">--</span></div>
    <div class="metric"><span class="dim">Gutter Win</span><span id="gutter-win">--</span></div>
    <div class="metric"><span class="dim">Gutter Loss</span><span id="gutter-loss">--</span></div>
    <div class="metric"><span class="dim">Regime</span><span id="regime">--</span></div>
    <div class="metric"><span class="dim">VIX Status</span><span id="vix">--</span></div>
    <div class="metric"><span class="dim">Calendar</span><span id="calendar">--</span></div>
    <div class="metric"><span class="dim">Target Mode</span><span id="target-mode">--</span></div>
  </div>
</div>

<div class="grid grid-2" style="margin-top:0">
  <div class="card" style="max-height:280px;overflow-y:auto">
    <div class="card-title">Recent Bars (last 20)</div>
    <table>
      <thead><tr><th>Time</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Vol</th><th>VWAP</th><th>Z</th></tr></thead>
      <tbody id="bars-table"></tbody>
    </table>
  </div>
  <div class="grid" style="padding:0;gap:8px">
    <div class="card" style="max-height:140px;overflow-y:auto">
      <div class="card-title">Tape (last 20)</div>
      <table>
        <thead><tr><th>Time</th><th>Price</th><th>Vol</th><th>Side</th></tr></thead>
        <tbody id="tape-table"></tbody>
      </table>
    </div>
    <div class="card" style="max-height:132px;overflow-y:auto">
      <div class="card-title">Volume Profile (top 15)</div>
      <table>
        <thead><tr><th>Price</th><th>Volume</th><th>Bar</th></tr></thead>
        <tbody id="vp-table"></tbody>
      </table>
    </div>
  </div>
</div>

<div class="grid grid-2" style="margin-top:0">
  <div class="card" style="max-height:200px;overflow-y:auto">
    <div class="card-title">Fill History</div>
    <table>
      <thead><tr><th>Action</th><th>Dir</th><th>Price</th><th>P&L</th><th>Reason</th></tr></thead>
      <tbody id="fills-table"></tbody>
    </table>
  </div>
  <div class="card" style="max-height:200px;overflow-y:auto">
    <div class="card-title">Historical Sessions</div>
    <div class="sessions-bar" id="sessions-bar"></div>
    <div id="hist-data" style="margin-top:8px;font-size:11px;color:#5c6370">Select a date above.</div>
  </div>
</div>

<script>
const fmt = (n, dec=2) => n == null ? '--' : Number(n).toFixed(dec);
const fmtPnl = (n) => { if(n==null) return '--'; const s=n>=0?'+':''; return s+fmt(n); }
const pnlClass = (n) => n >= 0 ? 'green' : 'red';

let lastData = null;

async function fetchData() {
  try {
    const resp = await fetch('/api/data');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const d = await resp.json();
    lastData = d;
    update(d);
    document.getElementById('error-bar').style.display = 'none';
  } catch(e) {
    const eb = document.getElementById('error-bar');
    eb.textContent = 'Error: ' + e.message;
    eb.style.display = 'block';
    document.getElementById('dot').className = 'dot';
    document.getElementById('status').textContent = 'DISCONNECTED';
  }
}

function update(d) {
  // Header
  document.getElementById('date-badge').textContent = d.date || '--';
  const et = new Date().toLocaleTimeString('en-US', {timeZone:'America/New_York',hour12:false});
  document.getElementById('time-badge').textContent = et + ' ET';
  document.getElementById('rth-badge').textContent = d.rth ? 'RTH' : 'ETH';
  const dot = document.getElementById('dot');
  const st = document.getElementById('status');
  if (d.connected) { dot.className='dot live'; st.textContent='LIVE'; }
  else { dot.className='dot'; st.textContent='OFFLINE'; }

  // Live price
  const last = d.live_last ?? d.last ?? 0;
  document.getElementById('last').textContent = fmt(last);
  document.getElementById('last').className = 'big-num ' + (last >= (d.vwap||0) ? 'green' : 'red');
  document.getElementById('bid').textContent = fmt(d.bid ?? d.live_bid);
  document.getElementById('ask').textContent = fmt(d.ask ?? d.live_ask);
  document.getElementById('vwap').textContent = fmt(d.vwap);
  document.getElementById('vwap-std').textContent = fmt(d.vwap_std);
  const z = d.z_score ?? 0;
  const zel = document.getElementById('z-score');
  zel.textContent = fmt(z);
  zel.className = Math.abs(z) > 1.2 ? (z>0?'red':'green') : 'yellow';
  document.getElementById('vpoc').textContent = fmt(d.vpoc);

  // Position
  const bot = d.bot || {};
  const pos = bot.pos;
  const posEl = document.getElementById('pos-panel');
  if (pos) {
    const col = pos.dir === 'LONG' ? 'green' : 'red';
    posEl.innerHTML = `<div class="${col}" style="font-weight:bold;font-size:14px">${pos.dir} x${pos.contracts_remaining}</div>
      <div class="metric"><span class="dim">Entry</span><span>${fmt(pos.ep)}</span></div>
      <div class="metric"><span class="dim">SL</span><span class="red">${fmt(pos.sl)}</span></div>
      <div class="metric"><span class="dim">TP</span><span class="green">${fmt(pos.tp)}</span></div>`;
  } else {
    posEl.innerHTML = '<span class="dim">FLAT</span>';
  }

  // P&L
  const dp = bot.daily_pnl ?? 0;
  document.getElementById('daily-pnl').textContent = fmtPnl(dp);
  document.getElementById('daily-pnl').className = 'big-num ' + pnlClass(dp);
  document.getElementById('total-pnl').className = pnlClass(bot.total_pnl??0);
  document.getElementById('total-pnl').textContent = fmtPnl(bot.total_pnl);
  document.getElementById('open-pnl').className = pnlClass(bot.open_pnl??0);
  document.getElementById('open-pnl').textContent = fmtPnl(bot.open_pnl);
  document.getElementById('max-dd').textContent = fmt(bot.max_dd);
  document.getElementById('balance').textContent = '$' + fmt(bot.account_balance);
  document.getElementById('mll-floor').textContent = '$' + fmt(bot.mll_floor);
  const mlr = bot.mll_remaining ?? 0;
  const mlrEl = document.getElementById('mll-rem');
  mlrEl.textContent = '$' + fmt(mlr);
  mlrEl.className = mlr > 500 ? 'green' : 'red';

  // Stats
  document.getElementById('trades').textContent = (bot.trades ?? bot.trades_today ?? 0) + ' / ' + (bot.max_trades ?? MAX_TRADES ?? '--');
  document.getElementById('max-trades').textContent = bot.max_trades ?? '--';
  document.getElementById('gutter-win').textContent = bot.gutter_win ? '✓ ON' : '✗ OFF';
  document.getElementById('gutter-win').className = bot.gutter_win ? 'green' : 'red';
  document.getElementById('gutter-loss').textContent = bot.gutter_loss ? '✓ ON' : '✗ OFF';
  document.getElementById('gutter-loss').className = bot.gutter_loss ? 'green' : 'red';
  const reg = d.regime || {};
  document.getElementById('regime').textContent = (reg.session_type || '--') + ' / ' + (reg.vwap_regime || '--');
  document.getElementById('vix').textContent = reg.vix_status || '--';
  document.getElementById('calendar').textContent = reg.calendar_event || 'none';
  document.getElementById('target-mode').textContent = d.target_mode || '--';

  // Bars table
  const barsEl = document.getElementById('bars-table');
  const bars = (d.bars || []).slice(-20).reverse();
  barsEl.innerHTML = bars.map(b => {
    const zc = b.z > 1.2 ? 'red' : b.z < -1.2 ? 'green' : 'dim';
    return `<tr>
      <td class="dim">${(b.ts||'').slice(11,16)}</td>
      <td>${fmt(b.o)}</td>
      <td class="green">${fmt(b.h)}</td>
      <td class="red">${fmt(b.l)}</td>
      <td>${fmt(b.c)}</td>
      <td class="dim">${Math.round(b.v||0)}</td>
      <td class="blue">${fmt(b.vwap)}</td>
      <td class="${zc}">${fmt(b.z)}</td>
    </tr>`;
  }).join('');

  // Tape
  const tapeEl = document.getElementById('tape-table');
  const tape = (d.tape || []).slice(-20).reverse();
  tapeEl.innerHTML = tape.map(t => {
    const sc = t.side==='BUY'?'tag-buy':t.side==='SELL'?'tag-sell':'tag-flat';
    return `<tr>
      <td class="dim">${t.time||''}</td>
      <td>${fmt(t.price)}</td>
      <td>${t.vol||0}</td>
      <td><span class="tag ${sc}">${t.side||'?'}</span></td>
    </tr>`;
  }).join('');

  // Volume profile
  const vpEl = document.getElementById('vp-table');
  const vp = d.top_vol || [];
  const maxVol = Math.max(...vp.map(v=>v.vol||0), 1);
  vpEl.innerHTML = vp.slice(0,15).map(v => {
    const pct = Math.round((v.vol||0)/maxVol*100);
    const bar = '█'.repeat(Math.round(pct/5));
    return `<tr>
      <td>${fmt(v.price)}</td>
      <td>${Math.round(v.vol||0)}</td>
      <td class="cyan" style="font-size:10px">${bar}</td>
    </tr>`;
  }).join('');

  // Fill history
  const fillsEl = document.getElementById('fills-table');
  const trades = bot.bot_trades || d.bot_trades || [];
  fillsEl.innerHTML = trades.slice(0,20).map(t => {
    const pc = (t.pnl||0)>=0?'green':'red';
    return `<tr>
      <td>${t.action||'?'}</td>
      <td><span class="tag ${t.dir==='LONG'?'tag-buy':'tag-sell'}">${t.dir||'?'}</span></td>
      <td>${fmt(t.price)}</td>
      <td class="${pc}">${fmtPnl(t.pnl)}</td>
      <td class="dim">${t.reason||''}</td>
    </tr>`;
  }).join('');
}

async function loadSessions() {
  try {
    const resp = await fetch('/api/sessions');
    const d = await resp.json();
    const bar = document.getElementById('sessions-bar');
    bar.innerHTML = (d.dates||[]).map(dt =>
      `<button class="sess-btn" onclick="loadDay('${dt}')">${dt}</button>`
    ).join('');
  } catch(e) {}
}

async function loadDay(date) {
  const el = document.getElementById('hist-data');
  el.textContent = 'Loading ' + date + '...';
  try {
    const resp = await fetch('/api/day/' + date);
    const d = await resp.json();
    const trades = d.trades || [];
    const stats = d.stats || {};
    el.innerHTML = `<b>${date}</b> — ${trades.length} trades | Win: ${((stats.win_rate||0)*100).toFixed(1)}% | Expectancy: $${(stats.expectancy||0).toFixed(2)} | PF: ${(stats.profit_factor||0).toFixed(2)}`;
  } catch(e) {
    el.textContent = 'Error: ' + e.message;
  }
}

async function toggleGW() {
  await fetch('/api/toggle_gw', {method:'POST'});
}
async function toggleGL() {
  await fetch('/api/toggle_gl', {method:'POST'});
}

// Start
fetchData();
loadSessions();
setInterval(fetchData, 500);
</script>
</body>
</html>"#;

// ── State types ────────────────────────────────────────────────────────────────

type SharedState = Arc<RwLock<AppState>>;
type StatsCache = Arc<Mutex<Option<BacktestStats>>>;

#[derive(Clone)]
struct AppRouterState {
    state: SharedState,
    stats_cache: StatsCache,
}

// ── Server entry point ─────────────────────────────────────────────────────────

pub async fn run(
    state: Arc<RwLock<AppState>>,
    stats_cache: Arc<Mutex<Option<BacktestStats>>>,
    port: u16,
) {
    let router_state = AppRouterState { state, stats_cache };

    let app = Router::new()
        .route("/", get(index_handler))
        .route("/api/data", get(data_handler))
        .route("/api/sessions", get(sessions_handler))
        .route("/api/day/{date}", get(day_handler))
        .route("/api/toggle_gw", post(toggle_gw_handler))
        .route("/api/toggle_gl", post(toggle_gl_handler))
        .with_state(router_state);

    let addr = format!("0.0.0.0:{port}");
    tracing::info!("Web server listening on http://{addr}");

    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}

// ── Handlers ───────────────────────────────────────────────────────────────────

async fn index_handler() -> Html<&'static str> {
    Html(INDEX_HTML)
}

async fn data_handler(
    State(ctx): State<AppRouterState>,
) -> impl IntoResponse {
    let st = ctx.state.read().await;
    let stats = ctx.stats_cache.lock().await.clone().unwrap_or_default();

    let sess = st.session.as_ref();
    let date = sess.map(|s| s.date.clone()).unwrap_or_else(|| {
        chrono::Utc::now().format("%Y-%m-%d").to_string()
    });

    let bars: Vec<serde_json::Value> = sess
        .map(|s| {
            s.bars
                .iter()
                .map(|b| {
                    serde_json::json!({
                        "ts": b.ts.to_rfc3339(),
                        "o": b.o, "h": b.h, "l": b.l, "c": b.c,
                        "v": b.v, "vwap": b.vwap,
                        "vwap_std": b.vwap_std, "z": b.z,
                        "vpoc": b.vpoc
                    })
                })
                .collect()
        })
        .unwrap_or_default();

    let cur_bar: serde_json::Value = sess
        .and_then(|s| s.cur_bar.as_ref())
        .map(|cb| {
            serde_json::json!({
                "ts": cb.ts, "o": cb.o, "h": cb.h, "l": cb.l, "c": cb.c, "v": cb.v
            })
        })
        .unwrap_or(serde_json::Value::Null);

    let tape: Vec<serde_json::Value> = sess
        .map(|s| {
            s.tape
                .iter()
                .rev()
                .take(100)
                .map(|t| {
                    serde_json::json!({
                        "time": t.time, "price": t.price,
                        "vol": t.vol, "side": t.side
                    })
                })
                .collect()
        })
        .unwrap_or_default();

    // Top volume profile levels
    let top_vol: Vec<serde_json::Value> = sess
        .map(|s| {
            let mut vp: Vec<(f64, f64)> = s
                .vol_profile
                .iter()
                .map(|(k, v)| (k.0, *v))
                .collect();
            vp.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
            vp.truncate(15);
            vp.iter()
                .map(|(p, v)| serde_json::json!({"price": p, "vol": v}))
                .collect()
        })
        .unwrap_or_default();

    let bot = &st.bot;
    let pos_json = bot.pos.as_ref().map(|p| {
        serde_json::json!({
            "dir": p.dir, "ep": p.ep, "sl": p.sl, "tp": p.tp,
            "contracts_remaining": p.contracts_remaining
        })
    });

    let bot_trades: Vec<serde_json::Value> = bot
        .trade_history
        .iter()
        .map(|t| {
            serde_json::json!({
                "action": t.action, "dir": t.dir, "price": t.price,
                "pnl": t.pnl, "bar_idx": t.bar_idx,
                "reason": t.reason, "size": t.size
            })
        })
        .collect();

    let regime = &st.regime;

    let out = serde_json::json!({
        "date": date,
        "connected": st.connected,
        "rth": st.rth,
        "target_mode": st.target_mode,
        "time_stop_mins": st.time_stop_mins,
        "live_bid": st.live.bid,
        "live_ask": st.live.ask,
        "live_last": st.live.last,
        "last": st.live.last,
        "bid": st.live.bid,
        "ask": st.live.ask,
        "vwap": sess.map(|s| s.vwap).unwrap_or(0.0),
        "vwap_std": sess.map(|s| s.vwap_std).unwrap_or(0.0),
        "z_score": sess.map(|s| s.z_score()).unwrap_or(0.0),
        "vpoc": sess.map(|s| s.vpoc).unwrap_or(0.0),
        "open": sess.map(|s| s.open).unwrap_or(0.0),
        "high": sess.map(|s| s.high).unwrap_or(0.0),
        "low": sess.map(|s| s.low).unwrap_or(0.0),
        "volume": sess.map(|s| s.total_volume).unwrap_or(0.0),
        "trade_count": sess.map(|s| s.trade_count).unwrap_or(0),
        "bars": bars,
        "current_bar": cur_bar,
        "tape": tape,
        "top_vol": top_vol,
        "bot": {
            "account_name": bot.account_name,
            "total_pnl": bot.total_pnl,
            "daily_pnl": bot.daily_pnl,
            "open_pnl": bot.open_pnl,
            "max_dd": bot.max_dd,
            "trades": bot.trades_today,
            "trades_today": bot.trades_today,
            "max_trades": MAX_TRADES,
            "pos": pos_json,
            "active_orders": bot.active_orders,
            "account_balance": bot.account_balance,
            "mll_floor": bot.mll_floor,
            "mll_remaining": bot.mll_remaining,
            "peak_eod_balance": bot.peak_eod_balance,
            "gutter_win": bot.gutter_win,
            "gutter_loss": bot.gutter_loss,
        },
        "bot_trades": bot_trades,
        "stats": serde_json::to_value(&stats).unwrap_or_default(),
        "regime": {
            "vix_proxy": regime.vix_proxy,
            "vix_status": regime.vix_status,
            "vwap_crossings": regime.vwap_crossings,
            "vwap_regime": regime.vwap_regime,
            "calendar_event": regime.calendar_event,
            "high_impact_behavior": regime.high_impact_behavior,
            "session_type": regime.session_type,
            "prev_vwap": regime.prev_vwap,
            "prev_vpoc": regime.prev_vpoc,
        },
        "errors": [],
    });

    Json(out)
}

async fn sessions_handler() -> impl IntoResponse {
    let dates = Session::list_sessions();
    Json(serde_json::json!({ "dates": dates }))
}

async fn day_handler(
    Path(date): Path<String>,
    State(ctx): State<AppRouterState>,
) -> impl IntoResponse {
    let gutter_win = ctx.state.read().await.bot.gutter_win;
    let gutter_loss = ctx.state.read().await.bot.gutter_loss;

    match Session::load(&date) {
        Err(e) => (
            StatusCode::NOT_FOUND,
            Json(serde_json::json!({ "error": e.to_string() })),
        ),
        Ok(sess) => {
            let (snapshot, trades) = run_backtest(&sess.bars, gutter_win, gutter_loss);
            let stats = build_day_stats(&trades);
            let trade_vals: Vec<serde_json::Value> = trades
                .iter()
                .map(|t| {
                    serde_json::json!({
                        "action": t.action, "dir": t.dir, "price": t.price,
                        "pnl": t.pnl, "bar_idx": t.bar_idx,
                        "reason": t.reason, "size": t.size
                    })
                })
                .collect();
            let resp = serde_json::json!({
                "date": date,
                "snapshot": snapshot,
                "trades": trade_vals,
                "stats": stats,
                "bars": sess.bars.len(),
            });
            (StatusCode::OK, Json(resp))
        }
    }
}

async fn toggle_gw_handler(State(ctx): State<AppRouterState>) -> impl IntoResponse {
    let mut st = ctx.state.write().await;
    st.bot.gutter_win = !st.bot.gutter_win;
    let val = st.bot.gutter_win;
    Json(serde_json::json!({ "gutter_win": val }))
}

async fn toggle_gl_handler(State(ctx): State<AppRouterState>) -> impl IntoResponse {
    let mut st = ctx.state.write().await;
    st.bot.gutter_loss = !st.bot.gutter_loss;
    let val = st.bot.gutter_loss;
    Json(serde_json::json!({ "gutter_loss": val }))
}

fn build_day_stats(trades: &[crate::types::TradeRecord]) -> serde_json::Value {
    let exits: Vec<&crate::types::TradeRecord> = trades
        .iter()
        .filter(|t| t.action == "EXIT" && t.reason != "SCALE OUT")
        .collect();

    if exits.is_empty() {
        return serde_json::json!({ "trades": 0 });
    }

    let n = exits.len();
    let wins: Vec<f64> = exits.iter().filter(|t| t.pnl > 0.0).map(|t| t.pnl).collect();
    let losses: Vec<f64> = exits.iter().filter(|t| t.pnl < 0.0).map(|t| t.pnl).collect();
    let n_wins = wins.len();
    let n_losses = losses.len();
    let n_nonflat = n_wins + n_losses;
    let win_rate = if n_nonflat > 0 { n_wins as f64 / n_nonflat as f64 } else { 0.0 };
    let avg_win = if n_wins > 0 { wins.iter().sum::<f64>() / n_wins as f64 } else { 0.0 };
    let avg_loss = if n_losses > 0 { losses.iter().sum::<f64>() / n_losses as f64 } else { 0.0 };
    let gross_profit: f64 = wins.iter().sum();
    let gross_loss: f64 = losses.iter().map(|x| x.abs()).sum();
    let profit_factor = if gross_loss > 0.0 { gross_profit / gross_loss } else { 0.0 };
    let expectancy = win_rate * avg_win + (1.0 - win_rate) * avg_loss;
    let total_pnl: f64 = exits.iter().map(|t| t.pnl).sum();

    serde_json::json!({
        "trades": n,
        "wins": n_wins,
        "losses": n_losses,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "total_pnl": total_pnl,
    })
}
