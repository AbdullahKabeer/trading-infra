"""
Combine Backtest — Multi-day VWAP strategy backtester
======================================================
Runs the VWAP mean-reversion strategy across cached sessions
to simulate a Topstep combine.

  python combine_backtest.py                    # all cached days
  python combine_backtest.py 2026-03-10 2026-03-24  # date range
"""

import sys
from collections import defaultdict
from combine_live import (
    Session, list_sessions, fetch_history_sync, auth_sync,
    TARGET_MODE, Z_THRESH, CONTRACTS, MAX_TRADES, STOP_RATIO,
    MIN_STOP_TICKS, MAX_STOP_TICKS, TRAIL_ACTIVATE, TRAIL_DISTANCE,
    EXIT_MIN_TICKS, TIME_STOP_MINS, TICK_SIZE, TICK_VALUE,
    GUTTER_GOAL, GUTTER_DD, COMMISSION_RT,
)


def run_backtest(bars, gutter_win=False, gutter_loss=False, daily_pnl_start=0):
    """Run backtest on a single day's bars. Returns (stats_dict, trades_list)."""
    cum_pv = 0; cum_v = 0; cum_p2v = 0; vp_dict = defaultdict(float); vpoc = 0
    pos = None; trades = []; trades_today = 0; total_pnl = 0
    peak_pnl = 0; max_dd = 0
    daily_pnl = daily_pnl_start
    vpoc_hist = []
    
    # 1. INITIALIZE THE LOCKOUT STATE
    locked_side = None

    for i, b in enumerate(bars):
        h, l, c, v = b["h"], b["l"], b["c"], b["v"]
        tp = (h + l + c) / 3
        cum_pv += tp * v; cum_v += v; cum_p2v += (tp ** 2) * v
        vwap = cum_pv / cum_v if cum_v > 0 else 0
        var = (cum_p2v / cum_v) - (vwap ** 2) if cum_v > 0 else 0
        std = var ** 0.5 if var > 0 else 0

        lo_lv = round(l * 4) / 4; hi_lv = round(h * 4) / 4
        n_levels = max(1, int((hi_lv - lo_lv) / 0.25) + 1)
        vpl = v / n_levels
        lv = lo_lv
        while lv <= hi_lv:
            vp_dict[lv] += vpl; lv += 0.25
        if vp_dict: vpoc = max(vp_dict, key=vp_dict.get)
        vpoc_hist.append(vpoc)

        # Stability check
        stable = True
        if len(vpoc_hist) >= 20:
            recent = vpoc_hist[-20:]
            valid = [p for p in recent if p and p > 0]
            if len(valid) >= 2:
                migration = abs(valid[-1] - valid[0]) / len(valid)
                stable = migration <= 0.15

        if i < 15: continue
        target = vpoc if TARGET_MODE == "volume" else round(vwap / TICK_SIZE) * TICK_SIZE
        if not target: continue
        target_z = (c - target) / std if std >= TICK_SIZE else 0

        if pos:
            ticks = (c - pos["ep"]) / TICK_SIZE if pos["dir"] == "LONG" else (pos["ep"] - c) / TICK_SIZE
            epnl = ticks * TICK_VALUE * CONTRACTS
            cur_eq = total_pnl + epnl

            bt = i - pos["bar_idx"]
            if pos["dir"] == "LONG":
                if gutter_win:
                    gut_tp = pos["ep"] + ((GUTTER_GOAL - daily_pnl) / (CONTRACTS * TICK_VALUE)) * TICK_SIZE
                    if h >= gut_tp:
                        ticks = (gut_tp - pos["ep"]) / TICK_SIZE; pnl = ticks * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS
                        total_pnl += pnl; daily_pnl += pnl
                        trades.append({"action": "EXIT", "dir": "LONG", "price": gut_tp, "pnl": pnl, "bar_idx": i}); pos = None; continue
                if gutter_loss and daily_pnl > -GUTTER_DD:
                    remaining_l = (daily_pnl + GUTTER_DD) / (CONTRACTS * TICK_VALUE)
                    if remaining_l > 0:
                        gut_sl = pos["ep"] - remaining_l * TICK_SIZE
                        if l <= gut_sl:
                            ticks = (gut_sl - pos["ep"]) / TICK_SIZE; pnl = ticks * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS
                            total_pnl += pnl; daily_pnl += pnl
                            trades.append({"action": "EXIT", "dir": "LONG", "price": gut_sl, "pnl": pnl, "bar_idx": i}); pos = None; continue
                if l <= pos["sl"]:
                    ticks = (pos["sl"] - pos["ep"]) / TICK_SIZE; pnl = ticks * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS
                    total_pnl += pnl; daily_pnl += pnl
                    trades.append({"action": "EXIT", "dir": "LONG", "price": pos["sl"], "pnl": pnl, "bar_idx": i}); pos = None; continue
                if h > pos["bp"]: pos["bp"] = h
                ur = (pos["bp"] - pos["ep"]) / TICK_SIZE; cur = (c - pos["ep"]) / TICK_SIZE
                if target and c >= target and cur >= EXIT_MIN_TICKS:
                    ticks = (c - pos["ep"]) / TICK_SIZE; pnl = ticks * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS
                    total_pnl += pnl; daily_pnl += pnl
                    trades.append({"action": "EXIT", "dir": "LONG", "price": c, "pnl": pnl, "bar_idx": i}); pos = None; continue
                if ur >= TRAIL_ACTIVATE:
                    tl = round((pos["bp"] - TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                    if tl > pos["sl"]: pos["sl"] = tl
                if ur >= 10 and pos["sl"] < pos["ep"]: pos["sl"] = pos["ep"]
                if bt > TIME_STOP_MINS and cur > -4:
                    ticks = (c - pos["ep"]) / TICK_SIZE; pnl = ticks * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS
                    total_pnl += pnl; daily_pnl += pnl
                    trades.append({"action": "EXIT", "dir": "LONG", "price": c, "pnl": pnl, "bar_idx": i}); pos = None; continue
            else:  # SHORT
                if gutter_win:
                    gut_tp = pos["ep"] - ((GUTTER_GOAL - daily_pnl) / (CONTRACTS * TICK_VALUE)) * TICK_SIZE
                    if l <= gut_tp:
                        ticks = (pos["ep"] - gut_tp) / TICK_SIZE; pnl = ticks * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS
                        total_pnl += pnl; daily_pnl += pnl
                        trades.append({"action": "EXIT", "dir": "SHORT", "price": gut_tp, "pnl": pnl, "bar_idx": i}); pos = None; continue
                if gutter_loss and daily_pnl > -GUTTER_DD:
                    remaining_l = (daily_pnl + GUTTER_DD) / (CONTRACTS * TICK_VALUE)
                    if remaining_l > 0:
                        gut_sl = pos["ep"] + remaining_l * TICK_SIZE
                        if h >= gut_sl:
                            ticks = (pos["ep"] - gut_sl) / TICK_SIZE; pnl = ticks * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS
                            total_pnl += pnl; daily_pnl += pnl
                            trades.append({"action": "EXIT", "dir": "SHORT", "price": gut_sl, "pnl": pnl, "bar_idx": i}); pos = None; continue
                if h >= pos["sl"]:
                    ticks = (pos["ep"] - pos["sl"]) / TICK_SIZE; pnl = ticks * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS
                    total_pnl += pnl; daily_pnl += pnl
                    trades.append({"action": "EXIT", "dir": "SHORT", "price": pos["sl"], "pnl": pnl, "bar_idx": i}); pos = None; continue
                if l < pos["bp"]: pos["bp"] = l
                ur = (pos["ep"] - pos["bp"]) / TICK_SIZE; cur = (pos["ep"] - c) / TICK_SIZE
                if target and c <= target and cur >= EXIT_MIN_TICKS:
                    ticks = (pos["ep"] - c) / TICK_SIZE; pnl = ticks * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS
                    total_pnl += pnl; daily_pnl += pnl
                    trades.append({"action": "EXIT", "dir": "SHORT", "price": c, "pnl": pnl, "bar_idx": i}); pos = None; continue
                if ur >= TRAIL_ACTIVATE:
                    tl = round((pos["bp"] + TRAIL_DISTANCE * TICK_SIZE) / TICK_SIZE) * TICK_SIZE
                    if tl < pos["sl"]: pos["sl"] = tl
                if ur >= 10 and pos["sl"] > pos["ep"]: pos["sl"] = pos["ep"]
                if bt > TIME_STOP_MINS and cur > -4:
                    ticks = (pos["ep"] - c) / TICK_SIZE; pnl = ticks * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS
                    total_pnl += pnl; daily_pnl += pnl
                    trades.append({"action": "EXIT", "dir": "SHORT", "price": c, "pnl": pnl, "bar_idx": i}); pos = None; continue
        else:
            if i < 45 or i > 360 or trades_today >= MAX_TRADES: continue
            if gutter_win and round(daily_pnl, 2) >= GUTTER_GOAL - 100.0: continue
            if gutter_loss and round(daily_pnl, 2) <= -(GUTTER_DD - 100.0): continue
            
            # 2. THE SIGNAL RESET CHECK
            if locked_side == "LONG" and target_z > -0.5: locked_side = None
            if locked_side == "SHORT" and target_z < 0.5: locked_side = None
            
            if not stable: continue
            if abs(target_z) >= Z_THRESH:
                dist = abs(c - target)
                sd = max(min(dist * STOP_RATIO, MAX_STOP_TICKS * TICK_SIZE), MIN_STOP_TICKS * TICK_SIZE)
                
                # 3. ENFORCE THE LOCKOUT ON ENTRY
                if target_z < 0 and locked_side != "LONG":
                    sl = round((c - sd) / TICK_SIZE) * TICK_SIZE; tp = target + 4 * TICK_SIZE
                    pos = {"dir": "LONG", "ep": c, "sl": sl, "tp": tp, "bp": c, "bar_idx": i}
                    trades.append({"action": "ENTER", "dir": "LONG", "price": c, "bar_idx": i}); trades_today += 1
                    locked_side = "LONG" # Set the lock
                elif target_z > 0 and locked_side != "SHORT":
                    sl = round((c + sd) / TICK_SIZE) * TICK_SIZE; tp = target - 4 * TICK_SIZE
                    pos = {"dir": "SHORT", "ep": c, "sl": sl, "tp": tp, "bp": c, "bar_idx": i}
                    trades.append({"action": "ENTER", "dir": "SHORT", "price": c, "bar_idx": i}); trades_today += 1
                    locked_side = "SHORT" # Set the lock

        cur_eq = total_pnl
        if pos:
            cur_ticks = (c - pos["ep"]) / TICK_SIZE if pos["dir"] == "LONG" else (pos["ep"] - c) / TICK_SIZE
            cur_eq += cur_ticks * TICK_VALUE * CONTRACTS
        if cur_eq > peak_pnl: peak_pnl = cur_eq
        if peak_pnl - cur_eq > max_dd: max_dd = peak_pnl - cur_eq

    # Close any open position at EOD
    if pos:
        c = bars[-1]["c"]
        ticks = (c - pos["ep"]) / TICK_SIZE if pos["dir"] == "LONG" else (pos["ep"] - c) / TICK_SIZE
        pnl = ticks * TICK_VALUE * CONTRACTS - COMMISSION_RT * CONTRACTS
        total_pnl += pnl; daily_pnl += pnl
        trades.append({"action": "EXIT", "dir": pos["dir"], "price": c, "pnl": pnl, "bar_idx": len(bars) - 1})

    return {
        "total_pnl": total_pnl, "daily_pnl": daily_pnl, "trades": trades_today,
        "max_trades": MAX_TRADES, "max_dd": max_dd,
        "gutter_win": gutter_win, "gutter_loss": gutter_loss
    }, trades

def run_combine(dates, gutter_win=True, gutter_loss=True):
    """Run a multi-day combine simulation across given dates."""
    print("=" * 70)
    print("  COMBINE BACKTEST — VWAP Mean Reversion")
    print("=" * 70)
    print(f"  Mode: {TARGET_MODE} | Z: {Z_THRESH} | Contracts: {CONTRACTS}")
    print(f"  Gutters: Win={'ON' if gutter_win else 'OFF'} (${GUTTER_GOAL}) | Loss={'ON' if gutter_loss else 'OFF'} (${GUTTER_DD})")
    print(f"  Commission: ${COMMISSION_RT}/RT | Days: {len(dates)}")
    print("-" * 70)

    cumulative_pnl = 0
    peak_cumulative = 0
    max_trailing_dd = 0
    total_trades = 0
    winning_days = 0
    losing_days = 0
    results = []

    for d_str in dates:
        sess = Session.load(d_str)
        if not sess or len(sess.bars) < 30:
            print(f"  {d_str}: skipped (no data or < 30 bars)")
            continue

        stats, day_trades = run_backtest(sess.bars, gutter_win=gutter_win, gutter_loss=gutter_loss)
        day_pnl = stats["total_pnl"]
        cumulative_pnl += day_pnl
        total_trades += stats["trades"]

        if cumulative_pnl > peak_cumulative:
            peak_cumulative = cumulative_pnl
        trailing_dd = peak_cumulative - cumulative_pnl
        if trailing_dd > max_trailing_dd:
            max_trailing_dd = trailing_dd

        if day_pnl >= 0: winning_days += 1
        else: losing_days += 1

        indicator = "🟢" if day_pnl >= 0 else "🔴"
        results.append({
            "date": d_str, "pnl": day_pnl, "trades": stats["trades"],
            "max_dd": stats["max_dd"], "cumulative": cumulative_pnl
        })
        print(f"  {indicator} {d_str} | P&L: ${day_pnl:>8.2f} | Trades: {stats['trades']:>2} | DD: ${stats['max_dd']:>7.2f} | Cum: ${cumulative_pnl:>9.2f}")

    print("-" * 70)
    print(f"  {'RESULTS':^66}")
    print("-" * 70)
    print(f"  Total P&L:       ${cumulative_pnl:>10.2f}")
    print(f"  Total Trades:     {total_trades:>10}")
    print(f"  Winning Days:     {winning_days:>10}  ({winning_days}/{winning_days + losing_days})")
    print(f"  Losing Days:      {losing_days:>10}")
    print(f"  Max Trailing DD:  ${max_trailing_dd:>10.2f}")
    print(f"  Peak Cumulative:  ${peak_cumulative:>10.2f}")
    if total_trades > 0:
        avg_per_trade = cumulative_pnl / total_trades
        print(f"  Avg P&L/Trade:    ${avg_per_trade:>10.2f}")
    print("=" * 70)

    return results


if __name__ == "__main__":
    # Parse optional date range from CLI args
    all_dates = list_sessions()
    all_dates.reverse()  # chronological order

    if len(sys.argv) >= 3:
        start, end = sys.argv[1], sys.argv[2]
        dates = [d for d in all_dates if start <= d <= end]
    elif len(sys.argv) == 2:
        # Single date
        dates = [sys.argv[1]]
    else:
        from datetime import datetime, timedelta
        from combine_live import ET
        now = datetime.now(ET).date()
        start = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        end = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        dates = [d for d in all_dates if start <= d <= end]

    if not dates:
        print("  No cached sessions found. Run combine_live.py first to backfill data.")
        print("  Or fetch data now? Authenticating...")
        token = auth_sync()
        if token:
            from datetime import datetime, timedelta
            from combine_live import ET
            now = datetime.now(ET).date()
            for i in range(10):
                d = now - timedelta(days=i)
                if d.weekday() < 5:
                    d_str = d.strftime("%Y-%m-%d")
                    print(f"  Fetching {d_str}...", end="", flush=True)
                    s = fetch_history_sync(token, d_str)
                    if s:
                        s.save()
                        print(f" {len(s.bars)} bars")
                        dates.append(d_str)
                    else:
                        print(" no data")
            dates.sort()

    if dates:
        run_combine(dates, gutter_win=True, gutter_loss=True)
    else:
        print("  No data available to backtest.")
