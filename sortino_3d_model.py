#!/usr/bin/env python3
import argparse
import copy
import csv
import itertools
import json
import math
import statistics
import sys
from datetime import datetime

import combine_live as cl


def frange(start, stop, step):
    values = []
    x = float(start)
    stop = float(stop)
    step = float(step)
    if step <= 0:
        raise ValueError("step must be > 0")
    while x <= stop + 1e-12:
        values.append(round(x, 6))
        x += step
    return values


def sortino_ratio(returns, target=0.0):
    if not returns:
        return 0.0
    excess = [r - target for r in returns]
    downside = [min(0.0, r - target) for r in returns]
    downside_sq_mean = sum(x * x for x in downside) / len(downside)
    downside_dev = math.sqrt(downside_sq_mean)
    if downside_dev == 0:
        return float("inf") if statistics.fmean(excess) > 0 else 0.0
    return statistics.fmean(excess) / downside_dev


def load_sessions(limit_days=None):
    all_dates = cl.list_sessions()
    if limit_days is not None and limit_days > 0:
        all_dates = all_dates[:limit_days]

    sessions = []
    for ds in all_dates:
        s = cl.Session.load(ds)
        if not s or len(s.bars) <= 15:
            continue
        sessions.append((ds, copy.deepcopy(s.bars)))
    return sessions


def evaluate_combo(sessions, stop_ratio, z_thresh, max_trades):
    prev = (cl.STOP_RATIO, cl.Z_THRESH, cl.MAX_TRADES)
    cl.STOP_RATIO = float(stop_ratio)
    cl.Z_THRESH = float(z_thresh)
    cl.MAX_TRADES = int(max_trades)

    try:
        day_pnls = []
        exit_pnls = []
        trade_count = 0
        max_dd_worst = 0.0

        for _, bars in sessions:
            state, trades = cl.run_backtest(copy.deepcopy(bars), gutter_win=True, gutter_loss=True, use_ai=False)
            day_pnls.append(float(state.get("daily_pnl", 0.0)))
            trade_count += int(state.get("trades", 0) or 0)
            max_dd_worst = max(max_dd_worst, float(state.get("max_dd", 0.0) or 0.0))
            for t in trades:
                if t.get("action") == "EXIT" and isinstance(t.get("pnl"), (int, float)):
                    exit_pnls.append(float(t["pnl"]))

        if not day_pnls:
            return {
                "stop_ratio": stop_ratio,
                "z_thresh": z_thresh,
                "max_trades": max_trades,
                "sortino": 0.0,
                "total_pnl": 0.0,
                "avg_day_pnl": 0.0,
                "trades": 0,
                "max_dd_worst": 0.0,
                "exit_count": 0,
            }

        return {
            "stop_ratio": float(stop_ratio),
            "z_thresh": float(z_thresh),
            "max_trades": int(max_trades),
            "sortino": float(sortino_ratio(day_pnls, target=0.0)),
            "total_pnl": float(sum(day_pnls)),
            "avg_day_pnl": float(statistics.fmean(day_pnls)),
            "trades": int(trade_count),
            "max_dd_worst": float(max_dd_worst),
            "exit_count": int(len(exit_pnls)),
        }
    finally:
        cl.STOP_RATIO, cl.Z_THRESH, cl.MAX_TRADES = prev


def make_3d_plot(rows, out_png):
    try:
        import matplotlib.pyplot as plt
        from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
    except Exception:
        print("[warn] matplotlib not available; skipping PNG plot")
        return False

    x = [r["stop_ratio"] for r in rows]
    y = [r["z_thresh"] for r in rows]
    z = [r["max_trades"] for r in rows]
    c = [r["sortino"] if math.isfinite(r["sortino"]) else 999 for r in rows]

    fig = plt.figure(figsize=(11, 7))
    ax = fig.add_subplot(111, projection="3d")
    sc = ax.scatter(x, y, z, c=c, cmap="viridis", s=35, alpha=0.9)
    ax.set_xlabel("STOP_RATIO")
    ax.set_ylabel("Z_THRESH")
    ax.set_zlabel("MAX_TRADES")
    ax.set_title("3D Parameter Model (color = Sortino)")
    cb = fig.colorbar(sc, ax=ax, shrink=0.8, pad=0.1)
    cb.set_label("Sortino")

    best = max(rows, key=lambda r: (r["sortino"], r["total_pnl"]))
    ax.scatter([best["stop_ratio"]], [best["z_thresh"]], [best["max_trades"]],
               c="red", s=90, marker="*", label="Best")
    ax.legend(loc="upper left")

    plt.tight_layout()
    plt.savefig(out_png, dpi=160)
    plt.close(fig)
    return True


def main():
    parser = argparse.ArgumentParser(description="Sweep STOP_RATIO, Z_THRESH, MAX_TRADES and model Sortino in 3D.")
    parser.add_argument("--ratio-min", type=float, default=0.20)
    parser.add_argument("--ratio-max", type=float, default=1.20)
    parser.add_argument("--ratio-step", type=float, default=0.10)
    parser.add_argument("--z-min", type=float, default=0.50)
    parser.add_argument("--z-max", type=float, default=3.00)
    parser.add_argument("--z-step", type=float, default=0.10)
    parser.add_argument("--max-trades-min", type=int, default=1)
    parser.add_argument("--max-trades-max", type=int, default=10)
    parser.add_argument("--days", type=int, default=0, help="0 = all saved sessions")
    parser.add_argument("--out-prefix", default="sortino_3d")
    args = parser.parse_args()

    limit_days = None if args.days == 0 else args.days
    sessions = load_sessions(limit_days=limit_days)
    if not sessions:
        print("No sessions found in es_sessions/")
        sys.exit(1)

    ratio_values = frange(args.ratio_min, args.ratio_max, args.ratio_step)
    z_values = frange(args.z_min, args.z_max, args.z_step)
    max_trade_values = list(range(args.max_trades_min, args.max_trades_max + 1))

    sys.optimize_mode = True
    rows = []
    total = len(ratio_values) * len(z_values) * len(max_trade_values)
    i = 0

    for ratio, zt, mt in itertools.product(ratio_values, z_values, max_trade_values):
        i += 1
        row = evaluate_combo(sessions, ratio, zt, mt)
        rows.append(row)
        if i % 25 == 0 or i == total:
            print(f"[{i}/{total}] ratio={ratio:.2f} z={zt:.2f} maxTrades={mt} sortino={row['sortino']:.4f}")

    rows.sort(key=lambda r: (r["sortino"], r["total_pnl"]), reverse=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = f"{args.out_prefix}_{ts}.csv"
    json_path = f"{args.out_prefix}_{ts}.json"
    png_path = f"{args.out_prefix}_{ts}.png"

    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["stop_ratio", "z_thresh", "max_trades", "sortino", "total_pnl", "avg_day_pnl", "trades", "max_dd_worst", "exit_count"])
        w.writeheader()
        for r in rows:
            out = dict(r)
            if not math.isfinite(out["sortino"]):
                out["sortino"] = 1e9
            w.writerow(out)

    with open(json_path, "w") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "sessions_used": [d for d, _ in sessions],
            "count": len(rows),
            "top10": rows[:10],
            "all": rows,
        }, f, indent=2)

    plotted = make_3d_plot(rows, png_path)

    print("\nTop 10 by Sortino:")
    for k, r in enumerate(rows[:10], start=1):
        s = r["sortino"]
        s_txt = "inf" if not math.isfinite(s) else f"{s:.4f}"
        print(f"{k:2d}. ratio={r['stop_ratio']:.2f} z={r['z_thresh']:.2f} maxTrades={r['max_trades']:2d} | sortino={s_txt} pnl={r['total_pnl']:.2f}")

    print(f"\nSaved: {csv_path}")
    print(f"Saved: {json_path}")
    if plotted:
        print(f"Saved: {png_path}")
    else:
        print("PNG not generated (matplotlib missing)")


if __name__ == "__main__":
    main()
