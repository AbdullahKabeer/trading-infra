use anyhow::{anyhow, Result};
use reqwest::Client;
use std::time::{Duration, Instant};
use crate::config::{API, TOKEN_MAX_AGE_SECS};

pub struct TokenHandle {
    pub token: String,
    issued_at: Instant,
}

impl TokenHandle {
    pub fn new(token: String) -> Self {
        Self { token, issued_at: Instant::now() }
    }

    pub fn needs_refresh(&self) -> bool {
        self.issued_at.elapsed() >= Duration::from_secs(TOKEN_MAX_AGE_SECS)
    }

    pub fn bearer(&self) -> String {
        format!("Bearer {}", self.token)
    }
}

pub async fn login(client: &Client) -> Result<TokenHandle> {
    let username = std::env::var("PROJECT_X_USERNAME")
        .map_err(|_| anyhow!("PROJECT_X_USERNAME not set"))?;
    let api_key = std::env::var("PROJECT_X_API_KEY")
        .map_err(|_| anyhow!("PROJECT_X_API_KEY not set"))?;

    let resp: serde_json::Value = client
        .post(format!("{}/Auth/loginKey", API))
        .json(&serde_json::json!({ "userName": username, "apiKey": api_key }))
        .send()
        .await?
        .json()
        .await?;

    if resp["success"].as_bool().unwrap_or(false) {
        let token = resp["token"]
            .as_str()
            .ok_or_else(|| anyhow!("no token in auth response"))?
            .to_string();
        tracing::info!("Authenticated successfully");
        Ok(TokenHandle::new(token))
    } else {
        let code = resp["errorCode"].as_i64().unwrap_or(-1);
        Err(anyhow!("Auth failed (errorCode={code}): {resp}"))
    }
}

pub async fn refresh_if_needed(client: &Client, handle: &mut TokenHandle) -> Result<()> {
    if handle.needs_refresh() {
        tracing::info!("Token expired, refreshing...");
        *handle = login(client).await?;
    }
    Ok(())
}
