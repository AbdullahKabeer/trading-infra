use anyhow::Result;
use reqwest::Client;
use crate::config::API;
use crate::types::ServerPositionState;

pub async fn search_open(
    client: &Client,
    bearer: &str,
    account_id: i64,
    contract_id: &str,
) -> Result<ServerPositionState> {
    let resp: serde_json::Value = client
        .post(format!("{}/Position/searchOpen", API))
        .header("Authorization", bearer)
        .json(&serde_json::json!({ "accountId": account_id }))
        .send()
        .await?
        .json()
        .await?;

    let positions = resp["positions"].as_array().cloned().unwrap_or_default();

    if positions.is_empty() {
        return Ok(ServerPositionState::Flat);
    }

    let same = positions
        .iter()
        .find(|p| p["contractId"].as_str().unwrap_or("") == contract_id);

    match same {
        None => Ok(ServerPositionState::Flat),
        Some(p) => {
            let size = extract_size(p);
            let avg_price = extract_avg_price(p);
            let side = extract_side(p);
            Ok(ServerPositionState::Open { size, avg_price, side })
        }
    }
}

fn extract_size(p: &serde_json::Value) -> u32 {
    for key in &["size", "qty", "quantity"] {
        if let Some(v) = p[key].as_f64() {
            return v.abs() as u32;
        }
    }
    0
}

fn extract_avg_price(p: &serde_json::Value) -> Option<f64> {
    for key in &["averagePrice", "avgPrice", "avgFillPrice", "entryPrice", "openPrice"] {
        if let Some(v) = p[key].as_f64() {
            if v > 0.0 {
                return Some(v);
            }
        }
    }
    None
}

fn extract_side(p: &serde_json::Value) -> Option<serde_json::Value> {
    for key in &["side", "direction", "positionSide", "type"] {
        if !p[key].is_null() {
            return Some(p[key].clone());
        }
    }
    None
}

#[allow(dead_code)]
pub async fn close_contract(
    client: &Client,
    bearer: &str,
    account_id: i64,
    contract_id: &str,
) -> Result<bool> {
    let resp: serde_json::Value = client
        .post(format!("{}/Position/closeContract", crate::config::API))
        .header("Authorization", bearer)
        .json(&serde_json::json!({ "accountId": account_id, "contractId": contract_id }))
        .send()
        .await?
        .json()
        .await?;
    Ok(resp["success"].as_bool().unwrap_or(false))
}

#[allow(dead_code)]
pub async fn partial_close_contract(
    client: &Client,
    bearer: &str,
    account_id: i64,
    contract_id: &str,
    size: u32,
) -> Result<bool> {
    let resp: serde_json::Value = client
        .post(format!("{}/Position/partialCloseContract", crate::config::API))
        .header("Authorization", bearer)
        .json(&serde_json::json!({
            "accountId": account_id,
            "contractId": contract_id,
            "size": size,
        }))
        .send()
        .await?
        .json()
        .await?;
    Ok(resp["success"].as_bool().unwrap_or(false))
}

/// Returns the close action ("BUY" or "SELL") from a server-side position side value.
pub fn close_action_from_side(side: &serde_json::Value) -> Option<&'static str> {
    if let Some(n) = side.as_i64() {
        // 0 = Long → close with SELL; 1 = Short → close with BUY
        return Some(if n == 0 { "SELL" } else { "BUY" });
    }
    if let Some(s) = side.as_str() {
        let u = s.to_uppercase();
        if ["LONG", "BUY", "B"].contains(&u.as_str()) {
            return Some("SELL");
        }
        if ["SHORT", "SELL", "S"].contains(&u.as_str()) {
            return Some("BUY");
        }
    }
    None
}
