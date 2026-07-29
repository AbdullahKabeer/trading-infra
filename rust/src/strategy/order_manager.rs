use std::sync::Arc;
use tokio::sync::{mpsc, RwLock};
use tokio::time::{interval, sleep, Duration};
use uuid::Uuid;

use crate::api::auth::TokenHandle;
use crate::api::orders::OrderClient;
use crate::api::positions::{close_action_from_side, search_open};
use crate::config::{CONTRACTS, SYNC_INTERVAL_SECS};
use crate::types::{AppState, Direction, FillEvent, ServerPositionState, TradeCommand};

pub async fn run(
    client: Arc<reqwest::Client>,
    token: Arc<RwLock<TokenHandle>>,
    account_id: i64,
    contract_id: String,
    mut cmd_rx: mpsc::Receiver<TradeCommand>,
    fill_tx: mpsc::Sender<FillEvent>,
    state: Arc<RwLock<AppState>>,
) {
    let mut sync_interval = interval(Duration::from_secs(SYNC_INTERVAL_SECS));
    // Skip the first immediate tick
    sync_interval.tick().await;

    // Track orphan position confirmations: (consecutive_confirmations, last_avg_price)
    let mut orphan_confirm: u32 = 0;
    // Track the current known position uuid (set on Entered, cleared on Closed)
    let mut active_pos_uuid: Option<Uuid> = None;
    // Throttle broker P&L rebase to once per 60s, only when flat
    let mut last_rebase = std::time::Instant::now() - std::time::Duration::from_secs(60);

    loop {
        tokio::select! {
            Some(cmd) = cmd_rx.recv() => {
                maybe_refresh(&client, &token).await;
                handle_command(
                    &client, &token, account_id, &contract_id,
                    cmd, &fill_tx, &state, &mut active_pos_uuid, &mut orphan_confirm,
                ).await;
            }
            _ = sync_interval.tick() => {
                maybe_refresh(&client, &token).await;
                sync_from_server(
                    &client, &token, account_id, &contract_id,
                    &fill_tx, &state, &mut active_pos_uuid, &mut orphan_confirm,
                    &mut last_rebase,
                ).await;
            }
        }
    }
}

async fn maybe_refresh(client: &reqwest::Client, token: &Arc<RwLock<TokenHandle>>) {
    let needs = token.read().await.needs_refresh();
    if needs {
        match crate::api::auth::login(client).await {
            Ok(new_handle) => {
                *token.write().await = new_handle;
                tracing::info!("Token refreshed in OrderManager");
            }
            Err(e) => tracing::warn!("Token refresh failed: {e}"),
        }
    }
}

async fn handle_command(
    client: &reqwest::Client,
    token: &Arc<RwLock<TokenHandle>>,
    account_id: i64,
    contract_id: &str,
    cmd: TradeCommand,
    fill_tx: &mpsc::Sender<FillEvent>,
    state: &Arc<RwLock<AppState>>,
    active_pos_uuid: &mut Option<Uuid>,
    orphan_confirm: &mut u32,
) {
    let is_dry = state.read().await.dry_run;
    let bearer = token.read().await.bearer();
    let oc = OrderClient {
        client,
        bearer: bearer.clone(),
        account_id,
        contract_id: contract_id.to_string(),
    };

    match cmd {
        TradeCommand::Enter {
            dir, sl_price, tp_price, entry_price, entry_vwap,
            tp_buffer_ticks, bar_idx, atr: _,
        } => {
            // Local guard: active_pos_uuid is set immediately when we place an order,
            // long before the API propagates the fill — prevents duplicate entries
            // from rapid bar events while the first fill is still in-flight.
            if active_pos_uuid.is_some() {
                tracing::warn!("Enter: duplicate blocked — already tracking position {:?}", active_pos_uuid);
                let _ = fill_tx.send(FillEvent::Error("duplicate entry blocked".into())).await;
                return;
            }

            // Secondary check: verify server is flat before entering
            match search_open(client, &bearer, account_id, contract_id).await {
                Err(e) => {
                    tracing::warn!("Enter: position check failed ({e}) — aborting entry");
                    let _ = fill_tx.send(FillEvent::Error(format!("position check failed: {e}"))).await;
                    // Reset entry_pending in bot via Error
                    return;
                }
                Ok(ServerPositionState::Unknown) => {
                    tracing::warn!("Enter: position state unknown — aborting entry");
                    let _ = fill_tx.send(FillEvent::Error("position state unknown before entry".into())).await;
                    return;
                }
                Ok(ServerPositionState::Open { .. }) => {
                    tracing::warn!("Enter: server shows open position — aborting entry");
                    let _ = fill_tx.send(FillEvent::Error("server already has position".into())).await;
                    return;
                }
                Ok(ServerPositionState::Flat) => {}
            }

            let action = match dir {
                Direction::Long => "BUY",
                Direction::Short => "SELL",
            };

            let pos_uuid = Uuid::new_v4();

            // Reserve the UUID immediately — any subsequent Enter command will be
            // blocked by the active_pos_uuid guard above, even before the fill lands.
            *active_pos_uuid = Some(pos_uuid);
            *orphan_confirm = 0;

            if is_dry {
                tracing::info!("[DRY RUN] Enter {dir:?} @ ~{entry_price:.2} SL={sl_price:.2} TP={tp_price:.2}");
                let _ = fill_tx.send(FillEvent::Entered {
                    pos_uuid,
                    fill_price: entry_price,
                    dir,
                    sl: sl_price,
                    tp: tp_price,
                    bar_idx,
                    entry_vwap,
                    tp_buffer_ticks,
                }).await;
                return;
            }

            // Place limit entry at signal price — fills at exact price, zero slippage.
            let entry_order_id = match oc.place_entry_limit(action, CONTRACTS, entry_price).await {
                Err(e) => {
                    tracing::error!("place_entry_limit failed: {e}");
                    *active_pos_uuid = None;
                    let _ = fill_tx.send(FillEvent::Error(format!("place_entry_limit: {e}"))).await;
                    return;
                }
                Ok(resp) if !resp["success"].as_bool().unwrap_or(false) => {
                    tracing::warn!("Entry limit rejected: {resp}");
                    *active_pos_uuid = None;
                    let _ = fill_tx.send(FillEvent::Error(format!("entry rejected: {resp}"))).await;
                    return;
                }
                Ok(resp) => resp["orderId"].as_i64().unwrap_or(0),
            };

            // Poll active_orders every 500 ms until fill confirmed (max 60 s).
            // On each tick we check whether the entry order_id has left the open-orders
            // list (status Filled/Cancelled/Expired/Rejected) and then read the actual
            // fill price from the open position.
            let fill_price = 'poll: {
                for attempt in 0usize..120 {
                    sleep(Duration::from_millis(500)).await;

                    let orders = match oc.active_orders().await {
                        Ok(o) => o,
                        Err(e) => {
                            tracing::warn!("active_orders poll failed (attempt {attempt}): {e}");
                            continue;
                        }
                    };

                    match orders.iter().find(|o| o["id"].as_i64().unwrap_or(0) == entry_order_id) {
                        // Order gone from open list — check whether position opened
                        None => {
                            match search_open(client, &bearer, account_id, contract_id).await {
                                Ok(ServerPositionState::Open { avg_price, .. }) => {
                                    tracing::info!("Entry filled @ {avg_price:?}");
                                    break 'poll avg_price.unwrap_or(entry_price);
                                }
                                _ => {
                                    // Small race: order disappeared but position not visible yet.
                                    // Give the server one more tick.
                                    if attempt + 1 < 120 { continue; }
                                    break 'poll entry_price; // best-effort fallback
                                }
                            }
                        }
                        // Order explicitly filled
                        Some(o) if o["status"].as_i64() == Some(2) => {
                            let fp = o["filledPrice"].as_f64().unwrap_or(entry_price);
                            tracing::info!("Entry filled (status=2) @ {fp:.2}");
                            break 'poll fp;
                        }
                        // Cancelled / expired / rejected — give up on this signal
                        Some(o) if matches!(o["status"].as_i64(), Some(3) | Some(4) | Some(5)) => {
                            let st = o["status"].as_i64().unwrap_or(-1);
                            tracing::warn!("Entry limit not filled: order_status={st}");
                            *active_pos_uuid = None;
                            *orphan_confirm = 0;
                            let _ = fill_tx.send(FillEvent::Error(format!("entry order status={st}"))).await;
                            return;
                        }
                        // Still open (status 0/1/6) — keep polling
                        _ => continue,
                    }
                }

                // 60-second timeout — cancel the stale entry order and abort
                tracing::warn!("Entry limit timed out — cancelling order {entry_order_id}");
                let _ = oc.cancel(entry_order_id).await;
                *active_pos_uuid = None;
                *orphan_confirm = 0;
                let _ = fill_tx.send(FillEvent::Error("entry limit timed out".into())).await;
                return;
            };

            tracing::info!("ENTERED {dir:?} @ {fill_price:.2} (uuid={pos_uuid})");

            // Place SL and TP as explicit orders — no Auto OCO dependency.
            let close_action = if dir == Direction::Long { "SELL" } else { "BUY" };
            match oc.place_stop(close_action, CONTRACTS, sl_price).await {
                Ok(resp) if resp["success"].as_bool().unwrap_or(false) =>
                    tracing::info!("SL placed @ {sl_price:.2} (id={})", resp["orderId"]),
                Ok(resp) => tracing::warn!("SL order failed: {resp}"),
                Err(e)   => tracing::warn!("SL order error: {e}"),
            }
            match oc.place_limit(close_action, CONTRACTS, tp_price).await {
                Ok(resp) if resp["success"].as_bool().unwrap_or(false) =>
                    tracing::info!("TP placed @ {tp_price:.2} (id={})", resp["orderId"]),
                Ok(resp) => tracing::warn!("TP order failed: {resp}"),
                Err(e)   => tracing::warn!("TP order error: {e}"),
            }

            let _ = fill_tx.send(FillEvent::Entered {
                pos_uuid,
                fill_price,
                dir,
                sl: sl_price,
                tp: tp_price,
                bar_idx,
                entry_vwap,
                tp_buffer_ticks,
            }).await;

            publish_active_orders(&oc, fill_tx, state).await;
        }

        TradeCommand::Exit { pos_uuid, reason, price: ref_price, bar_idx } => {
            // Check if we actually have a position
            match search_open(client, &bearer, account_id, contract_id).await {
                Err(e) => {
                    tracing::warn!("Exit: position check failed ({e}) — skip");
                    return;
                }
                Ok(ServerPositionState::Unknown) => {
                    tracing::warn!("Exit: unknown position state — skip");
                    return;
                }
                Ok(ServerPositionState::Flat) => {
                    // Position already closed externally
                    tracing::info!("Exit: server already flat (external close detected)");
                    *active_pos_uuid = None;
                    *orphan_confirm = 0;
                    let _ = fill_tx.send(FillEvent::ExternalClose {
                        fill_price: ref_price,
                        reason: "SERVER FILL".to_string(),
                        bar_idx,
                        size: CONTRACTS,
                    }).await;
                    return;
                }
                Ok(ServerPositionState::Open { size, side, .. }) => {
                    // Cancel brackets first
                    let dir_guess = side.as_ref().and_then(|s| {
                        if let Some(n) = s.as_i64() {
                            Some(if n == 0 { Direction::Long } else { Direction::Short })
                        } else if let Some(st) = s.as_str() {
                            match st.to_uppercase().as_str() {
                                "LONG" | "BUY" | "B" => Some(Direction::Long),
                                _ => Some(Direction::Short),
                            }
                        } else {
                            None
                        }
                    });

                    if let Some(dir) = dir_guess {
                        if let Err(e) = oc.cancel_all_brackets(dir).await {
                            tracing::warn!("cancel_all_brackets failed: {e}");
                        }
                    }

                    // Place closing market order
                    let close_action = side
                        .as_ref()
                        .and_then(|s| close_action_from_side(s))
                        .unwrap_or(if ref_price > 0.0 { "SELL" } else { "BUY" });

                    let close_size = size.max(1);

                    if is_dry {
                        tracing::info!("[DRY RUN] Exit @ {ref_price:.2} reason={reason}");
                        *active_pos_uuid = None;
                        *orphan_confirm = 0;
                        let _ = fill_tx.send(FillEvent::Closed {
                            pos_uuid,
                            fill_price: ref_price,
                            reason,
                            bar_idx,
                            size: close_size,
                        }).await;
                        return;
                    }

                    if let Err(e) = oc.place_market(close_action, close_size, None, None, ref_price).await {
                        tracing::error!("Exit market order failed: {e}");
                        return;
                    }

                    // Wait and verify flat
                    sleep(Duration::from_millis(500)).await;
                    match search_open(client, &bearer, account_id, contract_id).await {
                        Ok(ServerPositionState::Flat) => {
                            *active_pos_uuid = None;
                            *orphan_confirm = 0;
                            tracing::info!("CLOSED @ {ref_price:.2} ({reason})");
                            let _ = fill_tx.send(FillEvent::Closed {
                                pos_uuid,
                                fill_price: ref_price,
                                reason,
                                bar_idx,
                                size: close_size,
                            }).await;
                        }
                        Ok(ServerPositionState::Unknown) => {
                            tracing::warn!("Exit: verification returned Unknown — will retry next sync");
                        }
                        Ok(ServerPositionState::Open { .. }) => {
                            tracing::warn!("Exit: still showing open after close attempt — will retry next sync");
                        }
                        Err(e) => {
                            tracing::warn!("Exit: verification failed ({e}) — will retry next sync");
                        }
                    }
                }
            }
        }

        TradeCommand::ModifyStop { pos_uuid, new_stop } => {
            // Determine direction from bot state
            let dir = {
                let st = state.read().await;
                st.bot.pos.as_ref().map(|p| {
                    if p.dir == "LONG" { Direction::Long } else { Direction::Short }
                })
            };
            if let Some(dir) = dir {
                match oc.update_stop(dir, new_stop).await {
                    Ok(true) => {
                        let _ = fill_tx.send(FillEvent::StopModified { pos_uuid, new_stop }).await;
                    }
                    Ok(false) => {
                        tracing::debug!("update_stop: no matching order found");
                        // Still notify bot so it can update local state
                        if is_dry {
                            let _ = fill_tx.send(FillEvent::StopModified { pos_uuid, new_stop }).await;
                        }
                    }
                    Err(e) => tracing::warn!("update_stop failed: {e}"),
                }
            } else if is_dry {
                let _ = fill_tx.send(FillEvent::StopModified { pos_uuid, new_stop }).await;
            }
        }

        TradeCommand::ModifyTp { pos_uuid, new_tp } => {
            let dir = {
                let st = state.read().await;
                st.bot.pos.as_ref().map(|p| {
                    if p.dir == "LONG" { Direction::Long } else { Direction::Short }
                })
            };
            if let Some(dir) = dir {
                match oc.update_tp(dir, new_tp).await {
                    Ok(true) => {
                        let _ = fill_tx.send(FillEvent::TpModified { pos_uuid, new_tp }).await;
                    }
                    Ok(false) => {
                        tracing::debug!("update_tp: no matching order found");
                        if is_dry {
                            let _ = fill_tx.send(FillEvent::TpModified { pos_uuid, new_tp }).await;
                        }
                    }
                    Err(e) => tracing::warn!("update_tp failed: {e}"),
                }
            } else if is_dry {
                let _ = fill_tx.send(FillEvent::TpModified { pos_uuid, new_tp }).await;
            }
        }

        TradeCommand::CancelAllBrackets { dir } => {
            if let Err(e) = oc.cancel_all_brackets(dir).await {
                tracing::warn!("cancel_all_brackets failed: {e}");
            }
        }

        TradeCommand::ScaleOut { pos_uuid, price, bar_idx } => {
            let dir = {
                let st = state.read().await;
                st.bot.pos.as_ref().map(|p| {
                    if p.dir == "LONG" { Direction::Long } else { Direction::Short }
                })
            };

            if is_dry {
                let _ = fill_tx.send(FillEvent::ScaledOut {
                    pos_uuid,
                    fill_price: price,
                    contracts_sold: 1,
                    bar_idx,
                }).await;
                return;
            }

            // Cancel TP bracket
            if let Some(dir) = dir {
                let orders = oc.active_orders().await.unwrap_or_default();
                let expected_close_side = if dir == Direction::Long { 1i64 } else { 0i64 };
                for o in &orders {
                    if o["contractId"].as_str().unwrap_or("") == contract_id
                        && o["type"].as_i64() == Some(1)
                        && o["side"].as_i64() == Some(expected_close_side)
                        && matches!(o["status"].as_i64(), Some(0) | Some(1))
                    {
                        let oid = o["id"].as_i64().unwrap_or(0);
                        let _ = oc.cancel(oid).await;
                    }
                }
            }

            // Place 1-lot market close
            let close_action = match dir {
                Some(Direction::Long) => "SELL",
                Some(Direction::Short) => "BUY",
                None => {
                    tracing::warn!("ScaleOut: unknown direction");
                    return;
                }
            };

            match oc.place_market(close_action, 1, None, None, price).await {
                Ok(resp) if resp["success"].as_bool().unwrap_or(false) => {
                    let _ = fill_tx.send(FillEvent::ScaledOut {
                        pos_uuid,
                        fill_price: price,
                        contracts_sold: 1,
                        bar_idx,
                    }).await;
                }
                Ok(resp) => tracing::warn!("ScaleOut market order failed: {resp}"),
                Err(e) => tracing::warn!("ScaleOut place_market error: {e}"),
            }
        }

        TradeCommand::ToggleGutterWin => {
            state.write().await.bot.gutter_win = !state.read().await.bot.gutter_win;
            tracing::info!("gutter_win toggled to {}", state.read().await.bot.gutter_win);
        }

        TradeCommand::ToggleGutterLoss => {
            state.write().await.bot.gutter_loss = !state.read().await.bot.gutter_loss;
            tracing::info!("gutter_loss toggled to {}", state.read().await.bot.gutter_loss);
        }
    }
}

async fn sync_from_server(
    client: &reqwest::Client,
    token: &Arc<RwLock<TokenHandle>>,
    account_id: i64,
    contract_id: &str,
    fill_tx: &mpsc::Sender<FillEvent>,
    state: &Arc<RwLock<AppState>>,
    active_pos_uuid: &mut Option<Uuid>,
    orphan_confirm: &mut u32,
    last_rebase: &mut std::time::Instant,
) {
    let is_dry = state.read().await.dry_run;
    let bearer = token.read().await.bearer();
    let oc = OrderClient {
        client,
        bearer: bearer.clone(),
        account_id,
        contract_id: contract_id.to_string(),
    };

    // Publish active orders
    publish_active_orders(&oc, fill_tx, state).await;

    // Check if bot thinks it has a position
    let bot_has_pos = state.read().await.bot.pos.is_some();

    match search_open(client, &bearer, account_id, contract_id).await {
        Err(e) => {
            tracing::debug!("sync_from_server: position check error ({e}) — skipping");
        }
        Ok(ServerPositionState::Unknown) => {
            tracing::debug!("sync_from_server: unknown state — skipping");
        }
        Ok(ServerPositionState::Flat) => {
            // Rebase daily P&L from broker once per 60s while flat
            if !bot_has_pos && last_rebase.elapsed() >= std::time::Duration::from_secs(60) {
                *last_rebase = std::time::Instant::now();
                let today_start = chrono::Utc::now().format("%Y-%m-%dT00:00:00.000Z").to_string();
                match crate::api::trades::search(client, &bearer, account_id, &today_start, None).await {
                    Ok(trades) => {
                        let pnl: f64 = trades.iter()
                            .filter_map(|t| {
                                let gross = t["profitAndLoss"].as_f64()?;
                                let fees  = t["fees"].as_f64().unwrap_or(0.0);
                                let comm  = t["commissions"].as_f64().unwrap_or(0.0);
                                Some(gross - fees - comm)
                            })
                            .sum();
                        let completed = trades.iter()
                            .filter(|t| !t["profitAndLoss"].is_null())
                            .count() as u32;
                        let _ = fill_tx.send(FillEvent::Rebase { pnl, trades: completed }).await;
                    }
                    Err(e) => tracing::debug!("P&L rebase fetch failed: {e}"),
                }
            }

            if bot_has_pos {
                // Bot thinks open, server is flat — SL or TP bracket filled externally.
                // Cancel the surviving bracket leg before notifying the bot.
                tracing::info!("sync_from_server: server flat but bot has position → ExternalClose");
                let dir = state.read().await.bot.pos.as_ref().map(|p| {
                    if p.dir == "LONG" { Direction::Long } else { Direction::Short }
                });
                if let Some(dir) = dir {
                    if let Err(e) = oc.cancel_all_brackets(dir).await {
                        tracing::warn!("cancel_all_brackets on external close failed: {e}");
                    }
                }
                let bar_idx = state.read().await
                    .session.as_ref().map(|s| s.bars.len() as i64).unwrap_or(0);
                let last_price = state.read().await.live.last;
                *active_pos_uuid = None;
                *orphan_confirm = 0;
                let _ = fill_tx.send(FillEvent::ExternalClose {
                    fill_price: last_price,
                    reason: "SERVER FILL".to_string(),
                    bar_idx,
                    size: CONTRACTS,
                }).await;
            }
        }
        Ok(ServerPositionState::Open { size, avg_price, side }) => {
            if !bot_has_pos && active_pos_uuid.is_none() {
                // Orphan: server has position but bot thinks flat
                *orphan_confirm += 1;
                tracing::warn!(
                    "sync_from_server: orphan position detected (confirm={orphan_confirm}/3) size={size}"
                );

                if *orphan_confirm >= 3 {
                    tracing::warn!("sync_from_server: auto-closing orphan position");
                    *orphan_confirm = 0;

                    let close_action = side
                        .as_ref()
                        .and_then(|s| close_action_from_side(s))
                        .unwrap_or("SELL");

                    if !is_dry {
                        let ref_price = if let Some(p) = avg_price {
                            p
                        } else {
                            state.read().await.live.last
                        };
                        if let Err(e) = oc.place_market(close_action, size.max(1), None, None, ref_price).await {
                            tracing::error!("Orphan close failed: {e}");
                        }
                    } else {
                        tracing::info!("[DRY RUN] Would auto-close orphan position");
                    }
                }
            } else {
                // Server open and bot has pos — reset orphan counter
                *orphan_confirm = 0;
            }
        }
    }
}

async fn publish_active_orders(
    oc: &OrderClient<'_>,
    fill_tx: &mpsc::Sender<FillEvent>,
    state: &Arc<RwLock<AppState>>,
) {
    match oc.active_orders().await {
        Ok(orders) => {
            state.write().await.bot.active_orders = orders.clone();
            let _ = fill_tx.send(FillEvent::ActiveOrders(orders)).await;
        }
        Err(e) => tracing::debug!("active_orders poll failed: {e}"),
    }
}
