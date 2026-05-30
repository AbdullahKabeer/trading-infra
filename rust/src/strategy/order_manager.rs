use std::sync::Arc;
use tokio::sync::{mpsc, RwLock};
use tokio::time::{interval, sleep, Duration};
use uuid::Uuid;

use crate::api::auth::TokenHandle;
use crate::api::orders::OrderClient;
use crate::api::positions::{close_action_from_side, search_open};
use crate::config::{CONTRACTS, DRY_RUN, SYNC_INTERVAL_SECS, TICK_SIZE};
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
            // Verify server is flat before entering
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

            if DRY_RUN {
                tracing::info!("[DRY RUN] Enter {dir:?} @ ~{entry_price:.2} SL={sl_price:.2} TP={tp_price:.2}");
                *active_pos_uuid = Some(pos_uuid);
                *orphan_confirm = 0;
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

            match oc.place_market(action, CONTRACTS, Some(sl_price), Some(tp_price), entry_price).await {
                Err(e) => {
                    tracing::error!("place_market failed: {e}");
                    let _ = fill_tx.send(FillEvent::Error(format!("place_market: {e}"))).await;
                    return;
                }
                Ok(resp) => {
                    if !resp["success"].as_bool().unwrap_or(false) {
                        tracing::warn!("place_market returned failure: {resp}");
                        let _ = fill_tx.send(FillEvent::Error(format!("place failed: {resp}"))).await;
                        return;
                    }
                }
            }

            // Wait for fill
            sleep(Duration::from_secs(1)).await;

            // Re-check position to get actual fill price
            let fill_price = match search_open(client, &bearer, account_id, contract_id).await {
                Ok(ServerPositionState::Open { avg_price, .. }) => {
                    avg_price.unwrap_or(entry_price)
                }
                _ => entry_price,
            };

            *active_pos_uuid = Some(pos_uuid);
            *orphan_confirm = 0;

            tracing::info!("ENTERED {dir:?} @ {fill_price:.2} (uuid={pos_uuid})");
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

            // Publish active orders
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

                    if DRY_RUN {
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
                        if DRY_RUN {
                            let _ = fill_tx.send(FillEvent::StopModified { pos_uuid, new_stop }).await;
                        }
                    }
                    Err(e) => tracing::warn!("update_stop failed: {e}"),
                }
            } else if DRY_RUN {
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
                        if DRY_RUN {
                            let _ = fill_tx.send(FillEvent::TpModified { pos_uuid, new_tp }).await;
                        }
                    }
                    Err(e) => tracing::warn!("update_tp failed: {e}"),
                }
            } else if DRY_RUN {
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

            if DRY_RUN {
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
) {
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
            if bot_has_pos {
                // Bot thinks open, server is flat — external bracket fill
                tracing::info!("sync_from_server: server flat but bot has position → ExternalClose");
                let bar_idx = {
                    let st = state.read().await;
                    // Use bar count as proxy for bar_idx
                    st.session.as_ref().map(|s| s.bars.len() as i64).unwrap_or(0)
                };
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

                    if !DRY_RUN {
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
