use anyhow::Result;
use reqwest::Client;
use crate::config::API;

pub async fn search(
    client: &Client,
    bearer: &str,
    account_id: i64,
    start_ts: &str,
    end_ts: Option<&str>,
) -> Result<Vec<serde_json::Value>> {
    let mut body = serde_json::json!({
        "accountId": account_id,
        "startTimestamp": start_ts,
    });
    if let Some(end) = end_ts {
        body["endTimestamp"] = end.into();
    }
    let resp: serde_json::Value = client
        .post(format!("{API}/Trade/search"))
        .header("Authorization", bearer)
        .json(&body)
        .send()
        .await?
        .json()
        .await?;
    Ok(resp["trades"].as_array().cloned().unwrap_or_default())
}
