#!/usr/bin/env python3
"""
ES Tick Backtester v1
=====================
Uses raw tick data (T/Q) from es_ticks/*.csv to simulate VWAPBot strategy.
Accurate SL/TP hits and trailing stop movements.

Usage: python tick_backtest.py es_ticks/2026-03-27.csv
"""

import os, sys, csv, json
from datetime import datetime, timedelta
from collections import defaultdict

# --- CONFIG (Mirror combine_live.py) ---
TICK_SIZE = 0.25
TICK_VALUE = 12.50
CONTRACTS = 1
Z_THRESH = 1.5
STOP_RATIO = 0.5
MIN_STOP_TICKS = 6
MAX_STOP_TICKS = 24
TRAIL_ACTIVATE = 20
TRAIL_DISTANCE = 16
EXIT_MIN_TICKS = 12
GUTTER_GOAL = 1500
GUTTER_DD = 1900
TIME_STOP_MINS = 90
MAX_TRADES = 5

class TickSession:
    def __init__(self, date_str):
        self.date = date_str
        self.bars = []
        self.cur_bar = None
        self.vol_profile = defaultdict(float)
        self.cum_pv = 0.0; self.cum_v = 0; self.cum_p2v = 0.0
        self.vwap = 0.0; self.vwap_std = 0.0
        self.open = 0.0; self.high = 0.0; self.low = 999999.0; self.last = 0.0
        self.total_volume = 0
        self.vpoc_history = []
        self.std_history = []
        self.bar_ranges = []
        self.atr_history = []
        self._prev_close = None

    def process_trade(self, price, vol, ts):
        self.total_volume += vol
        self.last = price
        if self.open == 0: self.open = price
        if price > self.high: self.high = price
        if price < self.low: self.low = price
        
        self.vol_profile[price] += vol
        self.cum_pv += price * vol
        self.cum_v += vol
        self.cum_p2v += (price ** 2) * vol
        
        if self.cum_v > 0:
            self.vwap = self.cum_pv / self.cum_v
            var = (self.cum_p2v / self.cum_v) - (self.vwap ** 2)
            self.vwap_std = var ** 0.5 if var > 0 else 0

        # Bar aggregation (1-min)
        bar_ts = ts.replace(second=0, microsecond=0)
        if self.cur_bar is None or self.cur_bar["ts"] != bar_ts:
            if self.cur_bar:
                self.bars.append(self.cur_bar)
                self.vpoc_history.append(self.vpoc)
                self.std_history.append(self.vwap_std)
                
                # ATR tracking
                cb = self.cur_bar
                tr = max(cb["h"] - cb["l"], 
                         abs(cb["h"] - self._prev_close) if self._prev_close else cb["h"] - cb["l"],
                         abs(cb["l"] - self._prev_close) if self._prev_close else cb["h"] - cb["l"])
                self.bar_ranges.append(tr)
                self._prev_close = cb["c"]
                lb = self.bar_ranges[-20:] if len(self.bar_ranges) >= 20 else self.bar_ranges
                self.atr_history.append(sum(lb) / len(lb) if lb else 0)
                
            self.cur_bar = {"ts": bar_ts, "o": price, "h": price, "l": price, "c": price, "v": vol}
        else:
            b = self.cur_bar
            if price > b["h"]: b["h"] = price
            if price < b["l"]: b["l"] = price
            b["c"] = price; b["v"] += vol

    @property
    def vpoc(self):
        return max(self.vol_profile, key=self.vol_profile.get) if self.vol_profile else 0

    def get_atr_slope(self, lookback=10):
        if len(self.atr_history) < lookback: return 0
        atr_now = self.atr_history[-1]
        atr_past = self.atr_history[-lookback]
        if atr_past <= 0: return 0
        return (atr_now - atr_past) / atr_past

    def is_stable(self, max_migration=0.15, lookback=20):
        if len(self.vpoc_history) < lookback: return True
        recent = self.vpoc_history[-lookback:]
        valid = [p for p in recent if p > 0]
        if len(valid) < 2: return True
        migration = abs(valid[-1] - valid[0]) / len(valid)
        return migration <= max_migration

def run_backtest(csv_path):
    if not os.path.exists(csv_path):
        print(f"File not found: {csv_path}")
        return

    print(f"  [BT] Loading {csv_path}...")
    date_str = os.path.basename(csv_path).replace(".csv", "")
    sess = TickSession(date_str)
    
    pos = None
    trades = []
    total_pnl = 0.0
    daily_pnl = 0.0
    trades_today = 0
    last_exit_bar = -999
    peak_pnl = 0.0
    max_dd = 0.0

    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = datetime.strptime(row['ts'], "%Y-%m-%d %H:%M:%S.%f")
            
            if row['type'] == 'Q':
                continue # for now, we use trade prices for hits
            
            price = float(row['price'])
            vol = int(row['volume'])
            sess.process_trade(price, vol, ts)
            
            # Indicator state
            vwap = sess.vwap
            vpoc = sess.vpoc
            std = sess.vwap_std
            bar_idx = len(sess.bars)
            
            # --- Logic (Tick-by-Tick) ---
            if pos:
                ep = pos["ep"]
                direction = pos["dir"]
                ticks = (price - ep) / TICK_SIZE if direction == "LONG" else (ep - price) / TICK_SIZE
                cur_pnl = ticks * TICK_VALUE * CONTRACTS
                
                # Peak/DD tracking
                session_equity = total_pnl + cur_pnl
                if session_equity > peak_pnl: peak_pnl = session_equity
                dd = peak_pnl - session_equity
                if dd > max_dd: max_dd = dd

                # Gutter Win/Loss
                if daily_pnl + cur_pnl >= GUTTER_GOAL:
                    exit_price = ep + ((GUTTER_GOAL - daily_pnl) / (CONTRACTS * TICK_VALUE)) * TICK_SIZE if direction == "LONG" else ep - ((GUTTER_GOAL - daily_pnl) / (CONTRACTS * TICK_VALUE)) * TICK_SIZE
                    pnl = GUTTER_GOAL - daily_pnl
                    trades.append({"ts": ts, "dir": direction, "ep": ep, "exit": exit_price, "pnl": pnl, "reason": "GUTTER WIN"})
                    total_pnl += pnl; daily_pnl += pnl; pos = None; last_exit_bar = bar_idx; trades_today += 1; continue

                if daily_pnl + cur_pnl <= -GUTTER_DD:
                    exit_price = ep - ((GUTTER_DD + daily_pnl) / (CONTRACTS * TICK_VALUE)) * TICK_SIZE if direction == "LONG" else ep + ((GUTTER_DD + daily_pnl) / (CONTRACTS * TICK_VALUE)) * TICK_SIZE
                    pnl = -GUTTER_DD - daily_pnl
                    trades.append({"ts": ts, "dir": direction, "ep": ep, "exit": exit_price, "pnl": pnl, "reason": "GUTTER LOSS"})
                    total_pnl += pnl; daily_pnl += pnl; pos = None; last_exit_bar = bar_idx; trades_today += 1; continue

                # SL/TP
                if direction == "LONG":
                    if price <= pos["sl"]:
                        pnl = (pos["sl"] - ep) / TICK_SIZE * TICK_VALUE * CONTRACTS
                        trades.append({"ts": ts, "dir": "LONG", "ep": ep, "exit": pos["sl"], "pnl": pnl, "reason": "STOP LOSS"})
                        total_pnl += pnl; daily_pnl += pnl; pos = None; last_exit_bar = bar_idx; continue
                    
                    target = round(vwap / TICK_SIZE) * TICK_SIZE
                    if target and price >= target and ticks >= EXIT_MIN_TICKS:
                        pnl = (price - ep) / TICK_SIZE * TICK_VALUE * CONTRACTS
                        trades.append({"ts": ts, "dir": "LONG", "ep": ep, "exit": price, "pnl": pnl, "reason": "TARGET"})
                        total_pnl += pnl; daily_pnl += pnl; pos = None; last_exit_bar = bar_idx; continue
                    
                    # Trailing / BE
                    if price > pos["bp"]: pos["bp"] = price
                    ur = (pos["bp"] - ep) / TICK_SIZE
                    if ur >= TRAIL_ACTIVATE:
                        tl = round((pos["bp"] - TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                        if tl > pos["sl"]: pos["sl"] = tl
                    if ur >= 10 and pos["sl"] < ep: pos["sl"] = ep

                else: # SHORT
                    if price >= pos["sl"]:
                        pnl = (ep - pos["sl"]) / TICK_SIZE * TICK_VALUE * CONTRACTS
                        trades.append({"ts": ts, "dir": "SHORT", "ep": ep, "exit": pos["sl"], "pnl": pnl, "reason": "STOP LOSS"})
                        total_pnl += pnl; daily_pnl += pnl; pos = None; last_exit_bar = bar_idx; continue
                    
                    target = round(vwap / TICK_SIZE) * TICK_SIZE
                    if target and price <= target and ticks >= EXIT_MIN_TICKS:
                        pnl = (ep - price) / TICK_SIZE * TICK_VALUE * CONTRACTS
                        trades.append({"ts": ts, "dir": "SHORT", "ep": ep, "exit": price, "pnl": pnl, "reason": "TARGET"})
                        total_pnl += pnl; daily_pnl += pnl; pos = None; last_exit_bar = bar_idx; continue
                    
                    # Trailing / BE
                    if price < pos["bp"]: pos["bp"] = price
                    ur = (ep - pos["bp"]) / TICK_SIZE
                    if ur >= TRAIL_ACTIVATE:
                        tl = round((pos["bp"] + TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                        if tl < pos["sl"]: pos["sl"] = tl
                    if ur >= 10 and pos["sl"] > ep: pos["sl"] = ep

            else:
                # Entry Logic
                if bar_idx < 45 or bar_idx > 360 or trades_today >= MAX_TRADES: continue
                if bar_idx - last_exit_bar < 5: continue
                if not sess.is_stable(): continue
                
                target = round(vwap / TICK_SIZE) * TICK_SIZE
                if not target or std < TICK_SIZE: continue
                z = (price - target) / std
                
                if abs(z) >= Z_THRESH:
                    if sess.get_atr_slope() > 0.10: continue
                    
                    dist = abs(price - target)
                    sd = max(min(dist * STOP_RATIO, MAX_STOP_TICKS * TICK_SIZE), MIN_STOP_TICKS * TICK_SIZE)
                    
                    if z < -Z_THRESH: # LONG
                        sl = round((price - sd) / TICK_SIZE) * TICK_SIZE
                        pos = {"dir": "LONG", "ep": price, "sl": sl, "bp": price, "bar_idx": bar_idx}
                        trades_today += 1
                        print(f"  [ENTRY] LONG @ {price} | SL: {sl} | Z: {z:.2f}")
                    elif z > Z_THRESH: # SHORT
                        sl = round((price + sd) / TICK_SIZE) * TICK_SIZE
                        pos = {"dir": "SHORT", "ep": price, "sl": sl, "bp": price, "bar_idx": bar_idx}
                        trades_today += 1
                        print(f"  [ENTRY] SHORT @ {price} | SL: {sl} | Z: {z:.2f}")

    print("\n" + "="*40)
    print(f"  RESULTS for {date_str}")
    print("="*40)
    for t in trades:
        print(f"  {t['ts'].strftime('%H:%M:%S')} | {t['dir']:>5} | In: {t['ep']:>7.2f} | Out: {t['exit']:>7.2f} | PnL: ${t['pnl']:>8.2f} | {t['reason']}")
    
    print("-" * 40)
    print(f"  Total Trades: {len(trades)}")
    print(f"  Final PnL:    ${total_pnl:.2f}")
    print(f"  Max Drawdown: ${max_dd:.2f}")
    print("="*40)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python tick_backtest.py <path_to_csv>")
    else:
        run_backtest(sys.argv[1])
