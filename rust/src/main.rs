mod api;
mod config;
mod feed;
mod market;
mod strategy;
mod tui;
mod types;
mod web;

use std::sync::Arc;
use tokio::sync::{broadcast, mpsc, watch, Mutex, RwLock};
use tracing_subscriber::EnvFilter;

use crate::api::auth;
use crate::config::{
    API, CONTRACT, DATA_DIR, SAVE_INTERVAL_SECS, STATS_INTERVAL_SECS, TICK_DIR, WEB_PORT,
};
use crate::market::regime::{fetch_vix_proxy, load_prev_session_levels};
use crate::market::session::Session;
use crate::strategy::backtest::build_stats;
use crate::types::{AppState, BacktestStats, BarEvent, FillEvent, RegimeState, TickEvent, TradeCommand};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // ── Environment & Logging ──────────────────────────────────────────────────
    dotenvy::dotenv().ok();
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();

    // ── Data directories ───────────────────────────────────────────────────────
    std::fs::create_dir_all(DATA_DIR)?;
    std::fs::create_dir_all(TICK_DIR)?;

    // ── HTTP client ────────────────────────────────────────────────────────────
    let client = Arc::new(
        reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .build()?,
    );

    // ── Auth ───────────────────────────────────────────────────────────────────
    tracing::info!("Logging in...");
    let token_handle = auth::login(&client).await?;
    let token = Arc::new(RwLock::new(token_handle));

    // ── Account & Contract discovery ───────────────────────────────────────────
    let bearer = token.read().await.bearer();

    let accts: serde_json::Value = client
        .post(format!("{API}/Account/search"))
        .header("Authorization", &bearer)
        .json(&serde_json::json!({"onlyActiveAccounts": true}))
        .send()
        .await?
        .json()
        .await?;

    let accts = accts["accounts"].as_array().cloned().unwrap_or_default();
    let prac = accts
        .iter()
        .find(|a| {
            a["name"]
                .as_str()
                .unwrap_or("")
                .to_uppercase()
                .contains("PRAC")
                && a["canTrade"].as_bool().unwrap_or(false)
        })
        .or_else(|| {
            accts
                .iter()
                .find(|a| a["canTrade"].as_bool().unwrap_or(false))
        });

    let account_id = prac
        .and_then(|a| a["id"].as_i64())
        .ok_or_else(|| anyhow::anyhow!("No tradeable account found"))?;
    let account_name = prac
        .and_then(|a| a["name"].as_str())
        .unwrap_or("")
        .to_string();
    tracing::info!("Account: {account_name} (id={account_id})");

    let contracts: serde_json::Value = client
        .post(format!("{API}/Contract/available"))
        .header("Authorization", &bearer)
        .json(&serde_json::json!({"live": false}))
        .send()
        .await?
        .json()
        .await?;

    let contracts_arr = contracts["contracts"].as_array().cloned().unwrap_or_default();
    let contract = contracts_arr
        .iter()
        .find(|c| c["id"].as_str().unwrap_or("") == CONTRACT)
        .ok_or_else(|| anyhow::anyhow!("Contract {CONTRACT} not found"))?;
    let contract_id = contract["id"].as_str().unwrap_or(CONTRACT).to_string();
    tracing::info!("Contract: {contract_id}");

    // ── Regime state ───────────────────────────────────────────────────────────
    let mut regime = RegimeState::new();
    regime.check_calendar();
    load_prev_session_levels(&mut regime);

    // Fetch VIX proxy asynchronously (best effort)
    if let Some(vix) = fetch_vix_proxy(&client, &bearer).await {
        regime.vix_proxy = Some(vix);
        regime.vix_status = Some(if vix > 30.0 {
            "EXTREME"
        } else if vix > 22.0 {
            "HIGH"
        } else {
            "NORMAL"
        }.to_string());
    }

    // ── App state ──────────────────────────────────────────────────────────────
    let state = Arc::new(RwLock::new(AppState {
        session: None,
        bot: {
            let mut snap = crate::types::BotSnapshot::default();
            snap.account_name = Some(account_name.clone());
            snap.gutter_win = true;
            snap.gutter_loss = true;
            snap
        },
        regime,
        connected: false,
        live: Default::default(),
        log: std::collections::VecDeque::new(),
        target_mode: config::TARGET_MODE.to_string(),
        time_stop_mins: config::TIME_STOP_MINS,
        rth: false,
    }));

    // ── Backfill history ───────────────────────────────────────────────────────
    tracing::info!("Backfilling history...");
    if let Err(e) = crate::api::history::backfill(&client, &bearer).await {
        tracing::warn!("Backfill failed: {e}");
    }

    // ── Stats cache ────────────────────────────────────────────────────────────
    let stats_cache: Arc<Mutex<Option<BacktestStats>>> = Arc::new(Mutex::new(None));

    // ── Channels ───────────────────────────────────────────────────────────────
    let (bar_tx, bar_rx) = broadcast::channel::<BarEvent>(256);
    let (tick_tx, tick_rx) = broadcast::channel::<TickEvent>(1024);
    let (cmd_tx, cmd_rx) = mpsc::channel::<TradeCommand>(64);
    let (fill_tx, fill_rx) = mpsc::channel::<FillEvent>(64);
    let (shutdown_tx, shutdown_rx) = watch::channel(false);

    // ── Spawn tasks ────────────────────────────────────────────────────────────

    // WebSocket feed
    tokio::spawn(feed::websocket::run(
        client.clone(),
        token.clone(),
        state.clone(),
        bar_tx,
        tick_tx,
    ));

    // Strategy bot
    tokio::spawn(strategy::bot::run(
        bar_rx,
        tick_rx,
        cmd_tx.clone(),
        fill_rx,
        state.clone(),
    ));

    // Order manager
    tokio::spawn(strategy::order_manager::run(
        client.clone(),
        token.clone(),
        account_id,
        contract_id.clone(),
        cmd_rx,
        fill_tx,
        state.clone(),
    ));

    // Web server
    tokio::spawn(web::server::run(
        state.clone(),
        stats_cache.clone(),
        WEB_PORT,
    ));

    // Background stats worker (every STATS_INTERVAL_SECS)
    {
        let state_c = state.clone();
        let stats_c = stats_cache.clone();
        tokio::spawn(async move {
            let mut interval =
                tokio::time::interval(std::time::Duration::from_secs(STATS_INTERVAL_SECS));
            loop {
                interval.tick().await;
                let sessions = Session::list_sessions();
                let today = chrono::Utc::now().format("%Y-%m-%d").to_string();
                let mut all_trades: Vec<crate::types::TradeRecord> = Vec::new();

                for date in sessions.iter().filter(|d| d.as_str() < today.as_str()).take(60) {
                    if let Ok(sess) = Session::load(date) {
                        let (gw, gl) = {
                            let st = state_c.read().await;
                            (st.bot.gutter_win, st.bot.gutter_loss)
                        };
                        let (_, trades) = build_stats_from_session(&sess.bars, gw, gl);
                        all_trades.extend(trades);
                    }
                }

                let bot_snapshot = state_c.read().await.bot.clone();
                let dummy_bot = crate::strategy::bot::BotState::new();

                let stats = build_stats(&all_trades, config::MAX_TRADES, &dummy_bot);
                *stats_c.lock().await = Some(stats);
                tracing::debug!("Stats cache updated ({} trades)", all_trades.len());
            }
        });
    }

    // Session saver (every SAVE_INTERVAL_SECS)
    {
        let state_c = state.clone();
        tokio::spawn(async move {
            let mut interval =
                tokio::time::interval(std::time::Duration::from_secs(SAVE_INTERVAL_SECS));
            loop {
                interval.tick().await;
                let sess = state_c.read().await.session.clone();
                if let Some(s) = sess {
                    if let Err(e) = s.save() {
                        tracing::warn!("Session save failed: {e}");
                    }
                }
            }
        });
    }

    // ── TUI or headless ────────────────────────────────────────────────────────
    let is_tty = is_terminal::IsTerminal::is_terminal(&std::io::stdout());

    if is_tty {
        tracing::info!("Starting TUI (press q to quit)");
        tui::app::run(state.clone(), shutdown_rx).await?;
    } else {
        tracing::info!("Headless mode — waiting for Ctrl-C");
        tokio::signal::ctrl_c().await?;
    }

    // ── Shutdown ───────────────────────────────────────────────────────────────
    tracing::info!("Shutting down...");
    shutdown_tx.send(true).ok();

    // Final session save
    let sess = state.read().await.session.clone();
    if let Some(s) = sess {
        if let Err(e) = s.save() {
            tracing::warn!("Final session save failed: {e}");
        } else {
            tracing::info!("Session saved");
        }
    }

    Ok(())
}

fn build_stats_from_session(
    bars: &[crate::types::Bar],
    gutter_win: bool,
    gutter_loss: bool,
) -> (serde_json::Value, Vec<crate::types::TradeRecord>) {
    crate::strategy::backtest::run_backtest(bars, gutter_win, gutter_loss)
}
