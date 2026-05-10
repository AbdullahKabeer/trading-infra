import combine_live, sim_combine
import random

bars_list = sim_combine.load_all_sessions()
all_trades = []
for day, bars in bars_list:
    trades, _ = sim_combine.run_session_backtest(bars)
    all_trades.extend(trades)

print(f"Total trades: {len(all_trades)}")

def mt_trades():
    max_steps = 45 * 20
    pass_n = 0
    rng = random.Random(42)
    for _ in range(1000):
        pnl = 0.0
        peak = 0.0
        current_day_pnl = 0.0
        daily_pnls = []
        for step in range(1, max_steps + 1):
            trade = rng.choice(all_trades)
            if current_day_pnl + trade < -1900:
                trade = -1900 - current_day_pnl
            elif current_day_pnl + trade > 1500:
                trade = 1500 - current_day_pnl
            pnl += trade
            current_day_pnl += trade
            peak = max(peak, pnl)
            if pnl <= peak - 2000:
                break
            if pnl >= 3000:
                best = max(daily_pnls + [current_day_pnl])
                if best < pnl * 0.5:
                    pass_n += 1
                    break
            if step % 20 == 0:
                daily_pnls.append(current_day_pnl)
                current_day_pnl = 0.0
    print(f"Trade MC Pass Rate: {pass_n/1000*100:.1f}%")

mt_trades()
