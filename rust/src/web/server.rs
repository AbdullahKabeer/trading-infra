use std::sync::Arc;
use std::time::Duration;

use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        Path, State,
    },
    http::StatusCode,
    response::{Html, IntoResponse, Json},
    routing::{get, post},
    Router,
};
use tokio::sync::{Mutex, RwLock};

use crate::config::{CONTRACT, MAX_TRADES};
use crate::market::session::Session;
use crate::strategy::backtest::run_backtest;
use crate::types::{AppState, BacktestStats};

type SharedState = Arc<RwLock<AppState>>;
type StatsCache = Arc<Mutex<Option<BacktestStats>>>;

#[derive(Clone)]
struct Ctx {
    state: SharedState,
    #[allow(dead_code)]
    stats_cache: StatsCache,
}

pub async fn run(state: SharedState, stats_cache: StatsCache, port: u16) {
    let ctx = Ctx { state, stats_cache };
    let app = Router::new()
        .route("/", get(index_handler))
        .route("/ws", get(ws_handler))
        .route("/api/data", get(data_handler))
        .route("/api/sessions", get(sessions_handler))
        .route("/api/day/{date}", get(day_handler))
        .route("/api/toggle_gw", post(toggle_gw_handler))
        .route("/api/toggle_gl", post(toggle_gl_handler))
        .with_state(ctx);

    let addr = format!("0.0.0.0:{port}");
    tracing::info!("Dashboard: http://localhost:{port}");
    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}

async fn index_handler() -> Html<&'static str> {
    Html(INDEX_HTML)
}

async fn ws_handler(ws: WebSocketUpgrade, State(ctx): State<Ctx>) -> impl IntoResponse {
    ws.on_upgrade(|socket| ws_loop(socket, ctx))
}

async fn ws_loop(mut socket: WebSocket, ctx: Ctx) {
    let mut ticker = tokio::time::interval(Duration::from_millis(100));
    loop {
        ticker.tick().await;
        let payload = build_snapshot(&ctx).await;
        let txt = match serde_json::to_string(&payload) {
            Ok(s) => s,
            Err(_) => continue,
        };
        if socket.send(Message::Text(txt)).await.is_err() {
            break;
        }
    }
}

async fn data_handler(State(ctx): State<Ctx>) -> impl IntoResponse {
    Json(build_snapshot(&ctx).await)
}

async fn build_snapshot(ctx: &Ctx) -> serde_json::Value {
    let st = ctx.state.read().await;
    let sess = st.session.as_ref();

    let date = sess
        .map(|s| s.date.clone())
        .unwrap_or_else(|| chrono::Utc::now().format("%Y-%m-%d").to_string());

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
                "ts": cb.ts, "o": cb.o, "h": cb.h,
                "l": cb.l, "c": cb.c, "v": cb.v
            })
        })
        .unwrap_or(serde_json::Value::Null);

    let tape: Vec<serde_json::Value> = sess
        .map(|s| {
            s.tape
                .iter()
                .rev()
                .take(80)
                .map(|t| {
                    serde_json::json!({
                        "time": t.time, "price": t.price,
                        "vol": t.vol, "side": t.side
                    })
                })
                .collect()
        })
        .unwrap_or_default();

    let top_vol: Vec<serde_json::Value> = sess
        .map(|s| {
            let mut vp: Vec<(f64, f64)> = s
                .vol_profile
                .iter()
                .map(|(k, v)| (k.0, *v))
                .collect();
            vp.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
            vp.truncate(25);
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
                "reason": t.reason, "size": t.size,
                "sl": t.sl, "tp": t.tp
            })
        })
        .collect();

    let phase = if !st.bot_enabled {
        "HALTED"
    } else if st.dry_run {
        "DRY RUN"
    } else if bot.pos.is_some() {
        "IN TRADE"
    } else if bot.trades_today >= MAX_TRADES {
        "HALTED"
    } else {
        "HUNTING"
    };

    let regime = &st.regime;

    serde_json::json!({
        "date": date,
        "connected": st.connected,
        "rth": st.rth,
        "contract": CONTRACT,
        "phase": phase,
        "dry_run": st.dry_run,
        "target_mode": st.target_mode,
        "live_bid": st.live.bid,
        "live_ask": st.live.ask,
        "live_last": st.live.last,
        "vwap": sess.map(|s| s.vwap).unwrap_or(0.0),
        "vwap_std": sess.map(|s| s.vwap_std).unwrap_or(0.0),
        "z_score": sess.map(|s| s.z_score()).unwrap_or(0.0),
        "vpoc": sess.map(|s| s.vpoc).unwrap_or(0.0),
        "open": sess.map(|s| s.open).unwrap_or(0.0),
        "high": sess.map(|s| s.high).unwrap_or(0.0),
        "low": sess.map(|s| s.low).unwrap_or(0.0),
        "volume": sess.map(|s| s.total_volume).unwrap_or(0.0),
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
    })
}

async fn sessions_handler() -> impl IntoResponse {
    let dates = Session::list_sessions();
    Json(serde_json::json!({ "dates": dates }))
}

async fn day_handler(
    Path(date): Path<String>,
    State(ctx): State<Ctx>,
) -> impl IntoResponse {
    let (gutter_win, gutter_loss) = {
        let st = ctx.state.read().await;
        (st.bot.gutter_win, st.bot.gutter_loss)
    };
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
                        "reason": t.reason, "size": t.size,
                        "sl": t.sl, "tp": t.tp
                    })
                })
                .collect();
            (
                StatusCode::OK,
                Json(serde_json::json!({
                    "date": date,
                    "snapshot": snapshot,
                    "trades": trade_vals,
                    "stats": stats,
                    "bars": sess.bars.len(),
                })),
            )
        }
    }
}

async fn toggle_gw_handler(State(ctx): State<Ctx>) -> impl IntoResponse {
    let mut st = ctx.state.write().await;
    st.bot.gutter_win = !st.bot.gutter_win;
    Json(serde_json::json!({ "gutter_win": st.bot.gutter_win }))
}

async fn toggle_gl_handler(State(ctx): State<Ctx>) -> impl IntoResponse {
    let mut st = ctx.state.write().await;
    st.bot.gutter_loss = !st.bot.gutter_loss;
    Json(serde_json::json!({ "gutter_loss": st.bot.gutter_loss }))
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
    let nw = wins.len();
    let nl = losses.len();
    let wr = if nw + nl > 0 { nw as f64 / (nw + nl) as f64 } else { 0.0 };
    let aw = if nw > 0 { wins.iter().sum::<f64>() / nw as f64 } else { 0.0 };
    let al = if nl > 0 { losses.iter().sum::<f64>() / nl as f64 } else { 0.0 };
    let gp: f64 = wins.iter().sum();
    let gl: f64 = losses.iter().map(|x| x.abs()).sum();
    let pf = if gl > 0.0 { gp / gl } else { 0.0 };
    let exp = wr * aw + (1.0 - wr) * al;
    let total: f64 = exits.iter().map(|t| t.pnl).sum();
    serde_json::json!({
        "trades": n, "wins": nw, "losses": nl,
        "win_rate": wr, "avg_win": aw, "avg_loss": al,
        "profit_factor": pf, "expectancy": exp, "total_pnl": total,
    })
}

const INDEX_HTML: &str = r##"<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ES Bot</title>
<style>
:root{--bg:#eef2f6;--panel:#ffffff;--line:#d6e0ea;--line2:#e8edf3;--text:#1f2d3d;--muted:#5f7085;--accent:#2f6bff;--good:#00a66a;--bad:#d64545;--warn:#b07a00;}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font:12px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Arial,sans-serif;letter-spacing:.08px;-webkit-font-smoothing:antialiased}

/* Header */
.hdr{position:fixed;top:0;left:0;right:0;height:38px;z-index:100;background:var(--panel);border-bottom:2px solid var(--line);display:flex;align-items:center;gap:0;padding:0}
.hdr-brand{font-size:11px;font-weight:800;letter-spacing:2.5px;color:var(--accent);padding:0 12px;border-right:1px solid var(--line);height:100%;display:flex;align-items:center}
.hdr-price{font-size:18px;font-weight:700;padding:0 14px;border-right:1px solid var(--line);height:100%;display:flex;align-items:center;gap:8px}
.hdr-price .chg{font-size:11px;font-weight:600}
.hdr-g{display:flex;align-items:center;gap:12px;padding:0 12px;height:100%;border-right:1px solid var(--line);font-size:11px}
.hdr-g .lb{color:var(--muted);font-size:10px;margin-right:2px}
.hdr-sp{flex:1}
.hdr-r{display:flex;align-items:center;gap:8px;padding:0 10px;height:100%}
.dot{width:7px;height:7px;border-radius:50%;background:#b0bec5;flex-shrink:0}
.dot.on{background:var(--good);box-shadow:0 0 0 3px rgba(0,166,106,.15);animation:hbeat 2s infinite}
@keyframes hbeat{0%,100%{box-shadow:0 0 0 2px rgba(0,166,106,.2)}50%{box-shadow:0 0 0 5px rgba(0,166,106,.04)}}
.phase{padding:3px 9px;border-radius:3px;font-size:9px;font-weight:800;letter-spacing:1.5px;border:1px solid currentColor}
.ph-h{color:var(--accent);background:rgba(47,107,255,.07)}
.ph-t{color:var(--good);background:rgba(0,166,106,.08)}
.ph-x{color:var(--bad);background:rgba(214,69,69,.08)}
.ph-d{color:var(--warn);background:rgba(176,122,0,.08)}
.hbtn{background:#f2f5f9;color:var(--text);border:1px solid var(--line);padding:3px 9px;cursor:pointer;font:600 10px/1 inherit;border-radius:3px;letter-spacing:.4px}
.hbtn:hover{background:#e8eef5}
.hbtn.on{background:rgba(0,166,106,.1);border-color:var(--good);color:var(--good)}
.hbtn.off{background:rgba(214,69,69,.06);border-color:rgba(214,69,69,.4);color:var(--bad)}

/* Layout */
.page{position:fixed;top:38px;left:0;right:0;bottom:0;display:flex;flex-direction:column;overflow:hidden}
.body-grid{flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,1fr) 330px;overflow:hidden}
.left-col{display:flex;flex-direction:column;overflow:hidden;min-width:0}
.right-col{overflow-y:auto;overflow-x:hidden;border-left:1px solid var(--line);background:var(--bg);padding:6px}
.bot-row{flex-shrink:0;height:185px;border-top:1px solid var(--line);display:grid;grid-template-columns:1fr 175px 1fr}

/* Session strip */
.sess-strip{flex-shrink:0;background:var(--panel);border-bottom:1px solid var(--line);padding:5px 10px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.sess-strip .lb{color:var(--muted);font-size:10px;letter-spacing:.4px}
.sess-strip .vl{font-size:12px;font-weight:700;margin-left:2px}
.sess-tl-wrap{flex:1;min-width:120px;display:flex;flex-direction:column;gap:2px}
.sess-tl{height:4px;background:#dce3ec;border-radius:2px;position:relative}
.sess-zone{position:absolute;height:4px;border-radius:2px}
.sess-cursor{position:absolute;width:2px;height:12px;top:-4px;border-radius:1px;background:var(--text);transform:translateX(-50%);z-index:2}
.sess-labels{display:flex;justify-content:space-between;font-size:8px;color:var(--muted)}

/* Chart */
.chart-wrap{flex:1;min-height:0;position:relative;overflow:hidden}
.chart-wrap canvas{display:block;cursor:crosshair}

/* Bottom row */
.bot-cell{overflow:hidden;padding:6px;display:flex;flex-direction:column}
.bot-cell+.bot-cell{border-left:1px solid var(--line)}
.bot-cell h4{font-size:9px;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin-bottom:4px;padding-bottom:3px;border-bottom:1px solid var(--line2);font-weight:600;flex-shrink:0}
.tc-row{display:flex;gap:0;flex:1;min-height:0;overflow:hidden}
.tc-wrap{flex:1;min-width:0;overflow:hidden}
.vp-wrap{width:110px;flex-shrink:0;overflow:hidden}
.tc-wrap canvas,.vp-wrap canvas{display:block}

/* Right col cards */
.card{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:7px;margin-bottom:6px}
.card h3{font-size:9px;text-transform:uppercase;letter-spacing:.9px;color:var(--muted);margin:0 0 5px;padding-bottom:4px;border-bottom:1px solid var(--line2);font-weight:600}

/* KPI grid */
.kv{display:grid;gap:4px}
.kv2{grid-template-columns:1fr 1fr}
.kv3{grid-template-columns:1fr 1fr 1fr}
.it{background:#f8fbff;border:1px solid #e2e9f2;border-radius:4px;padding:4px 6px}
.lb{font-size:9px;text-transform:uppercase;color:var(--muted);letter-spacing:.5px}
.vl{font-size:12px;font-weight:700;margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vl.lg{font-size:17px}
.g{color:var(--good)}.r{color:var(--bad)}.b{color:var(--accent)}.y{color:var(--warn)}.m{color:var(--muted)}

/* Confluence */
.cf-verdict{display:flex;align-items:center;justify-content:space-between;padding:5px 7px;border-radius:4px;margin-bottom:5px;font-size:11px;font-weight:700;letter-spacing:.5px}
.cf-go{background:rgba(0,166,106,.1);border:1px solid rgba(0,166,106,.25);color:var(--good)}
.cf-wait{background:rgba(176,122,0,.1);border:1px solid rgba(176,122,0,.25);color:var(--warn)}
.cf-halt{background:rgba(214,69,69,.1);border:1px solid rgba(214,69,69,.25);color:var(--bad)}
.cf-row{display:grid;grid-template-columns:13px 1fr auto;gap:5px;align-items:center;padding:3px 5px;border-radius:3px;margin-bottom:1px}
.cf-ok{background:rgba(0,166,106,.05)}
.cf-warn{background:rgba(176,122,0,.05)}
.cf-bad{background:rgba(214,69,69,.05)}
.cf-ic{font-size:9px;font-weight:900;text-align:center}
.cf-ok .cf-ic{color:var(--good)}.cf-warn .cf-ic{color:var(--warn)}.cf-bad .cf-ic{color:var(--bad)}
.cf-lb{font-size:9px;text-transform:uppercase;letter-spacing:.4px;color:var(--muted)}
.cf-vl{font-size:9px;font-weight:700;text-align:right;white-space:nowrap}
.cf-ok .cf-vl{color:#1f4d38}.cf-warn .cf-vl{color:#5a3d00}.cf-bad .cf-vl{color:#6b1f1f}

/* Z gauge */
.z-gauge{height:18px;background:#e8edf3;border-radius:3px;position:relative;overflow:hidden;border:1px solid var(--line);margin:5px 0}
.z-entry-long{position:absolute;height:100%;background:rgba(0,166,106,.15);pointer-events:none}
.z-entry-short{position:absolute;height:100%;background:rgba(0,166,106,.15);pointer-events:none}
.z-cursor{position:absolute;width:3px;height:100%;border-radius:1px;top:0;background:var(--text);transform:translateX(-50%);transition:left .4s,background .3s}
.z-center{position:absolute;width:1px;height:100%;background:var(--muted);opacity:.35;left:50%;top:0}
.z-label{font-size:9px;color:var(--muted);display:flex;justify-content:space-between;margin-top:1px}

/* Win streak */
.streak{display:flex;gap:3px;align-items:center;flex-wrap:wrap;margin-top:5px}
.streak .sq{width:14px;height:14px;border-radius:2px;flex-shrink:0}
.sq.win{background:var(--good)}.sq.loss{background:var(--bad)}.sq.empty{background:#dce3ec}

/* Position card */
.pos-flat{text-align:center;padding:10px;color:var(--muted);font-size:11px;letter-spacing:2px}
.pos-dir{font-size:18px;font-weight:700;margin-bottom:5px}
.pos-grid{display:grid;grid-template-columns:1fr 1fr;gap:4px}
.pos-item{background:#f8fbff;border:1px solid #e2e9f2;border-radius:4px;padding:4px 6px}
.pos-lb{font-size:9px;text-transform:uppercase;color:var(--muted);letter-spacing:.5px}
.pos-vl{font-size:12px;font-weight:700}
.r-bar{height:6px;background:#e2e9f2;border-radius:3px;margin-top:5px;overflow:hidden;position:relative}
.r-fill{height:6px;border-radius:3px;transition:width .5s,background .4s}

/* Tables */
table{width:100%;border-collapse:collapse;font-size:10px}
thead th{font-size:8px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);font-weight:600;background:#f5f8fc;text-align:left;padding:3px 5px;border-bottom:1px solid var(--line2)}
tbody td{padding:2px 5px;border-bottom:1px solid var(--line2);vertical-align:middle}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover td{background:#f1f6fb}

/* Toast */
#toast{display:none;position:fixed;bottom:10px;right:10px;z-index:999;background:rgba(214,69,69,.1);border:1px solid rgba(214,69,69,.35);color:var(--bad);border-radius:4px;padding:5px 12px;font-size:11px}

/* Scrollbar */
::-webkit-scrollbar{width:4px;height:4px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:#cdd5e0;border-radius:2px}
</style>
</head>
<body>

<!-- HEADER -->
<div class="hdr">
  <div class="hdr-brand">ES BOT</div>
  <div class="hdr-price">
    <span id="v-last" style="color:var(--text)">—</span>
    <span id="v-chg" class="chg m">—</span>
  </div>
  <div class="hdr-g">
    <span><span class="lb">Bid</span><b id="v-bid" class="g">—</b></span>
    <span><span class="lb">Ask</span><b id="v-ask" class="r">—</b></span>
    <span><span class="lb">VWAP</span><b id="v-vwap" class="b">—</b></span>
    <span><span class="lb">σ</span><b id="v-std" class="m">—</b></span>
    <span><span class="lb">Z</span><b id="v-z">—</b></span>
    <span><span class="lb">VPOC</span><b id="v-vpoc" class="y">—</b></span>
  </div>
  <div class="hdr-sp"></div>
  <div class="hdr-r">
    <span class="m" style="font-size:10px" id="sym">—</span>
    <span class="m" style="font-size:10px" id="clk">—</span>
    <span style="font-size:10px;font-weight:600" id="rth">—</span>
    <span class="phase ph-h" id="phase">CONN</span>
    <span class="m" style="font-size:10px" id="trd">—</span>
    <span class="dot" id="dot"></span>
    <button class="hbtn off" id="btn-gw" onclick="toggleGW()">GW</button>
    <button class="hbtn off" id="btn-gl" onclick="toggleGL()">GL</button>
  </div>
</div>

<div class="page">
  <div class="body-grid">

    <!-- LEFT: chart + tape -->
    <div class="left-col">

      <!-- Session strip + timeline -->
      <div class="sess-strip">
        <span><span class="lb">O</span><span class="vl" id="s-open">—</span></span>
        <span><span class="lb">H</span><span class="vl g" id="s-high">—</span></span>
        <span><span class="lb">L</span><span class="vl r" id="s-low">—</span></span>
        <span><span class="lb">Target</span><span class="vl b" id="s-tgt">—</span></span>
        <span><span class="lb">Vol</span><span class="vl m" id="s-vol">—</span></span>
        <span><span class="lb">Date</span><span class="vl m" id="s-date">—</span></span>
        <div class="sess-tl-wrap">
          <div class="sess-tl" id="sess-tl">
            <div class="sess-zone" id="sz-avoid" style="background:rgba(214,69,69,.35)"></div>
            <div class="sess-zone" id="sz-main" style="background:rgba(0,166,106,.25)"></div>
            <div class="sess-zone" id="sz-late" style="background:rgba(176,122,0,.3)"></div>
            <div class="sess-cursor" id="sess-cur"></div>
          </div>
          <div class="sess-labels"><span>9:30</span><span>11:00</span><span>13:00</span><span>15:00</span><span>16:00</span></div>
        </div>
      </div>

      <!-- Main chart -->
      <div class="chart-wrap" id="chart-wrap">
        <canvas id="c1"></canvas>
      </div>

      <!-- Tape + bars -->
      <div class="bot-row">
        <div class="bot-cell">
          <h4>Tape <span id="tc-lbl" style="font-weight:400;text-transform:none;letter-spacing:0"></span></h4>
          <div class="tc-row">
            <div class="tc-wrap"><canvas id="tc"></canvas></div>
            <div class="vp-wrap"><canvas id="vp"></canvas></div>
          </div>
        </div>
        <div class="bot-cell">
          <h4>Volume Profile <span id="vpoc-lbl" style="float:right;font-weight:700;color:var(--warn);text-transform:none;letter-spacing:0"></span></h4>
          <div style="overflow-y:auto;flex:1">
            <table><thead><tr><th>Price</th><th>Vol</th><th></th></tr></thead><tbody id="vb"></tbody></table>
          </div>
        </div>
        <div class="bot-cell">
          <h4>Recent Bars <span id="bar-lbl" style="font-weight:400;text-transform:none;letter-spacing:0"></span></h4>
          <div style="overflow-y:auto;flex:1">
            <table><thead><tr><th>Time</th><th>O</th><th>H</th><th>L</th><th>C</th><th>V</th><th>Z</th></tr></thead><tbody id="bb"></tbody></table>
          </div>
        </div>
      </div>
    </div>

    <!-- RIGHT: info panels -->
    <div class="right-col">

      <!-- P&L card -->
      <div class="card">
        <h3>P &amp; L</h3>
        <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:5px">
          <span id="p-day" style="font-size:22px;font-weight:700;color:var(--muted)">$0.00</span>
          <span class="m" style="font-size:10px">daily</span>
          <span style="margin-left:auto" id="p-open" class="m" style="font-size:11px;font-weight:600">—</span>
          <span class="m" style="font-size:9px">open</span>
        </div>
        <div class="kv kv3" style="margin-bottom:5px">
          <div class="it"><div class="lb">Total</div><div class="vl" id="p-tot">—</div></div>
          <div class="it"><div class="lb">Max DD</div><div class="vl r" id="p-dd">—</div></div>
          <div class="it"><div class="lb">Trades</div><div class="vl" id="p-trd">—</div></div>
        </div>
        <div class="streak" id="streak"></div>
      </div>

      <!-- Confluence scorecard -->
      <div class="card">
        <h3>Confluence <span id="cf-score" style="float:right;text-transform:none;letter-spacing:0;font-size:10px;font-weight:700">—</span></h3>
        <div id="cf-verdict" class="cf-verdict cf-wait">— EVALUATING</div>
        <div id="cf-grid"></div>
      </div>

      <!-- Z-score gauge -->
      <div class="card">
        <h3>Z-Score Signal <span id="zg-label" style="float:right;text-transform:none;letter-spacing:0;font-size:10px;font-weight:700;color:var(--muted)">—</span></h3>
        <div class="z-gauge">
          <div class="z-entry-long" style="left:58.3%;width:8.3%"></div>
          <div class="z-entry-long" style="left:66.7%;width:8.3%"></div>
          <div class="z-entry-short" style="right:58.3%;width:8.3%"></div>
          <div class="z-entry-short" style="right:66.7%;width:8.3%"></div>
          <div class="z-center"></div>
          <div class="z-cursor" id="z-cursor" style="left:50%"></div>
        </div>
        <div class="z-label"><span>-3σ</span><span>-2σ</span><span>-1σ</span><span style="color:var(--text)">0</span><span>+1σ</span><span>+2σ</span><span>+3σ</span></div>
      </div>

      <!-- Position manager -->
      <div class="card" id="pos-card">
        <h3>Position <span id="pos-age" style="float:right;font-weight:400;text-transform:none;letter-spacing:0;font-size:9px;color:var(--muted)"></span></h3>
        <div id="pos-body"><div class="pos-flat">— FLAT —</div></div>
      </div>

      <!-- Account / MLL -->
      <div class="card">
        <h3>Account &amp; Risk</h3>
        <div class="kv kv2" style="margin-bottom:5px">
          <div class="it"><div class="lb">Balance</div><div class="vl b" id="a-bal">—</div></div>
          <div class="it"><div class="lb">Peak EOD</div><div class="vl m" id="a-peak">—</div></div>
          <div class="it"><div class="lb">MLL Floor</div><div class="vl r" id="a-floor">—</div></div>
          <div class="it"><div class="lb">Remaining</div><div class="vl" id="a-mll">—</div></div>
        </div>
        <div class="r-bar"><div class="r-fill" id="mll-bar" style="width:100%;background:var(--good)"></div></div>
        <div style="display:flex;justify-content:space-between;margin-top:4px;font-size:9px;color:var(--muted)">
          <span>MLL Buffer</span><span id="a-name" class="m">—</span>
        </div>
      </div>

      <!-- Regime -->
      <div class="card">
        <h3>Regime &amp; Context</h3>
        <div class="kv kv2">
          <div class="it"><div class="lb">Session Type</div><div class="vl b" id="r-sess">—</div></div>
          <div class="it"><div class="lb">VWAP Regime</div><div class="vl" id="r-vreg">—</div></div>
          <div class="it"><div class="lb">VIX</div><div class="vl" id="r-vix">—</div></div>
          <div class="it"><div class="lb">Calendar</div><div class="vl y" id="r-cal">—</div></div>
          <div class="it"><div class="lb">Prev VWAP</div><div class="vl m" id="r-pvw">—</div></div>
          <div class="it"><div class="lb">Prev VPOC</div><div class="vl m" id="r-pvc">—</div></div>
        </div>
      </div>

      <!-- Active orders -->
      <div class="card">
        <h3>Active Orders</h3>
        <table><thead><tr><th>Role</th><th>Side</th><th>Price</th><th>Qty</th><th>Status</th></tr></thead>
        <tbody id="ord-body"></tbody></table>
      </div>

      <!-- Trade history -->
      <div class="card">
        <h3>Trade History <span id="hist-pnl" style="float:right;font-size:10px;font-weight:700;text-transform:none;letter-spacing:0">—</span></h3>
        <table><thead><tr><th>Act</th><th>Dir</th><th>Price</th><th>P&L</th><th>Why</th><th>Bar</th></tr></thead>
        <tbody id="ohb"></tbody></table>
      </div>

    </div>
  </div>
</div>
<div id="toast">&#9888; Disconnected — retrying&#8230;</div>

<script>
'use strict';
const $=id=>document.getElementById(id);
const F=(n,d=2)=>(n!=null&&isFinite(+n)&&+n!==0)?Number(n).toFixed(d):'—';
const FN=(n,d=2)=>(n!=null&&isFinite(+n))?Number(n).toFixed(d):'—';
const FP=n=>{if(n==null||!isFinite(+n))return'—';const v=+n;const a=Math.abs(v).toFixed(2);return(v>=0?'+$':'-$')+a};
const clamp=(v,lo,hi)=>Math.max(lo,Math.min(hi,v));

/* ─── Chart state (module-level, matches Python exactly) ─── */
const chartState={zoom:180,offset:0,hoverX:null,hoverY:null,drag:false,dragStartX:0,dragOffset:0};

let S=null,posTs=null,wsR=0;

/* ─── WebSocket ─── */
function connect(){
  const p=location.protocol==='https:'?'wss:':'ws:';
  const ws=new WebSocket(`${p}//${location.host}/ws`);
  ws.onmessage=ev=>{wsR=0;$('toast').style.display='none';try{render(JSON.parse(ev.data));}catch(e){console.error(e);}};
  ws.onclose=ws.onerror=()=>{$('toast').style.display='block';setTimeout(connect,Math.min(14000,700*Math.pow(1.6,wsR++)));};
}
connect();

/* ─── Clock ─── */
setInterval(()=>{
  const t=new Date().toLocaleTimeString('en-US',{timeZone:'America/New_York',hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
  $('clk').textContent=t+' ET';
  updateSessTimeline();
},1000);

/* ─── Position age ─── */
setInterval(()=>{
  if(!posTs){$('pos-age').textContent='';return;}
  const s=Math.floor((Date.now()-posTs)/1000),h=Math.floor(s/3600),m=Math.floor(s%3600/60),ss=s%60;
  $('pos-age').textContent=h?`${h}h ${m}m`:`${m}m ${ss.toString().padStart(2,'0')}s`;
},1000);

/* ─── Session timeline ─── */
function updateSessTimeline(){
  const now=new Date(),et=new Date(now.toLocaleString('en-US',{timeZone:'America/New_York'}));
  const h=et.getHours(),m=et.getMinutes(),s=et.getSeconds();
  const minSince930=(h-9)*60+(m-30)+(s/60);
  const totalMin=6*60+30; // 9:30 to 16:00 = 390 min
  const pct=Math.max(0,Math.min(100,minSince930/totalMin*100));
  const cur=$('sess-cur');if(cur)cur.style.left=pct+'%';
  // Zones as % of 9:30-16:00 window
  const avoid=$('sz-avoid'),main=$('sz-main'),late=$('sz-late');
  if(avoid){avoid.style.left='0%';avoid.style.width=(30/390*100).toFixed(2)+'%';}
  if(main){main.style.left=(30/390*100).toFixed(2)+'%';main.style.width=(300/390*100).toFixed(2)+'%';}
  if(late){late.style.left=(330/390*100).toFixed(2)+'%';late.style.width=(60/390*100).toFixed(2)+'%';}
}
updateSessTimeline();

/* ─── Confluence compute ─── */
function computeConfluence(d){
  const bot=d.bot||{},reg=d.regime||{};
  const z=d.z_score||0;
  const now=new Date(),et=new Date(now.toLocaleString('en-US',{timeZone:'America/New_York'}));
  const h=et.getHours(),m=et.getMinutes();
  const minSince930=(h-9)*60+(m-30);
  const inRTH=d.rth&&minSince930>=0&&minSince930<390;
  const avoidOpen=minSince930<30;
  const avoidClose=minSince930>330;
  const vr=reg.vwap_regime||'NEUTRAL',vs=reg.vix_status||'NORMAL';
  const tradesLeft=(bot.max_trades||4)-(bot.trades_today||0);
  const mllRem=bot.mll_remaining||0;
  const factors=[
    {lb:'Z-Score',vl:`${z>=0?'+':''}${z.toFixed(2)}σ`,ok:Math.abs(z)>=1.2,warn:Math.abs(z)>=0.8&&Math.abs(z)<1.2,note:Math.abs(z)>=1.5?'STRONG':Math.abs(z)>=1.2?'ENTRY':'WEAK'},
    {lb:'VWAP Regime',vl:vr,ok:vr==='BALANCED',warn:vr==='NEUTRAL',note:vr==='BALANCED'?'mean reverting':vr==='TRENDING'?'trend day':'mixed'},
    {lb:'VIX',vl:`${reg.vix_proxy?reg.vix_proxy.toFixed(1):'?'} ${vs}`,ok:vs==='NORMAL',warn:vs==='HIGH',note:vs==='EXTREME'?'stay out':vs==='HIGH'?'reduce size':'normal'},
    {lb:'Calendar',vl:reg.calendar_event||'Clear',ok:!reg.calendar_event,warn:false,note:reg.calendar_event?'high impact':'clear'},
    {lb:'Session',vl:reg.session_type||'—',ok:reg.session_type==='RANGING',warn:reg.session_type==='NEUTRAL',note:reg.session_type==='RANGING'?'reverting':reg.session_type==='TRENDING'?'trend day':'mixed'},
    {lb:'Trades Left',vl:`${tradesLeft} / ${bot.max_trades||4}`,ok:tradesLeft>1,warn:tradesLeft===1,note:tradesLeft>0?'budget ok':'exhausted'},
    {lb:'MLL Buffer',vl:`$${Math.round(mllRem)}`,ok:mllRem>1000,warn:mllRem>300&&mllRem<=1000,note:mllRem>2000?'ample':mllRem>500?'reducing':'critical'},
    {lb:'Time Window',vl:`${h.toString().padStart(2,'0')}:${m.toString().padStart(2,'0')}`,ok:inRTH&&!avoidOpen&&!avoidClose,warn:avoidOpen,note:!inRTH?'outside RTH':avoidOpen?'open range':avoidClose?'close avoid':'in window'},
  ];
  const ok=factors.filter(f=>f.ok).length,warn=factors.filter(f=>!f.ok&&!f.warn&&f.warn!==true).length;
  const bad=factors.filter(f=>!f.ok&&!f.warn).length;
  const score=factors.filter(f=>f.ok).length;
  const total=factors.length;
  const verdict=bad>=2||score<4?'HALT':score<6?'WAIT':'GO';
  return{factors,score,total,verdict};
}

function renderConfluence(d){
  const {factors,score,total,verdict}=computeConfluence(d);
  $('cf-score').textContent=`${score}/${total}`;
  const vEl=$('cf-verdict');
  const vMap={GO:{cls:'cf-go',txt:`✓ ${score}/${total} — GO`},WAIT:{cls:'cf-wait',txt:`⚡ ${score}/${total} — WAIT`},HALT:{cls:'cf-halt',txt:`✗ ${score}/${total} — HALT`}};
  const vm=vMap[verdict]||vMap.WAIT;
  vEl.className='cf-verdict '+vm.cls;
  vEl.textContent=vm.txt;
  $('cf-grid').innerHTML=factors.map(f=>{
    const cls=f.ok?'cf-ok':f.warn?'cf-warn':'cf-bad';
    const ic=f.ok?'✓':f.warn?'⚡':'✗';
    return`<div class="cf-row ${cls}"><span class="cf-ic">${ic}</span><span class="cf-lb">${f.lb}<span style="font-weight:400;opacity:.7;margin-left:3px">${f.note}</span></span><span class="cf-vl">${f.vl}</span></div>`;
  }).join('');
}

/* ─── Z gauge ─── */
function renderZGauge(z){
  const cap=3,pct=clamp((z+cap)/(2*cap)*100,0,100);
  const cur=$('z-cursor');if(cur){cur.style.left=pct+'%';cur.style.background=Math.abs(z)>=1.5?'var(--accent)':Math.abs(z)>=1.2?'var(--good)':'var(--muted)';}
  const lbl=$('zg-label');if(lbl){
    const s=Math.abs(z)>=1.5?'STRONG':Math.abs(z)>=1.2?'ENTRY':Math.abs(z)>=0.8?'WEAK':'NEUTRAL';
    const cl=Math.abs(z)>=1.2?'b':Math.abs(z)>=0.8?'y':'m';
    lbl.textContent=`${z>=0?'+':''}${z.toFixed(2)}σ ${s}`;
    lbl.className=cl;
  }
}

/* ─── Win streak ─── */
function renderStreak(trades){
  const exits=(trades||[]).filter(t=>t.action==='EXIT'&&t.reason!=='SCALE OUT').slice(-10);
  const n=10;
  let html='';
  for(let i=0;i<n;i++){
    const t=exits[n-1-i>exits.length-1?-1:i];
    if(!t){html+=`<div class="sq empty" title="—"></div>`;continue;}
    const win=(t.pnl||0)>0;
    html+=`<div class="sq ${win?'win':'loss'}" title="${win?'+':''}$${(t.pnl||0).toFixed(0)} ${t.reason||''}"></div>`;
  }
  $('streak').innerHTML=html;
}

/* ─── Main render ─── */
function render(d){
  S=d;
  const bot=d.bot||{},reg=d.regime||{};

  // Header
  const on=d.connected;
  $('dot').className=on?'dot on':'dot';
  $('sym').textContent=d.contract||'—';
  const rth=d.rth;
  $('rth').textContent=rth?'● RTH':'○ ETH';
  $('rth').style.color=rth?'var(--good)':'var(--muted)';
  const phase=d.phase||'HUNTING';
  const pb=$('phase');pb.textContent=phase;
  pb.className='phase '+({HUNTING:'ph-h','IN TRADE':'ph-t',HALTED:'ph-x','DRY RUN':'ph-d'}[phase]||'ph-h');
  $('trd').textContent=`${bot.trades_today??0}/${bot.max_trades??'?'} trades`;
  $('btn-gw').className=bot.gutter_win?'hbtn on':'hbtn off';
  $('btn-gl').className=bot.gutter_loss?'hbtn on':'hbtn off';

  // Price (header)
  const last=d.live_last||0,vwap=d.vwap||0;
  const lastEl=$('v-last');lastEl.textContent=last?last.toFixed(2):'—';
  lastEl.style.color=last&&vwap?(last>=vwap?'var(--good)':'var(--bad)'):'var(--text)';
  const chg=vwap&&last?((last-vwap)/0.25):0;
  const chgEl=$('v-chg');chgEl.textContent=chg?`${chg>=0?'+':''}${chg.toFixed(0)}t`:'—';
  chgEl.style.color=chg>=0?'var(--good)':'var(--bad)';
  $('v-bid').textContent=d.live_bid?.toFixed(2)||'—';
  $('v-ask').textContent=d.live_ask?.toFixed(2)||'—';
  $('v-vwap').textContent=FN(d.vwap);
  $('v-std').textContent=FN(d.vwap_std);
  const z=d.z_score||0;
  const zEl=$('v-z');zEl.textContent=(z>=0?'+':'')+z.toFixed(2)+'σ';
  zEl.style.color=Math.abs(z)>1.5?'var(--accent)':Math.abs(z)>1.2?'var(--good)':Math.abs(z)>0.8?'var(--warn)':'var(--muted)';
  zEl.style.fontWeight=Math.abs(z)>1.2?'800':'600';
  $('v-vpoc').textContent=F(d.vpoc);

  // Session strip
  $('s-open').textContent=F(d.open);$('s-high').textContent=F(d.high);$('s-low').textContent=F(d.low);
  $('s-tgt').textContent=(d.target_mode||'VWAP').toUpperCase();
  $('s-vol').textContent=d.volume?Math.round(d.volume).toLocaleString():'—';
  $('s-date').textContent=d.date||'—';

  // P&L
  const dp=bot.daily_pnl||0;
  const dpEl=$('p-day');dpEl.textContent=FP(dp);dpEl.style.color=dp>=0?'var(--good)':'var(--bad)';
  $('p-open').textContent=FP(bot.open_pnl);
  $('p-open').style.color=(bot.open_pnl||0)>=0?'var(--good)':'var(--bad)';
  $('p-tot').textContent=FP(bot.total_pnl);$('p-tot').style.color=(bot.total_pnl||0)>=0?'var(--good)':'var(--bad)';
  $('p-dd').textContent=bot.max_dd?`-$${Math.abs(bot.max_dd).toFixed(2)}`:'—';
  $('p-trd').textContent=`${bot.trades_today||0}/${bot.max_trades||4}`;
  renderStreak(d.bot_trades);

  // Confluence
  renderConfluence(d);
  renderZGauge(z);

  // Position
  const pos=bot.pos;
  if(pos){
    if(!posTs)posTs=Date.now();
    const cl=pos.dir==='LONG'?'var(--good)':'var(--bad)';
    const riskT=Math.abs((pos.ep-pos.sl)/0.25);
    const rr=riskT>0?Math.abs((pos.tp-pos.ep)/pos.ep*pos.ep/(pos.sl-pos.ep)).toFixed(2):'—';
    const rrN=riskT>0?(Math.abs(pos.tp-pos.ep)/Math.abs(pos.ep-pos.sl)).toFixed(2):'—';
    const opnl=bot.open_pnl||0;
    const opnlR=riskT>0?(opnl/(riskT*12.5*(pos.contracts_remaining||1))).toFixed(2):'—';
    $('pos-body').innerHTML=`
<div class="pos-dir" style="color:${cl}">${pos.dir} &times; ${pos.contracts_remaining||1}</div>
<div class="pos-grid">
<div class="pos-item"><div class="pos-lb">Entry</div><div class="pos-vl b">${FN(pos.ep)}</div></div>
<div class="pos-item"><div class="pos-lb">Stop Loss</div><div class="pos-vl r">${FN(pos.sl)}</div></div>
<div class="pos-item"><div class="pos-lb">Take Profit</div><div class="pos-vl g">${FN(pos.tp)}</div></div>
<div class="pos-item"><div class="pos-lb">Open P&L</div><div class="pos-vl ${opnl>=0?'g':'r'}">${FP(opnl)}</div></div>
<div class="pos-item"><div class="pos-lb">Risk (ticks)</div><div class="pos-vl">${riskT.toFixed(0)}t</div></div>
<div class="pos-item"><div class="pos-lb">R Multiple</div><div class="pos-vl ${+opnlR>=0?'g':'r'}">${opnlR}R</div></div>
</div>
<div class="r-bar"><div class="r-fill" style="width:${clamp(50+Math.min(50,+opnlR||0)*20,0,100)}%;background:${opnl>=0?'var(--good)':'var(--bad)'}"></div></div>`;
  }else{posTs=null;$('pos-body').innerHTML='<div class="pos-flat">— FLAT —</div>';}

  // Account
  $('a-bal').textContent=`$${FN(bot.account_balance,0)}`;
  $('a-peak').textContent=`$${FN(bot.peak_eod_balance,0)}`;
  $('a-floor').textContent=`$${FN(bot.mll_floor,0)}`;
  const mllRem=bot.mll_remaining||0;
  const mllEl=$('a-mll');mllEl.textContent=`$${Math.round(mllRem)}`;mllEl.style.color=mllRem>1000?'var(--good)':mllRem>300?'var(--warn)':'var(--bad)';
  const mllTotal=(bot.account_balance||50000)-(bot.mll_floor||47500);
  const mllPct=mllTotal>0?clamp(mllRem/mllTotal*100,0,100):100;
  const mllBar=$('mll-bar');mllBar.style.width=mllPct+'%';mllBar.style.background=mllPct>60?'var(--good)':mllPct>25?'var(--warn)':'var(--bad)';
  $('a-name').textContent=bot.account_name||'—';

  // Regime
  $('r-sess').textContent=reg.session_type||'—';
  const vrEl=$('r-vreg');vrEl.textContent=reg.vwap_regime||'—';vrEl.style.color=reg.vwap_regime==='TRENDING'?'var(--bad)':reg.vwap_regime==='BALANCED'?'var(--good)':'var(--muted)';
  const vsEl=$('r-vix');vsEl.textContent=`${reg.vix_proxy?reg.vix_proxy.toFixed(1):'?'} ${reg.vix_status||'?'}`;vsEl.style.color=reg.vix_status==='EXTREME'?'var(--bad)':reg.vix_status==='HIGH'?'var(--warn)':'var(--good)';
  const calEl=$('r-cal');calEl.textContent=reg.calendar_event||'Clear';calEl.style.color=reg.calendar_event?'var(--bad)':'var(--good)';
  $('r-pvw').textContent=FN(reg.prev_vwap);$('r-pvc').textContent=FN(reg.prev_vpoc);

  // Active orders
  const ords=bot.active_orders||[];
  const TY={1:'LMT',2:'MKT',4:'STP'},SD={0:'BUY',1:'SELL'},ST={0:'Pending',1:'Working',2:'Filled',3:'Cancelled',4:'Rejected'};
  $('ord-body').innerHTML=ords.length?ords.map(o=>{
    const isSL=o.type===4,isTP=(o.type===1&&o.customTag&&o.customTag.includes('TP'));
    const role=isSL?'<span class="r">SL</span>':(isTP?'<span class="g">TP</span>':'<span class="b">ENTRY</span>');
    return`<tr><td>${role}</td><td style="color:${o.side===0?'var(--good)':'var(--bad)'};font-weight:700">${SD[o.side]||'?'}</td><td>${FN(o.limitPrice??o.stopPrice)}</td><td class="m">${o.size??'—'}</td><td class="m">${ST[o.status]||'?'}</td></tr>`;
  }).join(''):'<tr><td colspan="5" class="m" style="text-align:center;padding:6px">No open orders</td></tr>';

  // Trade history
  const hist=(d.bot_trades||[]).slice().reverse().slice(0,20);
  const totFP=hist.filter(t=>t.action==='EXIT').reduce((s,t)=>s+(t.pnl||0),0);
  const hpEl=$('hist-pnl');hpEl.textContent=FP(totFP);hpEl.style.color=totFP>=0?'var(--good)':'var(--bad)';
  $('ohb').innerHTML=hist.map(t=>{
    const isEnter=t.action==='ENTER';
    const pnlCl=(t.pnl||0)>=0?'g':'r';
    return`<tr><td style="font-size:9px;color:${isEnter?'var(--accent)':'var(--muted)'}">${t.action}</td><td style="font-weight:700;color:${t.dir==='LONG'?'var(--good)':'var(--bad)'}">${t.dir?.slice(0,1)||'?'}</td><td>${FN(t.price)}</td><td style="font-weight:700;color:${pnlCl==='g'?'var(--good)':'var(--bad)'}">${isEnter?'—':FP(t.pnl)}</td><td class="m" style="max-width:60px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:9px">${t.reason||''}</td><td class="m">${t.bar_idx??'—'}</td></tr>`;
  }).join('');

  // Volume profile table
  const tv=d.top_vol||[];
  const vpoc=d.vpoc;
  if(tv.length){
    const mV=Math.max(...tv.map(v=>v.vol));
    $('vpoc-lbl').textContent=vpoc?`VPOC ${vpoc.toFixed(2)}`:'';
    $('vb').innerHTML=tv.slice(0,18).map(v=>{
      const isP=vpoc&&Math.abs(v.price-vpoc)<0.001;
      const w=Math.round((v.vol/mV)*60);
      return`<tr${isP?' style="background:rgba(176,122,0,.07)"':''}><td${isP?' class="y"':{}}>${F(v.price)}</td><td class="m">${Math.round(v.vol).toLocaleString()}</td><td><div style="width:${w}px;height:6px;background:${isP?'rgba(176,122,0,.6)':'rgba(47,107,255,.3)'};border-radius:2px"></div></td></tr>`;
    }).join('');
  }

  // Recent bars
  const bars2=(d.bars||[]).slice(-12).reverse();
  $('bar-lbl').textContent=`(${(d.bars||[]).length})`;
  $('bb').innerHTML=bars2.map(b=>{
    const zv=+(b.z||0),zc=zv>1.2?'g':zv<-1.2?'r':'m';
    const up=(+b.c)>=(+b.o);
    return`<tr><td class="m">${(b.ts||'').slice(11,16)}</td><td>${FN(b.o)}</td><td class="g">${FN(b.h)}</td><td class="r">${FN(b.l)}</td><td style="font-weight:700;color:${up?'var(--good)':'var(--bad)'}">${FN(b.c)}</td><td class="m">${Math.round(b.v||0)}</td><td class="${zc}">${FN(b.z)}</td></tr>`;
  }).join('');

  // Tape label
  $('tc-lbl').textContent=`(${(d.tape||[]).length} prints)`;

  // Render charts
  const allBars=[...(d.bars||[])];
  if(d.current_bar&&d.current_bar.c)allBars.push(d.current_bar);
  const prof=d.top_vol||[];
  const chartH=Math.max(260,$('chart-wrap').offsetHeight);
  candles('c1',allBars,chartH,d.bot_trades,prof,bot.pos,
    {last:d.live_last,bid:d.live_bid,ask:d.live_ask,open:d.open,high:d.high,low:d.low},90);
  tickC('tc','vp',d.tape,prof,($('tc-wrap').offsetHeight||160),allBars);
}

/* ═══════════════════════════════════════════════════════════════
   CANDLES — exact port of Python combine_live.py candles()
   ═══════════════════════════════════════════════════════════════ */
function candles(id,bars,H,bot_trades,profile,botPos,liveVals,maxTradeBars){
  const c=$(id);if(!c)return;
  c._chartArgs={id,bars,H,bot_trades,profile,botPos,liveVals,maxTradeBars};
  const rerender=()=>{const a=c._chartArgs;if(a)candles(a.id,a.bars,a.H,a.bot_trades,a.profile,a.botPos,a.liveVals,a.maxTradeBars);};

  if(!c._interactiveBound){
    c.addEventListener('wheel',e=>{
      const a=c._chartArgs;if(!a||!a.bars||!a.bars.length)return;
      e.preventDefault();
      const rect=c.getBoundingClientRect(),xPos=e.clientX-rect.left;
      const padL=52,padR=86,cW=Math.max(1,c.clientWidth-padL-padR);
      const oldZoom=clamp(Math.round(chartState.zoom||a.bars.length),40,a.bars.length);
      const maxOldOffset=Math.max(0,a.bars.length-oldZoom);
      const oldOffset=clamp(Math.round(chartState.offset||0),0,maxOldOffset);
      const oldStart=Math.max(0,a.bars.length-oldZoom-oldOffset);
      const ratio=clamp((xPos-padL)/cW,0,1);
      const anchor=oldStart+Math.round(ratio*Math.max(0,oldZoom-1));
      let newZoom=oldZoom+(e.deltaY>0?12:-12);
      newZoom=clamp(newZoom,40,a.bars.length);
      const maxStart=Math.max(0,a.bars.length-newZoom);
      let newStart=Math.round(anchor-ratio*Math.max(0,newZoom-1));
      newStart=clamp(newStart,0,maxStart);
      chartState.zoom=newZoom;
      chartState.offset=a.bars.length-newZoom-newStart;
      rerender();
    },{passive:false});

    c.addEventListener('mousedown',e=>{
      chartState.drag=true;chartState.dragStartX=e.clientX;
      chartState.dragOffset=chartState.offset||0;c.style.cursor='grabbing';
    });
    window.addEventListener('mouseup',()=>{chartState.drag=false;c.style.cursor='crosshair';});
    c.addEventListener('mousemove',e=>{
      const rect=c.getBoundingClientRect();
      chartState.hoverX=e.clientX-rect.left;chartState.hoverY=e.clientY-rect.top;
      if(chartState.drag){
        const a=c._chartArgs;
        if(a&&a.bars&&a.bars.length){
          const padL=52,padR=86,cW=Math.max(1,c.clientWidth-padL-padR);
          const visCount=clamp(Math.round(chartState.zoom||a.bars.length),40,a.bars.length);
          const barPx=Math.max(1,cW/Math.max(1,visCount));
          const dx=e.clientX-chartState.dragStartX,deltaBars=Math.round(dx/barPx);
          const maxOffset=Math.max(0,a.bars.length-visCount);
          chartState.offset=clamp(chartState.dragOffset+deltaBars,0,maxOffset);
        }
      }
      rerender();
    });
    c.addEventListener('mouseleave',()=>{chartState.hoverX=null;chartState.hoverY=null;rerender();});
    c._interactiveBound=true;
  }

  const dp=devicePixelRatio||1,W=c.parentElement.offsetWidth||800;
  c.width=W*dp;c.height=H*dp;c.style.width=W+'px';c.style.height=H+'px';
  const x=c.getContext('2d');x.scale(dp,dp);x.clearRect(0,0,W,H);
  c.style.cursor=chartState.drag?'grabbing':'crosshair';

  if(!bars||!bars.length){
    x.fillStyle='#7a8798';x.font='11px monospace';x.textAlign='center';
    x.fillText('Waiting for market data…',W/2,H/2);return;
  }

  const hasPnl=Array.isArray(bot_trades)&&bot_trades.some(t=>t&&t.action==='EXIT'&&isFinite(Number(t.pnl))&&isFinite(Number(t.bar_idx)));
  const pad={t:10,b:14,l:52,r:86},cW=W-pad.l-pad.r;
  const indH=86,pnlGap=8;

  chartState.zoom=clamp(Math.round(chartState.zoom||Math.min(220,bars.length)),40,bars.length);
  const visCount=clamp(chartState.zoom,40,bars.length);
  const maxOffset=Math.max(0,bars.length-visCount);
  chartState.offset=clamp(Math.round(chartState.offset||0),0,maxOffset);
  const start=Math.max(0,bars.length-visCount-chartState.offset);
  const vis=bars.slice(start,start+visCount);
  const slot=cW/Math.max(1,vis.length),bw=Math.max(2,slot*0.65);
  const priceBottom=H-pad.b-indH-pnlGap;
  const mn=Math.min(...vis.map(b=>+(b.l??b.c??0)))-0.25;
  const mx=Math.max(...vis.map(b=>+(b.h??b.c??0)))+0.25;
  const rng=mx-mn||1,cH=Math.max(80,priceBottom-pad.t);
  const tY=v=>pad.t+(1-(v-mn)/rng)*cH;

  // In-chart volume profile overlay (right side)
  const inR=(profile||[]).filter(v=>+v.price>=mn&&+v.price<=mx);
  const profileW=Math.max(70,Math.floor(cW*0.2));
  const profileX=W-pad.r-profileW;
  if(inR.length){
    const mV=Math.max(...inR.map(v=>+v.vol),1);
    x.fillStyle='rgba(242,246,251,0.92)';x.fillRect(profileX,pad.t,profileW,cH);
    inR.forEach(v=>{
      const y=tY(+v.price),w=(+v.vol/mV)*(profileW-8),bh=Math.max(1,(cH/rng)*0.25+0.5);
      x.fillStyle='rgba(47,107,255,0.28)';x.fillRect(profileX+profileW-w-2,y-bh/2,w,bh);
    });
  }

  // Y-axis grid
  x.fillStyle='#5f7085';x.font='9px monospace';x.textAlign='right';
  for(let i=0;i<=5;i++){
    const v=mn+(rng/5)*i,y=tY(v);
    x.fillText(v.toFixed(2),pad.l-3,y+3);
    x.strokeStyle='#e6ecf3';x.lineWidth=.5;x.beginPath();x.moveTo(pad.l,y);x.lineTo(W-pad.r,y);x.stroke();
  }

  // Candles
  vis.forEach((b,i)=>{
    if(!b.c)return;
    const cx=pad.l+i*slot+slot/2,up=(+(b.c??0))>=(+(b.o??b.c??0)),col=up?'#00a66a':'#d64545';
    x.strokeStyle=col;x.lineWidth=1;x.beginPath();
    x.moveTo(cx,tY(+(b.h??b.c??0)));x.lineTo(cx,tY(+(b.l??b.c??0)));x.stroke();
    const bt=tY(Math.max(+(b.o??b.c??0),+b.c));
    const bb2=tY(Math.min(+(b.o??b.c??0),+b.c));
    x.fillStyle=b._cur?col+'99':col;x.fillRect(cx-bw/2,bt,bw,Math.max(1,bb2-bt));
  });

  // drawLine helper (exact Python port)
  const drawLine=(prop,col,label)=>{
    x.beginPath();let f=true,lastVal=null;
    vis.forEach((b,i)=>{
      if(b[prop]){
        const cx=pad.l+i*slot+slot/2,cy=tY(+b[prop]);
        if(f){x.moveTo(cx,cy);f=false;}else x.lineTo(cx,cy);
        lastVal=+b[prop];
      }
    });
    x.strokeStyle=col;x.lineWidth=1.5;x.stroke();
    if(lastVal){
      const y=tY(lastVal);
      x.fillStyle=col;x.fillRect(W-pad.r,y-6,pad.r,12);
      x.fillStyle='#102033';x.font='8px monospace';x.textAlign='left';
      x.fillText(`${label} ${lastVal.toFixed(1)}`,W-pad.r+2,y+3);
    }
  };
  drawLine('vpoc','#b07a00','VPOC');
  drawLine('vpoc60','#cf8a1a','VPOC60');
  drawLine('vwap','#2f6bff','VWAP');
  drawLine('hma','#7b61ff','HMA');

  // Trade windows (shaded EP-zone boxes) — exact Python port
  const tradeWindows=[];
  if(Array.isArray(bot_trades)&&bot_trades.length){
    let openTrade=null;
    bot_trades.forEach(t=>{
      if(!t||!isFinite(Number(t.bar_idx)))return;
      const bi=Number(t.bar_idx);
      if(t.action==='ENTER'){
        const mb=Math.max(1,Number(t.max_bars||maxTradeBars||90));
        openTrade={entry_idx:bi,dir:t.dir||'LONG',ep:Number(t.price),sl:Number(t.sl),tp:Number(t.tp),max_idx:bi+mb,exit_idx:null};
        tradeWindows.push(openTrade);
      }else if(t.action==='EXIT'&&openTrade&&openTrade.exit_idx===null){
        openTrade.exit_idx=bi;openTrade=null;
      }
    });
  }

  tradeWindows.forEach(tr=>{
    if(!isFinite(tr.ep)||!isFinite(tr.sl)||!isFinite(tr.tp))return;
    const chartEnd=start+vis.length-1;
    const plannedLeft=Math.max(start,Number(tr.entry_idx));
    const plannedRight=Math.min(chartEnd,Number(tr.max_idx));
    if(plannedRight<start||plannedLeft>chartEnd||plannedRight<plannedLeft)return;
    const x1=pad.l+(plannedLeft-start)*slot+slot/2;
    const x2=pad.l+(plannedRight-start)*slot+slot/2;
    const yEP=tY(tr.ep),ySL=tY(tr.sl),yTP=tY(tr.tp);
    const topReward=Math.min(yEP,yTP),hReward=Math.max(1,Math.abs(yEP-yTP));
    const topRisk=Math.min(yEP,ySL),hRisk=Math.max(1,Math.abs(yEP-ySL));
    x.fillStyle='rgba(0,166,106,0.12)';x.fillRect(x1,topReward,Math.max(1,x2-x1),hReward);
    x.fillStyle='rgba(214,69,69,0.12)';x.fillRect(x1,topRisk,Math.max(1,x2-x1),hRisk);
    x.setLineDash([4,3]);
    x.strokeStyle='#d64545';x.lineWidth=1;x.beginPath();x.moveTo(x1,ySL);x.lineTo(x2,ySL);x.stroke();
    x.strokeStyle='#00a66a';x.beginPath();x.moveTo(x1,yTP);x.lineTo(x2,yTP);x.stroke();
    x.setLineDash([]);
    if(isFinite(Number(tr.max_idx))&&tr.max_idx>=start&&tr.max_idx<=chartEnd){
      const xm=pad.l+(Number(tr.max_idx)-start)*slot+slot/2;
      x.setLineDash([2,3]);x.strokeStyle='#b07a00';x.lineWidth=1;
      x.beginPath();x.moveTo(xm,pad.t);x.lineTo(xm,priceBottom);x.stroke();x.setLineDash([]);
      x.fillStyle='#b07a00';x.fillRect(xm-16,pad.t+2,32,10);
      x.fillStyle='#fff';x.textAlign='center';x.font='8px monospace';x.fillText('MAX',xm,pad.t+10);
    }
    if(tr.exit_idx!==null&&isFinite(Number(tr.exit_idx))&&tr.exit_idx>=start&&tr.exit_idx<=chartEnd){
      const xe=pad.l+(Number(tr.exit_idx)-start)*slot+slot/2;
      x.setLineDash([]);x.strokeStyle='#1f2d3d';x.lineWidth=1.2;
      x.beginPath();x.moveTo(xe,pad.t);x.lineTo(xe,priceBottom);x.stroke();
      x.fillStyle='#1f2d3d';x.fillRect(xe-16,pad.t+14,32,10);
      x.fillStyle='#fff';x.textAlign='center';x.font='8px monospace';x.fillText('EXIT',xe,pad.t+22);
    }
  });

  // Active position EP/SL/TP lines
  if(botPos&&botPos.ep){
    const levels=[{p:botPos.ep,c:'#17324f',k:'EP'},{p:botPos.sl,c:'#d64545',k:'SL'},{p:botPos.tp,c:'#00a66a',k:'TP'}].filter(v=>v.p&&isFinite(+v.p));
    levels.forEach(lv=>{
      const y=tY(+lv.p);
      x.setLineDash([4,3]);x.strokeStyle=lv.c;x.lineWidth=1;
      x.beginPath();x.moveTo(pad.l,y);x.lineTo(W-pad.r,y);x.stroke();x.setLineDash([]);
      x.fillStyle=lv.c;x.fillRect(pad.l+2,y-5,34,10);
      x.fillStyle='#fff';x.textAlign='center';x.font='8px monospace';x.fillText(lv.k,pad.l+19,y+3);
    });
  }

  // Live price tick
  const curPrice=vis[vis.length-1]?.c;
  if(curPrice){
    const y=tY(+curPrice);
    x.fillStyle=(+vis[vis.length-1].c>=(+(vis[vis.length-1].o??curPrice)))?'#00a66a':'#d64545';
    x.fillRect(W-pad.r,y-5,pad.r,11);
    x.fillStyle='#102033';x.textAlign='left';x.font='8px monospace';x.fillText((+curPrice).toFixed(1),W-pad.r+2,y+3);
  }

  // Live session guide lines (open/high/low/bid/ask/last)
  const lv=liveVals||{};
  const guides=[{k:'open',lb:'OPEN',c:'#5f7085'},{k:'high',lb:'HIGH',c:'#00a66a'},{k:'low',lb:'LOW',c:'#d64545'},{k:'bid',lb:'BID',c:'#00a66a'},{k:'ask',lb:'ASK',c:'#d64545'},{k:'last',lb:'LAST',c:'#1f2d3d'}];
  guides.forEach(g=>{
    const v=Number(lv[g.k]);
    if(!isFinite(v)||v<=0||v<mn||v>mx)return;
    const y=tY(v);
    x.setLineDash([3,3]);x.strokeStyle=g.c;x.lineWidth=.9;
    x.beginPath();x.moveTo(pad.l,y);x.lineTo(W-pad.r,y);x.stroke();x.setLineDash([]);
    x.fillStyle=g.c;x.fillRect(pad.l+2,y-5,42,10);
    x.fillStyle='#fff';x.textAlign='center';x.font='8px monospace';x.fillText(g.lb,pad.l+23,y+3);
  });

  // Trade entry/exit markers
  if(bot_trades&&bot_trades.length){
    bot_trades.forEach(t=>{
      const localIdx=+t.bar_idx-start;
      if(localIdx<0||localIdx>=vis.length)return;
      const cx=pad.l+localIdx*slot+slot/2,cy=tY(+t.price);
      x.beginPath();x.arc(cx,cy,4,0,Math.PI*2);
      x.fillStyle=t.action==='ENTER'?(t.dir==='LONG'?'#00b578':'#e25555'):'#d19b20';
      x.fill();x.strokeStyle='#1f2d3d';x.lineWidth=1;x.stroke();
      if(t.action==='EXIT'&&t.reason){
        const rs=String(t.reason).length>12?`${String(t.reason).slice(0,12)}…`:String(t.reason);
        x.fillStyle='rgba(247,250,253,.96)';
        x.fillRect(cx+6,cy-10,Math.max(34,rs.length*6)+6,11);
        x.strokeStyle='#d2deea';x.lineWidth=.8;x.strokeRect(cx+6,cy-10,Math.max(34,rs.length*6)+6,11);
        x.fillStyle='#27435e';x.textAlign='left';x.font='8px monospace';x.fillText(rs,cx+9,cy-2);
      }
    });
  }

  // Indicator sub-panel (volume + Z + volatility + P&L)
  {
    const panelTop=priceBottom+pnlGap,panelBottom=H-pad.b,panelH=Math.max(1,panelBottom-panelTop);
    x.fillStyle='rgba(246,249,252,0.95)';x.fillRect(pad.l,panelTop,cW,panelH);
    x.strokeStyle='#e1e9f2';x.lineWidth=1;x.strokeRect(pad.l,panelTop,cW,panelH);

    const volMax=Math.max(1,...vis.map(b=>Number(b.v)||0));
    vis.forEach((b,i)=>{
      const v=Math.max(0,Number(b.v)||0),h=(v/volMax)*panelH;
      const bx=pad.l+i*slot+Math.max(1,slot*0.15),bwv=Math.max(1,slot*0.7);
      x.fillStyle='rgba(143,164,191,0.28)';x.fillRect(bx,panelBottom-h,bwv,h);
    });

    // Volatility (vwap_std)
    const volatVals=vis.map(b=>Math.max(0,Number(b.vwap_std)||0));
    const volatMax=Math.max(0.01,...volatVals);
    const vtY=v=>panelBottom-(Math.max(0,v)/volatMax)*panelH;
    x.strokeStyle='#cf8a1a';x.lineWidth=1.2;x.beginPath();
    volatVals.forEach((v,i)=>{const px=pad.l+i*slot+slot/2,py=vtY(v);if(i===0)x.moveTo(px,py);else x.lineTo(px,py);});
    x.stroke();

    // Z-score
    const zVals=vis.map(b=>{
      if(isFinite(Number(b.z)))return Number(b.z);
      const vw=Number(b.vwap),sd=Number(b.vwap_std),c2=Number(b.c);
      return(isFinite(vw)&&isFinite(sd)&&sd>0.01&&isFinite(c2))?((c2-vw)/sd):0;
    });
    const zCap=Math.max(2,Math.min(6,Math.max(...zVals.map(v=>Math.abs(v||0)),2)));
    const zY=v=>panelTop+(1-((Math.max(-zCap,Math.min(zCap,v))+zCap)/(2*zCap)))*panelH;
    const z0=zY(0);
    x.setLineDash([3,3]);x.strokeStyle='rgba(123,97,255,0.35)';x.lineWidth=1;
    x.beginPath();x.moveTo(pad.l,z0);x.lineTo(W-pad.r,z0);x.stroke();x.setLineDash([]);
    x.strokeStyle='#7b61ff';x.lineWidth=1.4;x.beginPath();
    zVals.forEach((v,i)=>{const px=pad.l+i*slot+slot/2,py=zY(v);if(i===0)x.moveTo(px,py);else x.lineTo(px,py);});
    x.stroke();

    // P&L equity line
    if(hasPnl){
      const exits=(bot_trades||[]).filter(t=>t&&t.action==='EXIT'&&isFinite(Number(t.pnl))&&isFinite(Number(t.bar_idx))).map(t=>({bar_idx:Number(t.bar_idx),pnl:Number(t.pnl)})).sort((a,b)=>a.bar_idx-b.bar_idx);
      let cp=0,ei=0;
      while(ei<exits.length&&exits[ei].bar_idx<start){cp+=exits[ei].pnl;ei++;}
      const pnlVals=[];
      for(let i=0;i<vis.length;i++){const gi=start+i;while(ei<exits.length&&exits[ei].bar_idx<=gi){cp+=exits[ei].pnl;ei++;}pnlVals.push(cp);}
      if(pnlVals.length){
        const pMin=Math.min(0,...pnlVals),pMax=Math.max(0,...pnlVals),pR=Math.max(1,pMax-pMin);
        const pY=v=>panelTop+(1-(v-pMin)/pR)*panelH,p0=pY(0);
        x.setLineDash([4,3]);x.strokeStyle='#b8c6d8';x.lineWidth=1;
        x.beginPath();x.moveTo(pad.l,p0);x.lineTo(W-pad.r,p0);x.stroke();x.setLineDash([]);
        x.strokeStyle='#1f2d3d';x.lineWidth=1.7;x.beginPath();
        pnlVals.forEach((v,i)=>{const px=pad.l+i*slot+slot/2,py=pY(v);if(i===0)x.moveTo(px,py);else x.lineTo(px,py);});
        x.stroke();
        x.fillStyle='#5f7085';x.font='8px monospace';x.textAlign='right';
        x.fillText(FN(pMax,0),pad.l-3,panelTop+8);x.fillText('0',pad.l-3,p0+3);x.fillText(FN(pMin,0),pad.l-3,panelBottom-2);
      }
    }

    // Sub-panel right labels
    x.fillStyle='#5f7085';x.font='8px monospace';x.textAlign='left';
    x.fillText('VOL',W-pad.r+2,panelTop+8);x.fillText('Z',W-pad.r+2,panelTop+18);x.fillText('VOLAT',W-pad.r+2,panelTop+28);
    if(hasPnl)x.fillText('P/L',W-pad.r+2,panelTop+38);
  }

  // Time axis
  x.fillStyle='#70839a';x.font='9px monospace';x.textAlign='center';
  const lC=Math.max(2,Math.floor(cW/60)),step=Math.max(1,Math.floor(vis.length/lC));
  for(let i=0;i<vis.length;i+=step){
    if(!vis[i].ts)continue;
    let d=new Date((vis[i].ts||'').replace(' ','T'));
    if(!isNaN(d))x.fillText(d.toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'2-digit',minute:'2-digit',hour12:false}),pad.l+i*slot+slot/2,H-3);
  }

  // Crosshair + OHLCVWAP tooltip
  if(chartState.hoverX!=null&&chartState.hoverY!=null&&vis.length){
    const idx=clamp(Math.round((chartState.hoverX-pad.l)/Math.max(slot,1)),0,vis.length-1);
    const hb=vis[idx];
    const cx=pad.l+idx*slot+slot/2,cy=tY(+(hb.c||0));
    x.setLineDash([4,3]);x.strokeStyle='rgba(42,68,100,.55)';x.lineWidth=1;
    x.beginPath();x.moveTo(cx,pad.t);x.lineTo(cx,H-pad.b);x.stroke();
    x.beginPath();x.moveTo(pad.l,cy);x.lineTo(W-pad.r,cy);x.stroke();
    x.setLineDash([]);
    const ts=(hb.ts||'').toString();
    const tLabel=ts.includes('T')?ts.slice(11,16):ts.includes(' ')?ts.split(' ')[1].slice(0,5):`#${start+idx}`;
    const info1=`${tLabel}  O:${F(hb.o)} H:${F(hb.h)} L:${F(hb.l)} C:${F(hb.c)} V:${hb.v||0}`;
    const info2=`VWAP:${F(hb.vwap,1)}  VPOC:${F(hb.vpoc,1)}  Z:${FN(hb.z)}  σ:${FN(hb.vwap_std)}`;
    x.font='10px monospace';
    const tw=Math.max(x.measureText(info1).width,x.measureText(info2).width)+12;
    const tx=clamp(cx+10,pad.l+4,W-pad.r-tw-4),ty=pad.t+6;
    x.fillStyle='rgba(247,250,253,.96)';x.fillRect(tx,ty,tw,30);
    x.strokeStyle='#d2deea';x.lineWidth=.8;x.strokeRect(tx,ty,tw,30);
    x.fillStyle='#27435e';x.textAlign='left';x.font='10px monospace';
    x.fillText(info1,tx+6,ty+11);x.fillText(info2,tx+6,ty+23);
  }
}

/* ═══════════════════════════════════════════════════════════════
   TICK CHART — exact port of Python combine_live.py tickC()
   ═══════════════════════════════════════════════════════════════ */
function tickC(tid,pid,tape,prof,H,bars){
  const c=$(tid);if(!c)return;
  const dp=devicePixelRatio||1,W=c.parentElement.offsetWidth||200;
  c.width=W*dp;c.height=H*dp;c.style.width=W+'px';c.style.height=H+'px';
  const x=c.getContext('2d');x.scale(dp,dp);x.clearRect(0,0,W,H);
  const pad={t:10,b:14,l:52,r:4},cW=W-pad.l-pad.r,cH=H-pad.t-pad.b;

  let ap=[];
  if(tape&&tape.length)ap.push(...tape.map(t=>+(t.price||0)));
  if(bars&&bars.length){const lb=bars.slice(-30);ap.push(...lb.map(b=>+(b.h||0)),...lb.map(b=>+(b.l||0)));}
  if(ap.length<2&&prof&&prof.length)ap.push(...prof.map(p=>+p.price));
  if(ap.length<2){x.fillStyle='#7a8798';x.font='11px monospace';x.textAlign='center';x.fillText('Waiting…',W/2,H/2);return;}

  const mn=Math.min(...ap)-0.25,mx=Math.max(...ap)+0.25,rng=mx-mn||1;
  const tY=v=>pad.t+(1-(v-mn)/rng)*cH;

  x.fillStyle='#5f7085';x.font='9px monospace';x.textAlign='right';
  for(let i=0;i<=5;i++){
    const v=mn+(rng/5)*i,y=tY(v);
    x.fillText(v.toFixed(2),pad.l-3,y+3);
    x.strokeStyle='#e6ecf3';x.lineWidth=.5;x.beginPath();x.moveTo(pad.l,y);x.lineTo(W-pad.r,y);x.stroke();
  }

  if(tape&&tape.length>=2){
    const sx=cW/Math.max(tape.length-1,1);
    x.beginPath();
    tape.forEach((t,i)=>{const px=pad.l+i*sx,py=tY(+(t.price||0));if(!i)x.moveTo(px,py);else x.lineTo(px,py);});
    x.strokeStyle='#8fa4bf';x.lineWidth=1;x.stroke();
    tape.forEach((t,i)=>{
      const px=pad.l+i*sx,py=tY(+(t.price||0));
      x.beginPath();x.arc(px,py,1.5,0,Math.PI*2);
      x.fillStyle=t.side==='BUY'?'#00a66a':(t.side==='SELL'?'#d64545':'#7f8fa3');x.fill();
    });
  }else{
    x.fillStyle='#70839a';x.font='10px monospace';x.textAlign='center';x.fillText('No tape prints yet',pad.l+cW/2,H/2);
  }

  // Volume profile sidebar canvas
  const pc=$(pid);if(!pc)return;
  const pW=pc.parentElement.offsetWidth||110;
  pc.width=pW*dp;pc.height=H*dp;pc.style.width=pW+'px';pc.style.height=H+'px';
  const p=pc.getContext('2d');p.scale(dp,dp);p.clearRect(0,0,pW,H);
  if(!prof||!prof.length)return;
  const inR=prof.filter(v=>+v.price>=mn&&+v.price<=mx);if(!inR.length)return;
  const mV=Math.max(...inR.map(v=>+v.vol)),bH2=Math.max(1,(cH/rng)*0.25+0.5);
  inR.forEach(v=>{
    const y=tY(+v.price),w=(+v.vol/mV)*(pW-6);
    p.fillStyle=+v.vol===mV?'#b07a00':'rgba(47,107,255,0.35)';p.fillRect(2,y-bH2/2,w,bH2);
    if(inR.length<=30){p.fillStyle='#70839a';p.font='7px monospace';p.fillText(Math.round(+v.vol)+'',w+4,y+3);}
  });
}

/* ─── Resize observer ─── */
const ro=new ResizeObserver(()=>{if(S)render(S);});
ro.observe($('chart-wrap'));ro.observe($('tc-wrap'));

async function toggleGW(){await fetch('/api/toggle_gw',{method:'POST'});}
async function toggleGL(){await fetch('/api/toggle_gl',{method:'POST'});}
</script>
</body>
</html>"##;
