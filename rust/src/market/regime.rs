use chrono::{Utc, Duration};
use reqwest::Client;
use crate::config::{API, HIGH_IMPACT_BEHAVIOR, VX_CONTRACT, VWAP_CROSS_TRENDING, VWAP_CROSS_BALANCED};
use crate::types::RegimeState;

impl RegimeState {
    pub fn new() -> Self {
        Self {
            vwap_regime: "NEUTRAL".to_string(),
            session_type: "NEUTRAL".to_string(),
            high_impact_behavior: HIGH_IMPACT_BEHAVIOR.to_string(),
            session_type_bar: -1,
            ..Default::default()
        }
    }

    pub fn check_calendar(&mut self) {
        let today = Utc::now().format("%Y-%m-%d").to_string();
        let dates = crate::config::high_impact_dates();
        self.calendar_event = dates.get(today.as_str()).map(|s| s.to_string());
        if let Some(ref e) = self.calendar_event {
            tracing::info!("High-impact event today: {e} — behavior: {HIGH_IMPACT_BEHAVIOR}");
        }
    }

    pub fn update_vwap_crossings(&mut self, vwap_history: &[f64], price_history: &[f64], lookback: usize) {
        if vwap_history.len() < 2 || price_history.len() < 2 { return; }
        let vh = tail(vwap_history, lookback);
        let ph = tail(price_history, lookback);
        let n = vh.len().min(ph.len());
        let mut crossings = 0u32;
        for i in 1..n {
            let prev_above = ph[i-1] > vh[i-1];
            let curr_above = ph[i] > vh[i];
            if prev_above != curr_above { crossings += 1; }
        }
        self.vwap_crossings = crossings;
        self.vwap_regime = if crossings <= VWAP_CROSS_TRENDING {
            "TRENDING"
        } else if crossings >= VWAP_CROSS_BALANCED {
            "BALANCED"
        } else {
            "NEUTRAL"
        }.to_string();
    }

    pub fn classify_session(&mut self, bars: &[crate::types::Bar], bar_idx: usize, atr: f64) {
        let session_type_bars = crate::config::SESSION_TYPE_BARS;
        if bar_idx < session_type_bars { return; }
        if self.session_type_bar >= session_type_bars as i64 { return; }
        if bars.len() < session_type_bars { return; }

        let slice = &bars[..session_type_bars];
        let session_type = session_type_heuristic(slice, atr);
        self.session_type = session_type.to_string();
        self.session_type_bar = bar_idx as i64;
        tracing::info!("Session type at bar={bar_idx}: {}", self.session_type);
    }

    #[allow(dead_code)]
    pub fn reset_for_new_day(&mut self) {
        self.vwap_crossings = 0;
        self.session_type = "NEUTRAL".to_string();
        self.session_type_bar = -1;
        self.check_calendar();
    }
}

fn session_type_heuristic(bars: &[crate::types::Bar], atr: f64) -> &'static str {
    if bars.is_empty() || atr <= 0.0 { return "NEUTRAL"; }
    let total_range = bars.iter().map(|b| b.h).fold(f64::NEG_INFINITY, f64::max)
        - bars.iter().map(|b| b.l).fold(f64::INFINITY, f64::min);
    if total_range > 2.5 * atr { return "TRENDING"; }
    if bars.len() >= 2 {
        let vpoc_travel = (bars.last().unwrap().vpoc - bars[0].vpoc).abs();
        if vpoc_travel > 2.0 { return "TRENDING"; }
    }
    if total_range < atr { return "RANGING"; }
    "NEUTRAL"
}

pub async fn fetch_vix_proxy(client: &Client, bearer: &str) -> Option<f64> {
    let now = Utc::now();
    let start = (now - Duration::days(7)).to_rfc3339();
    let payload = serde_json::json!({
        "contractId": VX_CONTRACT,
        "live": false,
        "startTime": start,
        "endTime": now.to_rfc3339(),
        "unit": 2,
        "unitNumber": 1440,
        "limit": 5,
    });
    let resp: serde_json::Value = client
        .post(format!("{API}/History/retrieveBars"))
        .header("Authorization", bearer)
        .json(&payload)
        .send()
        .await
        .ok()?
        .json()
        .await
        .ok()?;

    let bars = resp["bars"].as_array()?;
    let last = bars.last()?;
    let vix = last["c"].as_f64()?;
    let status = if vix > 30.0 { "EXTREME" } else if vix > 22.0 { "HIGH" } else { "NORMAL" };
    tracing::info!("VIX proxy: {vix:.2} ({status})");
    Some(vix)
}

pub fn load_prev_session_levels(regime: &mut RegimeState) {
    let sessions = crate::market::session::Session::list_sessions();
    let today = Utc::now().format("%Y-%m-%d").to_string();
    let prev = sessions.iter().find(|s| s.as_str() < today.as_str());
    let Some(prev_date) = prev else { return; };
    if let Ok(ps) = crate::market::session::Session::load(prev_date) {
        if !ps.bars.is_empty() {
            regime.prev_vwap = Some(ps.vwap);
            regime.prev_vpoc = Some(ps.vpoc);
            regime.prev_close = ps.bars.last().map(|b| b.c);
            tracing::info!(
                "Prev session {prev_date}: VWAP={:.2} VPOC={:.2} Close={:.2}",
                ps.vwap, ps.vpoc,
                ps.bars.last().map(|b| b.c).unwrap_or(0.0)
            );
        }
    }
}

fn tail(v: &[f64], n: usize) -> &[f64] {
    &v[v.len().saturating_sub(n)..]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_vwap_crossings_trending() {
        let mut regime = RegimeState::new();
        // Price stays above VWAP — 0 crossings → TRENDING
        let vwap: Vec<f64> = (0..20).map(|_| 5000.0).collect();
        let price: Vec<f64> = (0..20).map(|_| 5010.0).collect();
        regime.update_vwap_crossings(&vwap, &price, 20);
        assert_eq!(regime.vwap_crossings, 0);
        assert_eq!(regime.vwap_regime, "TRENDING");
    }

    #[test]
    fn test_vwap_crossings_balanced() {
        let mut regime = RegimeState::new();
        let vwap: Vec<f64> = (0..20).map(|_| 5000.0).collect();
        // Alternating above/below — many crossings
        let price: Vec<f64> = (0..20).map(|i| if i % 2 == 0 { 5010.0 } else { 4990.0 }).collect();
        regime.update_vwap_crossings(&vwap, &price, 20);
        assert!(regime.vwap_crossings >= 3);
        assert_eq!(regime.vwap_regime, "BALANCED");
    }
}
