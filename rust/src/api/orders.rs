use anyhow::Result;
use reqwest::Client;
use crate::config::{API, DRY_RUN};
use crate::types::Direction;

pub struct OrderClient<'a> {
    pub client: &'a Client,
    pub bearer: String,
    pub account_id: i64,
    pub contract_id: String,
}

impl<'a> OrderClient<'a> {
    pub async fn place_market(
        &self,
        action: &str,
        size: u32,
        sl_price: Option<f64>,
        tp_price: Option<f64>,
        ref_price: f64,
    ) -> Result<serde_json::Value> {
        let side = if action.to_uppercase() == "BUY" { 0 } else { 1 };
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();

        // Account has Auto OCO Brackets enabled — sending bracket fields in the
        // order request is rejected by the platform ("Brackets cannot be used
        // with Position Brackets"). Place a plain market order; Auto OCO will
        // create the brackets automatically, and we immediately modify them to
        // the strategy's desired SL/TP prices (see update_stop / update_tp calls
        // in order_manager after entry).
        let p = serde_json::json!({
            "accountId": self.account_id,
            "contractId": self.contract_id,
            "type": 2,
            "side": side,
            "size": size,
            "customTag": format!("B_{ts}"),
        });

        tracing::info!("ORDER -> {action} {size} @ MKT | ref={ref_price:.2} sl={sl_price:?} tp={tp_price:?}");

        if DRY_RUN {
            tracing::info!("[DRY RUN] {p}");
            return Ok(serde_json::json!({ "success": true, "orderId": 999 }));
        }

        let resp: serde_json::Value = self
            .client
            .post(format!("{}/Order/place", API))
            .header("Authorization", &self.bearer)
            .json(&p)
            .send()
            .await?
            .json()
            .await?;

        if resp["success"].as_bool().unwrap_or(false) {
            tracing::info!("Order placed: {}", resp["orderId"]);
        } else {
            tracing::warn!("Order failed: {resp}");
        }
        Ok(resp)
    }

    /// Place a limit entry order. Unlike market orders this fills at the exact
    /// signal price (zero slippage when filled). The caller must poll
    /// `active_orders()` until the order disappears (filled/cancelled) and then
    /// read the fill price from the open position.
    pub async fn place_entry_limit(
        &self,
        action: &str,
        size: u32,
        limit_price: f64,
    ) -> Result<serde_json::Value> {
        let side = if action.to_uppercase() == "BUY" { 0 } else { 1 };
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let p = serde_json::json!({
            "accountId": self.account_id,
            "contractId": self.contract_id,
            "type": 1,   // Limit
            "side": side,
            "size": size,
            "limitPrice": limit_price,
            "customTag": format!("ENTRY_{ts}"),
        });
        tracing::info!("ENTRY LIMIT -> {action} {size} @ {limit_price:.2}");
        if DRY_RUN {
            return Ok(serde_json::json!({ "success": true, "orderId": 996 }));
        }
        let resp: serde_json::Value = self.client
            .post(format!("{}/Order/place", API))
            .header("Authorization", &self.bearer)
            .json(&p)
            .send().await?.json().await?;
        if resp["success"].as_bool().unwrap_or(false) {
            tracing::info!("Entry limit placed: order_id={}", resp["orderId"]);
        } else {
            tracing::warn!("Entry limit rejected: {resp}");
        }
        Ok(resp)
    }

    pub async fn place_stop(
        &self,
        action: &str,
        size: u32,
        stop_price: f64,
    ) -> Result<serde_json::Value> {
        let side = if action.to_uppercase() == "BUY" { 0 } else { 1 };
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let p = serde_json::json!({
            "accountId": self.account_id,
            "contractId": self.contract_id,
            "type": 4,
            "side": side,
            "size": size,
            "stopPrice": stop_price,
            "customTag": format!("SL_{ts}"),
        });
        tracing::info!("BRACKET SL -> {action} {size} Stop @ {stop_price:.2}");
        if DRY_RUN {
            return Ok(serde_json::json!({ "success": true, "orderId": 998 }));
        }
        let resp: serde_json::Value = self.client
            .post(format!("{}/Order/place", API))
            .header("Authorization", &self.bearer)
            .json(&p)
            .send().await?.json().await?;
        if resp["success"].as_bool().unwrap_or(false) {
            tracing::info!("SL bracket placed: {}", resp["orderId"]);
        } else {
            tracing::warn!("SL bracket failed: {resp}");
        }
        Ok(resp)
    }

    pub async fn place_limit(
        &self,
        action: &str,
        size: u32,
        limit_price: f64,
    ) -> Result<serde_json::Value> {
        let side = if action.to_uppercase() == "BUY" { 0 } else { 1 };
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let p = serde_json::json!({
            "accountId": self.account_id,
            "contractId": self.contract_id,
            "type": 1,
            "side": side,
            "size": size,
            "limitPrice": limit_price,
            "customTag": format!("TP_{ts}"),
        });
        tracing::info!("BRACKET TP -> {action} {size} Limit @ {limit_price:.2}");
        if DRY_RUN {
            return Ok(serde_json::json!({ "success": true, "orderId": 997 }));
        }
        let resp: serde_json::Value = self.client
            .post(format!("{}/Order/place", API))
            .header("Authorization", &self.bearer)
            .json(&p)
            .send().await?.json().await?;
        if resp["success"].as_bool().unwrap_or(false) {
            tracing::info!("TP bracket placed: {}", resp["orderId"]);
        } else {
            tracing::warn!("TP bracket failed: {resp}");
        }
        Ok(resp)
    }

    pub async fn cancel(&self, order_id: i64) -> Result<bool> {
        if DRY_RUN {
            return Ok(true);
        }
        let resp: serde_json::Value = self
            .client
            .post(format!("{}/Order/cancel", API))
            .header("Authorization", &self.bearer)
            .json(&serde_json::json!({ "accountId": self.account_id, "orderId": order_id }))
            .send()
            .await?
            .json()
            .await?;
        Ok(resp["success"].as_bool().unwrap_or(false))
    }

    pub async fn modify(&self, order_id: i64, stop_price: Option<f64>, limit_price: Option<f64>) -> Result<bool> {
        if DRY_RUN {
            return Ok(true);
        }
        let mut p = serde_json::json!({ "accountId": self.account_id, "orderId": order_id });
        if let Some(sp) = stop_price { p["stopPrice"] = sp.into(); }
        if let Some(lp) = limit_price { p["limitPrice"] = lp.into(); }

        let resp: serde_json::Value = self
            .client
            .post(format!("{}/Order/modify", API))
            .header("Authorization", &self.bearer)
            .json(&p)
            .send()
            .await?
            .json()
            .await?;
        Ok(resp["success"].as_bool().unwrap_or(false))
    }

    pub async fn active_orders(&self) -> Result<Vec<serde_json::Value>> {
        let resp = self
            .client
            .post(format!("{}/Order/searchOpen", API))
            .header("Authorization", &self.bearer)
            .json(&serde_json::json!({ "accountId": self.account_id }))
            .send()
            .await?;

        if resp.status().as_u16() == 401 {
            tracing::warn!("active_orders: 401 Unauthorized");
            return Ok(vec![]);
        }

        let text = resp.text().await?;
        if text.trim().is_empty() {
            return Ok(vec![]);
        }

        let val: serde_json::Value = serde_json::from_str(&text)?;
        Ok(val["orders"].as_array().cloned().unwrap_or_default())
    }

    #[allow(dead_code)]
    pub async fn search_orders(
        &self,
        start_ts: &str,
        end_ts: Option<&str>,
    ) -> Result<Vec<serde_json::Value>> {
        let mut body = serde_json::json!({
            "accountId": self.account_id,
            "startTimestamp": start_ts,
        });
        if let Some(end) = end_ts {
            body["endTimestamp"] = end.into();
        }
        let resp: serde_json::Value = self
            .client
            .post(format!("{}/Order/search", API))
            .header("Authorization", &self.bearer)
            .json(&body)
            .send()
            .await?
            .json()
            .await?;
        Ok(resp["orders"].as_array().cloned().unwrap_or_default())
    }

    /// Find and modify the SL bracket order for the current position direction.
    pub async fn update_stop(&self, dir: Direction, new_stop: f64) -> Result<bool> {
        let orders = self.active_orders().await?;
        let expected_close_side = if dir == Direction::Long { 1 } else { 0 };
        for o in &orders {
            if o["contractId"].as_str().unwrap_or("") == self.contract_id
                && o["type"].as_i64() == Some(4)
                && o["side"].as_i64() == Some(expected_close_side)
                && matches!(o["status"].as_i64(), Some(0) | Some(1))
            {
                let oid = o["id"].as_i64().unwrap_or(0);
                if self.modify(oid, Some(new_stop), None).await? {
                    tracing::info!("Server stop updated to {new_stop:.2}");
                    return Ok(true);
                }
            }
        }
        Ok(false)
    }

    /// Find and modify the TP bracket order for the current position direction.
    pub async fn update_tp(&self, dir: Direction, new_tp: f64) -> Result<bool> {
        let orders = self.active_orders().await?;
        let expected_close_side = if dir == Direction::Long { 1 } else { 0 };
        for o in &orders {
            if o["contractId"].as_str().unwrap_or("") == self.contract_id
                && o["type"].as_i64() == Some(1)
                && o["side"].as_i64() == Some(expected_close_side)
                && matches!(o["status"].as_i64(), Some(0) | Some(1))
            {
                let oid = o["id"].as_i64().unwrap_or(0);
                if self.modify(oid, None, Some(new_tp)).await? {
                    tracing::info!("Server TP updated to {new_tp:.2}");
                    return Ok(true);
                }
            }
        }
        Ok(false)
    }

    /// Cancel all bracket orders (SL + TP) for the given position direction.
    pub async fn cancel_all_brackets(&self, dir: Direction) -> Result<()> {
        let orders = self.active_orders().await?;
        let expected_close_side = if dir == Direction::Long { 1 } else { 0 };
        for o in &orders {
            if o["contractId"].as_str().unwrap_or("") == self.contract_id
                && matches!(o["type"].as_i64(), Some(1) | Some(4))
                && o["side"].as_i64() == Some(expected_close_side)
                && matches!(o["status"].as_i64(), Some(0) | Some(1))
            {
                let oid = o["id"].as_i64().unwrap_or(0);
                let _ = self.cancel(oid).await;
            }
        }
        Ok(())
    }
}
