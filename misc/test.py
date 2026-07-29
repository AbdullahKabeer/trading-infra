"""
Monte Carlo Combine Simulator (TopstepX Accurate)
=============================================================
* Enforces the Maximum Loss Limit (EOD Trailing Drawdown).
* Accurately locks the trailing limit at the starting balance.
* Uses Gutters as your Personal Daily Loss Limit (PDLL).
"""

import os, glob, random
import numpy as np
from collections import defaultdict
from combine_live import Session, TICK_SIZE, TICK_VALUE, COMMISSION_RT
from topstep_v11 import TPO 

# ============================================================
# ACCOUNT & SIMULATION PARAMETERS (50K Combine)
# ============================================================
COMBINE_STARTING_BAL = 50000
COMBINE_PROFIT_TARGET = 3000
COMBINE_EOD_TRAILING_DD = 2000  

GUTTER_WIN = 1500
GUTTER_LOSS = -1900  

MONTE_CARLO_ITERATIONS = 1000
MAX_DAYS_PER_COMBINE = 60 

# ============================================================
# STRAT 1: ORIGINAL LIVE BOT (Cumulative VPOC)
# ============================================================
def simulate_live_bot_day(sess):
    pos = None; trades = 0; daily_pnl = 0.0
    lowest_intraday_pnl = 0.0  
    
    vpoc_hist = []
    cum_pv = 0; cum_v = 0; cum_p2v = 0
    vol_profile = defaultdict(float)
    
    for i, b in enumerate(sess.bars):
        h, l, c, v = b["h"], b["l"], b["c"], b["v"]
        
        tp = (h + l + c) / 3
        cum_pv += tp * v; cum_v += v; cum_p2v += (tp ** 2) * v
        vwap = cum_pv / cum_v if cum_v > 0 else 0
        var = (cum_p2v / cum_v) - (vwap ** 2) if cum_v > 0 else 0
        std = var ** 0.5 if var > 0 else 0
        
        lo_lv = round(l * 4) / 4; hi_lv = round(h * 4) / 4
        n_levels = max(1, int((hi_lv - lo_lv) / 0.25) + 1); vpl = v / n_levels; lv = lo_lv
        while lv <= hi_lv: vol_profile[lv] += vpl; lv += 0.25
        vpoc = max(vol_profile, key=vol_profile.get) if vol_profile else 0
        vpoc_hist.append(vpoc)
        
        stable = True
        if len(vpoc_hist) >= 20:
            recent = [p for p in vpoc_hist[-20:] if p > 0]
            if len(recent) >= 2:
                stable = (abs(recent[-1] - recent[0]) / len(recent)) <= 0.15
                
        if i < 45: continue
        target = round(vwap / TICK_SIZE) * TICK_SIZE
        z = (c - target) / std if std >= TICK_SIZE else 0
        
        temp_pnl = daily_pnl
        if pos:
            cur_ticks = (l - pos["ep"]) / TICK_SIZE if pos["dir"] == "LONG" else (pos["ep"] - h) / TICK_SIZE
            temp_pnl += (cur_ticks * TICK_VALUE) - COMMISSION_RT
        if temp_pnl < lowest_intraday_pnl: lowest_intraday_pnl = temp_pnl

        if pos:
            if pos["dir"] == "LONG":
                gut_tp = pos["ep"] + ((GUTTER_WIN - daily_pnl) / TICK_VALUE) * TICK_SIZE
                if h >= gut_tp: 
                    daily_pnl += ((gut_tp - pos["ep"]) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT
                    pos = None; break 
                gut_sl = pos["ep"] - ((-GUTTER_LOSS - daily_pnl) / TICK_VALUE) * TICK_SIZE
                if l <= gut_sl:
                    daily_pnl += ((gut_sl - pos["ep"]) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT
                    pos = None; break 
                    
                if l <= pos["sl"]: daily_pnl += ((pos["sl"] - pos["ep"]) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; continue
                if h > pos["bp"]: pos["bp"] = h
                ur = (pos["bp"] - pos["ep"]) / TICK_SIZE; cur = (c - pos["ep"]) / TICK_SIZE
                if c >= target and cur >= 12: daily_pnl += ((c - pos["ep"]) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; continue
                if ur >= 20: 
                    tl = round((pos["bp"] - 16 * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                    if tl > pos["sl"]: pos["sl"] = tl
                if ur >= 10 and pos["sl"] < pos["ep"]: pos["sl"] = pos["ep"]
                if (i - pos["bar_idx"]) > 90 and cur > -4: daily_pnl += ((c - pos["ep"]) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; continue
                
            else: 
                gut_tp = pos["ep"] - ((GUTTER_WIN - daily_pnl) / TICK_VALUE) * TICK_SIZE
                if l <= gut_tp: 
                    daily_pnl += ((pos["ep"] - gut_tp) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT
                    pos = None; break
                gut_sl = pos["ep"] + ((-GUTTER_LOSS - daily_pnl) / TICK_VALUE) * TICK_SIZE
                if h >= gut_sl:
                    daily_pnl += ((pos["ep"] - gut_sl) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT
                    pos = None; break
                    
                if h >= pos["sl"]: daily_pnl += ((pos["ep"] - pos["sl"]) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; continue
                if l < pos["bp"]: pos["bp"] = l
                ur = (pos["ep"] - pos["bp"]) / TICK_SIZE; cur = (pos["ep"] - c) / TICK_SIZE
                if c <= target and cur >= 12: daily_pnl += ((pos["ep"] - c) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; continue
                if ur >= 20: 
                    tl = round((pos["bp"] + 16 * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                    if tl < pos["sl"]: pos["sl"] = tl
                if ur >= 10 and pos["sl"] > pos["ep"]: pos["sl"] = pos["ep"]
                if (i - pos["bar_idx"]) > 90 and cur > -4: daily_pnl += ((pos["ep"] - c) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; continue

        elif stable and i <= 360 and trades < 50 and daily_pnl < GUTTER_WIN and daily_pnl > GUTTER_LOSS:
            if abs(z) >= 1.5:
                dist = abs(c - target)
                sd = max(min(dist * 0.5, 24 * TICK_SIZE), 6 * TICK_SIZE)
                if z < -1.5:
                    pos = {"dir": "LONG", "ep": c, "sl": round((c - sd) / TICK_SIZE) * TICK_SIZE, "bp": c, "bar_idx": i}
                    trades += 1
                elif z > 1.5:
                    pos = {"dir": "SHORT", "ep": c, "sl": round((c + sd) / TICK_SIZE) * TICK_SIZE, "bp": c, "bar_idx": i}
                    trades += 1

    if pos:
        c = sess.bars[-1]["c"]
        pnl = ((c - pos["ep"]) / TICK_SIZE if pos["dir"] == "LONG" else (pos["ep"] - c) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT
        daily_pnl += pnl
        if daily_pnl < lowest_intraday_pnl: lowest_intraday_pnl = daily_pnl

    return daily_pnl, lowest_intraday_pnl, trades

# ============================================================
# STRAT 2: NEW UPGRADED LIVE BOT (Rolling 20-Min VPOC)
# ============================================================
def simulate_rolling_live_bot_day(sess):
    pos = None; trades = 0; daily_pnl = 0.0
    lowest_intraday_pnl = 0.0  
    
    bar_profiles = []
    rolling_vpoc_hist = []
    cum_pv = 0; cum_v = 0; cum_p2v = 0
    
    for i, b in enumerate(sess.bars):
        h, l, c, v = b["h"], b["l"], b["c"], b["v"]
        
        tp = (h + l + c) / 3
        cum_pv += tp * v; cum_v += v; cum_p2v += (tp ** 2) * v
        vwap = cum_pv / cum_v if cum_v > 0 else 0
        var = (cum_p2v / cum_v) - (vwap ** 2) if cum_v > 0 else 0
        std = var ** 0.5 if var > 0 else 0
        
        b_prof = defaultdict(float)
        lo_lv = round(l * 4) / 4; hi_lv = round(h * 4) / 4
        n_levels = max(1, int((hi_lv - lo_lv) / 0.25) + 1); vpl = v / n_levels; lv = lo_lv
        while lv <= hi_lv:
            b_prof[lv] += vpl; lv += 0.25
            
        bar_profiles.append(b_prof)
        if len(bar_profiles) > 20: 
            bar_profiles.pop(0) 
            
        combined = defaultdict(float)
        for prof in bar_profiles:
            for price_lvl, vol in prof.items():
                combined[price_lvl] += vol
        rolling_vpoc = max(combined, key=combined.get) if combined else 0
        rolling_vpoc_hist.append(rolling_vpoc)
        
        stable = True
        if len(rolling_vpoc_hist) >= 20:
            recent = [p for p in rolling_vpoc_hist[-20:] if p > 0]
            if len(recent) >= 2:
                stable = (abs(recent[-1] - recent[0]) / len(recent)) <= 0.15
                
        if i < 45: continue
        target = round(vwap / TICK_SIZE) * TICK_SIZE
        z = (c - target) / std if std >= TICK_SIZE else 0
        
        temp_pnl = daily_pnl
        if pos:
            cur_ticks = (l - pos["ep"]) / TICK_SIZE if pos["dir"] == "LONG" else (pos["ep"] - h) / TICK_SIZE
            temp_pnl += (cur_ticks * TICK_VALUE) - COMMISSION_RT
        if temp_pnl < lowest_intraday_pnl: lowest_intraday_pnl = temp_pnl

        if pos:
            if pos["dir"] == "LONG":
                gut_tp = pos["ep"] + ((GUTTER_WIN - daily_pnl) / TICK_VALUE) * TICK_SIZE
                if h >= gut_tp: daily_pnl += ((gut_tp - pos["ep"]) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; break 
                gut_sl = pos["ep"] - ((-GUTTER_LOSS - daily_pnl) / TICK_VALUE) * TICK_SIZE
                if l <= gut_sl: daily_pnl += ((gut_sl - pos["ep"]) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; break 
                    
                if l <= pos["sl"]: daily_pnl += ((pos["sl"] - pos["ep"]) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; continue
                if h > pos["bp"]: pos["bp"] = h
                ur = (pos["bp"] - pos["ep"]) / TICK_SIZE; cur = (c - pos["ep"]) / TICK_SIZE
                if c >= target and cur >= 12: daily_pnl += ((c - pos["ep"]) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; continue
                if ur >= 20: 
                    tl = round((pos["bp"] - 16 * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                    if tl > pos["sl"]: pos["sl"] = tl
                if ur >= 10 and pos["sl"] < pos["ep"]: pos["sl"] = pos["ep"]
                if (i - pos["bar_idx"]) > 90 and cur > -4: daily_pnl += ((c - pos["ep"]) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; continue
                
            else: 
                gut_tp = pos["ep"] - ((GUTTER_WIN - daily_pnl) / TICK_VALUE) * TICK_SIZE
                if l <= gut_tp: daily_pnl += ((pos["ep"] - gut_tp) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; break
                gut_sl = pos["ep"] + ((-GUTTER_LOSS - daily_pnl) / TICK_VALUE) * TICK_SIZE
                if h >= gut_sl: daily_pnl += ((pos["ep"] - gut_sl) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; break
                    
                if h >= pos["sl"]: daily_pnl += ((pos["ep"] - pos["sl"]) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; continue
                if l < pos["bp"]: pos["bp"] = l
                ur = (pos["ep"] - pos["bp"]) / TICK_SIZE; cur = (pos["ep"] - c) / TICK_SIZE
                if c <= target and cur >= 12: daily_pnl += ((pos["ep"] - c) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; continue
                if ur >= 20: 
                    tl = round((pos["bp"] + 16 * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                    if tl < pos["sl"]: pos["sl"] = tl
                if ur >= 10 and pos["sl"] > pos["ep"]: pos["sl"] = pos["ep"]
                if (i - pos["bar_idx"]) > 90 and cur > -4: daily_pnl += ((pos["ep"] - c) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; continue

        elif stable and i <= 360 and trades < 50 and daily_pnl < GUTTER_WIN and daily_pnl > GUTTER_LOSS:
            if abs(z) >= 1.5:
                dist = abs(c - target)
                sd = max(min(dist * 0.5, 24 * TICK_SIZE), 6 * TICK_SIZE)
                if z < -1.5:
                    pos = {"dir": "LONG", "ep": c, "sl": round((c - sd) / TICK_SIZE) * TICK_SIZE, "bp": c, "bar_idx": i}
                    trades += 1
                elif z > 1.5:
                    pos = {"dir": "SHORT", "ep": c, "sl": round((c + sd) / TICK_SIZE) * TICK_SIZE, "bp": c, "bar_idx": i}
                    trades += 1

    if pos:
        c = sess.bars[-1]["c"]
        pnl = ((c - pos["ep"]) / TICK_SIZE if pos["dir"] == "LONG" else (pos["ep"] - c) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT
        daily_pnl += pnl
        if daily_pnl < lowest_intraday_pnl: lowest_intraday_pnl = daily_pnl

    return daily_pnl, lowest_intraday_pnl, trades

# ============================================================
# MONTE CARLO ENGINE (UPDATED TDD LOGIC)
# ============================================================
def run_monte_carlo(sessions, strategy_func):
    passed = 0; failed = 0
    days_to_pass = []; fail_reasons = defaultdict(int)
    
    print(f"  Pre-calculating historical days for '{strategy_func.__name__}'...")
    daily_results = [strategy_func(sess) for sess in sessions if len(sess.bars) > 100]
    
    print(f"  Running {MONTE_CARLO_ITERATIONS} combine attempts...")
    for _ in range(MONTE_CARLO_ITERATIONS):
        acct_bal = COMBINE_STARTING_BAL
        highest_eod_bal = COMBINE_STARTING_BAL
        days_traded = 0
        status = "TRADING"
        
        while status == "TRADING":
            days_traded += 1
            if days_traded > MAX_DAYS_PER_COMBINE:
                status = "FAILED_TIME"; break
                
            day_pnl, lowest_intraday, _ = random.choice(daily_results)
            
            # --- TOPSTEP TDD CALCULATION ---
            # Max loss limit trails the highest EOD balance, but NEVER exceeds the starting balance.
            raw_tdd_limit = highest_eod_bal - COMBINE_EOD_TRAILING_DD
            tdd_limit = min(raw_tdd_limit, COMBINE_STARTING_BAL)
            
            # 1. Intraday Rule Check (Did we breach the limit mid-trade?)
            if (acct_bal + lowest_intraday) <= tdd_limit:
                status = "FAILED_MAX_LOSS"; break
                
            # 2. End of Day Updates
            acct_bal += day_pnl
            if acct_bal > highest_eod_bal:
                highest_eod_bal = acct_bal
                
            # 3. Check EOD Pass/Fail
            if acct_bal >= (COMBINE_STARTING_BAL + COMBINE_PROFIT_TARGET):
                status = "PASSED"; break
                
        if status == "PASSED":
            passed += 1
            days_to_pass.append(days_traded)
        else:
            failed += 1
            fail_reasons[status] += 1

    pass_rate = (passed / MONTE_CARLO_ITERATIONS) * 100
    avg_days = np.mean(days_to_pass) if days_to_pass else 0
    
    print(f"\n{'='*40}")
    print(f" RESULTS: {strategy_func.__name__}")
    print(f"{'='*40}")
    print(f" Pass Rate:         {pass_rate:.1f}% ({passed}/{MONTE_CARLO_ITERATIONS})")
    print(f" Avg Days to Pass:  {avg_days:.1f} days")
    print(f" Failures Breakdown:")
    for reason, count in fail_reasons.items():
        print(f"   - {reason}: {count} times ({(count/MONTE_CARLO_ITERATIONS)*100:.1f}%)")

# ============================================================
# EXECUTION
# ============================================================
if __name__ == "__main__":
    files = glob.glob(os.path.join("es_sessions", "*.json"))
    all_sessions = [Session.load(os.path.basename(f).replace(".json", "")) for f in files]
    all_sessions = [s for s in all_sessions if s is not None]
    
    if len(all_sessions) < 10:
        print("Not enough historical data in 'es_sessions' to run a valid Monte Carlo.")
    else:
        print(f"Loaded {len(all_sessions)} historical sessions.")
        run_monte_carlo(all_sessions, simulate_live_bot_day)
        run_monte_carlo(all_sessions, simulate_rolling_live_bot_day)