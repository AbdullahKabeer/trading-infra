use anyhow::Result;
use chrono::{DateTime, Datelike, Duration, NaiveDate, NaiveTime, TimeZone, Utc, Weekday};
use chrono_tz::America::New_York;
use reqwest::Client;
use std::fs;
use std::path::Path;
use crate::config::{API, CONTRACT, DATA_DIR, DAYS_BACK};
use crate::market::session::Session;

fn is_rth(ts: &DateTime<Utc>) -> bool {
    let et = ts.with_timezone(&New_York);
    let t = et.time();
    let wd = et.weekday();
    let open = NaiveTime::from_hms_opt(9, 30, 0).unwrap();
    let close = NaiveTime::from_hms_opt(16, 0, 0).unwrap();
    !matches!(wd, Weekday::Sat | Weekday::Sun) && t >= open && t < close
}

pub fn parse_bar_ts(val: &serde_json::Value) -> Option<DateTime<Utc>> {
    if let Some(s) = val.as_str() {
        // ISO format
        if let Ok(dt) = DateTime::parse_from_rfc3339(s) {
            return Some(dt.with_timezone(&Utc));
        }
        if let Ok(dt) = DateTime::parse_from_rfc3339(&s.replace("Z", "+00:00")) {
            return Some(dt.with_timezone(&Utc));
        }
    }
    if let Some(n) = val.as_f64() {
        let secs = if n > 1e10 { n / 1000.0 } else { n };
        return DateTime::from_timestamp(secs as i64, 0);
    }
    None
}

pub async fn backfill(client: &Client, bearer: &str) -> Result<()> {
    fs::create_dir_all(DATA_DIR)?;
    let now_et = Utc::now().with_timezone(&New_York);
    let today = now_et.date_naive();

    // Collect weekdays going back DAYS_BACK
    let mut dates: Vec<NaiveDate> = Vec::new();
    let mut d = today;
    while dates.len() < (DAYS_BACK as usize + 1) {
        if !matches!(d.weekday(), Weekday::Sat | Weekday::Sun) {
            dates.push(d);
        }
        d -= Duration::days(1);
    }
    dates.reverse();

    for day in &dates {
        let date_str = day.format("%Y-%m-%d").to_string();
        let is_today = *day == today;
        let path = Path::new(DATA_DIR).join(format!("{date_str}.json"));

        // Today's session is built live from the websocket feed; skip API fetch for today
        if is_today { continue; }

        // Prior session files are immutable — skip if already cached
        if path.exists() {
            tracing::debug!("{date_str}: cached, skipping");
            continue;
        }

        let et_open = New_York
            .from_local_datetime(&day.and_hms_opt(9, 30, 0).unwrap())
            .single()
            .unwrap()
            .with_timezone(&Utc);
        let et_close = New_York
            .from_local_datetime(&day.and_hms_opt(16, 0, 0).unwrap())
            .single()
            .unwrap()
            .with_timezone(&Utc);

        let start = et_open;
        let end = if is_today { Utc::now() } else { et_close };
        if start >= end { continue; }

        tracing::info!("{date_str}: fetching RTH bars...");

        let contracts = candidate_contracts(day);
        let mut api_bars: Vec<serde_json::Value> = vec![];

        for cid in &contracts {
            let payload = serde_json::json!({
                "contractId": cid,
                "live": false,
                "startTime": start.to_rfc3339(),
                "endTime": end.to_rfc3339(),
                "unit": 2,
                "unitNumber": 1,
                "limit": 500,
                "includePartialBar": is_today,
            });

            let resp: serde_json::Value = client
                .post(format!("{}/History/retrieveBars", API))
                .header("Authorization", bearer)
                .json(&payload)
                .send()
                .await?
                .json()
                .await?;

            let bars = resp["bars"].as_array().cloned().unwrap_or_default();
            let rth_bars: Vec<_> = bars.into_iter().filter(|b| {
                parse_bar_ts(&b["t"]).map(|ts| is_rth(&ts)).unwrap_or(false)
            }).collect();

            if !rth_bars.is_empty() {
                api_bars = rth_bars;
                break;
            }
            tokio::time::sleep(tokio::time::Duration::from_millis(300)).await;
        }

        if api_bars.is_empty() {
            tracing::info!("{date_str}: 0 bars");
            continue;
        }

        // API returns newest first — reverse
        api_bars.reverse();

        let mut sess = Session::new(&date_str);
        for b in &api_bars {
            if let Some(ts) = parse_bar_ts(&b["t"]) {
                sess.add_bar(
                    b["o"].as_f64().unwrap_or(0.0),
                    b["h"].as_f64().unwrap_or(0.0),
                    b["l"].as_f64().unwrap_or(0.0),
                    b["c"].as_f64().unwrap_or(0.0),
                    b["v"].as_f64().unwrap_or(0.0),
                    ts,
                );
            }
        }

        tracing::info!("{date_str}: {} bars VWAP={:.2}", sess.bars.len(), sess.vwap);
        sess.save()?;

        tokio::time::sleep(tokio::time::Duration::from_millis(800)).await;
    }
    Ok(())
}

fn candidate_contracts(day: &NaiveDate) -> Vec<String> {
    let yr2 = day.year() % 100;
    let m = day.month();
    let historical: Vec<String> = match m {
        1..=3 => vec![format!("CON.F.US.EP.H{yr2:02}"), format!("CON.F.US.EP.Z{:02}", (day.year() - 1) % 100)],
        4..=6 => vec![format!("CON.F.US.EP.M{yr2:02}"), format!("CON.F.US.EP.H{yr2:02}")],
        7..=9 => vec![format!("CON.F.US.EP.U{yr2:02}"), format!("CON.F.US.EP.M{yr2:02}")],
        _ => vec![format!("CON.F.US.EP.Z{yr2:02}"), format!("CON.F.US.EP.U{yr2:02}")],
    };
    let mut result = vec![CONTRACT.to_string()];
    for c in historical {
        if !result.contains(&c) {
            result.push(c);
        }
    }
    result
}
