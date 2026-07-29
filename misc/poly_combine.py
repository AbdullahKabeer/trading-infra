"""
Polygon 50K Combine Simulator (Sniper Architecture)
====================================================================
Fetches 6 months of SPY data from Polygon (scaled to match ES).
Runs a chronological Combine backtest using the Live Bot's exact
Signal Reset (Circuit Breaker) and Defensive Scalper parameters.

USAGE: python polygon_combine_test.py --polygon YOUR_API_KEY
"""

import argparse, time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, time as dtime
from collections import defaultdict

# ============================================================
# CONSTANTS & PARAMETERS
# ============================================================
COMBINE_STARTING_BAL = 50000
COMBINE_PROFIT_TARGET = 3000
COMBINE_EOD_TRAILING_DD = 2000  

GUTTER_WIN = 1500
GUTTER_LOSS = -1900  

TICK_SIZE = 0.25
TICK_VALUE = 12.50
COMMISSION_RT = 2.80
CONTRACTS = 2

# THE "SNIPER" LIVE BOT PARAMETERS
Z_THRESH = 1.8
STOP_RATIO = 0.4
EXIT_MIN_TICKS = 8
TRAIL_ACTIVATE = 16
TRAIL_DISTANCE = 16
TIME_STOP_MINS = 90
MAX_TRADES = 50

# ============================================================
# POLYGON DATA FETCHER
# ============================================================
def fetch_polygon_data(api_key, months=6):
    import requests
    print(f"\n  [DATA] Fetching SPY from Polygon.io ({months} months)...")
    all_data = []
    end = datetime.now()
    cur = end - timedelta(days=months*30)
    
    while cur < end:
        ce = min(cur + timedelta(days=14), end)
        url = f"https://api.polygon.io/v2/aggs/ticker/SPY/range/1/minute/{cur.strftime('%Y-%m-%d')}/{ce.strftime('%Y-%m-%d')}?adjusted=true&sort=asc&limit=50000&apiKey={api_key}"
        try:
            r = requests.get(url, timeout=30).json()
            if r.get("resultsCount", 0) > 0:
                d = pd.DataFrame(r["results"])
                d["datetime"] = pd.to_datetime(d["t"], unit="ms")
                d = d.rename(columns={"o":"open", "h":"high", "l":"low", "c":"close", "v":"volume"})
                all_data.append(d[["datetime", "open", "high", "low", "close", "volume"]])
                print(f"    Loaded: {len(d)} bars ({cur.date()} to {ce.date()})")
        except Exception as e: 
            print(f"    Error fetching: {e}")
        cur = ce
        time.sleep(12) # Respect Polygon free tier limits (5 calls/min)
        
    if not all_data: raise ValueError("No data returned from Polygon.")
    df = pd.concat(all_data).sort_values("datetime").reset_index(drop=True)
    return df.drop_duplicates(subset="datetime")

def build_sessions(df):
    df["datetime"] = pd.to_datetime(df["datetime"])
    
    # Scale SPY to ES ($500 SPY -> $5000 ES)
    avg_price = df["close"].mean()
    if avg_price < 1000:
        print(f"  [DATA] SPY detected (avg ${avg_price:.0f}). Scaling prices 10x to match ES points.")
        for col in ["open", "high", "low", "close"]: 
            df[col] = df[col] * 10
            
    if df["datetime"].dt.tz is None:
        df["datetime"] = df["datetime"].dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="NaT")
    else:
        df["datetime"] = df["datetime"].dt.tz_convert("America/New_York")
        
    df = df.dropna(subset=["datetime"])
    df["time"] = df["datetime"].dt.time
    df["date"] = df["datetime"].dt.date
    
    # Filter for RTH (9:30 AM to 4:00 PM ET)
    df = df[(df["time"] >= dtime(9, 30)) & (df["time"] <= dtime(15, 59))]
    
    sessions = []
    for date, group in df.groupby("date"):
        if len(group) < 30: continue
        
        # Round prices to ES ticks
        s = group.copy().reset_index(drop=True)
        for col in ["open", "high", "low", "close"]:
            s[col] = (s[col] / TICK_SIZE).round() * TICK_SIZE
            
        sessions.append(s)
        
    print(f"  [DATA] Successfully built {len(sessions)} full RTH trading sessions.")
    return sessions

# ============================================================
# CHRONOLOGICAL COMBINE SIMULATOR (WITH CIRCUIT BREAKER)
# ============================================================
def simulate_combine(sessions):
    print(f"\n{'='*60}")
    print(f"  STARTING 50K COMBINE SIMULATION (Chronological)")
    print(f"{'='*60}")
    
    acct_bal = COMBINE_STARTING_BAL
    highest_eod_bal = COMBINE_STARTING_BAL
    
    total_trades = 0
    passed = False
    blown = False
    blown_reason = ""
    
    for day_idx, sess in enumerate(sessions):
        if passed or blown: break
            
        pos = None
        trades_today = 0
        daily_pnl = 0.0
        lowest_intraday_pnl = 0.0  
        
        locked_side = None # THE CIRCUIT BREAKER
        
        cum_pv = 0; cum_v = 0; cum_p2v = 0
        vpoc_hist = []
        vol_profile = defaultdict(float)
        
        # Calculate max loss limit for today (Locks at 50K)
        raw_tdd_limit = highest_eod_bal - COMBINE_EOD_TRAILING_DD
        tdd_limit = min(raw_tdd_limit, COMBINE_STARTING_BAL)
        
        for i, row in sess.iterrows():
            h, l, c, v = row["high"], row["low"], row["close"], row["volume"]
            
            # VWAP & Std Dev
            tp = (h + l + c) / 3
            cum_pv += tp * v; cum_v += v; cum_p2v += (tp ** 2) * v
            vwap = cum_pv / cum_v if cum_v > 0 else 0
            var = (cum_p2v / cum_v) - (vwap ** 2) if cum_v > 0 else 0
            std = var ** 0.5 if var > 0 else 0
            
            # VPOC
            lo_lv = round(l * 4) / 4; hi_lv = round(h * 4) / 4
            n_levels = max(1, int((hi_lv - lo_lv) / 0.25) + 1); vpl = v / n_levels; lv = lo_lv
            while lv <= hi_lv: vol_profile[lv] += vpl; lv += 0.25
            vpoc = max(vol_profile, key=vol_profile.get) if vol_profile else 0
            vpoc_hist.append(vpoc)
            
            # Stability Check
            stable = True
            if len(vpoc_hist) >= 20:
                recent = [p for p in vpoc_hist[-20:] if p > 0]
                if len(recent) >= 2: stable = (abs(recent[-1] - recent[0]) / len(recent)) <= 0.15
                    
            if i < 45: continue
            target = round(vwap / TICK_SIZE) * TICK_SIZE
            z = (c - target) / std if std >= TICK_SIZE else 0
            
            # Intraday Drawdown Check (Combine Killer)
            temp_pnl = daily_pnl
            if pos:
                cur_ticks = (l - pos["ep"]) / TICK_SIZE if pos["dir"] == "LONG" else (pos["ep"] - h) / TICK_SIZE
                temp_pnl += (cur_ticks * TICK_VALUE * CONTRACTS) - (COMMISSION_RT * CONTRACTS)
            if temp_pnl < lowest_intraday_pnl: lowest_intraday_pnl = temp_pnl
                
            if (acct_bal + temp_pnl) <= tdd_limit:
                blown = True; blown_reason = f"Hit Trailing DD mid-trade (Limit: ${tdd_limit:,.2f})"
                break

            # Trade Management
            if pos:
                if pos["dir"] == "LONG":
                    gut_tp = pos["ep"] + ((GUTTER_WIN - daily_pnl) / (TICK_VALUE * CONTRACTS)) * TICK_SIZE
                    if h >= gut_tp: daily_pnl += ((gut_tp - pos["ep"]) / TICK_SIZE) * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS; pos = None; continue
                    gut_sl = pos["ep"] - ((-GUTTER_LOSS - daily_pnl) / (TICK_VALUE * CONTRACTS)) * TICK_SIZE
                    if l <= gut_sl: daily_pnl += ((gut_sl - pos["ep"]) / TICK_SIZE) * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS; pos = None; continue
                        
                    if l <= pos["sl"]: daily_pnl += ((pos["sl"] - pos["ep"]) / TICK_SIZE) * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS; pos = None; continue
                    if h > pos["bp"]: pos["bp"] = h
                    
                    ur = (pos["bp"] - pos["ep"]) / TICK_SIZE; cur = (c - pos["ep"]) / TICK_SIZE
                    if c >= target and cur >= EXIT_MIN_TICKS: daily_pnl += ((c - pos["ep"]) / TICK_SIZE) * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS; pos = None; continue
                    if ur >= TRAIL_ACTIVATE: 
                        tl = round((pos["bp"] - TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                        if tl > pos["sl"]: pos["sl"] = tl
                    if ur >= 10 and pos["sl"] < pos["ep"]: pos["sl"] = pos["ep"]
                    if (i - pos["bar_idx"]) > TIME_STOP_MINS and cur > -4: daily_pnl += ((c - pos["ep"]) / TICK_SIZE) * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS; pos = None; continue
                    
                else: # SHORT
                    gut_tp = pos["ep"] - ((GUTTER_WIN - daily_pnl) / (TICK_VALUE * CONTRACTS)) * TICK_SIZE
                    if l <= gut_tp: daily_pnl += ((pos["ep"] - gut_tp) / TICK_SIZE) * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS; pos = None; continue
                    gut_sl = pos["ep"] + ((-GUTTER_LOSS - daily_pnl) / (TICK_VALUE * CONTRACTS)) * TICK_SIZE
                    if h >= gut_sl: daily_pnl += ((pos["ep"] - gut_sl) / TICK_SIZE) * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS; pos = None; continue
                        
                    if h >= pos["sl"]: daily_pnl += ((pos["ep"] - pos["sl"]) / TICK_SIZE) * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS; pos = None; continue
                    if l < pos["bp"]: pos["bp"] = l
                    
                    ur = (pos["ep"] - pos["bp"]) / TICK_SIZE; cur = (pos["ep"] - c) / TICK_SIZE
                    if c <= target and cur >= EXIT_MIN_TICKS: daily_pnl += ((pos["ep"] - c) / TICK_SIZE) * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS; pos = None; continue
                    if ur >= TRAIL_ACTIVATE: 
                        tl = round((pos["bp"] + TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                        if tl < pos["sl"]: pos["sl"] = tl
                    if ur >= 10 and pos["sl"] > pos["ep"]: pos["sl"] = pos["ep"]
                    if (i - pos["bar_idx"]) > TIME_STOP_MINS and cur > -4: daily_pnl += ((pos["ep"] - c) / TICK_SIZE) * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS; pos = None; continue

            # Entry Logic
            elif i <= 360 and trades_today < MAX_TRADES and daily_pnl < GUTTER_WIN and daily_pnl > GUTTER_LOSS:
                
                # SIGNAL RESET UNLOCK
                if locked_side == "LONG" and z > -0.5: locked_side = None
                if locked_side == "SHORT" and z < 0.5: locked_side = None
                
                if stable and abs(z) >= Z_THRESH:
                    dist = abs(c - target)
                    sd = max(min(dist * STOP_RATIO, 24 * TICK_SIZE), 6 * TICK_SIZE)
                    
                    # LOCKOUT ENFORCEMENT
                    if z <= -Z_THRESH and locked_side != "LONG":
                        pos = {"dir": "LONG", "ep": c, "sl": round((c - sd) / TICK_SIZE) * TICK_SIZE, "bp": c, "bar_idx": i}
                        trades_today += 1; total_trades += 1
                        locked_side = "LONG" 
                    elif z >= Z_THRESH and locked_side != "SHORT":
                        pos = {"dir": "SHORT", "ep": c, "sl": round((c + sd) / TICK_SIZE) * TICK_SIZE, "bp": c, "bar_idx": i}
                        trades_today += 1; total_trades += 1
                        locked_side = "SHORT"

        if blown: break

        # Force close EOD
        if pos:
            c = sess.iloc[-1]["close"]
            pnl = ((c - pos["ep"]) / TICK_SIZE if pos["dir"] == "LONG" else (pos["ep"] - c) / TICK_SIZE) * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS
            daily_pnl += pnl

        # End of Day Accounting
        acct_bal += daily_pnl
        if acct_bal > highest_eod_bal:
            highest_eod_bal = acct_bal
            
        date_str = sess.iloc[0]["date"].strftime("%Y-%m-%d")
        print(f"  Day {day_idx+1:<3} ({date_str}): {'🟢' if daily_pnl > 0 else '🔴'} P&L: ${daily_pnl:>7.2f} | Trades: {trades_today:>2} | Balance: ${acct_bal:,.2f}")

        # Win Condition
        if acct_bal >= (COMBINE_STARTING_BAL + COMBINE_PROFIT_TARGET):
            passed = True
            break

    # Results
    print(f"\n{'='*60}")
    print(f"  COMBINE RESULTS")
    print(f"{'='*60}")
    if passed:
        print(f"  ✅ PASSED IN {day_idx+1} TRADING DAYS!")
        print(f"  Final Balance:  ${acct_bal:,.2f}")
    elif blown:
        print(f"  ❌ BLOWN ACCOUNT on Day {day_idx+1}")
        print(f"  Reason: {blown_reason}")
    else:
        print(f"  ⏳ TIME RAN OUT (6 Months Limit)")
        print(f"  Final Balance:  ${acct_bal:,.2f}")
        
    print(f"  Total Trades Taken: {total_trades}")

# ============================================================
# EXECUTION
# ============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--polygon", type=str, required=True, help="Your Polygon.io API key")
    args = parser.parse_args()
    
    try:
        raw_df = fetch_polygon_data(args.polygon, months=6)
        sessions = build_sessions(raw_df)
        simulate_combine(sessions)
    except Exception as e:
        print(f"\n  [ERROR] Execution failed: {e}")