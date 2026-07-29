use anyhow::Result;
use futures_util::{SinkExt, StreamExt};
use std::sync::Arc;
use tokio::sync::RwLock;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;

use crate::config::USER_HUB;
use crate::types::AppState;

const SIGNALR_SEP: char = '\x1e';

pub async fn run(
    token: Arc<RwLock<crate::api::auth::TokenHandle>>,
    state: Arc<RwLock<AppState>>,
    account_id: i64,
) {
    let mut backoff = 3u64;
    loop {
        let tok_str = token.read().await.token.clone();
        match connect_and_stream(&tok_str, &state, account_id).await {
            Ok(_) => { backoff = 3; }
            Err(e) => {
                tracing::warn!("UserHub WS error: {e} — reconnecting in {backoff}s");
                tokio::time::sleep(tokio::time::Duration::from_secs(backoff)).await;
                backoff = (backoff * 2).min(60);
            }
        }
    }
}

async fn connect_and_stream(
    token: &str,
    state: &Arc<RwLock<AppState>>,
    account_id: i64,
) -> Result<()> {
    // SignalR negotiate on user hub
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()?;

    let nego: serde_json::Value = client
        .post(format!("{USER_HUB}/negotiate?negotiateVersion=1&access_token={token}"))
        .send()
        .await?
        .json()
        .await?;

    let ct = nego["connectionToken"]
        .as_str()
        .or_else(|| nego["connectionId"].as_str())
        .unwrap_or("")
        .to_string();

    let ws_url = format!("wss://rtc.topstepx.com/hubs/user?id={ct}&access_token={token}");
    let (ws_stream, _) = connect_async(&ws_url).await?;
    let (mut write, mut read) = ws_stream.split();

    // Handshake
    write.send(Message::Text(format!(r#"{{"protocol":"json","version":1}}{SIGNALR_SEP}"#))).await?;
    let _ = read.next().await;

    tracing::info!("UserHub connected");

    // Subscribe to account, orders, positions, trades
    let subs = [
        format!(r#"{{"type":1,"target":"SubscribeAccounts","arguments":[],"invocationId":"u1"}}{SIGNALR_SEP}"#),
        format!(r#"{{"type":1,"target":"SubscribeOrders","arguments":[{account_id}],"invocationId":"u2"}}{SIGNALR_SEP}"#),
        format!(r#"{{"type":1,"target":"SubscribePositions","arguments":[{account_id}],"invocationId":"u3"}}{SIGNALR_SEP}"#),
        format!(r#"{{"type":1,"target":"SubscribeTrades","arguments":[{account_id}],"invocationId":"u4"}}{SIGNALR_SEP}"#),
    ];
    for s in &subs { write.send(Message::Text(s.clone())).await?; }
    tracing::info!("UserHub subscribed (account={account_id})");

    while let Some(msg) = read.next().await {
        let text = match msg? {
            Message::Text(t) => t,
            Message::Ping(d) => { write.send(Message::Pong(d)).await?; continue; }
            Message::Close(_) => return Err(anyhow::anyhow!("UserHub closed by server")),
            _ => continue,
        };

        for chunk in text.split(SIGNALR_SEP) {
            let chunk = chunk.trim();
            if chunk.is_empty() { continue; }
            if let Ok(val) = serde_json::from_str::<serde_json::Value>(chunk) {
                if val["type"].as_i64() == Some(6) { continue; }
                let target = val["target"].as_str().unwrap_or("");
                let args = val["arguments"].as_array().cloned().unwrap_or_default();
                if args.is_empty() { continue; }

                match target {
                    "GatewayUserAccount" => on_account(args, state).await,
                    "GatewayUserOrder"   => on_order(args, state).await,
                    "GatewayUserPosition"=> on_position(args, state).await,
                    "GatewayUserTrade"   => on_trade(args, state).await,
                    _ => {}
                }
            }
        }
    }
    Ok(())
}

async fn on_account(args: Vec<serde_json::Value>, state: &Arc<RwLock<AppState>>) {
    for item in &args {
        if let Some(bal) = item["balance"].as_f64() {
            let mut st = state.write().await;
            st.account_balance_live = Some(bal);
            tracing::debug!("UserHub: account balance={bal:.2}");
        }
    }
}

async fn on_order(args: Vec<serde_json::Value>, state: &Arc<RwLock<AppState>>) {
    // Merge real-time order updates into bot.active_orders
    let mut st = state.write().await;
    for item in &args {
        if !item.is_object() { continue; }
        let oid = item["id"].as_i64().unwrap_or(0);
        let status = item["status"].as_i64().unwrap_or(-1);
        // status 1=Open 2=Filled 3=Cancelled 4=Expired 5=Rejected 6=Pending
        if matches!(status, 2 | 3 | 4 | 5) {
            // Remove closed order from active list
            st.bot.active_orders.retain(|o| o["id"].as_i64().unwrap_or(-1) != oid);
        } else if status == 1 || status == 6 {
            // Upsert open/pending order
            if let Some(existing) = st.bot.active_orders.iter_mut().find(|o| o["id"].as_i64().unwrap_or(-1) == oid) {
                *existing = item.clone();
            } else {
                st.bot.active_orders.push(item.clone());
            }
        }
        let status_str = match status { 1=>"Open", 2=>"Filled", 3=>"Cancelled", 4=>"Expired", 5=>"Rejected", 6=>"Pending", _=>"?" };
        tracing::debug!("UserHub: order id={oid} status={status_str}");
    }
}

async fn on_position(args: Vec<serde_json::Value>, state: &Arc<RwLock<AppState>>) {
    for item in &args {
        if !item.is_object() { continue; }
        let size = item["size"].as_f64().unwrap_or(0.0);
        let avg = item["averagePrice"].as_f64().unwrap_or(0.0);
        let pos_type = item["type"].as_i64().unwrap_or(0); // 1=Long 2=Short
        tracing::debug!("UserHub: position size={size} avg={avg:.2} type={pos_type}");
    }
    let _ = state;
}

async fn on_trade(args: Vec<serde_json::Value>, state: &Arc<RwLock<AppState>>) {
    for item in &args {
        if !item.is_object() { continue; }
        let price = item["price"].as_f64().unwrap_or(0.0);
        let pnl = item["profitAndLoss"].as_f64(); // null = entry, Some = exit
        let fees = item["fees"].as_f64().unwrap_or(0.0);
        let size = item["size"].as_i64().unwrap_or(0);
        let side = item["side"].as_i64().unwrap_or(0); // 0=Bid 1=Ask
        let side_str = if side == 0 { "BUY" } else { "SELL" };
        let voided = item["voided"].as_bool().unwrap_or(false);

        if voided { continue; }

        if let Some(pnl_val) = pnl {
            tracing::info!("UserHub: fill {side_str} {size}x{price:.2} pnl={pnl_val:.2} fees={fees:.2}");
            // Closing fill — update daily P&L displayed in UI
            let mut st = state.write().await;
            st.bot.daily_pnl += pnl_val - fees;
        } else {
            tracing::info!("UserHub: entry fill {side_str} {size}x{price:.2} fees={fees:.2}");
        }
    }
}
