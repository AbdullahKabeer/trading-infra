"""
Monte Carlo Combine Simulator (v3 - Defensive Scalper + Circuit Breaker)
========================================================================
Tests the fully optimized VWAP strategy with the Signal Reset lockouts.
Accurately models Topstep 50K account rules and locked TDD.
"""

import os, glob, random
import numpy as np
from collections import defaultdict
from combine_live import Session, TICK_SIZE, TICK_VALUE, COMMISSION_RT

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
# DEFENSIVE SCALPER PARAMETERS (Optimized)
# ============================================================
Z_THRESH = 1.5
STOP_RATIO = 0.4        # Tighter leash
EXIT_MIN_TICKS = 8      # Takes 2 points of profit instead of 3
TRAIL_ACTIVATE = 16     # Trails at 4 points of profit instead of 5
TRAIL_DISTANCE = 16

# ============================================================
# SIMULATOR (WITH SIGNAL RESET CIRCUIT BREAKER)
# ============================================================
def simulate_defensive_bot_day(sess):
    pos = None; trades = 0; daily_pnl = 0.0
    lowest_intraday_pnl = 0.0  
    
    vpoc_hist = []
    cum_pv = 0; cum_v = 0; cum_p2v = 0
    vol_profile = defaultdict(float)
    
    # 1. INITIALIZE THE LOCKOUT
    locked_side = None
    
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
        
        # Track Intraday Drawdown mid-bar
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
                if c >= target and cur >= EXIT_MIN_TICKS: daily_pnl += ((c - pos["ep"]) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; continue
                if ur >= TRAIL_ACTIVATE: 
                    tl = round((pos["bp"] - TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
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
                if c <= target and cur >= EXIT_MIN_TICKS: daily_pnl += ((pos["ep"] - c) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; continue
                if ur >= TRAIL_ACTIVATE: 
                    tl = round((pos["bp"] + TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                    if tl < pos["sl"]: pos["sl"] = tl
                if ur >= 10 and pos["sl"] > pos["ep"]: pos["sl"] = pos["ep"]
                if (i - pos["bar_idx"]) > 90 and cur > -4: daily_pnl += ((pos["ep"] - c) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT; pos = None; continue

        elif i <= 360 and trades < 50 and daily_pnl < GUTTER_WIN and daily_pnl > GUTTER_LOSS:
            
            # 2. SIGNAL RESET CHECK
            if locked_side == "LONG" and z > -0.5: locked_side = None
            if locked_side == "SHORT" and z < 0.5: locked_side = None
            
            if stable and abs(z) >= Z_THRESH:
                dist = abs(c - target)
                sd = max(min(dist * STOP_RATIO, 24 * TICK_SIZE), 6 * TICK_SIZE)
                
                # 3. ENFORCE LOCKOUT ON ENTRY
                if z <= -Z_THRESH and locked_side != "LONG":
                    pos = {"dir": "LONG", "ep": c, "sl": round((c - sd) / TICK_SIZE) * TICK_SIZE, "bp": c, "bar_idx": i}
                    trades += 1
                    locked_side = "LONG" # Lock it out
                elif z >= Z_THRESH and locked_side != "SHORT":
                    pos = {"dir": "SHORT", "ep": c, "sl": round((c + sd) / TICK_SIZE) * TICK_SIZE, "bp": c, "bar_idx": i}
                    trades += 1
                    locked_side = "SHORT" # Lock it out

    # Force close EOD
    if pos:
        c = sess.bars[-1]["c"]
        pnl = ((c - pos["ep"]) / TICK_SIZE if pos["dir"] == "LONG" else (pos["ep"] - c) / TICK_SIZE) * TICK_VALUE - COMMISSION_RT
        daily_pnl += pnl
        if daily_pnl < lowest_intraday_pnl: lowest_intraday_pnl = daily_pnl

    return daily_pnl, lowest_intraday_pnl, trades

# ============================================================
# MONTE CARLO ENGINE (CORRECT TOPSTEP TDD LOGIC)
# ============================================================
def run_monte_carlo(sessions, strategy_func):
    passed = 0; failed = 0
    days_to_pass = []; fail_reasons = defaultdict(int)
    
    print(f"  Pre-calculating historical days for '{strategy_func.__name__}'...")
    daily_results = [strategy_func(sess) for sess in sessions if len(sess.bars) > 100]
    
    # Filter out days where the bot simply found 0 trades
    valid_days = [d for d in daily_results if d[2] > 0]
    if not valid_days: 
        print("No valid trades found in dataset."); return
    
    print(f"  Running {MONTE_CARLO_ITERATIONS} randomized Combine attempts...")
    for _ in range(MONTE_CARLO_ITERATIONS):
        acct_bal = COMBINE_STARTING_BAL
        highest_eod_bal = COMBINE_STARTING_BAL
        days_traded = 0
        status = "TRADING"
        
        while status == "TRADING":
            days_traded += 1
            if days_traded > MAX_DAYS_PER_COMBINE:
                status = "FAILED_TIME"; break
                
            day_pnl, lowest_intraday, _ = random.choice(valid_days)
            
            # --- CORRECT TOPSTEP TDD CALCULATION ---
            # Limit trails highest EOD balance, but NEVER exceeds starting balance.
            raw_tdd_limit = highest_eod_bal - COMBINE_EOD_TRAILING_DD
            tdd_limit = min(raw_tdd_limit, COMBINE_STARTING_BAL)
            
            # 1. Intraday Rule Check (Did it breach mid-trade?)
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
    
    print(f"\n{'='*45}")
    print(f" RESULTS: Defensive Scalper + Circuit Breaker")
    print(f"{'='*45}")
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
        run_monte_carlo(all_sessions, simulate_defensive_bot_day)