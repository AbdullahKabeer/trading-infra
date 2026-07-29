mod api;
mod backtest_runner;
mod backtest_server;
mod config;
mod feed;
mod market;
mod startup_menu;
mod strategy;
mod types;
mod web;

use std::sync::Arc;
use tokio::sync::{broadcast, mpsc, Mutex, RwLock};
use tracing_subscriber::EnvFilter;

use crate::api::auth;
use crate::config::{
    API, DATA_DIR, SAVE_INTERVAL_SECS, STATS_INTERVAL_SECS, TICK_DIR, WEB_PORT,
};
use crate::market::regime::{fetch_vix_proxy, load_prev_session_levels};
use crate::market::session::Session;
use crate::strategy::backtest::build_stats;
use crate::types::{
    AppState, BacktestStats, BarEvent, FillEvent, InstrumentCfg, LiveStrategy, RegimeState,
    TickEvent, TradeCommand,
};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // ── Backtest subcommands — no auth needed ─────────────────────────────────
    let raw_args: Vec<String> = std::env::args().collect();
    if raw_args.get(1).map(|s| s.as_str()) == Some("backtest") {
        backtest_runner::run_from_cli(&raw_args[2..]);
        return Ok(());
    }
    if raw_args.get(1).map(|s| s.as_str()) == Some("backtest-server") {
        return backtest_server::run_server(&raw_args[2..]).await;
    }

    // ── Instrument selection ───────────────────────────────────────────────────
    let has_flag = raw_args.iter().any(|s| s == "--es" || s == "--nq");
    let is_tty = is_terminal::IsTerminal::is_terminal(&std::io::stdin());
    let use_nq = if !has_flag && is_tty {
        match startup_menu::run_menu()? {
            Some(sel) => sel.use_nq,
            None => return Ok(()),
        }
    } else {
        raw_args.iter().any(|s| s == "--nq")
    };
    let (live_strategy, live_instrument) = if use_nq {
        (LiveStrategy::FirstPullback, InstrumentCfg::nq())
    } else {
        (LiveStrategy::VwapReclaim, InstrumentCfg::es())
    };

    // ── Logging ────────────────────────────────────────────────────────────────
    dotenvy::dotenv().ok();
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let log_file = std::fs::File::create("bot.log")?;
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_writer(std::sync::Mutex::new(log_file))
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
    eprintln!("Logging in...");
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
    let tradeable: Vec<_> = accts
        .iter()
        .filter(|a| a["canTrade"].as_bool().unwrap_or(false))
        .collect();

    let (account_id, account_name) = if is_tty && tradeable.len() > 1 {
        let choices: Vec<(String, i64)> = tradeable
            .iter()
            .map(|a| {
                (
                    a["name"].as_str().unwrap_or("?").to_string(),
                    a["id"].as_i64().unwrap_or(0),
                )
            })
            .collect();
        match startup_menu::select_account(&choices)? {
            Some(idx) => (choices[idx].1, choices[idx].0.clone()),
            None => return Ok(()),
        }
    } else {
        let acct = tradeable
            .iter()
            .copied()
            .find(|a| {
                let n = a["name"].as_str().unwrap_or("").to_uppercase();
                n.contains("150") && n.contains("PRAC")
            })
            .or_else(|| {
                tradeable.iter().copied().find(|a| {
                    a["name"].as_str().unwrap_or("").to_uppercase().contains("PRAC")
                })
            })
            .or_else(|| tradeable.first().copied())
            .ok_or_else(|| anyhow::anyhow!("No tradeable account found"))?;
        (
            acct["id"].as_i64().unwrap_or(0),
            acct["name"].as_str().unwrap_or("").to_string(),
        )
    };
    eprintln!("Account: {account_name} (id={account_id})");

    let contracts: serde_json::Value = client
        .post(format!("{API}/Contract/available"))
        .header("Authorization", &bearer)
        .json(&serde_json::json!({"live": false}))
        .send()
        .await?
        .json()
        .await?;

    let contracts_arr = contracts["contracts"].as_array().cloned().unwrap_or_default();
    let target_contract = live_instrument.contract;
    let contract = contracts_arr
        .iter()
        .find(|c| c["id"].as_str().unwrap_or("") == target_contract)
        .ok_or_else(|| {
            let available: Vec<&str> = contracts_arr
                .iter()
                .filter_map(|c| c["id"].as_str())
                .collect();
            anyhow::anyhow!(
                "Contract {target_contract} not found. Available: {available:?}"
            )
        })?;
    let contract_id = contract["id"].as_str().unwrap_or(target_contract).to_string();
    eprintln!("Contract: {contract_id}");

    // ── Regime state ───────────────────────────────────────────────────────────
    let mut regime = RegimeState::new();
    regime.check_calendar();
    load_prev_session_levels(&mut regime);

    if let Some(vix) = fetch_vix_proxy(&client, &bearer).await {
        regime.vix_proxy = Some(vix);
        regime.vix_status = Some(
            if vix > 30.0 {
                "EXTREME"
            } else if vix > 22.0 {
                "HIGH"
            } else {
                "NORMAL"
            }
            .to_string(),
        );
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
        bot_enabled: config::BOT_ACTIVE,
        dry_run: config::DRY_RUN,
        historical_bars: Vec::new(),
        dom: Vec::new(),
        account_balance_live: None,
        last_feed_secs: 0,
    }));

    // ── Backfill history ───────────────────────────────────────────────────────
    eprintln!("Backfilling history...");
    if let Err(e) = crate::api::history::backfill(&client, &bearer).await {
        eprintln!("Backfill warning: {e}");
    }

    // ── Historical bars ────────────────────────────────────────────────────────
    {
        let today = chrono::Utc::now().format("%Y-%m-%d").to_string();
        let sessions = Session::list_sessions();
        let mut hist: Vec<crate::types::Bar> = Vec::new();
        for date in sessions
            .iter()
            .filter(|d| d.as_str() < today.as_str())
            .rev()
            .take(config::DAYS_BACK as usize)
        {
            if let Ok(sess) = Session::load(date) {
                hist.extend(sess.bars);
            }
        }
        let n = hist.len();
        state.write().await.historical_bars = hist;
        tracing::info!("Loaded {n} historical bars from prior sessions");
    }

    // ── Stats cache ────────────────────────────────────────────────────────────
    let stats_cache: Arc<Mutex<Option<BacktestStats>>> = Arc::new(Mutex::new(None));

    // ── Channels ───────────────────────────────────────────────────────────────
    let (bar_tx, bar_rx) = broadcast::channel::<BarEvent>(256);
    let (tick_tx, tick_rx) = broadcast::channel::<TickEvent>(1024);
    let (cmd_tx, cmd_rx) = mpsc::channel::<TradeCommand>(64);
    let (fill_tx, fill_rx) = mpsc::channel::<FillEvent>(64);

    // ── Spawn tasks ────────────────────────────────────────────────────────────

    tokio::spawn(feed::websocket::run(
        client.clone(),
        token.clone(),
        state.clone(),
        bar_tx,
        tick_tx,
    ));

    tokio::spawn(feed::user_hub::run(
        token.clone(),
        state.clone(),
        account_id,
    ));

    // Seed today's P&L from broker
    let (seed_pnl, seed_trades) = {
        let today_start = chrono::Utc::now()
            .format("%Y-%m-%dT00:00:00.000Z")
            .to_string();
        match crate::api::trades::search(&client, &bearer, account_id, &today_start, None).await {
            Ok(trades) => {
                let pnl: f64 = trades
                    .iter()
                    .filter_map(|t| {
                        let gross = t["profitAndLoss"].as_f64()?;
                        let fees = t["fees"].as_f64().unwrap_or(0.0);
                        let comm = t["commissions"].as_f64().unwrap_or(0.0);
                        Some(gross - fees - comm)
                    })
                    .sum();
                let completed: u32 = trades
                    .iter()
                    .filter(|t| !t["profitAndLoss"].is_null())
                    .count() as u32;
                eprintln!("Seeded daily P&L: ${pnl:.2} from {completed} closing fills");
                (pnl, completed)
            }
            Err(e) => {
                eprintln!("Could not seed P&L: {e}");
                (0.0, 0)
            }
        }
    };

    {
        let mut st = state.write().await;
        st.bot.daily_pnl = seed_pnl;
        st.bot.total_pnl = seed_pnl;
        st.bot.trades_today = seed_trades;
    }

    tokio::spawn(strategy::bot::run(
        bar_rx,
        tick_rx,
        cmd_tx.clone(),
        fill_rx,
        state.clone(),
        live_strategy,
        live_instrument,
        seed_pnl,
        seed_trades,
    ));

    tokio::spawn(strategy::order_manager::run(
        client.clone(),
        token.clone(),
        account_id,
        contract_id.clone(),
        cmd_rx,
        fill_tx,
        state.clone(),
    ));

    tokio::spawn(web::server::run(
        state.clone(),
        stats_cache.clone(),
        WEB_PORT,
    ));

    // Background stats worker
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
                for date in sessions
                    .iter()
                    .filter(|d| d.as_str() < today.as_str())
                    .take(60)
                {
                    if let Ok(sess) = Session::load(date) {
                        let (gw, gl) = {
                            let st = state_c.read().await;
                            (st.bot.gutter_win, st.bot.gutter_loss)
                        };
                        let (_, trades) =
                            crate::strategy::backtest::run_backtest(&sess.bars, gw, gl);
                        all_trades.extend(trades);
                    }
                }
                let dummy_bot = crate::strategy::bot::BotState::new(
                    LiveStrategy::VwapReclaim,
                    InstrumentCfg::es(),
                    0.0,
                    0,
                );
                let stats = build_stats(&all_trades, config::MAX_TRADES, &dummy_bot);
                *stats_c.lock().await = Some(stats);
            }
        });
    }

    // Session saver
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

    // ── Open browser & wait for Ctrl-C ─────────────────────────────────────────
    let url = format!("http://localhost:{WEB_PORT}");
    eprintln!("\n  Dashboard → {url}\n  Ctrl-C to stop\n");
    let _ = std::process::Command::new("open").arg(&url).spawn();

    tokio::signal::ctrl_c().await?;
    eprintln!("Shutting down...");

    let sess = state.read().await.session.clone();
    if let Some(s) = sess {
        let _ = s.save();
    }

    Ok(())
}
