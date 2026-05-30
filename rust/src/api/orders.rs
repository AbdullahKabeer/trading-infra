use anyhow::Result;
use reqwest::Client;
use crate::config::{API, TICK_SIZE, DRY_RUN};
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

        let mut p = serde_json::json!({
            "accountId": self.account_id,
            "contractId": self.contract_id,
            "type": 2,
            "side": side,
            "size": size,
            "customTag": format!("B_{ts}"),
        });

        if let Some(sl) = sl_price {
            if sl > 0.0 && ref_price > 0.0 {
                let ticks = ((ref_price - sl).abs() / TICK_SIZE).round() as i64;
                let ticks = ticks.max(4);
                p["stopLossBracket"] = serde_json::json!({ "ticks": ticks, "type": 4 });
            }
        }
        if let Some(tp) = tp_price {
            if tp > 0.0 && ref_price > 0.0 {
                let ticks = ((tp - ref_price).abs() / TICK_SIZE).round().abs() as i64;
                if ticks > 0 {
                    p["takeProfitBracket"] = serde_json::json!({ "ticks": ticks, "type": 1 });
                }
            }
        }

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
            .post(format!("{}/Order/active", API))
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
