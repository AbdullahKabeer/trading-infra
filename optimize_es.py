"""
ES VWAP Strategy Optimizer
==========================
Self-contained grid search over key strategy parameters using the full
filter stack from combine_live.py.  Scores by Monte Carlo pass-rate +
Sortino ratio.  Leave-one-out cross-validation to detect overfitting on
the 15-session dataset.

Run:  python3 optimize_es.py
"""

import os, glob, json, math, random, itertools, time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
import multiprocessing

# ── TopStepX combine rules ──────────────────────────────────────────
TICK_SIZE   = 0.25
TICK_VALUE  = 12.50      # ES E-mini
COMMISSION  = 2.80       # per round-turn
GUTTER_GOAL = 1500
GUTTER_DD   = 1900
MAX_DD      = 2000       # MLL distance
PROFIT_TGT  = 3000
MAX_DAYS    = 45

# ── Fixed strategy constants (not swept) ────────────────────────────
MAX_TRADES      = 11
TIME_STOP_MINS  = 90
EXIT_MIN_TICKS  = 6
MIN_STOP_TICKS  = 4
MAX_STOP_TICKS  = 12
DIR_COOLDOWN_BARS = 8    # bars to block same-direction entries after a stop-out
MIN_VOL_REL     = 0.80   # volume confirmation threshold
SIGMA_EXP_MAX   = 1.25   # sigma panic filter
ATR_SLOPE_MAX   = 0.10   # ATR slope filter
LUNCH_START     = 120    # tod_mins
LUNCH_END       = 195
POWER_HOUR      = 330

# ── Parameter search space ──────────────────────────────────────────
PARAM_GRID = {
    "contracts":         [1, 2],
    "z_thresh":          [1.2, 1.5, 1.75, 2.0, 2.25, 2.5],
    "atr_stop_ratio":    [0.35, 0.50, 0.65, 0.80],
    "trail_activate":    [8,  12, 16, 20],
    "trail_distance":    [4,  6,  8,  10],
    "breakeven_ticks":   [4,  6,  8,  10],
    "vwap_slope_thresh": [0.15, 0.30, 0.50, 1.0],   # 1.0 = effectively disabled
}

N_MC_SIMS   = 1000      # Monte Carlo samples per param set
MIN_TRADES  = 20        # minimum trade count to avoid overfitting
WALK_FORWARD_SPLIT = 0.65   # fraction of sessions used for training; rest = held-out val


# ════════════════════════════════════════════════════════════════════
#  SESSION LOADER
# ════════════════════════════════════════════════════════════════════

def load_sessions(folder="es_sessions"):
    sessions = []
    for f in sorted(glob.glob(os.path.join(folder, "*.json"))):
        if os.path.getsize(f) < 1000:
            continue
        date = os.path.basename(f).replace(".json", "")
        with open(f) as fh:
            d = json.load(fh)
        bars = [b for b in d.get("bars", []) if b.get("v", 0) > 0]
        if len(bars) >= 60:
            sessions.append((date, bars))
    return sessions


# ════════════════════════════════════════════════════════════════════
#  FAST BACKTEST (full filter stack, parameterised)
# ════════════════════════════════════════════════════════════════════

def backtest_session(bars, p):
    contracts         = p["contracts"]
    z_thresh          = p["z_thresh"]
    atr_stop_ratio    = p["atr_stop_ratio"]
    trail_activate    = p["trail_activate"]
    trail_distance    = p["trail_distance"]
    breakeven_ticks   = p["breakeven_ticks"]
    vwap_slope_thresh = p["vwap_slope_thresh"]

    cum_pv = cum_v = cum_p2v = 0.0
    vp_dict   = defaultdict(float)
    vpoc_hist = []
    bt_ranges = []; bt_atr = []; prev_c = None
    vwap_hist = []
    trades_today = 0
    pos = None
    day_pnl = 0.0
    trade_log = []
    cooldown_long_until  = -999   # direction cooldown after stop-out
    cooldown_short_until = -999

    for i, b in enumerate(bars):
        h, l, c, o, v = b["h"], b["l"], b["c"], b["o"], b["v"]

        # ── VWAP / Std ──────────────────────────────────────────
        tp_v = (h + l + c) / 3
        cum_pv += tp_v * v; cum_v += v; cum_p2v += tp_v**2 * v
        vwap = cum_pv / cum_v if cum_v else 0
        var  = (cum_p2v / cum_v) - vwap**2 if cum_v else 0
        std  = math.sqrt(max(var, 0))
        vwap_hist.append(vwap)

        # ── Volume profile / VPOC ───────────────────────────────
        lo_lv = round(l * 4) / 4; hi_lv = round(h * 4) / 4
        n_lv  = max(1, int((hi_lv - lo_lv) / 0.25) + 1)
        vpl   = v / n_lv; lv = lo_lv
        while lv <= hi_lv:
            vp_dict[lv] += vpl; lv += 0.25
        vpoc = max(vp_dict, key=vp_dict.get) if vp_dict else c
        vpoc_hist.append(vpoc)

        # ── ATR ─────────────────────────────────────────────────
        tr = max(h - l,
                 abs(h - prev_c) if prev_c else h - l,
                 abs(l - prev_c) if prev_c else h - l)
        bt_ranges.append(tr); prev_c = c
        lb = bt_ranges[-20:] if len(bt_ranges) >= 20 else bt_ranges
        atr = sum(lb) / len(lb) if lb else 0
        bt_atr.append(atr)

        # ── Derived indicators ──────────────────────────────────
        atr_slope = 0.0
        if len(bt_atr) >= 10 and bt_atr[-10] > 0:
            atr_slope = (bt_atr[-1] - bt_atr[-10]) / bt_atr[-10]

        prev_std = bars[i - 5].get("_std", std) if i >= 5 else std
        sigma_exp = std / prev_std if prev_std > 0 else 1.0
        b["_std"] = std

        # Stable session (VPOC migration ≤ 0.15)
        stable = True
        if len(vpoc_hist) >= 20:
            rec = [x for x in vpoc_hist[-20:] if x and x > 0]
            if len(rec) >= 2:
                stable = abs(rec[-1] - rec[0]) / len(rec) <= 0.15

        # VWAP slope (1-bar diff)
        vwap_slope = vwap - vwap_hist[-2] if len(vwap_hist) >= 2 else 0.0

        # VWAP crossings in last 20 bars
        vh = vwap_hist[-20:]; ph = [bars[j]["c"] for j in range(max(0, i-19), i+1)]
        n = min(len(vh), len(ph))
        crossings = sum(
            1 for j in range(1, n)
            if (ph[j-1] > vh[j-1]) != (ph[j] > vh[j])
        )
        trending = crossings <= 1

        # Time-of-day (minutes since 09:30 ET)
        try:
            ts = b.get("ts", "")
            tp = ts.split(" ")[1] if " " in ts else ts
            hh, mm = int(tp[:2]), int(tp[3:5])
            tod = hh * 60 + mm - (9 * 60 + 30)
        except Exception:
            tod = 0

        target = round(vwap / TICK_SIZE) * TICK_SIZE if vwap else 0

        # ── POSITION MANAGEMENT ─────────────────────────────────
        if pos:
            _sz = pos.get("contracts_remaining", contracts)
            bt  = i - pos["bar_idx"]
            early_stop = int(TIME_STOP_MINS * 0.5)

            if pos["dir"] == "LONG":
                # Gutter TP
                if day_pnl < GUTTER_GOAL:
                    gut_tp = pos["ep"] + ((GUTTER_GOAL - day_pnl) / (_sz * TICK_VALUE)) * TICK_SIZE
                    if h >= gut_tp:
                        pnl = (gut_tp - pos["ep"]) / TICK_SIZE * TICK_VALUE * _sz
                        day_pnl += pnl; trade_log.append(pnl)
                        pos = None; continue
                # Gutter SL
                if day_pnl > -GUTTER_DD:
                    rem = (day_pnl + GUTTER_DD) / (_sz * TICK_VALUE)
                    if rem > 0:
                        gut_sl = pos["ep"] - rem * TICK_SIZE
                        if l <= gut_sl:
                            pnl = (gut_sl - pos["ep"]) / TICK_SIZE * TICK_VALUE * _sz
                            day_pnl += pnl; trade_log.append(pnl)
                            pos = None; continue
                # Stop
                if l <= pos["sl"]:
                    sl_fill = pos["sl"] - TICK_SIZE
                    pnl = (sl_fill - pos["ep"]) / TICK_SIZE * TICK_VALUE * _sz
                    day_pnl += pnl; trade_log.append(pnl)
                    # Only stop-outs (not breakeven) trigger cooldown
                    if pos["sl"] < pos["ep"]:
                        cooldown_long_until = i + DIR_COOLDOWN_BARS
                    pos = None; continue
                # Update best price
                if h > pos["bp"]: pos["bp"] = h
                ur  = (pos["bp"] - pos["ep"]) / TICK_SIZE
                cur = (c - pos["ep"]) / TICK_SIZE
                # Scale-out: exit 1 contract at VWAP, trail the remaining
                if (contracts > 1 and not pos.get("scale1_done")
                        and pos.get("tp") and h >= pos["tp"] and cur >= EXIT_MIN_TICKS):
                    so_pnl = cur * TICK_VALUE * 1
                    day_pnl += so_pnl; trade_log.append(so_pnl)
                    pos["contracts_remaining"] = _sz - 1
                    pos["scale1_done"] = True
                    if pos["sl"] < pos["ep"]: pos["sl"] = pos["ep"]
                    _sz = pos["contracts_remaining"]
                # Normal TP exit (all remaining contracts)
                elif pos.get("tp") and c >= pos["tp"] and cur >= EXIT_MIN_TICKS:
                    pnl = (c - pos["ep"]) / TICK_SIZE * TICK_VALUE * _sz
                    day_pnl += pnl; trade_log.append(pnl)
                    pos = None; continue
                # Trail
                if ur >= trail_activate:
                    tl = round((pos["bp"] - trail_distance * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                    if tl > pos["sl"]: pos["sl"] = tl
                # Breakeven
                if ur >= breakeven_ticks and pos["sl"] < pos["ep"]:
                    pos["sl"] = pos["ep"]
                # Enhanced time stop: early exit if losing, normal at full time
                if bt > TIME_STOP_MINS or (bt > early_stop and cur < -4):
                    pnl = cur * TICK_VALUE * _sz
                    day_pnl += pnl; trade_log.append(pnl)
                    pos = None; continue

            else:  # SHORT
                if day_pnl < GUTTER_GOAL:
                    gut_tp = pos["ep"] - ((GUTTER_GOAL - day_pnl) / (_sz * TICK_VALUE)) * TICK_SIZE
                    if l <= gut_tp:
                        pnl = (pos["ep"] - gut_tp) / TICK_SIZE * TICK_VALUE * _sz
                        day_pnl += pnl; trade_log.append(pnl)
                        pos = None; continue
                if day_pnl > -GUTTER_DD:
                    rem = (day_pnl + GUTTER_DD) / (_sz * TICK_VALUE)
                    if rem > 0:
                        gut_sl = pos["ep"] + rem * TICK_SIZE
                        if h >= gut_sl:
                            pnl = (pos["ep"] - gut_sl) / TICK_SIZE * TICK_VALUE * _sz
                            day_pnl += pnl; trade_log.append(pnl)
                            pos = None; continue
                if h >= pos["sl"]:
                    sl_fill = pos["sl"] + TICK_SIZE
                    pnl = (pos["ep"] - sl_fill) / TICK_SIZE * TICK_VALUE * _sz
                    day_pnl += pnl; trade_log.append(pnl)
                    if pos["sl"] > pos["ep"]:
                        cooldown_short_until = i + DIR_COOLDOWN_BARS
                    pos = None; continue
                if l < pos["bp"]: pos["bp"] = l
                ur  = (pos["ep"] - pos["bp"]) / TICK_SIZE
                cur = (pos["ep"] - c) / TICK_SIZE
                # Scale-out: exit 1 contract at VWAP, trail the remaining
                if (contracts > 1 and not pos.get("scale1_done")
                        and pos.get("tp") and l <= pos["tp"] and cur >= EXIT_MIN_TICKS):
                    so_pnl = cur * TICK_VALUE * 1
                    day_pnl += so_pnl; trade_log.append(so_pnl)
                    pos["contracts_remaining"] = _sz - 1
                    pos["scale1_done"] = True
                    if pos["sl"] > pos["ep"]: pos["sl"] = pos["ep"]
                    _sz = pos["contracts_remaining"]
                # Normal TP exit (all remaining contracts)
                elif pos.get("tp") and c <= pos["tp"] and cur >= EXIT_MIN_TICKS:
                    pnl = (pos["ep"] - c) / TICK_SIZE * TICK_VALUE * _sz
                    day_pnl += pnl; trade_log.append(pnl)
                    pos = None; continue
                if ur >= trail_activate:
                    tl = round((pos["bp"] + trail_distance * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                    if tl < pos["sl"]: pos["sl"] = tl
                if ur >= breakeven_ticks and pos["sl"] > pos["ep"]:
                    pos["sl"] = pos["ep"]
                if bt > TIME_STOP_MINS or (bt > early_stop and cur < -4):
                    pnl = cur * TICK_VALUE * _sz
                    day_pnl += pnl; trade_log.append(pnl)
                    pos = None; continue

        # ── ENTRY ────────────────────────────────────────────────
        else:
            if i < 45 or trades_today >= MAX_TRADES: continue
            if tod >= POWER_HOUR: continue
            if LUNCH_START <= tod <= LUNCH_END: continue
            if round(day_pnl, 2) >= GUTTER_GOAL: continue
            if round(day_pnl, 2) <= -GUTTER_DD: continue
            if not stable: continue
            if not target or std < TICK_SIZE: continue

            # VWAP crossings regime — raise z-thresh when trending
            eff_z = z_thresh * 1.3 if trending else z_thresh

            target_z = (c - target) / std
            z_abs    = abs(target_z)
            if z_abs < eff_z: continue

            # Direction cooldown after stop-out
            if target_z < 0 and i < cooldown_long_until: continue
            if target_z > 0 and i < cooldown_short_until: continue

            # Micro-structure filters
            if sigma_exp > SIGMA_EXP_MAX: continue
            if atr_slope > ATR_SLOPE_MAX: continue
            if target_z < 0 and vwap_slope < -vwap_slope_thresh: continue
            if target_z > 0 and vwap_slope >  vwap_slope_thresh: continue

            # Volume confirmation (approximate: use bar volume vs 5-bar avg)
            if i >= 5:
                avg_v5 = max(1.0, sum(bars[j]["v"] for j in range(i-5, i)) / 5)
                if v / avg_v5 < MIN_VOL_REL: continue

            # ATR-based stop
            if atr > 0:
                atr_ticks = max(1, int(atr / TICK_SIZE))
                sd = max(MIN_STOP_TICKS, min(MAX_STOP_TICKS, int(atr_ticks * atr_stop_ratio))) * TICK_SIZE
            else:
                sd = max(MIN_STOP_TICKS, min(MAX_STOP_TICKS, 6)) * TICK_SIZE

            # Dynamic TP
            tp_buf = max(3, int(z_abs * 2)) * TICK_SIZE

            comm = COMMISSION * contracts
            day_pnl -= comm

            if target_z < 0:   # LONG
                sl = round((c - sd) / TICK_SIZE) * TICK_SIZE
                tp = round((target + tp_buf) / TICK_SIZE) * TICK_SIZE
                pos = {"dir": "LONG",  "ep": c, "sl": sl, "tp": tp,
                       "bp": c, "bar_idx": i,
                       "contracts_remaining": contracts, "scale1_done": False}
                trades_today += 1
            else:               # SHORT
                sl = round((c + sd) / TICK_SIZE) * TICK_SIZE
                tp = round((target - tp_buf) / TICK_SIZE) * TICK_SIZE
                pos = {"dir": "SHORT", "ep": c, "sl": sl, "tp": tp,
                       "bp": c, "bar_idx": i,
                       "contracts_remaining": contracts, "scale1_done": False}
                trades_today += 1

    # Force-close at EOD
    if pos:
        _sz = pos.get("contracts_remaining", contracts)
        c   = bars[-1]["c"]
        cur = ((c - pos["ep"]) if pos["dir"] == "LONG" else (pos["ep"] - c)) / TICK_SIZE
        pnl = cur * TICK_VALUE * _sz
        day_pnl += pnl
        trade_log.append(pnl)

    return day_pnl, trade_log


# ════════════════════════════════════════════════════════════════════
#  MONTE CARLO COMBINE SIMULATOR
# ════════════════════════════════════════════════════════════════════

def simulate_combine(daily_pnls, rng):
    pnl = 0.0; peak = 0.0
    journey = []
    for day_idx in range(MAX_DAYS):
        day = rng.choice(daily_pnls)
        day = max(-GUTTER_DD, min(GUTTER_GOAL, day))
        pnl += day
        journey.append(day)
        if pnl > peak: peak = pnl
        if pnl <= peak - MAX_DD:
            return "BLOWN", day_idx + 1
        if pnl >= PROFIT_TGT:
            # Consistency rule: best single day must be < 50% of total profit
            if max(journey) < pnl * 0.5:
                return "PASSED", day_idx + 1
    return "TIMEOUT", MAX_DAYS


def monte_carlo(daily_pnls, n=N_MC_SIMS, seed=42):
    rng = random.Random(seed)
    passed = blown = 0
    pass_days = []
    for _ in range(n):
        r, d = simulate_combine(daily_pnls, rng)
        if r == "PASSED":
            passed += 1
            pass_days.append(d)
        elif r == "BLOWN":
            blown += 1
    avg_days = sum(pass_days) / len(pass_days) if pass_days else float(MAX_DAYS)
    return passed / n, blown / n, avg_days


def validate_on_sessions(params, sessions, n_sims=500, seed=77):
    """Run backtest on held-out sessions and return (pass_rate, blow_rate, avg_days, n_trades)."""
    day_pnls = []
    n_trades = 0
    for _, bars in sessions:
        day, tlog = backtest_session(bars, params)
        day_pnls.append(day)
        n_trades += len(tlog)
    if not day_pnls or n_trades < 5:
        return 0.0, 1.0, float(MAX_DAYS), n_trades
    pr, br, ad = monte_carlo(day_pnls, n=n_sims, seed=seed)
    return pr, br, ad, n_trades


# ════════════════════════════════════════════════════════════════════
#  METRICS
# ════════════════════════════════════════════════════════════════════

def sortino(trades):
    if len(trades) < 2: return 0.0
    mean = sum(trades) / len(trades)
    neg = [t for t in trades if t < 0]
    if not neg: return 10.0   # perfect — cap
    downside_dev = math.sqrt(sum(t**2 for t in neg) / len(neg))
    return mean / downside_dev if downside_dev > 0 else 0.0

def expectancy(trades):
    if not trades: return 0.0
    wins  = [t for t in trades if t > 0]
    loses = [t for t in trades if t <= 0]
    wr = len(wins) / len(trades)
    aw = sum(wins) / len(wins) if wins else 0
    al = abs(sum(loses) / len(loses)) if loses else 0
    return wr * aw - (1 - wr) * al

def profit_factor(trades):
    gross_w = sum(t for t in trades if t > 0)
    gross_l = abs(sum(t for t in trades if t < 0))
    return gross_w / gross_l if gross_l > 0 else (10.0 if gross_w > 0 else 0.0)


# ════════════════════════════════════════════════════════════════════
#  WORKER (one param set × all sessions)
# ════════════════════════════════════════════════════════════════════

def evaluate(args):
    params, sessions = args

    # Constraint pruning (avoids impossible combinations)
    if params["trail_distance"] >= params["trail_activate"]: return None
    if params["breakeven_ticks"] >= params["trail_activate"]: return None

    day_pnls   = []
    all_trades = []

    for _, bars in sessions:
        day, tlog = backtest_session(bars, params)
        day_pnls.append(day)
        all_trades.extend(tlog)

    n_trades = len(all_trades)
    if n_trades < MIN_TRADES:
        return None

    pass_rate, blow_rate, avg_days_to_pass = monte_carlo(day_pnls)
    srt   = sortino(all_trades)
    exp   = expectancy(all_trades)
    pf    = profit_factor(all_trades)
    wins  = [t for t in all_trades if t > 0]
    wr    = len(wins) / n_trades if n_trades else 0
    avg_w = sum(wins) / len(wins) if wins else 0
    loses = [t for t in all_trades if t <= 0]
    avg_l = abs(sum(loses) / len(loses)) if loses else 0
    rr    = avg_w / avg_l if avg_l > 0 else 0

    # Leave-one-out stability check — penalize params that overfit to single sessions
    loo_passes = []
    for leave_out in range(len(sessions)):
        loo_days = [p for j, p in enumerate(day_pnls) if j != leave_out]
        pr_loo, _, _ = monte_carlo(loo_days, n=200)
        loo_passes.append(pr_loo)
    loo_std = math.sqrt(sum((x - pass_rate)**2 for x in loo_passes) / len(loo_passes))

    # ── Composite score ─────────────────────────────────────────
    # PASSED is the only win. BLOWN and TIMEOUT are both failures (both cost money).
    # Primary: maximize pass_rate directly (100 pts scale).
    # Tiebreaker: prefer lower blow_rate (timeout is marginally less bad than blown,
    #             but both are failures — small coefficient so it never overrides pass_rate).
    # Anti-overfit: LOO std penalty (large, prevents cherry-picking sessions).
    timeout_rate = max(0.0, 1.0 - pass_rate - blow_rate)
    score = (pass_rate * 100) + (timeout_rate * 3) - (loo_std * 20)

    total_pnl = sum(day_pnls)

    return {
        **params,
        "pass_rate":        round(pass_rate, 4),
        "blow_rate":        round(blow_rate, 4),
        "avg_days_to_pass": round(avg_days_to_pass, 1),
        "sortino":          round(srt, 3),
        "expectancy":       round(exp, 2),
        "profit_factor":    round(pf, 3),
        "win_rate":         round(wr, 4),
        "avg_win":          round(avg_w, 2),
        "avg_loss":         round(avg_l, 2),
        "risk_reward":      round(rr, 3),
        "n_trades":         n_trades,
        "total_pnl":        round(total_pnl, 2),
        "loo_std":          round(loo_std, 4),
        "score":            round(score, 4),
    }


# ════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════

def build_grid():
    keys   = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]

def fmt(row):
    return (
        f"c={int(row['contracts'])}  z={row['z_thresh']:.2f}  atr_r={row['atr_stop_ratio']:.2f}"
        f"  trail={int(row['trail_activate'])}/{int(row['trail_distance'])}"
        f"  be={int(row['breakeven_ticks'])}"
        f"  slope={row['vwap_slope_thresh']:.2f}"
        f"  | pass={row['pass_rate']:.1%}  blow={row['blow_rate']:.1%}"
        f"  days={row['avg_days_to_pass']:.0f}"
        f"  sortino={row['sortino']:.2f}  pf={row['profit_factor']:.2f}"
        f"  wr={row['win_rate']:.1%}  rr={row['risk_reward']:.2f}"
        f"  n={row['n_trades']}  pnl=${row['total_pnl']:.0f}"
        f"  loo_std={row['loo_std']:.3f}  score={row['score']:.2f}"
    )

def main():
    print("=" * 70)
    print("  ES VWAP STRATEGY OPTIMIZER  (walk-forward)")
    print("=" * 70)

    all_sessions = load_sessions()
    if not all_sessions:
        print("ERROR: No sessions found in es_sessions/"); return

    # ── Walk-forward split ───────────────────────────────────────────
    n_total  = len(all_sessions)
    n_train  = max(20, int(n_total * WALK_FORWARD_SPLIT))
    n_val    = n_total - n_train
    train_sessions = all_sessions[:n_train]
    val_sessions   = all_sessions[n_train:]

    print(f"  Total sessions : {n_total}  |  {sum(len(b) for _, b in all_sessions)} bars")
    print(f"  Train          : {n_train} sessions  ({train_sessions[0][0]} → {train_sessions[-1][0]})")
    if val_sessions:
        print(f"  Val (held-out) : {n_val} sessions  ({val_sessions[0][0]} → {val_sessions[-1][0]})")
    else:
        print("  Val            : none (all sessions in train)")

    grid = build_grid()
    grid = [p for p in grid
            if p["trail_distance"] < p["trail_activate"]
            and p["breakeven_ticks"] < p["trail_activate"]]
    print(f"  Grid           : {len(grid)} valid combinations")
    print(f"  Workers        : {multiprocessing.cpu_count()} cores")
    print(f"  MC sims/set    : {N_MC_SIMS}  |  LOO folds: {n_train}")
    print()

    args = [(p, train_sessions) for p in grid]
    t0   = time.time()

    results = []
    cpus = multiprocessing.cpu_count()
    with ProcessPoolExecutor(max_workers=cpus) as ex:
        for i, res in enumerate(ex.map(evaluate, args, chunksize=4)):
            if res is not None:
                results.append(res)
            if (i + 1) % 50 == 0:
                pct = (i + 1) / len(grid) * 100
                elapsed = time.time() - t0
                eta = elapsed / (i + 1) * (len(grid) - i - 1)
                print(f"  {pct:5.1f}%  {i+1}/{len(grid)} combos  "
                      f"elapsed {elapsed:.0f}s  ETA {eta:.0f}s  "
                      f"valid {len(results)}", end="\r")

    elapsed = time.time() - t0
    print(f"\n\n  Done in {elapsed:.1f}s  |  {len(results)} valid results\n")

    results.sort(key=lambda x: x["score"], reverse=True)

    # ── Walk-forward validation on held-out sessions ─────────────────
    top_n = min(20, len(results))
    if val_sessions:
        print(f"  Validating top {top_n} param sets on held-out val sessions...")
        for r in results[:top_n]:
            vpr, vbr, vd, vn = validate_on_sessions(r, val_sessions)
            r["val_pass_rate"] = round(vpr, 4)
            r["val_blow_rate"] = round(vbr, 4)
            r["val_avg_days"]  = round(vd, 1)
            r["val_n_trades"]  = vn
        print()

        # Re-rank top results by average of train + val pass rates
        for r in results[:top_n]:
            r["wf_score"] = (r["pass_rate"] + r.get("val_pass_rate", 0)) / 2
        results[:top_n] = sorted(results[:top_n], key=lambda x: x["wf_score"], reverse=True)
    else:
        for r in results[:top_n]:
            r["val_pass_rate"] = r["pass_rate"]
            r["val_blow_rate"] = r["blow_rate"]
            r["val_avg_days"]  = r["avg_days_to_pass"]
            r["val_n_trades"]  = r["n_trades"]
            r["wf_score"]      = r["score"]

    # ── Output ───────────────────────────────────────────────────────
    print("=" * 70)
    print("  TOP 15  (sorted by avg train+val pass rate)")
    print("  TRAIN col = in-sample  |  VAL col = held-out (honest estimate)")
    print("=" * 70)
    for i, r in enumerate(results[:15]):
        val_str = (f"  val_pass={r['val_pass_rate']:.1%}  val_blow={r['val_blow_rate']:.1%}"
                   f"  val_days={r.get('val_avg_days', 0):.0f}"
                   f"  val_n={r.get('val_n_trades', 0)}")
        print(f"  #{i+1:02d}  {fmt(r)}")
        print(f"        {val_str}")

    best = results[0]
    print()
    print("=" * 70)
    print("  RECOMMENDED PARAMETERS  (best walk-forward score)")
    print("=" * 70)
    print(f"  CONTRACTS           = {int(best['contracts'])}")
    print(f"  Z_THRESH            = {best['z_thresh']}")
    print(f"  ATR_STOP_RATIO      = {best['atr_stop_ratio']}")
    print(f"  TRAIL_ACTIVATE      = {int(best['trail_activate'])}")
    print(f"  TRAIL_DISTANCE      = {int(best['trail_distance'])}")
    print(f"  BREAKEVEN_TICKS     = {int(best['breakeven_ticks'])}")
    print(f"  VWAP_SLOPE_THRESH   = {best['vwap_slope_thresh']}")
    print()
    print(f"  Train pass rate : {best['pass_rate']:.1%}  (in-sample,  {n_train} sessions)")
    if val_sessions:
        print(f"  Val   pass rate : {best['val_pass_rate']:.1%}  (out-of-sample, {n_val} sessions)  ← trust this one")
        gap = best["pass_rate"] - best["val_pass_rate"]
        if gap > 0.15:
            print(f"  ⚠  Large train/val gap ({gap:.1%}) — overfit signal. Consider simpler params.")
        elif gap > 0.08:
            print(f"  ~  Moderate train/val gap ({gap:.1%}) — some overfit, val estimate is realistic.")
        else:
            print(f"  ✓  Small train/val gap ({gap:.1%}) — params generalise well.")
    print(f"  Blow rate       : {best['blow_rate']:.1%} train  /  {best.get('val_blow_rate', 0):.1%} val")
    print(f"  Sortino         : {best['sortino']:.3f}")
    print(f"  Win rate        : {best['win_rate']:.1%}  |  R:R {best['risk_reward']:.2f}")
    print(f"  Avg W/L         : ${best['avg_win']:.0f} / ${best['avg_loss']:.0f}")
    print(f"  LOO σ           : {best['loo_std']:.4f}")
    print()

    # Save full results
    try:
        import csv
        if not results: return
        with open("optimization_results_es.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=results[0].keys())
            w.writeheader(); w.writerows(results)
        print("  Full results → optimization_results_es.csv")
    except Exception as e:
        print(f"  CSV save failed: {e}")

    # Auto-apply best params to combine_live.py
    apply = input("\n  Apply best params to combine_live.py? [y/N] ").strip().lower()
    if apply == "y":
        apply_to_live(best)

def apply_to_live(best):
    path = "combine_live.py"
    with open(path) as f:
        src = f.read()

    replacements = [
        ("CONTRACTS = ",      f"CONTRACTS = {int(best['contracts'])}"),
        ("Z_THRESH = ",       f"Z_THRESH = {best['z_thresh']}"),
        ("ATR_STOP_RATIO = ", f"ATR_STOP_RATIO = {best['atr_stop_ratio']}"),
        ("TRAIL_ACTIVATE = ", f"TRAIL_ACTIVATE = {int(best['trail_activate'])}"),
        ("TRAIL_DISTANCE = ", f"TRAIL_DISTANCE = {int(best['trail_distance'])}"),
        ("BREAKEVEN_TICKS = ", f"BREAKEVEN_TICKS = {int(best['breakeven_ticks'])}"),
        ("VWAP_SLOPE_THRESH = ", f"VWAP_SLOPE_THRESH = {best['vwap_slope_thresh']}"),
    ]

    lines_out = []
    for line in src.splitlines():
        replaced = False
        for prefix, new_val in replacements:
            # Match lines that START with the constant name (not inside strings/comments)
            stripped = line.lstrip()
            if stripped.startswith(prefix) and not stripped.startswith("#"):
                # Preserve inline comment if present
                comment = ""
                if "#" in line:
                    comment = "  " + line[line.index("#"):]
                lines_out.append(new_val + comment)
                replaced = True
                break
        if not replaced:
            lines_out.append(line)

    with open(path, "w") as f:
        f.write("\n".join(lines_out))

    # Syntax check
    import ast
    try:
        ast.parse("\n".join(lines_out))
        print(f"  ✓ Applied to {path} — syntax OK")
    except SyntaxError as e:
        print(f"  ✗ Syntax error after apply: {e}")

if __name__ == "__main__":
    main()
