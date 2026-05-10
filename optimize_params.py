import os, sys, glob, json, random, math
import pandas as pd
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
import multiprocessing

# Combine constants
TICK_SIZE = 0.25
TICK_VALUE = 12.50
MAX_DRAWDOWN = 2000
GUTTER_GOAL = 1500
GUTTER_DD = 1900
PROFIT_TARGET = 3000
COMMISSION_RT = 2.80

# Logic constraints — must match combine_live.py
try:
    from combine_live import (MAX_STOP_TICKS, MIN_STOP_TICKS, TRAIL_ACTIVATE,
                               TRAIL_DISTANCE, EXIT_MIN_TICKS, TIME_STOP_MINS,
                               ATR_STOP_RATIO, SIGMA_EXP_MAX as _SIGMA_EXP_MAX,
                               ATR_SLOPE_MAX as _ATR_SLOPE_MAX)
    SIGMA_EXP_MAX = _SIGMA_EXP_MAX if '_SIGMA_EXP_MAX' in dir() else 1.25
    ATR_SLOPE_MAX = _ATR_SLOPE_MAX if '_ATR_SLOPE_MAX' in dir() else 0.10
except (ImportError, AttributeError):
    MAX_STOP_TICKS = 12
    MIN_STOP_TICKS = 4
    TRAIL_ACTIVATE = 12
    TRAIL_DISTANCE = 4
    EXIT_MIN_TICKS = 6
    TIME_STOP_MINS = 90
    ATR_STOP_RATIO = 0.65
    SIGMA_EXP_MAX  = 1.25
    ATR_SLOPE_MAX  = 0.10

def load_all_sessions():
    import combine_live # load logic
    sessions = []
    for folder in ["es_sessions", "spy_proxy_sessions"]:
        if not os.path.exists(folder): continue
        for f in sorted(glob.glob(os.path.join(folder, "*.json"))):
            if os.path.getsize(f) < 1000: continue
            day = os.path.basename(f).replace(".json", "")
            combine_live.DATA_DIR = folder
            s = combine_live.Session.load(day)
            if s and s.bars and len(s.bars) >= 60:
                sessions.append((day, s.bars))
    return sessions

def run_session_backtest_opt(bars, z_thresh, contracts, max_trades, stop_ratio):
    cum_pv=0; cum_v=0; cum_p2v=0; vp_dict=defaultdict(float)
    vpoc_hist=[]; pos=None; trades=[]; trades_today=0
    session_pnl=0
    bt_bar_ranges=[]; bt_atr_history=[]; bt_prev_close=None

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
                if session_pnl < GUTTER_GOAL:
                    gut_tp=pos["ep"]+((GUTTER_GOAL-session_pnl)/(contracts*TICK_VALUE))*TICK_SIZE
                    if h>=gut_tp:
                        ticks=(gut_tp-pos["ep"])/TICK_SIZE; pnl=ticks*TICK_VALUE*contracts; session_pnl+=pnl; trades.append(pnl); pos=None; continue
                if session_pnl > -GUTTER_DD:
                    remaining=(session_pnl+GUTTER_DD)/(contracts*TICK_VALUE)
                    if remaining>0:
                        gut_sl=pos["ep"]-remaining*TICK_SIZE
                        if l<=gut_sl:
                            ticks=(gut_sl-pos["ep"])/TICK_SIZE; pnl=ticks*TICK_VALUE*contracts; session_pnl+=pnl; trades.append(pnl); pos=None; continue
                if l<=pos["sl"]:
                    ticks=(pos["sl"]-pos["ep"])/TICK_SIZE; pnl=ticks*TICK_VALUE*contracts; session_pnl+=pnl; trades.append(pnl); pos=None; continue
                if h>pos["bp"]: pos["bp"]=h
                ur=(pos["bp"]-pos["ep"])/TICK_SIZE; cur=(c-pos["ep"])/TICK_SIZE
                if target and c>=target and cur>=EXIT_MIN_TICKS:
                    ticks=(c-pos["ep"])/TICK_SIZE; pnl=ticks*TICK_VALUE*contracts; session_pnl+=pnl; trades.append(pnl); pos=None; continue
                if ur>=TRAIL_ACTIVATE:
                    tl=round((pos["bp"]-TRAIL_DISTANCE*TICK_SIZE)/TICK_SIZE)*TICK_SIZE
                    if tl>pos["sl"]: pos["sl"]=tl
                if ur>=10 and pos["sl"]<pos["ep"]: pos["sl"]=pos["ep"]
                if bt>TIME_STOP_MINS:
                    ticks=(c-pos["ep"])/TICK_SIZE; pnl=ticks*TICK_VALUE*contracts; session_pnl+=pnl; trades.append(pnl); pos=None; continue
            else:
                if session_pnl < GUTTER_GOAL:
                    gut_tp=pos["ep"]-((GUTTER_GOAL-session_pnl)/(contracts*TICK_VALUE))*TICK_SIZE
                    if l<=gut_tp:
                        ticks=(pos["ep"]-gut_tp)/TICK_SIZE; pnl=ticks*TICK_VALUE*contracts; session_pnl+=pnl; trades.append(pnl); pos=None; continue
                if session_pnl > -GUTTER_DD:
                    remaining=(session_pnl+GUTTER_DD)/(contracts*TICK_VALUE)
                    if remaining>0:
                        gut_sl=pos["ep"]+remaining*TICK_SIZE
                        if h>=gut_sl:
                            ticks=(pos["ep"]-gut_sl)/TICK_SIZE; pnl=ticks*TICK_VALUE*contracts; session_pnl+=pnl; trades.append(pnl); pos=None; continue
                if h>=pos["sl"]:
                    ticks=(pos["ep"]-pos["sl"])/TICK_SIZE; pnl=ticks*TICK_VALUE*contracts; session_pnl+=pnl; trades.append(pnl); pos=None; continue
                if l<pos["bp"]: pos["bp"]=l
                ur=(pos["ep"]-pos["bp"])/TICK_SIZE; cur=(pos["ep"]-c)/TICK_SIZE
                if target and c<=target and cur>=EXIT_MIN_TICKS:
                    ticks=(pos["ep"]-c)/TICK_SIZE; pnl=ticks*TICK_VALUE*contracts; session_pnl+=pnl; trades.append(pnl); pos=None; continue
                if ur>=TRAIL_ACTIVATE:
                    tl=round((pos["bp"]+TRAIL_DISTANCE*TICK_SIZE)/TICK_SIZE)*TICK_SIZE
                    if tl<pos["sl"]: pos["sl"]=tl
                if ur>=10 and pos["sl"]>pos["ep"]: pos["sl"]=pos["ep"]
                if bt>TIME_STOP_MINS:
                    ticks=(pos["ep"]-c)/TICK_SIZE; pnl=ticks*TICK_VALUE*contracts; session_pnl+=pnl; trades.append(pnl); pos=None; continue
        else:
            if i<45 or i>360 or trades_today>=max_trades: continue
            if not stable: continue
            if round(session_pnl,2)>=GUTTER_GOAL: continue 
            if round(session_pnl,2)<=-GUTTER_DD: continue 
            if abs(target_z)>=z_thresh:
                dist=abs(c-target)
                if sigma_exp>1.25: continue
                if atr_slope>0.10: continue
                sd=max(min(dist*stop_ratio,MAX_STOP_TICKS*TICK_SIZE),MIN_STOP_TICKS*TICK_SIZE)
                if target_z<0:
                    sl=round((c-sd)/TICK_SIZE)*TICK_SIZE
                    pos={"dir":"LONG","ep":c,"sl":sl,"bp":c,"bar_idx":i}
                    trades_today+=1
                    session_pnl-=COMMISSION_RT*contracts
                elif target_z>0:
                    sl=round((c+sd)/TICK_SIZE)*TICK_SIZE
                    pos={"dir":"SHORT","ep":c,"sl":sl,"bp":c,"bar_idx":i}
                    trades_today+=1
                    session_pnl-=COMMISSION_RT*contracts

    if pos:
        c=bars[-1]["c"]
        ticks=(c-pos["ep"])/TICK_SIZE if pos["dir"]=="LONG" else (pos["ep"]-c)/TICK_SIZE
        pnl=ticks*TICK_VALUE*contracts; session_pnl+=pnl; trades.append(pnl)

    net_trades = [t - COMMISSION_RT*contracts for t in trades]
    return net_trades, session_pnl

def simulate_combine(daily_results, max_days=45):
    pnl = 0.0
    peak_eod_balance = 0.0
    journey_pnls = []

    for day_idx, day_pnl in enumerate(daily_results):
        if day_idx >= max_days: break
        if day_pnl >= GUTTER_GOAL: day_pnl = GUTTER_GOAL
        if day_pnl <= -GUTTER_DD: day_pnl = -GUTTER_DD

        journey_pnls.append(day_pnl)
        pnl += day_pnl
        if pnl > peak_eod_balance: peak_eod_balance = pnl
        
        mll_floor = peak_eod_balance - MAX_DRAWDOWN
        if pnl <= mll_floor: return "BLOWN", pnl, day_idx + 1
        if pnl >= PROFIT_TARGET:
            best_day = max(journey_pnls)
            if best_day < pnl * 0.5:
                return "PASSED", pnl, day_idx + 1

    return "TIMEOUT", pnl, max_days

def evaluate_params(args):
    z_thresh, contracts, max_trades, stop_ratio, sessions_bars = args

    # Walk-forward: use first 70% of sessions for fitting, last 30% for validation
    split = max(10, int(len(sessions_bars) * 0.7))
    train_sessions = sessions_bars[:split]
    valid_sessions = sessions_bars[split:]

    daily_pnls = []
    all_trades = []
    for day, bars in train_sessions:
        trades, day_pnl = run_session_backtest_opt(bars, z_thresh, contracts, max_trades, stop_ratio)
        daily_pnls.append(day_pnl)
        all_trades.extend(trades)

    wins = [t for t in all_trades if t > 0]
    losses = [t for t in all_trades if t <= 0]
    total_trades = len(all_trades)

    # Require enough trades to avoid overfitting (at least 1 per session on average)
    if total_trades < max(30, len(train_sessions)):
        return (z_thresh, contracts, max_trades, stop_ratio, 0.0, 0.0, 0, 0, 0, total_trades)

    wr = len(wins) / total_trades if total_trades else 0
    avg_w = sum(wins) / len(wins) if wins else 0
    avg_l = abs(sum(losses) / len(losses)) if losses else 0
    rr = avg_w / avg_l if avg_l > 0 else 0
    expectancy = (wr * avg_w) - ((1 - wr) * avg_l)

    # Monte Carlo pass rate — track blow rate and days-to-pass
    N_SIMS = 500
    pass_count = 0
    blown_count = 0
    pass_days_list = []
    pnls = []

    for _ in range(N_SIMS):
        smpl = random.choices(daily_pnls, k=45)
        res, fin_pnl, d = simulate_combine(smpl)
        if res == "PASSED":
            pass_count += 1
            pass_days_list.append(d)
        elif res == "BLOWN":
            blown_count += 1
        pnls.append(fin_pnl)

    pass_rate = pass_count / N_SIMS
    blow_rate = blown_count / N_SIMS
    avg_mc_pnl = sum(pnls) / len(pnls) if pnls else 0
    avg_days_to_pass = sum(pass_days_list) / len(pass_days_list) if pass_days_list else 45.0

    # Validation pass rate (out-of-sample check — penalizes overfit)
    if valid_sessions:
        valid_pnls = [run_session_backtest_opt(bars, z_thresh, contracts, max_trades, stop_ratio)[1]
                      for _, bars in valid_sessions]
        v_pass, v_blow = 0, 0
        for _ in range(200):
            smpl = random.choices(valid_pnls, k=45)
            r, _, _ = simulate_combine(smpl)
            if r == "PASSED": v_pass += 1
            elif r == "BLOWN": v_blow += 1
        valid_pass_rate = v_pass / 200
    else:
        valid_pass_rate = pass_rate

    # Composite score: maximise pass %, minimise blow % and days-to-pass
    # Validation discount: penalise in-sample overfitting
    speed_bonus = max(0.0, (1.0 - avg_days_to_pass / 45.0)) * 20
    oos_penalty = max(0.0, pass_rate - valid_pass_rate) * 30
    score = (pass_rate * 70) + (expectancy * 0.1) - (blow_rate * 50) + speed_bonus - oos_penalty

    return (z_thresh, contracts, max_trades, stop_ratio, pass_rate, expectancy, rr, wr,
            avg_mc_pnl, total_trades, score, blow_rate, avg_days_to_pass, valid_pass_rate)

def main():
    print("Loading sessions...")
    sessions_bars = load_all_sessions()
    print(f"Loaded {len(sessions_bars)} sessions. Building grid...")
    
    z_options = [0.8, 1.0, 1.25, 1.5, 1.75, 2.0]
    c_options = [1, 2, 3]
    mt_options = [2, 3, 4, 6, 8, 10, 15, 20, 25]
    sr_options = [0.4, 0.5, 0.6]
    
    grid = []
    for z in z_options:
        for c in c_options:
            for mt in mt_options:
                for sr in sr_options:
                    grid.append((z, c, mt, sr, sessions_bars))
                    
    print(f"Executing parameter sweep over {len(grid)} combinations via multiprocessing...")
    
    results = []
    with ProcessPoolExecutor(max_workers=multiprocessing.cpu_count()) as executor:
        for res in executor.map(evaluate_params, grid):
            if res is None or len(res) < 14: continue
            if res[9] < max(30, 1):  # total_trades check
                continue
            results.append({
                "z_thresh":        res[0],
                "contracts":       res[1],
                "max_trades":      res[2],
                "stop_ratio":      res[3],
                "pass_rate":       res[4],
                "expectancy":      res[5],
                "risk_reward":     res[6],
                "win_rate":        res[7],
                "avg_mc_pnl":      res[8],
                "total_trades":    res[9],
                "score":           res[10],
                "blow_rate":       res[11],
                "avg_days_to_pass": res[12],
                "valid_pass_rate": res[13],
            })

    df = pd.DataFrame(results)
    df.sort_values(by="score", ascending=False, inplace=True)
    df.to_csv("optimization_results.csv", index=False)

    print("\n======= TOP 5 OPTIMIZED PARAMETERS =======")
    cols = ["z_thresh", "contracts", "max_trades", "stop_ratio",
            "pass_rate", "valid_pass_rate", "blow_rate", "avg_days_to_pass",
            "win_rate", "risk_reward", "expectancy", "total_trades", "score"]
    print(df[cols].head(5).to_string(index=False))
    print("\nFull data saved to optimization_results.csv")

if __name__ == "__main__":
    main()
