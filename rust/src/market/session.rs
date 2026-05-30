use anyhow::Result;
use chrono::{DateTime, Utc};
use ordered_float::OrderedFloat;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, VecDeque};
use std::fs;
use std::path::Path;

use crate::config::{DATA_DIR, TICK_SIZE};
use crate::types::{Bar, CurBar, Quote, Trade};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Session {
    pub date: String,
    pub bars: Vec<Bar>,
    pub cur_bar: Option<CurBar>,
    #[serde(with = "btree_ordered_float")]
    pub vol_profile: BTreeMap<OrderedFloat<f64>, f64>,
    pub vwap: f64,
    pub vwap_std: f64,
    pub vpoc: f64,
    pub cum_pv: f64,
    pub cum_v: f64,
    pub cum_p2v: f64,
    pub cum_delta: f64,
    pub vwap_history: Vec<f64>,
    pub atr_history: Vec<f64>,
    pub std_history: Vec<f64>,
    pub vpoc_history: Vec<f64>,
    pub bar_ranges: Vec<f64>,
    pub prev_close: Option<f64>,
    pub tape: VecDeque<Trade>,
    pub quotes: VecDeque<Quote>,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub last: f64,
    pub bid: f64,
    pub ask: f64,
    pub trade_count: u64,
    pub total_volume: f64,
    pub tick_count: u64,
    #[serde(skip)]
    pub bar_just_closed: bool,
}

// Custom serde for BTreeMap<OrderedFloat<f64>, f64>
mod btree_ordered_float {
    use ordered_float::OrderedFloat;
    use serde::{Deserialize, Deserializer, Serialize, Serializer};
    use std::collections::BTreeMap;

    pub fn serialize<S>(map: &BTreeMap<OrderedFloat<f64>, f64>, s: S) -> Result<S::Ok, S::Error>
    where S: Serializer {
        let converted: BTreeMap<String, f64> = map.iter().map(|(k, v)| (k.to_string(), *v)).collect();
        converted.serialize(s)
    }

    pub fn deserialize<'de, D>(d: D) -> Result<BTreeMap<OrderedFloat<f64>, f64>, D::Error>
    where D: Deserializer<'de> {
        let raw: BTreeMap<String, f64> = BTreeMap::deserialize(d)?;
        Ok(raw.into_iter().filter_map(|(k, v)| k.parse::<f64>().ok().map(|f| (OrderedFloat(f), v))).collect())
    }
}

impl Session {
    pub fn new(date: &str) -> Self {
        Self {
            date: date.to_string(),
            bars: vec![],
            cur_bar: None,
            vol_profile: BTreeMap::new(),
            vwap: 0.0, vwap_std: 0.0, vpoc: 0.0,
            cum_pv: 0.0, cum_v: 0.0, cum_p2v: 0.0,
            cum_delta: 0.0,
            vwap_history: vec![], atr_history: vec![],
            std_history: vec![], vpoc_history: vec![],
            bar_ranges: vec![], prev_close: None,
            tape: VecDeque::new(), quotes: VecDeque::new(),
            open: 0.0, high: 0.0, low: f64::MAX,
            last: 0.0, bid: 0.0, ask: 0.0,
            trade_count: 0, total_volume: 0.0, tick_count: 0,
            bar_just_closed: false,
        }
    }

    pub fn add_bar(&mut self, o: f64, h: f64, l: f64, c: f64, v: f64, ts: DateTime<Utc>) {
        // Update session stats
        if self.open == 0.0 { self.open = o; }
        if h > self.high { self.high = h; }
        if l < self.low { self.low = l; }
        self.last = c;
        self.total_volume += v;

        // Volume profile — distribute evenly across price range
        self.distribute_volume(l, h, v);

        // VWAP accumulators
        let tp = (h + l + c) / 3.0;
        self.cum_pv += tp * v;
        self.cum_v += v;
        self.cum_p2v += tp * tp * v;
        self.update_vwap();

        // Track histories
        self.vwap_history.push(self.vwap);
        self.vpoc_history.push(self.vpoc);
        self.std_history.push(self.vwap_std);

        // ATR
        let tr = true_range(h, l, self.prev_close);
        self.bar_ranges.push(tr);
        self.prev_close = Some(c);
        let lb = tail(&self.bar_ranges, 20);
        self.atr_history.push(lb.iter().sum::<f64>() / lb.len() as f64);

        // Rebuild z on bar
        let z = if self.vwap_std > 0.01 { (c - self.vwap) / self.vwap_std } else { 0.0 };

        self.bars.push(Bar {
            ts, o, h, l, c, v,
            vwap: round2(self.vwap),
            vwap_std: round4(self.vwap_std),
            z: round2(z),
            vpoc: self.vpoc,
            vpoc60: self.compute_vpoc60(),
        });
    }

    pub fn add_trade(&mut self, price: f64, vol: f64, side: &str, now: DateTime<Utc>) {
        self.trade_count += 1;
        self.total_volume += vol;
        self.last = price;

        match side {
            "BUY" => self.cum_delta += vol,
            "SELL" => self.cum_delta -= vol,
            _ => {}
        }

        if self.open == 0.0 { self.open = price; }
        if price > self.high { self.high = price; }
        if price < self.low { self.low = price; }

        let key = OrderedFloat(round_tick(price));
        *self.vol_profile.entry(key).or_insert(0.0) += vol;
        // Keep vpoc current after every trade
        self.vpoc = self.vol_profile.iter()
            .max_by(|a, b| a.1.partial_cmp(b.1).unwrap())
            .map(|(k, _)| k.0)
            .unwrap_or(0.0);

        self.cum_pv += price * vol;
        self.cum_v += vol;
        self.cum_p2v += price * price * vol;
        self.update_vwap();

        // Build current bar (1-min)
        let bar_key = now.format("%Y-%m-%dT%H:%M:00Z").to_string();
        match &mut self.cur_bar {
            Some(b) if b.ts == bar_key => {
                if price > b.h { b.h = price; }
                if price < b.l { b.l = price; }
                b.c = price;
                b.v += vol;
            }
            _ => {
                // Close previous bar
                if let Some(old) = self.cur_bar.take() {
                    self.finalize_cur_bar(old);
                    self.bar_just_closed = true;
                }
                self.cur_bar = Some(CurBar { ts: bar_key, o: price, h: price, l: price, c: price, v: vol });
            }
        }

        let time_str = now.format("%H:%M:%S%.3f").to_string();
        self.tape.push_back(Trade { time: time_str, price, vol, side: side.to_string() });
        if self.tape.len() > 500 { self.tape.pop_front(); }
    }

    fn finalize_cur_bar(&mut self, b: CurBar) {
        let h = b.h; let l = b.l; let c = b.c; let v = b.v;
        let tr = true_range(h, l, self.prev_close);
        self.bar_ranges.push(tr);
        self.prev_close = Some(c);
        let lb = tail(&self.bar_ranges, 20);
        let atr = lb.iter().sum::<f64>() / lb.len() as f64;
        self.atr_history.push(atr);
        self.vwap_history.push(self.vwap);
        self.vpoc_history.push(self.vpoc);
        self.std_history.push(self.vwap_std);

        let ts = b.ts.parse::<DateTime<Utc>>().unwrap_or(Utc::now());
        let z = if self.vwap_std > 0.01 { (c - self.vwap) / self.vwap_std } else { 0.0 };

        self.bars.push(Bar {
            ts,
            o: b.o, h: b.h, l: b.l, c, v,
            vwap: round2(self.vwap),
            vwap_std: round4(self.vwap_std),
            z: round2(z),
            vpoc: self.vpoc,
            vpoc60: self.compute_vpoc60(),
        });
    }

    pub fn add_quote(&mut self, bid: f64, ask: f64, now: DateTime<Utc>) {
        self.tick_count += 1;
        if bid > 0.0 { self.bid = bid; }
        if ask > 0.0 { self.ask = ask; }
        if self.bid > 0.0 && self.ask > 0.0 {
            self.last = (self.bid + self.ask) / 2.0;
        }
        let spread = if self.ask > 0.0 && self.bid > 0.0 { self.ask - self.bid } else { 0.0 };
        let time_str = now.format("%H:%M:%S%.3f").to_string();
        self.quotes.push_back(Quote { time: time_str, bid: self.bid, ask: self.ask, spread: round2(spread) });
        if self.quotes.len() > 50 { self.quotes.pop_front(); }
    }

    fn distribute_volume(&mut self, l: f64, h: f64, v: f64) {
        let lo = round_tick(l);
        let hi = round_tick(h);
        let n = ((hi - lo) / TICK_SIZE).round() as usize + 1;
        let n = n.max(1);
        let vpl = v / n as f64;
        let mut price = lo;
        while price <= hi + 1e-9 {
            *self.vol_profile.entry(OrderedFloat(round_tick(price))).or_insert(0.0) += vpl;
            price += TICK_SIZE;
        }
        self.vpoc = self.vol_profile.iter()
            .max_by(|a, b| a.1.partial_cmp(b.1).unwrap())
            .map(|(k, _)| k.0)
            .unwrap_or(0.0);
    }

    fn update_vwap(&mut self) {
        if self.cum_v > 0.0 {
            self.vwap = self.cum_pv / self.cum_v;
            let var = (self.cum_p2v / self.cum_v) - self.vwap * self.vwap;
            self.vwap_std = if var > 0.0 { var.sqrt() } else { 0.0 };
        }
    }

    pub fn compute_vpoc60(&self) -> f64 {
        let start = self.bars.len().saturating_sub(60);
        let mut vp: BTreeMap<OrderedFloat<f64>, f64> = BTreeMap::new();
        for b in &self.bars[start..] {
            let lo = round_tick(b.l);
            let hi = round_tick(b.h);
            let n = ((hi - lo) / TICK_SIZE).round() as usize + 1;
            let n = n.max(1);
            let vpl = b.v / n as f64;
            let mut p = lo;
            while p <= hi + 1e-9 {
                *vp.entry(OrderedFloat(round_tick(p))).or_insert(0.0) += vpl;
                p += TICK_SIZE;
            }
        }
        vp.iter().max_by(|a, b| a.1.partial_cmp(b.1).unwrap()).map(|(k, _)| k.0).unwrap_or(0.0)
    }

    pub fn vpoc_migration_speed(&self, lookback: usize) -> f64 {
        if self.vpoc_history.len() < lookback { return 0.0; }
        let recent: Vec<f64> = self.vpoc_history.iter().rev().take(lookback)
            .filter(|&&p| p > 0.0).cloned().collect();
        if recent.len() < 2 { return 0.0; }
        (recent[0] - recent[recent.len() - 1]).abs() / recent.len() as f64
    }

    pub fn is_session_stable(&self) -> bool {
        self.vpoc_migration_speed(20) <= 0.15
    }

    pub fn get_atr_slope(&self, lookback: usize) -> f64 {
        if self.atr_history.len() < lookback { return 0.0; }
        let now = *self.atr_history.last().unwrap();
        let past = self.atr_history[self.atr_history.len() - lookback];
        if past <= 0.0 { return 0.0; }
        (now - past) / past
    }

    pub fn current_atr(&self) -> f64 {
        self.atr_history.last().copied().unwrap_or(0.0)
    }

    pub fn z_score(&self) -> f64 {
        if self.vwap_std > 0.01 { (self.last - self.vwap) / self.vwap_std } else { 0.0 }
    }

    pub fn save(&self) -> Result<()> {
        fs::create_dir_all(DATA_DIR)?;
        let path = Path::new(DATA_DIR).join(format!("{}.json", self.date));
        let json = serde_json::to_string(self)?;
        fs::write(path, json)?;
        Ok(())
    }

    pub fn load(date: &str) -> Result<Self> {
        let path = Path::new(DATA_DIR).join(format!("{date}.json"));
        let data = fs::read_to_string(path)?;
        let mut sess: Session = serde_json::from_str(&data)?;
        // Rebuild derived histories from bars if missing
        if sess.atr_history.is_empty() && !sess.bars.is_empty() {
            sess.rebuild_histories();
        }
        Ok(sess)
    }

    fn rebuild_histories(&mut self) {
        let mut cum_pv = 0.0f64;
        let mut cum_v = 0.0f64;
        let mut cum_p2v = 0.0f64;
        let mut prev_close: Option<f64> = None;
        let mut ranges: Vec<f64> = vec![];
        let mut vp: BTreeMap<OrderedFloat<f64>, f64> = BTreeMap::new();

        for b in &self.bars {
            let (h, l, c, v) = (b.h, b.l, b.c, b.v);
            let tp = (h + l + c) / 3.0;
            cum_pv += tp * v; cum_v += v; cum_p2v += tp * tp * v;
            let vwap = if cum_v > 0.0 { cum_pv / cum_v } else { 0.0 };
            let var = if cum_v > 0.0 { (cum_p2v / cum_v) - vwap * vwap } else { 0.0 };
            let std = if var > 0.0 { var.sqrt() } else { 0.0 };

            let lo = round_tick(l); let hi = round_tick(h);
            let n = ((hi - lo) / TICK_SIZE).round() as usize + 1;
            let n = n.max(1);
            let vpl = v / n as f64;
            let mut p = lo;
            while p <= hi + 1e-9 { *vp.entry(OrderedFloat(round_tick(p))).or_insert(0.0) += vpl; p += TICK_SIZE; }
            let vpoc = vp.iter().max_by(|a, b| a.1.partial_cmp(b.1).unwrap()).map(|(k, _)| k.0).unwrap_or(c);

            let tr = true_range(h, l, prev_close);
            ranges.push(tr);
            prev_close = Some(c);
            let lb = tail(&ranges, 20);
            let atr = lb.iter().sum::<f64>() / lb.len() as f64;

            self.vwap_history.push(vwap);
            self.std_history.push(std);
            self.vpoc_history.push(vpoc);
            self.atr_history.push(atr);
        }
    }

    pub fn list_sessions() -> Vec<String> {
        let Ok(dir) = fs::read_dir(DATA_DIR) else { return vec![]; };
        let mut dates: Vec<String> = dir
            .flatten()
            .filter_map(|e| {
                let n = e.file_name();
                let s = n.to_string_lossy();
                if s.ends_with(".json") { Some(s.trim_end_matches(".json").to_string()) } else { None }
            })
            .collect();
        dates.sort_unstable_by(|a, b| b.cmp(a)); // newest first
        dates
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

fn true_range(h: f64, l: f64, prev_close: Option<f64>) -> f64 {
    let base = h - l;
    match prev_close {
        Some(pc) => base.max((h - pc).abs()).max((l - pc).abs()),
        None => base,
    }
}

fn tail(v: &[f64], n: usize) -> &[f64] {
    &v[v.len().saturating_sub(n)..]
}

pub fn round_tick(p: f64) -> f64 {
    (p / TICK_SIZE).round() * TICK_SIZE
}

fn round2(x: f64) -> f64 { (x * 100.0).round() / 100.0 }
fn round4(x: f64) -> f64 { (x * 10000.0).round() / 10000.0 }

// ── Tests ─────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn make_ts(s: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(s).unwrap().with_timezone(&Utc)
    }

    #[test]
    fn test_vwap_single_bar() {
        let mut sess = Session::new("2026-01-01");
        sess.add_bar(100.0, 102.0, 99.0, 101.0, 1000.0, make_ts("2026-01-01T14:30:00Z"));
        // tp = (h + l + c) / 3 = (102 + 99 + 101) / 3 = 100.667
        let expected_vwap = (102.0 + 99.0 + 101.0) / 3.0;
        assert!((sess.vwap - expected_vwap).abs() < 0.01);
    }

    #[test]
    fn test_vpoc_highest_volume_level() {
        let mut sess = Session::new("2026-01-01");
        // Add trades at specific prices
        let now = make_ts("2026-01-01T14:30:00Z");
        sess.add_trade(5000.0, 100.0, "BUY", now);
        sess.add_trade(5000.0, 200.0, "BUY", now);
        sess.add_trade(5000.25, 50.0, "SELL", now);
        // 5000.0 has 300 vol, 5000.25 has 50 — VPOC should be 5000.0
        assert_eq!(sess.vpoc, 5000.0);
    }

    #[test]
    fn test_cum_delta() {
        let mut sess = Session::new("2026-01-01");
        let now = make_ts("2026-01-01T14:30:00Z");
        sess.add_trade(5000.0, 100.0, "BUY", now);
        sess.add_trade(5000.0, 60.0, "SELL", now);
        assert!((sess.cum_delta - 40.0).abs() < 1e-9);
    }

    #[test]
    fn test_bar_close_on_new_minute() {
        let mut sess = Session::new("2026-01-01");
        let t1 = make_ts("2026-01-01T14:30:30Z");
        let t2 = make_ts("2026-01-01T14:31:15Z");
        sess.add_trade(5000.0, 10.0, "BUY", t1);
        assert!(!sess.bar_just_closed);
        sess.add_trade(5001.0, 10.0, "BUY", t2);
        assert!(sess.bar_just_closed);
        assert_eq!(sess.bars.len(), 1);
    }
}
