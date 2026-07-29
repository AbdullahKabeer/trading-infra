#!/usr/bin/env python3
"""
Gold (GCE) Backtester
=====================
Runs the VWAP Reversion strategy on 1-min bars from gc_sessions/

Parameters customized for Gold (GC):
Tick Size: 0.1
Tick Value: $10.0
"""

import os, sys, glob, json
from collections import defaultdict
from datetime import datetime

TICK_SIZE = 0.1
TICK_VALUE = 10.0
CONTRACTS = 1

# Strategy Params (These can be tweaked for GC)
Z_THRESH = 1.25
STOP_RATIO = 0.5
MIN_STOP_TICKS = 6       # 0.6 points ($60)
MAX_STOP_TICKS = 24      # 2.4 points ($240)
TRAIL_ACTIVATE = 20      # 2.0 points ($200)
TRAIL_DISTANCE = 16      # 1.6 points ($160)
EXIT_MIN_TICKS = 10      # minimum ticks to hit target

GUTTER_GOAL = 1500
GUTTER_DD = 1000         # tight loss limit for gold
TIME_STOP_MINS = 90
MAX_TRADES = 5

COMMISSION_RT = 2.80

def run_session_backtest(bars, date_str=""):
    cum_pv = 0; cum_v = 0; cum_p2v = 0; vp_dict = defaultdict(float)
    vpoc_hist = []; pos = None; trades = []; trades_today = 0
    session_pnl = 0.0
    bt_bar_ranges = []; bt_atr_history = []; bt_prev_close = None
    
    trade_logs = []

    for i in range(1, len(bars)):
        b = bars[i]
        c = b["c"]; h = b["h"]; l = b["l"]; o = b["o"]; v = b["v"]
        
        # VWAP
        tp_v = (h + l + c) / 3
        cum_pv += tp_v * v; cum_v += v; cum_p2v += (tp_v ** 2) * v
        vwap = cum_pv / cum_v if cum_v > 0 else 0
        var = (cum_p2v / cum_v) - (vwap ** 2) if cum_v > 0 else 0
        std = var ** 0.5 if var > 0 else 0
        b["std"] = std

        prev_std = bars[i-5].get("std", std) if i >= 5 else std
        sigma_exp = std / prev_std if prev_std > 0 else 1.0

        # ATR
        if bt_prev_close is None: bt_prev_close = c
        tr = max(h - l, abs(h - bt_prev_close), abs(l - bt_prev_close))
        bt_bar_ranges.append(tr); bt_prev_close = c
        
        lb = bt_bar_ranges[-20:] if len(bt_bar_ranges) >= 20 else bt_bar_ranges
        atr = sum(lb) / len(lb) if lb else 0
        bt_atr_history.append(atr)
        
        atr_slope = 0
        if len(bt_atr_history) >= 10 and bt_atr_history[-10] > 0:
            atr_slope = (bt_atr_history[-1] - bt_atr_history[-10]) / bt_atr_history[-10]

        # Volume Profile using TICK_SIZE steps
        lo_lv = round(l / TICK_SIZE) * TICK_SIZE
        hi_lv = round(h / TICK_SIZE) * TICK_SIZE
        n_levels = max(1, int((hi_lv - lo_lv) / TICK_SIZE) + 1)
        vpl = v / n_levels; lv = lo_lv
        while lv <= hi_lv:
            vp_dict[lv] += vpl; lv = round(lv + TICK_SIZE, 1)
            
        vpoc = max(vp_dict, key=vp_dict.get) if vp_dict else 0
        vpoc_hist.append(vpoc)

        stable = True
        if len(vpoc_hist) >= 20:
            recent_p = vpoc_hist[-20:]
            valid = [p for p in recent_p if p and p > 0]
            if len(valid) >= 2:
                migration = abs(valid[-1] - valid[0]) / len(valid)
                stable = migration <= 0.15 # 15% migration max

        if i < 15: continue
        
        target = round(vwap / TICK_SIZE) * TICK_SIZE
        if not target: continue
        target_z = (c - target) / std if std >= TICK_SIZE else 0

        # Position Management
        if pos:
            bt = i - pos["bar_idx"]
            
            if pos["dir"] == "LONG":
                # Max win limit
                if session_pnl < GUTTER_GOAL:
                    gut_tp = pos["ep"] + ((GUTTER_GOAL - session_pnl) / (CONTRACTS * TICK_VALUE)) * TICK_SIZE
                    if h >= gut_tp:
                        ticks = (gut_tp - pos["ep"]) / TICK_SIZE
                        pnl = ticks * TICK_VALUE * CONTRACTS
                        session_pnl += pnl
                        trades.append(pnl)
                        trade_logs.append(("LONG", pos["ep"], gut_tp, pnl, "Gutter Win"))
                        pos = None; continue
                        
                # Max loss limit
                if session_pnl > -GUTTER_DD:
                    remaining = (session_pnl + GUTTER_DD) / (CONTRACTS * TICK_VALUE)
                    if remaining > 0:
                        gut_sl = pos["ep"] - remaining * TICK_SIZE
                        if l <= gut_sl:
                            ticks = (gut_sl - pos["ep"]) / TICK_SIZE
                            pnl = ticks * TICK_VALUE * CONTRACTS
                            session_pnl += pnl
                            trades.append(pnl)
                            trade_logs.append(("LONG", pos["ep"], gut_sl, pnl, "Gutter Loss"))
                            pos = None; continue

                # Hard Stop
                if l <= pos["sl"]:
                    ticks = (pos["sl"] - pos["ep"]) / TICK_SIZE
                    pnl = ticks * TICK_VALUE * CONTRACTS
                    session_pnl += pnl
                    trades.append(pnl)
                    trade_logs.append(("LONG", pos["ep"], pos["sl"], pnl, "Stop Loss"))
                    pos = None; continue

                # Break-even and trailing
                if h > pos["bp"]: pos["bp"] = h
                ur = (pos["bp"] - pos["ep"]) / TICK_SIZE
                cur = (c - pos["ep"]) / TICK_SIZE
                
                # Take profit at VWAP
                if target and c >= target and cur >= EXIT_MIN_TICKS:
                    ticks = (c - pos["ep"]) / TICK_SIZE
                    pnl = ticks * TICK_VALUE * CONTRACTS
                    session_pnl += pnl
                    trades.append(pnl)
                    trade_logs.append(("LONG", pos["ep"], c, pnl, "Target"))
                    pos = None; continue

                # Trailing stops
                if ur >= TRAIL_ACTIVATE:
                    tl = round((pos["bp"] - TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                    if tl > pos["sl"]: pos["sl"] = tl
                    
                if ur >= 10 and pos["sl"] < pos["ep"]:
                    pos["sl"] = pos["ep"]  # move to breakeven
                    
                # Time Stop
                if bt > TIME_STOP_MINS:
                    ticks = (c - pos["ep"]) / TICK_SIZE
                    pnl = ticks * TICK_VALUE * CONTRACTS
                    session_pnl += pnl
                    trades.append(pnl)
                    trade_logs.append(("LONG", pos["ep"], c, pnl, "Time Stop"))
                    pos = None; continue

            else: # SHORT
                if session_pnl < GUTTER_GOAL:
                    gut_tp = pos["ep"] - ((GUTTER_GOAL - session_pnl) / (CONTRACTS * TICK_VALUE)) * TICK_SIZE
                    if l <= gut_tp:
                        ticks = (pos["ep"] - gut_tp) / TICK_SIZE
                        pnl = ticks * TICK_VALUE * CONTRACTS
                        session_pnl += pnl
                        trades.append(pnl)
                        trade_logs.append(("SHORT", pos["ep"], gut_tp, pnl, "Gutter Win"))
                        pos = None; continue

                if session_pnl > -GUTTER_DD:
                    remaining = (session_pnl + GUTTER_DD) / (CONTRACTS * TICK_VALUE)
                    if remaining > 0:
                        gut_sl = pos["ep"] + remaining * TICK_SIZE
                        if h >= gut_sl:
                            ticks = (pos["ep"] - gut_sl) / TICK_SIZE
                            pnl = ticks * TICK_VALUE * CONTRACTS
                            session_pnl += pnl
                            trades.append(pnl)
                            trade_logs.append(("SHORT", pos["ep"], gut_sl, pnl, "Gutter Loss"))
                            pos = None; continue

                if h >= pos["sl"]:
                    ticks = (pos["ep"] - pos["sl"]) / TICK_SIZE
                    pnl = ticks * TICK_VALUE * CONTRACTS
                    session_pnl += pnl
                    trades.append(pnl)
                    trade_logs.append(("SHORT", pos["ep"], pos["sl"], pnl, "Stop Loss"))
                    pos = None; continue

                if l < pos["bp"]: pos["bp"] = l
                ur = (pos["ep"] - pos["bp"]) / TICK_SIZE
                cur = (pos["ep"] - c) / TICK_SIZE
                
                if target and c <= target and cur >= EXIT_MIN_TICKS:
                    ticks = (pos["ep"] - c) / TICK_SIZE
                    pnl = ticks * TICK_VALUE * CONTRACTS
                    session_pnl += pnl
                    trades.append(pnl)
                    trade_logs.append(("SHORT", pos["ep"], c, pnl, "Target"))
                    pos = None; continue

                if ur >= TRAIL_ACTIVATE:
                    tl = round((pos["bp"] + TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                    if tl < pos["sl"]: pos["sl"] = tl
                    
                if ur >= 10 and pos["sl"] > pos["ep"]:
                    pos["sl"] = pos["ep"]

                if bt > TIME_STOP_MINS:
                    ticks = (pos["ep"] - c) / TICK_SIZE
                    pnl = ticks * TICK_VALUE * CONTRACTS
                    session_pnl += pnl
                    trades.append(pnl)
                    trade_logs.append(("SHORT", pos["ep"], c, pnl, "Time Stop"))
                    pos = None; continue

        else:
            # Entry logic
            if i < 45 or i > 360 or trades_today >= MAX_TRADES: continue
            if not stable: continue
            if round(session_pnl, 2) >= GUTTER_GOAL: continue 
            if round(session_pnl, 2) <= -GUTTER_DD: continue 
            
            if abs(target_z) >= Z_THRESH:
                dist = abs(c - target)
                if sigma_exp > 1.25: continue # Vol expansion panic
                if atr_slope > 0.10: continue # Upwards slope
                
                sd = max(min(dist * STOP_RATIO, MAX_STOP_TICKS * TICK_SIZE), MIN_STOP_TICKS * TICK_SIZE)
                
                if target_z < -Z_THRESH: # mean reversion -> go LONG
                    sl = round((c - sd) / TICK_SIZE) * TICK_SIZE
                    pos = {"dir": "LONG", "ep": c, "sl": sl, "bp": c, "bar_idx": i}
                    trades_today += 1
                    session_pnl -= COMMISSION_RT * CONTRACTS
                    
                elif target_z > Z_THRESH: # mena reversion -> go SHORT
                    sl = round((c + sd) / TICK_SIZE) * TICK_SIZE
                    pos = {"dir": "SHORT", "ep": c, "sl": sl, "bp": c, "bar_idx": i}
                    trades_today += 1
                    session_pnl -= COMMISSION_RT * CONTRACTS

    if pos: # eod close
        c = bars[-1]["c"]
        ticks = (c - pos["ep"]) / TICK_SIZE if pos["dir"] == "LONG" else (pos["ep"] - c) / TICK_SIZE
        pnl = ticks * TICK_VALUE * CONTRACTS
        session_pnl += pnl
        trades.append(pnl)
        trade_logs.append((pos["dir"], pos["ep"], c, pnl, "EOD Close"))

    net_trades = [t - COMMISSION_RT * CONTRACTS for t in trades]
    return net_trades, session_pnl, trade_logs

def log_results(folder="gc_sessions", detailed=False):
    files = sorted(glob.glob(os.path.join(folder, "*.json")))
    if not files:
        print(f"No json files found in {folder}/")
        return

    print("=" * 80)
    print(f"  GOLD (GCE) STRATEGY BACKTEST")
    print(f"  Sessions: {len(files)} | Z={Z_THRESH} | MTrades={MAX_TRADES}")
    print("=" * 80)

    total_pnl = 0.0
    all_net_trades = []
    
    for f in files:
        day = os.path.basename(f).replace(".json", "")
        with open(f, 'r') as fp:
            data = json.load(fp)
            bars = data.get("bars", [])
            
        if len(bars) < 60:
            continue
            
        net_trades, session_pnl, trade_logs = run_session_backtest(bars, day)
        total_pnl += session_pnl
        all_net_trades.extend(net_trades)
        
        if detailed and net_trades:
            print(f"\n[{day}] Session PnL: ${session_pnl:.2f}")
            for t in trade_logs:
                print(f"  {t[0]:>5} | Ep: {t[1]:.1f} | Exit: {t[2]:.1f} | PnL: ${t[3]:.2f} | {t[4]}")

    wins = [t for t in all_net_trades if t > 0]
    losses = [t for t in all_net_trades if t <= 0]
    wr = len(wins) / len(all_net_trades) if all_net_trades else 0
    avg_w = sum(wins) / len(wins) if wins else 0
    avg_l = abs(sum(losses) / len(losses)) if losses else 0
    
    print("\n" + "=" * 80)
    print(f"  Total PnL:    ${total_pnl:.2f}")
    print(f"  Total Trades: {len(all_net_trades)}")
    print(f"  Win Rate:     {wr*100:.1f}%")
    if losses:
        print(f"  R:R Ratio:    {avg_w / avg_l:.2f}")
    print(f"  Avg Win:      ${avg_w:.2f}")
    print(f"  Avg Loss:     ${avg_l:.2f}")
    print("=" * 80)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--detailed":
        log_results("gc_sessions", detailed=True)
    else:
        log_results("gc_sessions", detailed=False)
