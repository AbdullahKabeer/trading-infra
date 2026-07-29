"""
TOPSTEP COMBINE PASS PROBABILITY — MONTE CARLO
=================================================
Uses your actual 153 sessions to simulate Topstep Combine attempts.

Rules simulated:
  - Profit Target: $3,000
  - Trailing Drawdown: $2,000 from equity high
  - Max 45 sessions per attempt (realistic eval window)

Runs 1,000 Monte Carlo simulations.

USAGE: python sim_combine.py
  (requires es_sessions/ and/or spy_proxy_sessions/)
"""

import os, sys, glob, json, random
from collections import defaultdict

# Block ML
sys.modules['joblib'] = type(sys)('fake')
import combine_live

# Import params from bot config for single source of truth
try:
    from combine_live import (TRAILING_MLL_DISTANCE, ACCOUNT_START_BALANCE, 
                              GUTTER_GOAL, GUTTER_DD, PROFIT_TARGET, COMMISSION_RT,
                              TICK_SIZE, TICK_VALUE, CONTRACTS)
    MAX_DRAWDOWN = TRAILING_MLL_DISTANCE  # $2,000 trailing from peak EOD
except ImportError:
    MAX_DRAWDOWN = 2000
    GUTTER_GOAL = 1500
    GUTTER_DD = 1900
    ACCOUNT_START_BALANCE = 50000
    PROFIT_TARGET = 3000
    COMMISSION_RT = 2.80
    TICK_SIZE = 0.25
    TICK_VALUE = 12.50
    CONTRACTS = 1
    PROFIT_TARGET = 3000
    COMMISSION_RT = 2.80

def run_session_backtest(bars):
    """Run one session, return list of trade PnLs and session total.
    Uses the same parameters as combine_live for consistency."""
    cum_pv=0; cum_v=0; cum_p2v=0; vp_dict=defaultdict(float)
    vpoc_hist=[]; pos=None; trades=[]; trades_today=0
    session_pnl=0
    bt_bar_ranges=[]; bt_atr_history=[]; bt_prev_close=None

    # Import params from combine_live so sim always matches live bot
    try:
        from combine_live import (Z_THRESH, STOP_RATIO, MAX_STOP_TICKS, MIN_STOP_TICKS,
                                   TRAIL_ACTIVATE, TRAIL_DISTANCE, EXIT_MIN_TICKS,
                                   TIME_STOP_MINS, MAX_TRADES as _MT, TARGET_MODE)
    except ImportError:
        Z_THRESH=0.8; STOP_RATIO=0.5; MAX_STOP_TICKS=24; MIN_STOP_TICKS=6
        TRAIL_ACTIVATE=20; TRAIL_DISTANCE=16; EXIT_MIN_TICKS=12
        TIME_STOP_MINS=90; _MT=4; TARGET_MODE="vwap"
    else:
        _MT = min(_MT, 11)  # cap for sim

    for i in range(1, len(bars)):
        b=bars[i]; c=b["c"]; h=b["h"]; l=b["l"]; o=b["o"]; v=b["v"]

        tp_v=(h+l+c)/3
        cum_pv+=tp_v*v; cum_v+=v; cum_p2v+=(tp_v**2)*v
        vwap=cum_pv/cum_v if cum_v>0 else 0
        var=(cum_p2v/cum_v)-(vwap**2) if cum_v>0 else 0
        std=var**0.5 if var>0 else 0
        b["std"]=std

        prev_std=bars[i-5].get("std",std) if i>=5 else std
        sigma_exp=std/prev_std if prev_std>0 else 1.0

        # ATR tracking
        tr=max(h-l, abs(h-bt_prev_close) if bt_prev_close else h-l, abs(l-bt_prev_close) if bt_prev_close else h-l)
        bt_bar_ranges.append(tr); bt_prev_close=c
        lb=bt_bar_ranges[-20:] if len(bt_bar_ranges)>=20 else bt_bar_ranges
        bt_atr_history.append(sum(lb)/len(lb) if lb else 0)
        atr_slope=0
        if len(bt_atr_history)>=10 and bt_atr_history[-10]>0:
            atr_slope=(bt_atr_history[-1]-bt_atr_history[-10])/bt_atr_history[-10]

        lo_lv=round(l*4)/4; hi_lv=round(h*4)/4
        n_levels=max(1,int((hi_lv-lo_lv)/0.25)+1)
        vpl=v/n_levels; lv=lo_lv
        while lv<=hi_lv: vp_dict[lv]+=vpl; lv+=0.25
        vpoc=max(vp_dict,key=vp_dict.get) if vp_dict else 0
        vpoc_hist.append(vpoc)

        stable=True
        if len(vpoc_hist)>=20:
            recent_p=vpoc_hist[-20:]
            valid=[p for p in recent_p if p and p>0]
            if len(valid)>=2:
                migration=abs(valid[-1]-valid[0])/len(valid)
                stable=migration<=0.15

        if i<15: continue
        target=round(vwap/TICK_SIZE)*TICK_SIZE
        if not target: continue
        target_z=(c-target)/std if std>=TICK_SIZE else 0

        if pos:
            bt=i-pos["bar_idx"]
            if pos["dir"]=="LONG":
                # Gutter win: cap daily profit
                if session_pnl < GUTTER_GOAL:
                    gut_tp=pos["ep"]+((GUTTER_GOAL-session_pnl)/(CONTRACTS*TICK_VALUE))*TICK_SIZE
                    if h>=gut_tp:
                        ticks=(gut_tp-pos["ep"])/TICK_SIZE; pnl=ticks*TICK_VALUE*CONTRACTS
                        session_pnl+=pnl; trades.append(pnl); pos=None; continue
                # Gutter loss: cap daily loss
                if session_pnl > -GUTTER_DD:
                    remaining=(session_pnl+GUTTER_DD)/(CONTRACTS*TICK_VALUE)
                    if remaining>0:
                        gut_sl=pos["ep"]-remaining*TICK_SIZE
                        if l<=gut_sl:
                            ticks=(gut_sl-pos["ep"])/TICK_SIZE; pnl=ticks*TICK_VALUE*CONTRACTS
                            session_pnl+=pnl; trades.append(pnl); pos=None; continue
                if l<=pos["sl"]:
                    ticks=(pos["sl"]-pos["ep"])/TICK_SIZE; pnl=ticks*TICK_VALUE*CONTRACTS
                    session_pnl+=pnl; trades.append(pnl); pos=None; continue
                if h>pos["bp"]: pos["bp"]=h
                ur=(pos["bp"]-pos["ep"])/TICK_SIZE; cur=(c-pos["ep"])/TICK_SIZE
                if target and c>=target and cur>=EXIT_MIN_TICKS:
                    ticks=(c-pos["ep"])/TICK_SIZE; pnl=ticks*TICK_VALUE*CONTRACTS
                    session_pnl+=pnl; trades.append(pnl); pos=None; continue
                if ur>=TRAIL_ACTIVATE:
                    tl=round((pos["bp"]-TRAIL_DISTANCE*TICK_SIZE)/TICK_SIZE)*TICK_SIZE
                    if tl>pos["sl"]: pos["sl"]=tl
                if ur>=10 and pos["sl"]<pos["ep"]: pos["sl"]=pos["ep"]
                # Time stop — unconditional (matches combine_live)
                if bt>TIME_STOP_MINS:
                    ticks=(c-pos["ep"])/TICK_SIZE; pnl=ticks*TICK_VALUE*CONTRACTS
                    session_pnl+=pnl; trades.append(pnl); pos=None; continue
            else:
                # Gutter win: cap daily profit
                if session_pnl < GUTTER_GOAL:
                    gut_tp=pos["ep"]-((GUTTER_GOAL-session_pnl)/(CONTRACTS*TICK_VALUE))*TICK_SIZE
                    if l<=gut_tp:
                        ticks=(pos["ep"]-gut_tp)/TICK_SIZE; pnl=ticks*TICK_VALUE*CONTRACTS
                        session_pnl+=pnl; trades.append(pnl); pos=None; continue
                # Gutter loss: cap daily loss
                if session_pnl > -GUTTER_DD:
                    remaining=(session_pnl+GUTTER_DD)/(CONTRACTS*TICK_VALUE)
                    if remaining>0:
                        gut_sl=pos["ep"]+remaining*TICK_SIZE
                        if h>=gut_sl:
                            ticks=(pos["ep"]-gut_sl)/TICK_SIZE; pnl=ticks*TICK_VALUE*CONTRACTS
                            session_pnl+=pnl; trades.append(pnl); pos=None; continue
                if h>=pos["sl"]:
                    ticks=(pos["ep"]-pos["sl"])/TICK_SIZE; pnl=ticks*TICK_VALUE*CONTRACTS
                    session_pnl+=pnl; trades.append(pnl); pos=None; continue
                if l<pos["bp"]: pos["bp"]=l
                ur=(pos["ep"]-pos["bp"])/TICK_SIZE; cur=(pos["ep"]-c)/TICK_SIZE
                if target and c<=target and cur>=EXIT_MIN_TICKS:
                    ticks=(pos["ep"]-c)/TICK_SIZE; pnl=ticks*TICK_VALUE*CONTRACTS
                    session_pnl+=pnl; trades.append(pnl); pos=None; continue
                if ur>=TRAIL_ACTIVATE:
                    tl=round((pos["bp"]+TRAIL_DISTANCE*TICK_SIZE)/TICK_SIZE)*TICK_SIZE
                    if tl<pos["sl"]: pos["sl"]=tl
                if ur>=10 and pos["sl"]>pos["ep"]: pos["sl"]=pos["ep"]
                # Time stop — unconditional (matches combine_live)
                if bt>TIME_STOP_MINS:
                    ticks=(pos["ep"]-c)/TICK_SIZE; pnl=ticks*TICK_VALUE*CONTRACTS
                    session_pnl+=pnl; trades.append(pnl); pos=None; continue
        else:
            if i<45 or i>360 or trades_today>=_MT: continue
            if not stable: continue
            if round(session_pnl,2)>=GUTTER_GOAL: continue   # daily profit cap hit
            if round(session_pnl,2)<=-GUTTER_DD: continue    # daily loss cap hit
            if abs(target_z)>=Z_THRESH:
                dist=abs(c-target)
                if sigma_exp>1.25: continue
                if atr_slope>0.10: continue
                sd=max(min(dist*STOP_RATIO,MAX_STOP_TICKS*TICK_SIZE),MIN_STOP_TICKS*TICK_SIZE)
                if target_z<0:
                    sl=round((c-sd)/TICK_SIZE)*TICK_SIZE
                    pos={"dir":"LONG","ep":c,"sl":sl,"bp":c,"bar_idx":i}
                    trades_today+=1
                    session_pnl-=COMMISSION_RT*CONTRACTS
                elif target_z>0:
                    sl=round((c+sd)/TICK_SIZE)*TICK_SIZE
                    pos={"dir":"SHORT","ep":c,"sl":sl,"bp":c,"bar_idx":i}
                    trades_today+=1
                    session_pnl-=COMMISSION_RT*CONTRACTS

    if pos:
        c=bars[-1]["c"]
        ticks=(c-pos["ep"])/TICK_SIZE if pos["dir"]=="LONG" else (pos["ep"]-c)/TICK_SIZE
        pnl=ticks*TICK_VALUE*CONTRACTS; session_pnl+=pnl; trades.append(pnl)

    net_trades = [t - COMMISSION_RT*CONTRACTS for t in trades]
    return net_trades, session_pnl


def simulate_combine(daily_results, max_days=45):
    """Simulate one Topstep Combine attempt.
    
    MLL Rule: Continuous trailing limit of $2,000 below highest EOD balance.
    The floor only moves UP (as EOD balance makes new highs), never down.
    If account balance touches or drops below the floor, the account is blown.
    """
    pnl = 0.0
    peak_eod_balance = 0.0  # tracks highest end-of-day P/L (relative to start)
    journey_pnls = []

    for day_idx, day_pnl in enumerate(daily_results):
        if day_idx >= max_days:
            break

        # Cap daily P&L to match gutter limits
        if day_pnl >= GUTTER_GOAL:
            day_pnl = GUTTER_GOAL  # daily profit cap
        if day_pnl <= -GUTTER_DD:
            day_pnl = -GUTTER_DD   # daily loss cap

        journey_pnls.append(day_pnl)
        pnl += day_pnl

        # Update trailing MLL at EOD: floor = peak EOD balance - $2,000
        if pnl > peak_eod_balance:
            peak_eod_balance = pnl
        
        mll_floor = peak_eod_balance - MAX_DRAWDOWN

        # Check if blown (balance hit or dropped below MLL floor)
        if pnl <= mll_floor:
            return "BLOWN", pnl, day_idx + 1

        # Check if passed (Subject to 50% Consistency Rule)
        if pnl >= PROFIT_TARGET:
            best_day = max(journey_pnls)
            if best_day < pnl * 0.5:
                return "PASSED", pnl, day_idx + 1

    return "TIMEOUT", pnl, len(daily_results)


def load_all_sessions():
    sessions = []
    try:
        from combine_live import DATA_DIR
        folders = [DATA_DIR]
    except ImportError:
        folders = ["es_sessions", "spy_proxy_sessions"]
    
    for folder in folders:
        if not os.path.exists(folder): continue
        for f in sorted(glob.glob(os.path.join(folder, "*.json"))):
            if os.path.getsize(f) < 1000: continue
            day = os.path.basename(f).replace(".json", "")
            combine_live.DATA_DIR = folder
            s = combine_live.Session.load(day)
            if s and s.bars and len(s.bars) >= 60:
                sessions.append((day, s.bars))
    return sessions


def main():
    # Resolve Z_THRESH for display
    try:
        from combine_live import Z_THRESH as _z
    except ImportError:
        _z = 0.8
    print("=" * 70)
    print("  TOPSTEP 50K COMBINE — MONTE CARLO SIMULATION")
    print(f"  Strategy: VWAP Reversion | z={_z} | Sigma Panic | Trailing MLL ${MAX_DRAWDOWN}")
    print("=" * 70)

    sessions = load_all_sessions()
    print(f"\n  Loaded {len(sessions)} sessions")

    if not sessions:
        print("  No sessions found!")
        return

    # Pre-compute daily P&L for each session
    print("  Pre-computing daily P&L for all sessions...", end="", flush=True)
    daily_pnls = []
    all_trades = []
    for day, bars in sessions:
        trades, day_pnl = run_session_backtest(bars)
        daily_pnls.append({"day": day, "pnl": day_pnl, "trades": len(trades)})
        all_trades.extend(trades)
    print(f" done ({len(daily_pnls)} days)")

    # Stats
    pnls_only = [d["pnl"] for d in daily_pnls]
    win_days = [p for p in pnls_only if p > 0]
    loss_days = [p for p in pnls_only if p <= 0]
    flat_days = [p for p in pnls_only if p == 0]

    print(f"\n  --- SESSION STATS ---")
    print(f"  Win days:  {len(win_days)} ({len(win_days)/len(pnls_only)*100:.0f}%)")
    print(f"  Loss days: {len(loss_days)} ({len(loss_days)/len(pnls_only)*100:.0f}%)")
    print(f"  Flat days: {len(flat_days)}")
    print(f"  Avg day:   ${sum(pnls_only)/len(pnls_only):.2f}")
    print(f"  Best day:  ${max(pnls_only):.2f}")
    print(f"  Worst day: ${min(pnls_only):.2f}")
    print(f"  Total trades: {len(all_trades)}")
    
    wins = [t for t in all_trades if t > 0]
    losses = [t for t in all_trades if t <= 0]
    wr = len(wins)/len(all_trades) if all_trades else 0
    avg_w = sum(wins)/len(wins) if wins else 0
    avg_l = abs(sum(losses)/len(losses)) if losses else 0
    print(f"  WR: {wr*100:.1f}% | Avg Win: ${avg_w:.2f} | Avg Loss: ${avg_l:.2f} | R:R: {avg_w/avg_l:.2f}" if avg_l > 0 else "")

    # Monte Carlo
    N_SIMS = 1000
    MAX_DAYS = 45  # ~2 months of trading days

    print(f"\n  Running {N_SIMS} Monte Carlo simulations...")
    print(f"  Rules: ${PROFIT_TARGET} target | ${MAX_DRAWDOWN} trailing DD | {MAX_DAYS} day max")

    results = {"PASSED": 0, "BLOWN": 0, "TIMEOUT": 0}
    pass_days = []
    pass_pnls = []
    blow_days = []
    blow_pnls = []
    all_final_pnls = []

    random.seed(42)
    for _ in range(N_SIMS):
        # Random sample of days (with replacement)
        sample_indices = [random.randint(0, len(pnls_only) - 1) for _ in range(MAX_DAYS)]
        sample_pnls = [pnls_only[i] for i in sample_indices]
        
        outcome, final_pnl, days_used = simulate_combine(sample_pnls, MAX_DAYS)
        results[outcome] += 1
        all_final_pnls.append(final_pnl)
        
        if outcome == "PASSED":
            pass_days.append(days_used)
            pass_pnls.append(final_pnl)
        elif outcome == "BLOWN":
            blow_days.append(days_used)
            blow_pnls.append(final_pnl)

    pass_rate = results["PASSED"] / N_SIMS
    blow_rate = results["BLOWN"] / N_SIMS
    timeout_rate = results["TIMEOUT"] / N_SIMS

    print(f"\n{'='*70}")
    print(f"  RESULTS ({N_SIMS} simulations)")
    print(f"{'='*70}")
    print(f"\n  PASSED:  {results['PASSED']:>4} ({pass_rate*100:.1f}%)")
    print(f"  BLOWN:   {results['BLOWN']:>4} ({blow_rate*100:.1f}%)")
    print(f"  TIMEOUT: {results['TIMEOUT']:>4} ({timeout_rate*100:.1f}%)")

    if pass_days:
        print(f"\n  --- WHEN IT PASSES ---")
        print(f"  Avg days to pass:  {sum(pass_days)/len(pass_days):.0f}")
        print(f"  Fastest pass:      {min(pass_days)} days")
        print(f"  Slowest pass:      {max(pass_days)} days")
        print(f"  Avg P&L at pass:   ${sum(pass_pnls)/len(pass_pnls):.0f}")

    if blow_days:
        print(f"\n  --- WHEN IT BLOWS ---")
        print(f"  Avg days to blow:  {sum(blow_days)/len(blow_days):.0f}")
        print(f"  Fastest blow:      {min(blow_days)} days")
        print(f"  Avg P&L at blow:   ${sum(blow_pnls)/len(blow_pnls):.0f}")

    # Multi-account scaling
    print(f"\n{'='*70}")
    print(f"  MULTI-ACCOUNT SCALING")
    print(f"{'='*70}")
    
    for n in [1, 2, 3, 5]:
        prob_at_least_one = 1 - (1 - pass_rate) ** n
        expected_passes = n * pass_rate
        monthly_cost = n * 49 + 14.50  # combines + API
        activation_cost = expected_passes * 149
        total_cost = monthly_cost + activation_cost
        
        # Conservative: assume $1500 first payout (50% of $3K balance, 90% split)
        expected_revenue = expected_passes * 1350  
        net = expected_revenue - total_cost
        
        print(f"\n  {n} account(s):")
        print(f"    P(≥1 pass/month):  {prob_at_least_one*100:.0f}%")
        print(f"    E[passes/month]:   {expected_passes:.1f}")
        print(f"    Monthly cost:      ${total_cost:.0f}")
        print(f"    Expected revenue:  ${expected_revenue:.0f}")
        print(f"    Net/month:         ${net:.0f}")

    # EV analysis
    print(f"\n{'='*70}")
    print(f"  EXPECTED VALUE PER ATTEMPT")
    print(f"{'='*70}")
    
    avg_months = sum(pass_days) / len(pass_days) / 21 if pass_days else 2
    cost_per_attempt = avg_months * 49 + 14.50 * avg_months  # sub + API
    activation = 149
    first_payout = 1350  # conservative
    
    ev = pass_rate * (first_payout - activation) - (1 - pass_rate) * cost_per_attempt
    print(f"\n  Pass rate:           {pass_rate*100:.1f}%")
    print(f"  Avg months to pass:  {avg_months:.1f}")
    print(f"  Cost if pass:        ${avg_months*49 + activation + avg_months*14.50:.0f} (subs + activation + API)")
    print(f"  Revenue if pass:     ${first_payout:.0f} (first payout, conservative)")
    print(f"  Cost if fail:        ${cost_per_attempt:.0f} (subs + API)")
    print(f"  EV per attempt:      ${ev:.0f}")
    print(f"  Breakeven pass rate: {cost_per_attempt/(first_payout - activation + cost_per_attempt)*100:.1f}%")

    print(f"\n{'='*70}")
    print(f"  DONE")
    print(f"{'='*70}")

if __name__ == "__main__":
    main()