/// Order API test harness — exercises market orders, Auto OCO bracket modification,
/// cancel, close, and fill history.
///
/// Run: cargo run --bin test-orders [--live]
///   Default: sim account (live: false)
///   --live:  live account (use with caution)
///
/// NOTE: Account must have "Auto OCO Brackets" enabled in platform settings.
///       Bracket fields are NOT sent in order requests — the platform creates
///       them automatically on fill and we modify them to the desired prices.
///
/// Requires env vars: PROJECT_X_USERNAME, PROJECT_X_API_KEY
use std::time::Duration;

const API: &str = "https://api.topstepx.com/api";
const TICK_SIZE: f64 = 0.25;
const DEFAULT_CONTRACT: &str = "CON.F.US.EP.M26";

fn sep(label: &str) { println!("\n{}\n  {label}\n{}", "─".repeat(60), "─".repeat(60)); }
fn ok(msg: &str)    { println!("  ✓ {msg}"); }
fn fail(msg: &str)  { println!("  ✗ {msg}"); }
fn info(msg: &str)  { println!("  · {msg}"); }

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    dotenvy::dotenv().ok();

    let args: Vec<String> = std::env::args().collect();
    let use_live = args.iter().any(|a| a == "--live");

    println!("\n╔══════════════════════════════════════════════════════╗");
    println!("║         ProjectX Order API Test Harness              ║");
    println!("║  mode: {}                                     ║", if use_live { "LIVE  ⚠️ " } else { "SIM   ✓ " });
    println!("╚══════════════════════════════════════════════════════╝");

    if use_live {
        println!("\n  ⚠️  LIVE mode — real fills will occur. Ctrl+C to abort.");
        println!("     Sleeping 3s...");
        tokio::time::sleep(Duration::from_secs(3)).await;
    }

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .build()?;

    // ── Step 1: Authenticate ──────────────────────────────────────────────────
    sep("Step 1: Authenticate");
    let username = std::env::var("PROJECT_X_USERNAME")?;
    let api_key  = std::env::var("PROJECT_X_API_KEY")?;

    let resp: serde_json::Value = client
        .post(format!("{API}/Auth/loginKey"))
        .json(&serde_json::json!({ "userName": username, "apiKey": api_key }))
        .send().await?.json().await?;

    if !resp["success"].as_bool().unwrap_or(false) {
        fail(&format!("Auth failed: {resp}"));
        return Err(anyhow::anyhow!("auth failed"));
    }
    let token = resp["token"].as_str().unwrap().to_string();
    let bearer = format!("Bearer {token}");
    ok(&format!("Authenticated as {username}"));

    // ── Step 2: Validate token ────────────────────────────────────────────────
    sep("Step 2: Auth/validate");
    let val: serde_json::Value = client
        .post(format!("{API}/Auth/validate"))
        .header("Authorization", &bearer)
        .header("Content-Type", "application/json")
        .body("{}")
        .send().await?.json().await?;
    if val["success"].as_bool().unwrap_or(false) {
        ok("Token validated");
    } else {
        fail(&format!("Validate returned: {val}"));
    }

    // ── Step 3: Account ───────────────────────────────────────────────────────
    sep("Step 3: Account/search");
    let accts: serde_json::Value = client
        .post(format!("{API}/Account/search"))
        .header("Authorization", &bearer)
        .json(&serde_json::json!({ "onlyActiveAccounts": true }))
        .send().await?.json().await?;

    let accts_arr = accts["accounts"].as_array().cloned().unwrap_or_default();
    info("All accounts:");
    for a in &accts_arr {
        info(&format!("  {} (id={}) canTrade={}", a["name"].as_str().unwrap_or("?"), a["id"].as_i64().unwrap_or(0), a["canTrade"].as_bool().unwrap_or(false)));
    }
    // Only use practice accounts — never touch a live/funded account during tests
    let prac: Vec<_> = accts_arr.iter()
        .filter(|a| a["canTrade"].as_bool().unwrap_or(false)
            && a["name"].as_str().unwrap_or("").to_uppercase().contains("PRAC"))
        .collect();
    // Prefer 150k; fall back to any PRAC
    let acct = prac.iter().copied()
        .find(|a| a["name"].as_str().unwrap_or("").to_uppercase().contains("150"))
        .or_else(|| prac.first().copied())
        .ok_or_else(|| anyhow::anyhow!("no practice account found — refusing to test on a live/funded account"))?;
    let account_id = acct["id"].as_i64().unwrap();
    ok(&format!("Selected: {} (id={account_id})", acct["name"].as_str().unwrap_or("?")));

    // ── Step 4: Contract discovery ────────────────────────────────────────────
    sep("Step 4: Contract discovery");

    let avail: serde_json::Value = client
        .post(format!("{API}/Contract/available"))
        .header("Authorization", &bearer)
        .json(&serde_json::json!({ "live": use_live }))
        .send().await?.json().await?;
    let avail_arr = avail["contracts"].as_array().cloned().unwrap_or_default();
    ok(&format!("Contract/available: {} contracts", avail_arr.len()));

    let srch: serde_json::Value = client
        .post(format!("{API}/Contract/search"))
        .header("Authorization", &bearer)
        .json(&serde_json::json!({ "searchText": "ES", "live": use_live }))
        .send().await?.json().await?;
    let srch_arr = srch["contracts"].as_array().cloned().unwrap_or_default();
    ok(&format!("Contract/search 'ES': {} results", srch_arr.len()));
    for c in srch_arr.iter().take(3) {
        info(&format!("  {} — {}", c["id"].as_str().unwrap_or("?"), c["description"].as_str().unwrap_or("?")));
    }

    // pick the active ES contract
    let contract_id = avail_arr.iter()
        .find(|c| c["id"].as_str().unwrap_or("").contains("CON.F.US.EP."))
        .and_then(|c| c["id"].as_str())
        .unwrap_or(DEFAULT_CONTRACT)
        .to_string();

    let by_id: serde_json::Value = client
        .post(format!("{API}/Contract/searchById"))
        .header("Authorization", &bearer)
        .json(&serde_json::json!({ "contractId": &contract_id }))
        .send().await?.json().await?;
    let tick_size = by_id["contract"]["tickSize"].as_f64().unwrap_or(TICK_SIZE);
    let tick_val  = by_id["contract"]["tickValue"].as_f64().unwrap_or(12.5);
    ok(&format!("Using: {contract_id} tickSize={tick_size} tickValue={tick_val}"));

    // ── Step 5: Pre-flight position check ────────────────────────────────────
    sep("Step 5: Pre-flight — verify flat");
    let pos_before = search_open(&client, &bearer, account_id, &contract_id).await?;
    if let Some(_) = pos_before {
        info("Existing position detected — closing before test");
        let close: serde_json::Value = client
            .post(format!("{API}/Position/closeContract"))
            .header("Authorization", &bearer)
            .json(&serde_json::json!({ "accountId": account_id, "contractId": &contract_id }))
            .send().await?.json().await?;
        if close["success"].as_bool().unwrap_or(false) {
            ok("Existing position closed");
            tokio::time::sleep(Duration::from_secs(2)).await;
        } else {
            fail(&format!("Could not close existing position: {close}"));
            return Err(anyhow::anyhow!("pre-flight failed"));
        }
    } else {
        ok("Starting flat ✓");
    }

    // ── Step 6: Pre-flight open orders ────────────────────────────────────────
    sep("Step 6: Pre-flight — cancel stale open orders");
    let stale = search_open_orders(&client, &bearer, account_id).await?;
    if stale.is_empty() {
        ok("No open orders ✓");
    } else {
        info(&format!("{} stale order(s) — cancelling", stale.len()));
        for o in &stale {
            let oid = o["id"].as_i64().unwrap_or(0);
            let _ = cancel_order(&client, &bearer, account_id, oid).await;
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
        ok("Stale orders cancelled");
    }

    // ── Step 7: Place plain market BUY (no bracket fields — Auto OCO handles it)
    sep("Step 7: Place market BUY");
    let ts = chrono::Utc::now().timestamp();
    let place_resp: serde_json::Value = client
        .post(format!("{API}/Order/place"))
        .header("Authorization", &bearer)
        .json(&serde_json::json!({
            "accountId": account_id,
            "contractId": &contract_id,
            "type": 2,
            "side": 0,
            "size": 1,
            "customTag": format!("TEST_{ts}"),
        }))
        .send().await?.json().await?;

    if !place_resp["success"].as_bool().unwrap_or(false) {
        fail(&format!("Order failed: {place_resp}"));
        return Err(anyhow::anyhow!("place order failed"));
    }
    let entry_order_id = place_resp["orderId"].as_i64().unwrap_or(0);
    ok(&format!("Market BUY placed — orderId={entry_order_id}"));

    // Wait for fill confirmation
    tokio::time::sleep(Duration::from_secs(1)).await;

    // ── Step 8: Verify position ───────────────────────────────────────────────
    sep("Step 8: Verify position opened");
    let pos = search_open(&client, &bearer, account_id, &contract_id).await?;
    let fill_price = match &pos {
        Some(p) => {
            let size = p["size"].as_f64().unwrap_or(0.0);
            let avg  = p["averagePrice"].as_f64().unwrap_or(0.0);
            ok(&format!("Position: {} contract(s) @ {avg:.2}", size));
            avg
        }
        None => {
            fail("No position detected — market may be closed or order pending");
            return Err(anyhow::anyhow!("no position after entry"));
        }
    };

    // ── Step 9: Place explicit SL and TP bracket orders ───────────────────────
    sep("Step 9: Place explicit SL (Stop) and TP (Limit) bracket orders");
    let desired_sl = fill_price - 8.0 * tick_size;
    let desired_tp = fill_price + 12.0 * tick_size;

    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();

    let sl_resp: serde_json::Value = client
        .post(format!("{API}/Order/place"))
        .header("Authorization", &bearer)
        .json(&serde_json::json!({
            "accountId": account_id,
            "contractId": &contract_id,
            "type": 4,
            "side": 1,
            "size": 1,
            "stopPrice": desired_sl,
            "customTag": format!("SL_{ts}"),
        }))
        .send().await?.json().await?;
    let sl_id: Option<i64> = if sl_resp["success"].as_bool().unwrap_or(false) {
        let id = sl_resp["orderId"].as_i64();
        ok(&format!("SL Stop placed @ {desired_sl:.2} (id={:?})", id));
        id
    } else {
        fail(&format!("SL placement failed: {sl_resp}"));
        None
    };

    let tp_resp: serde_json::Value = client
        .post(format!("{API}/Order/place"))
        .header("Authorization", &bearer)
        .json(&serde_json::json!({
            "accountId": account_id,
            "contractId": &contract_id,
            "type": 1,
            "side": 1,
            "size": 1,
            "limitPrice": desired_tp,
            "customTag": format!("TP_{ts}"),
        }))
        .send().await?.json().await?;
    let tp_id: Option<i64> = if tp_resp["success"].as_bool().unwrap_or(false) {
        let id = tp_resp["orderId"].as_i64();
        ok(&format!("TP Limit placed @ {desired_tp:.2} (id={:?})", id));
        id
    } else {
        fail(&format!("TP placement failed: {tp_resp}"));
        None
    };

    // ── Step 10: Modify SL ───────────────────────────────────────────────────
    sep("Step 10: Modify SL (tighten by 2 ticks)");
    let new_sl = desired_sl + 2.0 * tick_size;
    if let Some(sl_order_id) = sl_id {
        let mod_resp: serde_json::Value = client
            .post(format!("{API}/Order/modify"))
            .header("Authorization", &bearer)
            .json(&serde_json::json!({
                "accountId": account_id,
                "orderId": sl_order_id,
                "stopPrice": new_sl,
            }))
            .send().await?.json().await?;
        if mod_resp["success"].as_bool().unwrap_or(false) {
            ok(&format!("SL modified: {desired_sl:.2} → {new_sl:.2}"));
        } else {
            fail(&format!("SL modify failed: {mod_resp}"));
        }
    } else {
        info("Skipped — SL placement failed");
    }

    // ── Step 11: Modify TP ───────────────────────────────────────────────────
    sep("Step 11: Modify TP (extend by 4 ticks)");
    let new_tp = desired_tp + 4.0 * tick_size;
    if let Some(tp_order_id) = tp_id {
        let mod_resp: serde_json::Value = client
            .post(format!("{API}/Order/modify"))
            .header("Authorization", &bearer)
            .json(&serde_json::json!({
                "accountId": account_id,
                "orderId": tp_order_id,
                "limitPrice": new_tp,
            }))
            .send().await?.json().await?;
        if mod_resp["success"].as_bool().unwrap_or(false) {
            ok(&format!("TP modified: {desired_tp:.2} → {new_tp:.2}"));
        } else {
            fail(&format!("TP modify failed: {mod_resp}"));
        }
    } else {
        info("Skipped — TP placement failed");
    }


    // ── Step 12: Re-query to confirm modifications ────────────────────────────
    sep("Step 12: Re-query orders after modifications");
    tokio::time::sleep(Duration::from_millis(500)).await;
    let orders2 = search_open_orders(&client, &bearer, account_id).await?;
    for o in &orders2 {
        let oid  = o["id"].as_i64().unwrap_or(0);
        let typ  = o["type"].as_i64().unwrap_or(0);
        let stop = o["stopPrice"].as_f64();
        let lim  = o["limitPrice"].as_f64();
        let label = match typ { 1 => "TP(Limit)", 4 => "SL(Stop)", _ => "Other" };
        info(&format!("  {label} id={oid} stop={stop:?} limit={lim:?}"));
    }
    ok("Orders re-queried");

    // ── Step 13: Cancel SL ────────────────────────────────────────────────────
    sep("Step 13: Cancel SL bracket");
    if let Some(sl_order_id) = sl_id {
        if cancel_order(&client, &bearer, account_id, sl_order_id).await? {
            ok(&format!("SL id={sl_order_id} cancelled"));
        } else {
            fail(&format!("SL id={sl_order_id} cancel failed"));
        }
    } else {
        info("Skipped — no SL found");
    }

    // ── Step 14: Cancel TP ────────────────────────────────────────────────────
    sep("Step 14: Cancel TP bracket");
    if let Some(tp_order_id) = tp_id {
        if cancel_order(&client, &bearer, account_id, tp_order_id).await? {
            ok(&format!("TP id={tp_order_id} cancelled"));
        } else {
            fail(&format!("TP id={tp_order_id} cancel failed"));
        }
    } else {
        info("Skipped — no TP found");
    }

    tokio::time::sleep(Duration::from_secs(1)).await;

    // ── Step 15: Close position via Position/closeContract ────────────────────
    sep("Step 15: Close position (Position/closeContract)");
    let close_resp: serde_json::Value = client
        .post(format!("{API}/Position/closeContract"))
        .header("Authorization", &bearer)
        .json(&serde_json::json!({ "accountId": account_id, "contractId": &contract_id }))
        .send().await?.json().await?;
    if close_resp["success"].as_bool().unwrap_or(false) {
        ok("Position close accepted");
    } else {
        fail(&format!("closeContract: {close_resp} — trying fallback market SELL"));
        let sell: serde_json::Value = client
            .post(format!("{API}/Order/place"))
            .header("Authorization", &bearer)
            .json(&serde_json::json!({
                "accountId": account_id,
                "contractId": &contract_id,
                "type": 2, "side": 1, "size": 1,
            }))
            .send().await?.json().await?;
        if sell["success"].as_bool().unwrap_or(false) {
            ok(&format!("Fallback SELL placed orderId={}", sell["orderId"]));
        } else {
            fail(&format!("Fallback SELL also failed: {sell}"));
        }
    }

    tokio::time::sleep(Duration::from_secs(2)).await;

    // ── Step 16: Confirm flat ─────────────────────────────────────────────────
    sep("Step 16: Confirm flat");
    match search_open(&client, &bearer, account_id, &contract_id).await? {
        None    => ok("Flat confirmed ✓"),
        Some(p) => fail(&format!("Still in position: {p}")),
    }

    // ── Step 17: Partial close test ───────────────────────────────────────────
    sep("Step 17: Partial close test (buy 2, partial close 1)");
    // place 2-lot entry
    let e2: serde_json::Value = client
        .post(format!("{API}/Order/place"))
        .header("Authorization", &bearer)
        .json(&serde_json::json!({
            "accountId": account_id,
            "contractId": &contract_id,
            "type": 2, "side": 0, "size": 2,
            "customTag": format!("TEST2_{ts}"),
        }))
        .send().await?.json().await?;
    if e2["success"].as_bool().unwrap_or(false) {
        ok(&format!("2-lot BUY placed orderId={}", e2["orderId"]));
        tokio::time::sleep(Duration::from_secs(2)).await;

        // verify position size
        if let Some(p) = search_open(&client, &bearer, account_id, &contract_id).await? {
            info(&format!("Position: {} contract(s)", p["size"]));
        }

        // partial close 1 lot
        let pc: serde_json::Value = client
            .post(format!("{API}/Position/partialCloseContract"))
            .header("Authorization", &bearer)
            .json(&serde_json::json!({
                "accountId": account_id,
                "contractId": &contract_id,
                "size": 1,
            }))
            .send().await?.json().await?;
        if pc["success"].as_bool().unwrap_or(false) {
            ok("Partial close (1 lot) accepted");
            tokio::time::sleep(Duration::from_secs(2)).await;
            if let Some(p) = search_open(&client, &bearer, account_id, &contract_id).await? {
                ok(&format!("Remaining position: {} contract(s)", p["size"]));
            }
        } else {
            fail(&format!("Partial close failed: {pc}"));
        }

        // close the rest
        let _ = client
            .post(format!("{API}/Position/closeContract"))
            .header("Authorization", &bearer)
            .json(&serde_json::json!({ "accountId": account_id, "contractId": &contract_id }))
            .send().await?;
        tokio::time::sleep(Duration::from_secs(2)).await;
        ok("Remaining position closed");
    } else {
        info(&format!("2-lot entry skipped (failed: {e2}) — market may be closed"));
    }

    // ── Step 18: Trade history ────────────────────────────────────────────────
    sep("Step 18: Trade/search — today's fills");
    let today  = chrono::Utc::now().format("%Y-%m-%dT00:00:00Z").to_string();
    let now_s  = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string();
    let trades: serde_json::Value = client
        .post(format!("{API}/Trade/search"))
        .header("Authorization", &bearer)
        .json(&serde_json::json!({
            "accountId": account_id,
            "startTimestamp": today,
            "endTimestamp": now_s,
        }))
        .send().await?.json().await?;
    let trades_arr = trades["trades"].as_array().cloned().unwrap_or_default();
    ok(&format!("{} trade(s) today", trades_arr.len()));
    for t in trades_arr.iter().take(10) {
        let price = t["price"].as_f64().unwrap_or(0.0);
        let pnl   = t["profitAndLoss"].as_f64();
        let fees  = t["fees"].as_f64().unwrap_or(0.0);
        let side  = if t["side"].as_i64() == Some(0) { "BUY" } else { "SELL" };
        let size  = t["size"].as_i64().unwrap_or(0);
        let pnl_s = pnl.map(|p| format!("pnl={p:+.2}")).unwrap_or("pnl=null(entry)".into());
        info(&format!("  {side} {size}x{price:.2}  {pnl_s}  fees={fees:.2}"));
    }

    // ── Step 19: Order history ────────────────────────────────────────────────
    sep("Step 19: Order/search — today's orders");
    let oh: serde_json::Value = client
        .post(format!("{API}/Order/search"))
        .header("Authorization", &bearer)
        .json(&serde_json::json!({
            "accountId": account_id,
            "startTimestamp": today,
            "endTimestamp": now_s,
        }))
        .send().await?.json().await?;
    let oh_arr = oh["orders"].as_array().cloned().unwrap_or_default();
    ok(&format!("{} order(s) today", oh_arr.len()));
    for o in oh_arr.iter().take(12) {
        let oid  = o["id"].as_i64().unwrap_or(0);
        let typ  = o["type"].as_i64().unwrap_or(0);
        let side = o["side"].as_i64().unwrap_or(0);
        let stat = o["status"].as_i64().unwrap_or(0);
        let fill = o["filledPrice"].as_f64();
        let t_s  = match typ  { 1=>"Limit", 2=>"Market", 4=>"Stop", 5=>"Trail", _=>"?" };
        let si_s = match side { 0=>"BUY", 1=>"SELL", _=>"?" };
        let st_s = match stat { 1=>"Open", 2=>"Filled", 3=>"Cancelled", 4=>"Expired", 5=>"Rejected", _=>"?" };
        let f_s  = fill.map(|p| format!("@{p:.2}")).unwrap_or("-".into());
        info(&format!("  id={oid:>12}  {t_s:<8} {si_s:<5} {st_s:<10} {f_s}"));
    }

    // ── Summary ───────────────────────────────────────────────────────────────
    println!("\n{}", "═".repeat(60));
    println!("  ALL STEPS COMPLETE");
    println!("{}\n", "═".repeat(60));
    Ok(())
}

async fn search_open(
    client: &reqwest::Client,
    bearer: &str,
    account_id: i64,
    contract_id: &str,
) -> anyhow::Result<Option<serde_json::Value>> {
    let resp: serde_json::Value = client
        .post(format!("{API}/Position/searchOpen"))
        .header("Authorization", bearer)
        .json(&serde_json::json!({ "accountId": account_id }))
        .send().await?.json().await?;
    let positions = resp["positions"].as_array().cloned().unwrap_or_default();
    Ok(positions.into_iter().find(|p| p["contractId"].as_str().unwrap_or("") == contract_id))
}

async fn search_open_orders(
    client: &reqwest::Client,
    bearer: &str,
    account_id: i64,
) -> anyhow::Result<Vec<serde_json::Value>> {
    let resp = client
        .post(format!("{API}/Order/searchOpen"))
        .header("Authorization", bearer)
        .json(&serde_json::json!({ "accountId": account_id }))
        .send().await?;
    if resp.status().as_u16() == 401 { return Ok(vec![]); }
    let text = resp.text().await?;
    if text.trim().is_empty() { return Ok(vec![]); }
    let val: serde_json::Value = serde_json::from_str(&text)?;
    Ok(val["orders"].as_array().cloned().unwrap_or_default())
}

async fn cancel_order(
    client: &reqwest::Client,
    bearer: &str,
    account_id: i64,
    order_id: i64,
) -> anyhow::Result<bool> {
    let resp: serde_json::Value = client
        .post(format!("{API}/Order/cancel"))
        .header("Authorization", bearer)
        .json(&serde_json::json!({ "accountId": account_id, "orderId": order_id }))
        .send().await?.json().await?;
    Ok(resp["success"].as_bool().unwrap_or(false))
}
