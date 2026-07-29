use axum::{
    extract::State,
    http::StatusCode,
    response::{Html, IntoResponse},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::sync::Arc;
use tokio::net::TcpListener;

use crate::backtest_runner::{monte_carlo_pass, run_backtest_pub, run_stat_tests, BtConfig};

// ── API types ──────────────────────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct ApiParams {
    pub dir: Option<String>,
    pub z_thresh: Option<f64>,
    pub atr_stop_ratio: Option<f64>,
    pub min_stop_ticks: Option<i32>,
    pub max_stop_ticks: Option<i32>,
    pub trail_activate: Option<f64>,
    pub trail_distance: Option<f64>,
    pub breakeven_ticks: Option<f64>,
    pub exit_min_ticks: Option<f64>,
    pub time_stop_mins: Option<i64>,
    pub max_trades: Option<u32>,
    pub lunch_skip: Option<bool>,
    pub target_mode: Option<String>,
    pub gutter_win: Option<bool>,
    pub scale_out: Option<bool>,
    pub tick_value: Option<f64>,
    pub daily_profit_cap: Option<f64>,
    pub daily_loss_limit: Option<f64>,
    pub profit_target: Option<f64>,
    pub strategy: Option<String>,
    pub orb_bars: Option<i64>,
    pub orb_target_mult: Option<f64>,
    pub slippage_ticks: Option<f64>,
}

impl ApiParams {
    fn to_config(&self, default_dir: &str) -> (BtConfig, String) {
        let mut cfg = BtConfig::default();
        if let Some(v) = self.z_thresh { cfg.z_thresh = v; }
        if let Some(v) = self.atr_stop_ratio { cfg.atr_stop_ratio = v; }
        if let Some(v) = self.min_stop_ticks { cfg.min_stop_ticks = v; }
        if let Some(v) = self.max_stop_ticks { cfg.max_stop_ticks = v; }
        if let Some(v) = self.trail_activate { cfg.trail_activate = v; }
        if let Some(v) = self.trail_distance { cfg.trail_distance = v; }
        if let Some(v) = self.breakeven_ticks { cfg.breakeven_ticks = v; }
        if let Some(v) = self.exit_min_ticks { cfg.exit_min_ticks = v; }
        if let Some(v) = self.time_stop_mins { cfg.time_stop_mins = v; }
        if let Some(v) = self.max_trades { cfg.max_trades = v; }
        if let Some(v) = self.lunch_skip { cfg.lunch_skip = v; }
        if let Some(ref v) = self.target_mode { cfg.target_mode = v.clone(); }
        if let Some(v) = self.gutter_win { cfg.gutter_win = v; }
        if let Some(v) = self.scale_out { cfg.scale_out = v; }
        if let Some(v) = self.tick_value { cfg.tick_value = v; }
        if let Some(v) = self.daily_profit_cap { cfg.daily_profit_cap = v; }
        if let Some(v) = self.daily_loss_limit { cfg.daily_loss_limit = v; }
        if let Some(v) = self.profit_target { cfg.profit_target = v; }
        if let Some(ref v) = self.strategy { cfg.strategy = v.clone(); }
        if let Some(v) = self.orb_bars { cfg.orb_bars = v; }
        if let Some(v) = self.orb_target_mult { cfg.orb_target_mult = v; }
        if let Some(v) = self.slippage_ticks { cfg.slippage_ticks = v; }
        let dir = self.dir.clone().unwrap_or_else(|| default_dir.to_string());
        (cfg, dir)
    }
}

#[derive(Serialize, Default)]
pub struct ApiStats {
    pub total_pnl: f64,
    pub win_rate: f64,
    pub trades: u32,
    pub max_dd: f64,
    pub avg_win: f64,
    pub avg_loss: f64,
    pub profit_factor: f64,
    pub rr: f64,
    pub expectancy: f64,
    pub max_consec_loss: f64,
    pub avg_trades_day: f64,
    pub active_days: usize,
    pub n_sessions: usize,
}

#[derive(Serialize, Default)]
pub struct ApiMonteCarlo {
    pub pass_rate: f64,
    pub fail_rate: f64,
    pub timeout_rate: f64,
    pub avg_days_to_pass: f64,
    pub avg_days_to_fail: f64,
    pub n_sims: usize,
    pub p5: Vec<f64>,
    pub p50: Vec<f64>,
    pub p95: Vec<f64>,
    // Gambler's ruin analytical baseline (zero-EV)
    pub zero_ev_pass_rate: f64,
}

#[derive(Serialize, Default)]
pub struct ApiStatTests {
    pub n_days: usize,
    pub n_trades: usize,
    pub mean_daily_pnl: f64,
    pub std_daily_pnl: f64,
    pub t_stat: f64,
    pub p_value_mean: f64,
    pub ci_mean_lo: f64,
    pub ci_mean_hi: f64,
    pub cohens_d: f64,
    pub win_rate_ci_lo: f64,
    pub win_rate_ci_hi: f64,
    pub p_value_winrate: f64,
    pub pass_rate_ci_lo: f64,
    pub pass_rate_ci_hi: f64,
    pub p_value_passrate: f64,
    pub power_at_current_n: f64,
    pub n_required_80_power: usize,
    pub sample_adequate: bool,
    pub market_correlation: f64,
    pub market_corr_pvalue: f64,
    pub market_beta: f64,
    pub market_alpha: f64,
    pub market_corr_ci_lo: f64,
    pub market_corr_ci_hi: f64,
}

#[derive(Serialize, Default)]
pub struct ApiPropFirm {
    pub best_day: f64,
    pub consistency_threshold: f64,
    pub consistency_violations: usize,
    pub loss_limit_days: usize,
    pub capped_days: usize,
    pub est_days_to_pass: f64,
    pub mll_breach_days: usize,
    pub mll_distance: f64,
}

#[derive(Serialize)]
pub struct ApiDayRow {
    pub date: String,
    pub trades: u32,
    pub wins: u32,
    pub losses: u32,
    pub daily_pnl: f64,
    pub cumul_pnl: f64,
    pub flag: String,
    pub capped: bool,
    pub loss_limited: bool,
    pub consistency_violation: bool,
}

#[derive(Serialize)]
pub struct ApiResponse {
    pub ok: bool,
    pub error: String,
    pub stats: ApiStats,
    pub prop_firm: ApiPropFirm,
    pub mc: ApiMonteCarlo,
    pub stat_tests: ApiStatTests,
    pub equity_curve: Vec<f64>,
    pub mll_curve: Vec<f64>,
    pub labels: Vec<String>,
    pub days: Vec<ApiDayRow>,
}

// ── Handlers ───────────────────────────────────────────────────────────────────

struct AppState {
    default_dir: String,
}

async fn index() -> Html<&'static str> {
    Html(HTML)
}

async fn run_handler(
    State(state): State<Arc<AppState>>,
    Json(params): Json<ApiParams>,
) -> impl IntoResponse {
    let (cfg, dir) = params.to_config(&state.default_dir);

    let sessions = match crate::backtest_runner::load_sessions_pub(&dir) {
        Ok(s) => s,
        Err(e) => {
            return (StatusCode::OK, Json(ApiResponse {
                ok: false,
                error: e.to_string(),
                ..Default::default()
            }));
        }
    };

    let result = run_backtest_pub(&sessions, &cfg);
    let day_results = &result.days;

    // Stats
    let n_closed = result.wins + result.losses;
    let win_rate = if n_closed > 0 { result.wins as f64 / n_closed as f64 * 100.0 } else { 0.0 };
    let gross_win: f64 = result.trade_log.iter().filter(|t| t.pnl > 0.0 && t.reason != "SCALE OUT").map(|t| t.pnl).sum();
    let gross_loss: f64 = result.trade_log.iter().filter(|t| t.pnl < 0.0 && t.reason != "SCALE OUT").map(|t| t.pnl.abs()).sum();
    let avg_win = if result.wins > 0 { gross_win / result.wins as f64 } else { 0.0 };
    let avg_loss = if result.losses > 0 { gross_loss / result.losses as f64 } else { 0.0 };
    let pf = if gross_loss > 0.0 { gross_win / gross_loss } else { f64::INFINITY };
    let rr = if avg_loss > 0.0 { avg_win / avg_loss } else { 0.0 };
    let expectancy = (win_rate / 100.0 * avg_win) - ((1.0 - win_rate / 100.0) * avg_loss);
    let days_with_trades = day_results.iter().filter(|d| d.trades > 0).count();
    let avg_trades_day = if days_with_trades > 0 { result.total_trades as f64 / days_with_trades as f64 } else { 0.0 };

    // Prop firm metrics
    let best_day = day_results.iter().map(|d| d.daily_pnl).fold(f64::NEG_INFINITY, f64::max);
    let best_day = if best_day == f64::NEG_INFINITY { 0.0 } else { best_day };
    let consistency_threshold = cfg.profit_target * 0.5;
    let consistency_violations = day_results.iter().filter(|d| d.daily_pnl >= consistency_threshold).count();
    let loss_limit_days = day_results.iter().filter(|d| d.loss_limited).count();
    let capped_days = day_results.iter().filter(|d| d.capped).count();
    let daily_exp = if days_with_trades > 0 { result.total_pnl / days_with_trades as f64 } else { 0.0 };
    let est_days = if daily_exp > 0.0 { cfg.profit_target / daily_exp } else { -1.0 };
    let mll_breach_days = {
        let mut peak = 0.0f64;
        day_results.iter().filter(|d| {
            if d.cumul_pnl > peak { peak = d.cumul_pnl; }
            peak - d.cumul_pnl > cfg.trailing_mll
        }).count()
    };

    // Equity + MLL curves
    let mut peak_eod = cfg.account_balance;
    let equity_curve: Vec<f64> = day_results.iter().map(|d| d.cumul_pnl).collect();
    let mll_curve: Vec<f64> = day_results.iter().map(|d| {
        let bal = cfg.account_balance + d.cumul_pnl;
        if bal > peak_eod { peak_eod = bal; }
        let mll = (peak_eod - cfg.trailing_mll) - cfg.account_balance;
        mll
    }).collect();
    let labels: Vec<String> = day_results.iter().map(|d| d.date.clone()).collect();

    let days: Vec<ApiDayRow> = day_results.iter().map(|d| ApiDayRow {
        date: d.date.clone(),
        trades: d.trades,
        wins: d.wins,
        losses: d.losses,
        daily_pnl: d.daily_pnl,
        cumul_pnl: d.cumul_pnl,
        flag: d.flag.to_string(),
        capped: d.capped,
        loss_limited: d.loss_limited,
        consistency_violation: d.daily_pnl >= consistency_threshold,
    }).collect();

    let stat_tests = {
        let active_pnls: Vec<f64> = day_results.iter()
            .filter(|d| d.trades > 0)
            .map(|d| d.daily_pnl)
            .collect();
        // Market return per active day: session open→close move (proxy for daily directional exposure)
        let mkt_map: std::collections::HashMap<&str, f64> = sessions.iter().filter_map(|s| {
            Some((s.date.as_str(), s.bars.last()?.c - s.bars.first()?.c))
        }).collect();
        let active_mkt: Vec<f64> = day_results.iter()
            .filter(|d| d.trades > 0)
            .map(|d| *mkt_map.get(d.date.as_str()).unwrap_or(&0.0))
            .collect();
        let zero_ev = cfg.trailing_mll / (cfg.trailing_mll + cfg.profit_target);
        let st = run_stat_tests(
            &active_pnls,
            &active_mkt,
            result.wins,
            result.losses,
            0.0,
            zero_ev,
            &cfg,
        );
        ApiStatTests {
            n_days: st.n_days,
            n_trades: st.n_trades,
            mean_daily_pnl: st.mean_daily_pnl,
            std_daily_pnl: st.std_daily_pnl,
            t_stat: st.t_stat,
            p_value_mean: st.p_value_mean,
            ci_mean_lo: st.ci_mean_lo,
            ci_mean_hi: st.ci_mean_hi,
            cohens_d: st.cohens_d,
            win_rate_ci_lo: st.win_rate_ci_lo,
            win_rate_ci_hi: st.win_rate_ci_hi,
            p_value_winrate: st.p_value_winrate,
            pass_rate_ci_lo: st.pass_rate_ci_lo,
            pass_rate_ci_hi: st.pass_rate_ci_hi,
            p_value_passrate: st.p_value_passrate,
            power_at_current_n: st.power_at_current_n,
            n_required_80_power: st.n_required_80_power,
            sample_adequate: st.sample_adequate,
            market_correlation: st.market_correlation,
            market_corr_pvalue: st.market_corr_pvalue,
            market_beta: st.market_beta,
            market_alpha: st.market_alpha,
            market_corr_ci_lo: st.market_corr_ci_lo,
            market_corr_ci_hi: st.market_corr_ci_hi,
        }
    };

    (StatusCode::OK, Json(ApiResponse {
        ok: true,
        error: String::new(),
        stats: ApiStats {
            total_pnl: result.total_pnl,
            win_rate,
            trades: result.total_trades,
            max_dd: result.max_dd,
            avg_win,
            avg_loss,
            profit_factor: if pf.is_infinite() { 999.0 } else { pf },
            rr,
            expectancy,
            max_consec_loss: result.max_consec_loss,
            avg_trades_day,
            active_days: days_with_trades,
            n_sessions: sessions.len(),
        },
        prop_firm: ApiPropFirm {
            best_day,
            consistency_threshold,
            consistency_violations,
            loss_limit_days,
            capped_days,
            est_days_to_pass: if est_days > 0.0 { est_days } else { -1.0 },
            mll_breach_days,
            mll_distance: cfg.trailing_mll,
        },
        mc: {
            let daily_pnls: Vec<f64> = day_results.iter().map(|d| d.daily_pnl).collect();
            let mc = monte_carlo_pass(&daily_pnls, &cfg);
            // Gambler's ruin zero-EV baseline: P(pass) = MLL / (MLL + target)
            let zero_ev = cfg.trailing_mll / (cfg.trailing_mll + cfg.profit_target);
            ApiMonteCarlo {
                pass_rate: mc.pass_rate,
                fail_rate: mc.fail_rate,
                timeout_rate: mc.timeout_rate,
                avg_days_to_pass: mc.avg_days_to_pass,
                avg_days_to_fail: mc.avg_days_to_fail,
                n_sims: mc.n_sims,
                p5: mc.p5,
                p50: mc.p50,
                p95: mc.p95,
                zero_ev_pass_rate: zero_ev,
            }
        },
        stat_tests,
        equity_curve,
        mll_curve,
        labels,
        days,
    }))
}

// ── Server entry ───────────────────────────────────────────────────────────────

pub async fn run_server(args: &[String]) -> anyhow::Result<()> {
    let mut dir = "../es_sessions".to_string();
    let mut port: u16 = 8081;

    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--port" => { if let Some(p) = args.get(i + 1) { port = p.parse().unwrap_or(port); } i += 1; }
            "--nq" => { dir = "../nq_sessions".to_string(); }
            "--es" => { dir = "../es_sessions".to_string(); }
            s if !s.starts_with('-') => dir = s.to_string(),
            _ => {}
        }
        i += 1;
    }

    let state = Arc::new(AppState { default_dir: dir.clone() });
    let app = Router::new()
        .route("/", get(index))
        .route("/api/run", post(run_handler))
        .with_state(state);

    println!("Backtest GUI  →  http://localhost:{port}");
    println!("Default dir   →  {dir}");
    println!("Ctrl+C to stop");
    let listener = TcpListener::bind(format!("0.0.0.0:{port}")).await?;
    axum::serve(listener, app).await?;
    Ok(())
}

// ── Embedded HTML ──────────────────────────────────────────────────────────────

impl Default for ApiResponse {
    fn default() -> Self {
        Self {
            ok: false, error: String::new(),
            stats: ApiStats::default(),
            prop_firm: ApiPropFirm::default(),
            mc: ApiMonteCarlo::default(),
            stat_tests: ApiStatTests::default(),
            equity_curve: vec![], mll_curve: vec![], labels: vec![], days: vec![],
        }
    }
}

const HTML: &str = r#"<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backtest · Strategy Lab</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#e6edf3;font-family:'Cascadia Code','JetBrains Mono','Consolas',monospace;font-size:12px;overflow:hidden}
header{background:#161b22;border-bottom:1px solid #30363d;padding:10px 16px;display:flex;align-items:center;gap:12px;height:42px}
header h1{font-size:14px;color:#58a6ff;font-weight:600}
#status{color:#8b949e;font-size:11px;margin-left:auto}
.layout{display:grid;grid-template-columns:260px 1fr;height:calc(100vh - 42px);overflow:hidden}
.sidebar{background:#161b22;border-right:1px solid #30363d;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:12px}
.sidebar h2{color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}
.param-row{margin-bottom:8px}
.param-row .lbl{display:flex;justify-content:space-between;color:#8b949e;margin-bottom:3px;font-size:11px}
.param-row .lbl span{color:#e6edf3;font-weight:600}
.param-row input[type=range]{width:100%;accent-color:#58a6ff;cursor:pointer}
.toggle-row{display:flex;align-items:center;gap:6px;margin-bottom:6px;color:#8b949e;font-size:11px;cursor:pointer}
.toggle-row input{accent-color:#58a6ff;cursor:pointer}
select{background:#21262d;border:1px solid #30363d;color:#e6edf3;padding:3px 6px;border-radius:4px;font-size:11px;width:100%;margin-bottom:8px}
.sep{border:none;border-top:1px solid #21262d;margin:4px 0}
.run-btn{background:#238636;color:#fff;border:none;padding:9px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:700;width:100%;letter-spacing:.5px}
.run-btn:hover{background:#2ea043}
.run-btn:active{background:#1a7f37}
.main{overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px}
.stats-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px}
.card .lbl{color:#8b949e;font-size:10px;margin-bottom:3px;text-transform:uppercase}
.card .val{font-size:16px;font-weight:700}
.pos{color:#3fb950}.neg{color:#f85149}.neu{color:#e6edf3}.warn{color:#d29922}
.prop-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.prop-card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:8px 10px}
.prop-card .lbl{color:#8b949e;font-size:10px;margin-bottom:2px;text-transform:uppercase}
.prop-card .val{font-size:13px;font-weight:700}
.pass{color:#3fb950}.fail{color:#f85149}.risk{color:#d29922}
.chart-area{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:10px;height:180px;position:relative}
.chart-area canvas{max-height:160px}
.table-area{background:#161b22;border:1px solid #30363d;border-radius:6px;overflow:hidden}
table{width:100%;border-collapse:collapse}
th{background:#21262d;color:#8b949e;padding:6px 10px;text-align:right;font-size:10px;text-transform:uppercase;position:sticky;top:0}
th:first-child{text-align:left}
td{padding:5px 10px;border-bottom:1px solid #1c2128;text-align:right;font-size:11px}
td:first-child{text-align:left;color:#8b949e}
tr:hover td{background:#21262d}
.tag{font-size:9px;padding:1px 4px;border-radius:3px;margin-left:4px;font-weight:600}
.tag-flag{background:#30363d;color:#d29922}
.tag-cap{background:#0d3276;color:#58a6ff}
.tag-ltd{background:#3d1a1a;color:#f85149}
.tag-con{background:#4d2d00;color:#d29922}
.section-hd{color:#8b949e;font-size:10px;text-transform:uppercase;letter-spacing:1px}
.mc-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:6px}
.mc-card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:8px 10px}
.mc-card .lbl{color:#8b949e;font-size:10px;margin-bottom:2px;text-transform:uppercase}
.mc-card .val{font-size:18px;font-weight:700}
.mc-card .sub{color:#8b949e;font-size:10px;margin-top:2px}
.mc-pass .val{color:#3fb950}
.stat-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:6px}
.stat-card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:8px 10px}
.stat-card .lbl{color:#8b949e;font-size:10px;margin-bottom:2px;text-transform:uppercase}
.stat-card .val{font-size:14px;font-weight:700}
.stat-card .sub{color:#8b949e;font-size:10px;margin-top:2px}
.sig{color:#3fb950}.insig{color:#f85149}.marg{color:#d29922}
.warn-box{background:#2d1f00;border:1px solid #d29922;border-radius:6px;padding:8px 10px;color:#d29922;font-size:11px;margin-bottom:6px}
</style>
</head>
<body>
<header>
  <h1>⚡ Strategy Lab</h1>
  <span id="sessionInfo" style="color:#58a6ff;font-size:11px"></span>
  <span id="status">Ready</span>
</header>
<div class="layout">
<div class="sidebar">

<div>
<h2>Sessions</h2>
<div class="param-row">
  <div class="lbl">Directory</div>
  <select id="dir" onchange="updateDir()">
    <option value="../es_sessions">ES (S&amp;P 500)</option>
    <option value="../nq_sessions">NQ (Nasdaq-100)</option>
  </select>
</div>
<div class="param-row">
  <div class="lbl">Tick Value <span id="tv_val">$12.50</span></div>
  <input type="range" id="tick_value" min="5" max="12.5" step="7.5" value="12.5" oninput="syncVal('tv','tick_value',2,'$')">
</div>
</div>

<hr class="sep">
<div>
<h2>Strategy</h2>
<select id="strategy" onchange="updateStrategy()">
  <optgroup label="── High Conviction ──">
  <option value="first_pullback">First Pullback ★</option>
  <option value="bar_rejection">Bar Rejection (candlestick)</option>
  <option value="z_cross">Z-Cross Confirmation</option>
  <option value="quiet_fade">Quiet Fade (low volume)</option>
  <option value="delta_fade">Delta Fade (order flow)</option>
  </optgroup>
  <optgroup label="── VWAP Fades ──">
  <option value="fade">VWAP Mean Reversion</option>
  <option value="vwap_reclaim">VWAP Reclaim Fade</option>
  </optgroup>
  <optgroup label="── Statistical ──">
  <option value="ou_vwap">OU/VWAP Mean Reversion</option>
  </optgroup>
  <optgroup label="── Experimental ──">
  <option value="exhaust">Exhaustion Fade</option>
  <option value="regime">Adaptive Regime (Hurst)</option>
  <option value="kalman">Kalman Filter Fade</option>
  <option value="opening_drive">Opening Drive</option>
  <option value="trend">VWAP Trend Follow</option>
  <option value="orb">Opening Range Breakout</option>
  </optgroup>
</select>
<div id="orb_params" style="display:none">
<div class="param-row">
  <div class="lbl">ORB Bars (minutes) <span id="ob_val">30</span></div>
  <input type="range" id="orb_bars" min="5" max="60" step="5" value="30" oninput="syncVal('ob','orb_bars',0)">
</div>
<div class="param-row">
  <div class="lbl">ORB Target Mult <span id="om_val">1.00</span></div>
  <input type="range" id="orb_target_mult" min="0.5" max="3.0" step="0.25" value="1.0" oninput="syncVal('om','orb_target_mult',2)">
</div>
</div>
<div id="ou_info" style="display:none;background:#0d2137;border:1px solid #1f6feb;border-radius:6px;padding:8px 10px;font-size:10px;color:#79c0ff;line-height:1.6;margin-top:6px">
  <strong style="color:#58a6ff">OU/VWAP</strong> — Ornstein-Uhlenbeck mean reversion on VWAP residual.<br>
  Entry/exit thresholds derived via Bertram (2010) analytic optimisation.<br>
  <span style="color:#8b949e">Z Threshold · ATR Stop · Trail · Scale Out are ignored.<br>
  ADF regime gate + half-life window + cost gate control activity.</span>
</div>
</div>

<hr class="sep">
<div>
<h2>Entry Filters</h2>
<div class="param-row">
  <div class="lbl">Z Threshold <span id="z_val">1.20</span></div>
  <input type="range" id="z_thresh" min="0.5" max="3.0" step="0.05" value="1.2" oninput="syncVal('z','z_thresh',2)">
</div>
<div class="param-row">
  <div class="lbl">ATR Stop Ratio <span id="atr_val">0.80</span></div>
  <input type="range" id="atr_stop_ratio" min="0.3" max="1.5" step="0.05" value="0.8" oninput="syncVal('atr','atr_stop_ratio',2)">
</div>
<div class="param-row">
  <div class="lbl">Min Stop Ticks <span id="minst_val">4</span></div>
  <input type="range" id="min_stop_ticks" min="2" max="12" step="1" value="4" oninput="syncVal('minst','min_stop_ticks',0)">
</div>
<div class="param-row">
  <div class="lbl">Max Stop Ticks <span id="maxst_val">12</span></div>
  <input type="range" id="max_stop_ticks" min="4" max="24" step="1" value="12" oninput="syncVal('maxst','max_stop_ticks',0)">
</div>
</div>

<hr class="sep">
<div>
<h2>Exit Management</h2>
<div class="param-row">
  <div class="lbl">Trail Activate (ticks) <span id="ton_val">8</span></div>
  <input type="range" id="trail_activate" min="2" max="20" step="1" value="8" oninput="syncVal('ton','trail_activate',0)">
</div>
<div class="param-row">
  <div class="lbl">Trail Distance (ticks) <span id="tdist_val">4</span></div>
  <input type="range" id="trail_distance" min="1" max="12" step="1" value="4" oninput="syncVal('tdist','trail_distance',0)">
</div>
<div class="param-row">
  <div class="lbl">Breakeven (ticks) <span id="be_val">6</span></div>
  <input type="range" id="breakeven_ticks" min="2" max="16" step="1" value="6" oninput="syncVal('be','breakeven_ticks',0)">
</div>
<div class="param-row">
  <div class="lbl">Time Stop (bars) <span id="ts_val">90</span></div>
  <input type="range" id="time_stop_mins" min="15" max="180" step="5" value="90" oninput="syncVal('ts','time_stop_mins',0)">
</div>
<div class="param-row">
  <div class="lbl">Target Mode</div>
  <select id="target_mode"><option value="vwap">VWAP</option><option value="vpoc">VPOC</option></select>
</div>
</div>

<hr class="sep">
<div>
<h2>Risk &amp; Friction</h2>
<div class="param-row">
  <div class="lbl">Slippage (ticks RT) <span id="slip_val">0.0</span></div>
  <input type="range" id="slippage_ticks" min="0" max="2" step="0.25" value="0" oninput="syncVal('slip','slippage_ticks',2)">
</div>
<div class="param-row">
  <div class="lbl">Max Trades/Day <span id="mt_val">11</span></div>
  <input type="range" id="max_trades" min="1" max="15" step="1" value="11" oninput="syncVal('mt','max_trades',0)">
</div>
<div class="param-row">
  <div class="lbl">Daily Loss Limit ($) <span id="dll_val">OFF</span></div>
  <input type="range" id="daily_loss_limit" min="0" max="2000" step="50" value="0" oninput="syncDll()">
</div>
<div class="param-row">
  <div class="lbl">Daily Profit Cap ($) <span id="dpc_val">$1500</span></div>
  <input type="range" id="daily_profit_cap" min="0" max="3000" step="50" value="1500" oninput="syncDpc()">
</div>
</div>

<hr class="sep">
<div>
<h2>Options</h2>
<label class="toggle-row"><input type="checkbox" id="lunch_skip" checked> Skip Lunch (11:30–12:45)</label>
<label class="toggle-row"><input type="checkbox" id="gutter_win"> Gutter Win Mode</label>
<label class="toggle-row"><input type="checkbox" id="scale_out" checked> Scale Out</label>
</div>

<hr class="sep">
<button class="run-btn" onclick="run()">▶  Run Backtest</button>
<div style="color:#8b949e;font-size:10px;margin-top:4px;text-align:center">or press Enter</div>

</div><!-- sidebar -->

<div class="main" id="main">
  <div id="placeholder" style="display:flex;align-items:center;justify-content:center;height:200px;color:#8b949e;font-size:13px">
    Configure parameters and click Run Backtest
  </div>
</div>
</div><!-- layout -->

<script>
const fmt = (v,d=2)=>v==null?'—':(v>=0?'+':'')+v.toFixed(d);
const fmtDollar = v=>v==null?'—':(v>=0?'+$':'-$')+Math.abs(v).toFixed(2);
function syncVal(id,slid,dec,prefix=''){
  const v=parseFloat(document.getElementById(slid).value);
  document.getElementById(id+'_val').textContent=prefix+v.toFixed(dec);
}
function syncDll(){
  const v=parseFloat(document.getElementById('daily_loss_limit').value);
  document.getElementById('dll_val').textContent=v===0?'OFF':'$'+v.toFixed(0);
}
function syncDpc(){
  const v=parseFloat(document.getElementById('daily_profit_cap').value);
  document.getElementById('dpc_val').textContent=v===0?'OFF':'$'+v.toFixed(0);
}
function updateDir(){
  const d=document.getElementById('dir').value;
  if(d.includes('nq')){document.getElementById('tick_value').value=5;document.getElementById('tv_val').textContent='$5.00';}
  else{document.getElementById('tick_value').value=12.5;document.getElementById('tv_val').textContent='$12.50';}
}
function updateStrategy(){
  const s=document.getElementById('strategy').value;
  document.getElementById('orb_params').style.display=s==='orb'?'block':'none';
  document.getElementById('ou_info').style.display=s==='ou_vwap'?'block':'none';
}
function getParams(){
  const g=id=>parseFloat(document.getElementById(id).value);
  const dll=g('daily_loss_limit');
  const dpc=g('daily_profit_cap');
  return{
    dir:document.getElementById('dir').value,
    strategy:document.getElementById('strategy').value,
    orb_bars:parseInt(g('orb_bars')),
    orb_target_mult:g('orb_target_mult'),
    slippage_ticks:g('slippage_ticks'),
    z_thresh:g('z_thresh'),
    atr_stop_ratio:g('atr_stop_ratio'),
    min_stop_ticks:parseInt(g('min_stop_ticks')),
    max_stop_ticks:parseInt(g('max_stop_ticks')),
    trail_activate:g('trail_activate'),
    trail_distance:g('trail_distance'),
    breakeven_ticks:g('breakeven_ticks'),
    exit_min_ticks:g('breakeven_ticks'),
    time_stop_mins:parseInt(g('time_stop_mins')),
    max_trades:parseInt(g('max_trades')),
    lunch_skip:document.getElementById('lunch_skip').checked,
    gutter_win:document.getElementById('gutter_win').checked,
    scale_out:document.getElementById('scale_out').checked,
    target_mode:document.getElementById('target_mode').value,
    tick_value:g('tick_value'),
    daily_loss_limit:dll===0?1e9:dll,
    daily_profit_cap:dpc===0?1e9:dpc,
    profit_target:3000
  };
}

let chart=null;
async function run(){
  document.getElementById('status').textContent='Running…';
  try{
    const resp=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(getParams())});
    const data=await resp.json();
    if(!data.ok){document.getElementById('status').textContent='Error: '+data.error;return;}
    render(data);
    document.getElementById('status').textContent='Done · '+data.stats.n_sessions+' sessions';
  }catch(e){document.getElementById('status').textContent='Error: '+e.message;}
}

function render(d){
  const s=d.stats;const p=d.prop_firm;const mc=d.mc||{};const st=d.stat_tests||{};
  document.getElementById('sessionInfo').textContent=d.labels[0]+' → '+d.labels[d.labels.length-1]+'  ·  '+s.n_sessions+' days';

  const cl=v=>v>0?'pos':v<0?'neg':'neu';
  const html=`
<div class="stats-grid">
  <div class="card"><div class="lbl">Total P&amp;L</div><div class="val ${cl(s.total_pnl)}">${fmtDollar(s.total_pnl)}</div></div>
  <div class="card"><div class="lbl">Win Rate</div><div class="val ${s.win_rate>=50?'pos':'warn'}">${s.win_rate.toFixed(1)}%</div></div>
  <div class="card"><div class="lbl">Profit Factor</div><div class="val ${cl(s.profit_factor-1)}">${s.profit_factor.toFixed(2)}</div></div>
  <div class="card"><div class="lbl">Max Drawdown</div><div class="val neg">-$${s.max_dd.toFixed(2)}</div></div>
  <div class="card"><div class="lbl">Avg Win</div><div class="val pos">+$${s.avg_win.toFixed(2)}</div></div>
  <div class="card"><div class="lbl">Avg Loss</div><div class="val neg">-$${s.avg_loss.toFixed(2)}</div></div>
  <div class="card"><div class="lbl">R:R</div><div class="val ${cl(s.rr-1)}">${s.rr.toFixed(2)}</div></div>
  <div class="card"><div class="lbl">Expectancy/Trade</div><div class="val ${cl(s.expectancy)}">${fmtDollar(s.expectancy)}</div></div>
</div>
<div class="section-hd" style="margin:4px 0 6px">Prop Firm · TopStep $50K</div>
<div class="prop-grid">
  <div class="prop-card"><div class="lbl">Est. Days to Pass</div><div class="val ${p.est_days_to_pass>0&&p.est_days_to_pass<=60?'pass':p.est_days_to_pass>0?'warn':'fail'}">${p.est_days_to_pass>0?Math.ceil(p.est_days_to_pass)+' days':'N/A (losing)'}</div></div>
  <div class="prop-card"><div class="lbl">MLL Breaches (EOD)</div><div class="val ${p.mll_breach_days===0?'pass':'fail'}">${p.mll_breach_days===0?'✓ SAFE':p.mll_breach_days+' days breached'}</div></div>
  <div class="prop-card"><div class="lbl">Consistency Rule</div><div class="val ${p.consistency_violations===0?'pass':'fail'}">${p.consistency_violations===0?'✓ PASS':p.consistency_violations+' violations >$'+p.consistency_threshold.toFixed(0)}</div></div>
  <div class="prop-card"><div class="lbl">Best Day</div><div class="val ${p.best_day>=p.consistency_threshold?'fail':'pass'}">${fmtDollar(p.best_day)}</div></div>
  ${p.loss_limit_days>0?`<div class="prop-card"><div class="lbl">Daily Loss Limit Hits</div><div class="val warn">${p.loss_limit_days} days stopped</div></div>`:''}
  ${p.capped_days>0?`<div class="prop-card"><div class="lbl">Daily Cap Hits</div><div class="val neu">${p.capped_days} days capped</div></div>`:''}
</div>
<div class="section-hd" style="margin:4px 0 6px">Monte Carlo · 10,000 Simulations</div>
<div class="mc-grid">
  <div class="mc-card mc-pass">
    <div class="lbl">Pass Rate</div>
    <div class="val">${mc.pass_rate!=null?(mc.pass_rate*100).toFixed(1)+'%':'—'}</div>
    <div class="sub">vs ${mc.zero_ev_pass_rate!=null?(mc.zero_ev_pass_rate*100).toFixed(0):'40'}% zero-EV baseline</div>
  </div>
  <div class="mc-card">
    <div class="lbl">Fail Rate</div>
    <div class="val neg">${mc.fail_rate!=null?(mc.fail_rate*100).toFixed(1)+'%':'—'}</div>
    <div class="sub">sim MLL breach</div>
  </div>
  <div class="mc-card">
    <div class="lbl">Timeout Rate</div>
    <div class="val warn">${mc.timeout_rate!=null?(mc.timeout_rate*100).toFixed(1)+'%':'—'}</div>
    <div class="sub">&gt;60 days</div>
  </div>
  <div class="mc-card">
    <div class="lbl">Avg Days to Pass</div>
    <div class="val ${mc.avg_days_to_pass>0?'pos':'neg'}">${mc.avg_days_to_pass>0?mc.avg_days_to_pass.toFixed(1)+' days':'N/A'}</div>
    <div class="sub">when passing</div>
  </div>
</div>
<div class="section-hd" style="margin:4px 0 6px">Statistical Significance</div>
${!st.sample_adequate?`<div class="warn-box">⚠ Small sample (${st.n_days} active days). Results below are estimates — normal approximation is valid for n≥30. Confidence intervals are wide; interpret with caution.</div>`:''}
<div class="stat-grid">
  <div class="stat-card">
    <div class="lbl">Mean Daily P&L</div>
    <div class="val ${st.mean_daily_pnl>0?'pos':'neg'}">${fmtDollar(st.mean_daily_pnl)}</div>
    <div class="sub">95% CI [${fmtDollar(st.ci_mean_lo)}, ${fmtDollar(st.ci_mean_hi)}]</div>
  </div>
  <div class="stat-card">
    <div class="lbl">t-stat / p-value (EV&gt;0)</div>
    <div class="val ${st.p_value_mean<0.05?'sig':st.p_value_mean<0.1?'marg':'insig'}">${st.t_stat!=null?st.t_stat.toFixed(2):'—'} / ${st.p_value_mean<0.001?'<0.001':st.p_value_mean!=null?st.p_value_mean.toFixed(3):'—'}</div>
    <div class="sub">${st.p_value_mean<0.05?'✓ significant α=0.05':st.p_value_mean<0.1?'marginal α=0.10':'✗ not significant'}</div>
  </div>
  <div class="stat-card">
    <div class="lbl">Cohen\'s d (effect size)</div>
    <div class="val ${st.cohens_d>0.5?'sig':st.cohens_d>0.2?'marg':'insig'}">${st.cohens_d!=null?st.cohens_d.toFixed(3):'—'}</div>
    <div class="sub">${st.cohens_d>0.8?'large':st.cohens_d>0.5?'medium':st.cohens_d>0.2?'small':'negligible'}</div>
  </div>
  <div class="stat-card">
    <div class="lbl">Win Rate 95% CI</div>
    <div class="val neu">[${st.win_rate_ci_lo!=null?(st.win_rate_ci_lo*100).toFixed(1):'—'}%, ${st.win_rate_ci_hi!=null?(st.win_rate_ci_hi*100).toFixed(1):'—'}%]</div>
    <div class="sub">p=${st.p_value_winrate<0.001?'<0.001':st.p_value_winrate!=null?st.p_value_winrate.toFixed(3):'—'} vs 50% H₀</div>
  </div>
  <div class="stat-card">
    <div class="lbl">Pass Rate 95% CI</div>
    <div class="val ${st.pass_rate_ci_lo!=null&&st.pass_rate_ci_lo*100>40?'sig':'marg'}">[${st.pass_rate_ci_lo!=null?(st.pass_rate_ci_lo*100).toFixed(1):'—'}%, ${st.pass_rate_ci_hi!=null?(st.pass_rate_ci_hi*100).toFixed(1):'—'}%]</div>
    <div class="sub">bootstrap p=${st.p_value_passrate<0.001?'<0.001':st.p_value_passrate!=null?st.p_value_passrate.toFixed(3):'—'} vs zero-EV</div>
  </div>
  <div class="stat-card">
    <div class="lbl">Power / Required n</div>
    <div class="val ${st.power_at_current_n>0.8?'sig':st.power_at_current_n>0.5?'marg':'insig'}">${st.power_at_current_n!=null?(st.power_at_current_n*100).toFixed(0):'—'}% / ${st.n_required_80_power!=null?st.n_required_80_power:9999} days</div>
    <div class="sub">power at current n · need for 80% power</div>
  </div>
</div>
<div class="section-hd" style="margin:6px 0 4px">Market Correlation · strat P&amp;L vs session move</div>
<div class="stat-grid">
  <div class="stat-card">
    <div class="lbl">Pearson r</div>
    <div class="val ${Math.abs(st.market_correlation||0)<0.3?'sig':Math.abs(st.market_correlation||0)<0.5?'marg':'insig'}">${(st.market_correlation||0).toFixed(3)}</div>
    <div class="sub">95% CI [${(st.market_corr_ci_lo||0).toFixed(3)}, ${(st.market_corr_ci_hi||0).toFixed(3)}]</div>
  </div>
  <div class="stat-card">
    <div class="lbl">p-value (H₀: r=0)</div>
    <div class="val ${(st.market_corr_pvalue||1)<0.05?'insig':(st.market_corr_pvalue||1)<0.1?'marg':'sig'}">${(st.market_corr_pvalue||1)<0.001?'<0.001':(st.market_corr_pvalue||1).toFixed(3)}</div>
    <div class="sub">${(st.market_corr_pvalue||1)<0.05?'✗ market-exposed':'✓ market-neutral'}</div>
  </div>
  <div class="stat-card">
    <div class="lbl">Beta / Alpha</div>
    <div class="val neu">${(st.market_beta||0).toFixed(3)} / ${(st.market_alpha||0)>=0?'+':''}$${Math.abs(st.market_alpha||0).toFixed(0)}</div>
    <div class="sub">$P&amp;L = α + β × session_move</div>
  </div>
</div>
<div class="chart-area">
  <canvas id="chart"></canvas>
</div>
<div class="table-area">
<table>
<thead><tr>
  <th>Date</th><th>Trades</th><th>W</th><th>L</th><th>Day P&amp;L</th><th>Cumul P&amp;L</th><th>Notes</th>
</tr></thead>
<tbody>
${d.days.map(r=>`<tr>
  <td>${r.date}</td>
  <td>${r.trades}</td>
  <td style="color:#3fb950">${r.wins}</td>
  <td style="color:#f85149">${r.losses}</td>
  <td class="${r.daily_pnl>=0?'pos':'neg'}">${fmtDollar(r.daily_pnl)}</td>
  <td class="${r.cumul_pnl>=0?'pos':'neg'}">${fmtDollar(r.cumul_pnl)}</td>
  <td>${r.flag?`<span class="tag tag-flag">${r.flag}</span>`:''
     }${r.capped?'<span class="tag tag-cap">CAP</span>':''
     }${r.loss_limited?'<span class="tag tag-ltd">LTD</span>':''
     }${r.consistency_violation?'<span class="tag tag-con">!CON</span>':''}</td>
</tr>`).join('')}
</tbody>
<tfoot><tr>
  <td><strong>TOTAL</strong></td>
  <td><strong>${s.trades}</strong></td>
  <td style="color:#3fb950"><strong>${d.days.reduce((a,r)=>a+r.wins,0)}</strong></td>
  <td style="color:#f85149"><strong>${d.days.reduce((a,r)=>a+r.losses,0)}</strong></td>
  <td class="${s.total_pnl>=0?'pos':'neg'}"><strong>${fmtDollar(s.total_pnl)}</strong></td>
  <td></td><td></td>
</tr></tfoot>
</table>
</div>
`;

  // Update main — preserve chart canvas
  const main=document.getElementById('main');
  main.innerHTML=html;

  // Chart.js
  const ctx=document.getElementById('chart').getContext('2d');
  if(chart)chart.destroy();
  chart=new Chart(ctx,{
    type:'line',
    data:{
      labels:d.labels,
      datasets:[
        {label:'Equity',data:d.equity_curve,borderColor:'#58a6ff',borderWidth:2,pointRadius:0,fill:false,tension:0.1},
        {label:'MLL Floor',data:d.mll_curve,borderColor:'#f85149',borderWidth:1,borderDash:[4,4],pointRadius:0,fill:false,tension:0},
        {label:'Profit Target',data:d.labels.map(()=>3000),borderColor:'#3fb950',borderWidth:1,borderDash:[4,4],pointRadius:0,fill:false},
      ]
    },
    options:{
      responsive:true,maintainAspectRatio:false,
      animation:{duration:300},
      plugins:{legend:{labels:{color:'#8b949e',font:{size:10}}},tooltip:{mode:'index',intersect:false}},
      scales:{
        x:{ticks:{color:'#8b949e',maxTicksLimit:12,font:{size:10}},grid:{color:'#21262d'}},
        y:{ticks:{color:'#8b949e',font:{size:10},callback:v=>'$'+v.toFixed(0)},grid:{color:'#21262d'}}
      }
    }
  });
}

document.addEventListener('keydown',e=>{if(e.key==='Enter')run();});
window.onload=()=>run();
</script>
</body>
</html>"#;
