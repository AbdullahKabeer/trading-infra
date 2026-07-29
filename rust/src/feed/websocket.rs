use anyhow::Result;
use chrono::{Datelike, NaiveTime, Timelike, Utc, Weekday};
use chrono_tz::America::New_York;
use futures_util::{SinkExt, StreamExt};
use reqwest::Client;
use std::sync::Arc;
use tokio::sync::{broadcast, RwLock};
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;

use crate::api::history::backfill;
use crate::config::{CONTRACT, HUB};
use crate::market::session::Session;
use crate::types::{AppState, BarEvent, DomLevel, LiveQuote, TickEvent};

const SIGNALR_SEP: char = '\x1e';

pub async fn run(
    client: Arc<Client>,
    token: Arc<tokio::sync::RwLock<crate::api::auth::TokenHandle>>,
    state: Arc<RwLock<AppState>>,
    bar_tx: broadcast::Sender<BarEvent>,
    tick_tx: broadcast::Sender<TickEvent>,
) {
    let mut backoff = 3u64;
    loop {
        {
            let mut th = token.write().await;
            if th.needs_refresh() {
                match crate::api::auth::login(&client).await {
                    Ok(new) => { *th = new; tracing::info!("Token refreshed"); }
                    Err(e) => { tracing::error!("Token refresh failed: {e}"); }
                }
            }
        }

        let bearer = token.read().await.bearer();
        let tok_str = token.read().await.token.clone();

        match connect_and_stream(&client, &bearer, &tok_str, &state, &bar_tx, &tick_tx).await {
            Ok(_) => { backoff = 3; }
            Err(e) => {
                tracing::warn!("WS error: {e} — reconnecting in {backoff}s");
                state.write().await.connected = false;
                tokio::time::sleep(tokio::time::Duration::from_secs(backoff)).await;
                backoff = (backoff * 2).min(30);
            }
        }
    }
}

async fn connect_and_stream(
    client: &Client,
    bearer: &str,
    token: &str,
    state: &Arc<RwLock<AppState>>,
    bar_tx: &broadcast::Sender<BarEvent>,
    tick_tx: &broadcast::Sender<TickEvent>,
) -> Result<()> {
    // SignalR negotiate
    let nego: serde_json::Value = client
        .post(format!("{HUB}/negotiate?negotiateVersion=1&access_token={token}"))
        .send()
        .await?
        .json()
        .await?;

    let ct = nego["connectionToken"]
        .as_str()
        .or_else(|| nego["connectionId"].as_str())
        .unwrap_or("")
        .to_string();

    let ws_url = format!("wss://rtc.topstepx.com/hubs/market?id={ct}&access_token={token}");
    let (ws_stream, _) = connect_async(&ws_url).await?;
    let (mut write, mut read) = ws_stream.split();

    // Handshake
    write.send(Message::Text(format!(r#"{{"protocol":"json","version":1}}{SIGNALR_SEP}"#))).await?;
    let _ = read.next().await; // consume handshake response

    state.write().await.connected = true;
    tracing::info!("WebSocket connected, running backfill...");

    // Backfill missed bars
    {
        let bearer_owned = bearer.to_string();
        let client_ref = client.clone();
        backfill(&client_ref, &bearer_owned).await.ok();
    }

    // Subscribe
    let subs = [
        format!(r#"{{"type":1,"target":"SubscribeContractQuotes","arguments":["{CONTRACT}"],"invocationId":"1"}}{SIGNALR_SEP}"#),
        format!(r#"{{"type":1,"target":"SubscribeContractTrades","arguments":["{CONTRACT}"],"invocationId":"2"}}{SIGNALR_SEP}"#),
        format!(r#"{{"type":1,"target":"SubscribeContractMarketDepth","arguments":["{CONTRACT}"],"invocationId":"3"}}{SIGNALR_SEP}"#),
    ];
    for s in &subs { write.send(Message::Text(s.clone())).await?; }
    tracing::info!("✓ Live stream active ({}) + DOM", CONTRACT);

    // Message loop
    while let Some(msg) = read.next().await {
        let text = match msg? {
            Message::Text(t) => t,
            Message::Ping(d) => { write.send(Message::Pong(d)).await?; continue; }
            Message::Close(_) => return Err(anyhow::anyhow!("WS closed by server")),
            _ => continue,
        };

        for chunk in text.split(SIGNALR_SEP) {
            let chunk = chunk.trim();
            if chunk.is_empty() { continue; }
            if let Ok(val) = serde_json::from_str::<serde_json::Value>(chunk) {
                if val["type"].as_i64() == Some(6) { continue; } // keepalive
                let target = val["target"].as_str().unwrap_or("");
                let args = val["arguments"].as_array().cloned().unwrap_or_default();
                if args.is_empty() { continue; }

                let rth = is_rth();
                match target {
                    "GatewayTrade" => on_trade(args, rth, state, bar_tx, tick_tx).await,
                    "GatewayQuote" => on_quote(args, rth, state).await,
                    "GatewayDepth" => on_depth(args, state).await,
                    _ => {}
                }
            }
        }
    }
    Ok(())
}

async fn on_trade(
    args: Vec<serde_json::Value>,
    rth: bool,
    state: &Arc<RwLock<AppState>>,
    bar_tx: &broadcast::Sender<BarEvent>,
    tick_tx: &broadcast::Sender<TickEvent>,
) {
    let dicts: Vec<&serde_json::Value> = args.iter().flat_map(|a| {
        if a.is_object() { vec![a] }
        else if let Some(arr) = a.as_array() { arr.iter().filter(|x| x.is_object()).collect() }
        else { vec![] }
    }).collect();

    let now = Utc::now();
    let mut st = state.write().await;
    st.last_feed_secs = now.timestamp() as u64;

    // Update live last price even outside RTH
    for td in &dicts {
        if let Some(p) = td["price"].as_f64() {
            st.live.last = p;
        }
    }

    st.rth = rth;

    // Ensure session exists
    let today = now.with_timezone(&New_York).format("%Y-%m-%d").to_string();
    if st.session.as_ref().map(|s| s.date != today).unwrap_or(true) {
        let loaded = Session::load(&today).ok();
        let sess = loaded.unwrap_or_else(|| Session::new(&today));
        st.session = Some(sess);
    }

    let sess = st.session.as_mut().unwrap();

    for td in &dicts {
        let price = match td["price"].as_f64() { Some(p) if p > 0.0 => p, _ => continue };
        let vol = td["volume"].as_f64().unwrap_or(1.0).max(1.0);
        let side = parse_side(&td["side"]);
        sess.add_trade(price, vol, &side, now);
    }

    let snap_price = sess.last;
    let snap_vwap = sess.vwap;
    let snap_vpoc = sess.vpoc;
    let snap_std = sess.vwap_std;
    let bar_idx = sess.bars.len() as i64 + if sess.cur_bar.is_some() { 1 } else { 0 };
    let bar_close_event = sess.bar_just_closed;
    sess.bar_just_closed = false;

    let spread = if sess.ask > 0.0 && sess.bid > 0.0 { sess.ask - sess.bid } else { 0.0 };
    let tod_mins = tod_minutes(&now);

    // TickEvent — every trade
    let tick_ev = TickEvent {
        price: snap_price, vwap: snap_vwap, vpoc: snap_vpoc, std: snap_std,
        bar_idx, rth, tod_mins, spread,
    };
    let _ = tick_tx.send(tick_ev);

    // BarEvent — only on bar close
    if bar_close_event && sess.bars.len() >= 2 {
        let bars = &sess.bars;
        let atr = sess.current_atr();
        let atr_slope = sess.get_atr_slope(10);
        let stable = sess.is_session_stable();

        let mom_5 = if bars.len() >= 5 { snap_price - bars[bars.len()-5].c } else { 0.0 };
        let velocity_10 = if bars.len() >= 10 { snap_price - bars[bars.len()-10].c } else { 0.0 };
        let recent5: Vec<_> = bars.iter().rev().take(5).collect();
        let av_v = recent5.iter().map(|b| b.v).sum::<f64>() / recent5.len() as f64;
        let av_r = recent5.iter().map(|b| b.h - b.l).sum::<f64>() / recent5.len() as f64;
        let cur_v = sess.cur_bar.as_ref().map(|b| b.v).unwrap_or(0.0);
        let cur_range = sess.cur_bar.as_ref().map(|b| b.h - b.l).unwrap_or(0.0);
        let vol_rel = if av_v > 0.0 { cur_v / av_v } else { 1.0 };
        let rng_rel = if av_r > 0.0 { cur_range / av_r } else { 1.0 };
        let cum_vol = sess.total_volume;
        let vol_60 = bars.iter().rev().take(60).map(|b| b.v).sum();
        let s_high = bars.iter().map(|b| b.h).fold(f64::NEG_INFINITY, f64::max).max(snap_price);
        let s_low = bars.iter().map(|b| b.l).fold(f64::INFINITY, f64::min).min(snap_price);
        let sess_pct = if s_high > s_low { (snap_price - s_low) / (s_high - s_low) } else { 0.5 };
        let poc_mig = {
            let hist: Vec<f64> = bars.iter().rev().take(20).map(|b| b.vpoc).collect();
            if hist.len() >= 2 { (hist[0] - hist[hist.len()-1]).abs() / 20.0 } else { 0.0 }
        };
        let sigma_exp = if sess.std_history.len() >= 5 && sess.std_history[sess.std_history.len()-5] > 0.0 {
            snap_std / sess.std_history[sess.std_history.len()-5]
        } else { 1.0 };
        let (prev_bar_o, prev_bar_h, prev_bar_l) = bars.last()
            .map(|b| (b.o, b.h, b.l))
            .unwrap_or((snap_price, snap_price, snap_price));
        let vpoc60 = sess.compute_vpoc60();
        let cum_delta_pct = sess.cum_delta / sess.total_volume.max(1.0);
        let vwap_slope = snap_vwap - bars.iter().rev().nth(1).map(|b| b.vwap).unwrap_or(snap_vwap);

        // Release write lock before sending
        drop(st);

        let bar_ev = BarEvent {
            bar_idx, price: snap_price, vwap: snap_vwap, vpoc: snap_vpoc,
            vpoc60, std: snap_std, vol: cur_v, bar_range: cur_range,
            tod_mins, vwap_dist: snap_price - snap_vwap,
            vpoc_dist: snap_price - snap_vpoc,
            vwap_slope, vwap_vpoc_dist: (snap_vwap - snap_vpoc).abs(),
            mom_5, velocity_10, vol_rel, rng_rel,
            cum_vol, vol_60, poc_mig, sess_pct,
            body_size: snap_price - prev_bar_o,
            wick_low: snap_price.min(prev_bar_l) - prev_bar_l,
            wick_high: prev_bar_h - snap_price.max(prev_bar_h),
            sigma_exp, prev_h: prev_bar_h, prev_l: prev_bar_l,
            atr_slope, atr, spread, cum_delta_pct, stable, rth,
        };
        let _ = bar_tx.send(bar_ev);
        return;
    }

    // Update live quote display
    st.live = LiveQuote { bid: sess.bid, ask: sess.ask, last: sess.last };
}

async fn on_quote(args: Vec<serde_json::Value>, _rth: bool, state: &Arc<RwLock<AppState>>) {
    let now = Utc::now();
    let mut st = state.write().await;
    st.last_feed_secs = now.timestamp() as u64;
    for item in &args {
        if !item.is_object() { continue; }
        if let Some(b) = item["bestBid"].as_f64() { if b > 0.0 { st.live.bid = b; } }
        if let Some(a) = item["bestAsk"].as_f64() { if a > 0.0 { st.live.ask = a; } }
        if st.live.bid > 0.0 && st.live.ask > 0.0 { st.live.last = (st.live.bid + st.live.ask) / 2.0; }
    }
    if let Some(ref mut sess) = st.session {
        for item in &args {
            if item.is_object() {
                let b = item["bestBid"].as_f64().unwrap_or(0.0);
                let a = item["bestAsk"].as_f64().unwrap_or(0.0);
                if b > 0.0 || a > 0.0 { sess.add_quote(b, a, now); }
            }
        }
    }
}

fn parse_side(val: &serde_json::Value) -> String {
    if let Some(n) = val.as_i64() {
        return match n { 1 => "BUY", 2 => "SELL", _ => "" }.to_string();
    }
    if let Some(s) = val.as_str() {
        let u = s.to_uppercase();
        return if ["B","BUY"].contains(&u.as_str()) { "BUY".to_string() }
            else if ["S","SELL","A"].contains(&u.as_str()) { "SELL".to_string() }
            else { u };
    }
    String::new()
}

async fn on_depth(args: Vec<serde_json::Value>, state: &Arc<RwLock<AppState>>) {
    // GatewayDepth payload: { timestamp, type, price, volume, currentVolume }
    // type: 1=Ask 2=Bid 3=BestAsk 4=BestBid 6=Reset 9=NewBestBid 10=NewBestAsk
    let mut st = state.write().await;
    for item in &args {
        if !item.is_object() { continue; }
        let dom_type = item["type"].as_i64().unwrap_or(0);
        let price = match item["price"].as_f64() { Some(p) if p > 0.0 => p, _ => continue };
        let volume = item["volume"].as_f64().unwrap_or(0.0);

        if dom_type == 6 {
            // Reset — clear the DOM
            st.dom.clear();
            continue;
        }

        let is_bid = matches!(dom_type, 2 | 4 | 9);
        let is_ask = matches!(dom_type, 1 | 3 | 10);
        if !is_bid && !is_ask { continue; }

        if let Some(level) = st.dom.iter_mut().find(|l| (l.price - price).abs() < 0.001) {
            if is_bid { level.bid_vol = volume; }
            if is_ask { level.ask_vol = volume; }
        } else {
            let mut level = DomLevel { price, bid_vol: 0.0, ask_vol: 0.0 };
            if is_bid { level.bid_vol = volume; }
            if is_ask { level.ask_vol = volume; }
            st.dom.push(level);
            // Keep DOM bounded — top 20 price levels sorted by price desc
            if st.dom.len() > 40 {
                st.dom.sort_by(|a, b| b.price.partial_cmp(&a.price).unwrap_or(std::cmp::Ordering::Equal));
                st.dom.truncate(40);
            }
        }
    }
}

pub fn is_rth() -> bool {
    let now_et = Utc::now().with_timezone(&New_York);
    let t = now_et.time();
    let wd = now_et.weekday();
    let open = NaiveTime::from_hms_opt(9, 30, 0).unwrap();
    let close = NaiveTime::from_hms_opt(16, 0, 0).unwrap();
    !matches!(wd, Weekday::Sat | Weekday::Sun) && t >= open && t < close
}

fn tod_minutes(now: &chrono::DateTime<Utc>) -> i64 {
    let et = now.with_timezone(&New_York);
    let mins = et.hour() as i64 * 60 + et.minute() as i64;
    mins - (9 * 60 + 30)
}
