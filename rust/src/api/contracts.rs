#![allow(dead_code)]
use anyhow::Result;
use reqwest::Client;
use crate::config::API;

pub async fn search(client: &Client, bearer: &str, text: &str, live: bool) -> Result<Vec<serde_json::Value>> {
    let resp: serde_json::Value = client
        .post(format!("{API}/Contract/search"))
        .header("Authorization", bearer)
        .json(&serde_json::json!({ "searchText": text, "live": live }))
        .send()
        .await?
        .json()
        .await?;
    Ok(resp["contracts"].as_array().cloned().unwrap_or_default())
}

pub async fn search_by_id(client: &Client, bearer: &str, contract_id: &str) -> Result<Option<serde_json::Value>> {
    let resp: serde_json::Value = client
        .post(format!("{API}/Contract/searchById"))
        .header("Authorization", bearer)
        .json(&serde_json::json!({ "contractId": contract_id }))
        .send()
        .await?
        .json()
        .await?;
    Ok(if resp["success"].as_bool().unwrap_or(false) {
        resp["contract"].as_object().map(|_| resp["contract"].clone())
    } else {
        None
    })
}
