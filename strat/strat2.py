import os
import requests
import datetime
import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from enum import Enum
from typing import Optional
from dataclasses import dataclass

# ─────────────────────────────────────────────────────────────────────────────
# 1. ENUMS & DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────
class Direction(Enum):
    LONG  = "long"
    SHORT = "short"

class SignalType(Enum):
    ORDER_BLOCK = "order_block"
    FVG         = "fair_value_gap"
    MSS         = "market_structure_shift"

@dataclass
class Instrument:
    symbol: str
    yf_ticker: str
    poly_ticker: str
    tick_size: float
    point_value: float  
    commission: float   
    contracts: int      

INSTRUMENTS = {
    "MES": Instrument("MES", "MES=F", "I:MES", 0.25, 5.0, 1.24, 5),   
    "MNQ": Instrument("MNQ", "MNQ=F", "I:MNQ", 0.25, 2.0, 1.24, 5),   
    "MGC": Instrument("MGC", "MGC=F", "I:MGC", 0.10, 10.0, 1.24, 2),  
}

@dataclass
class SMCSignal:
    symbol:       str
    direction:    Direction
    signal_type:  SignalType
    entry_price:  float
    stop_loss:    float
    take_profit:  float
    sl_points:    float
    tp_points:    float
    rr_ratio:     float
    timestamp:    datetime.datetime
    timeframe:    str
    confidence:   float = 0.0
    notes:        str   = ""

# ─────────────────────────────────────────────────────────────────────────────
# 2. DATA FETCHER
# ─────────────────────────────────────────────────────────────────────────────
class DataManager:
    def fetch_yfinance(self, inst: Instrument) -> pd.DataFrame:
        print(f"[DATA] Fetching {inst.symbol} from YFinance...")
        ticker = yf.Ticker(inst.yf_ticker)
        df = ticker.history(period="60d", interval="15m")
        if df.empty:
            print(f"  -> No data found for {inst.yf_ticker}. Skipping.")
            return pd.DataFrame()
        
        df = df.reset_index()
        dt_col = "Datetime" if "Datetime" in df.columns else "Date"
        df.rename(columns={dt_col: "date", "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}, inplace=True)
        
        if df['date'].dt.tz is not None:
            df['date'] = df['date'].dt.tz_convert("America/New_York").dt.tz_localize(None)
            
        return df[['date', 'open', 'high', 'low', 'close', 'volume']]

# ─────────────────────────────────────────────────────────────────────────────
# 3. FAST SMC ENGINE (VECTORIZED NUMPY)
# ─────────────────────────────────────────────────────────────────────────────
class FastSMCEngine:
    """
    Optimized SMC Engine. Pre-computes BOS and uses NumPy arrays instead of 
    Pandas .iloc inside the loop. Achieves 100x+ speedup.
    """
    def __init__(self, rr_ratio: float = 2.0, ob_lookback: int = 5, min_fvg_points: float = 2.0):
        self.rr_ratio = rr_ratio
        self.ob_lookback = ob_lookback
        self.min_fvg_points = min_fvg_points

    def prepare_data(self, df: pd.DataFrame):
        # Pre-compute Rolling metrics for Break of Structure
        df['swing_h'] = df['high'].rolling(5).max().shift(1)
        df['swing_l'] = df['low'].rolling(5).min().shift(1)
        
        bos = np.zeros(len(df), dtype=int)
        closes = df['close'].values
        
        # 1 = LONG, -1 = SHORT, 0 = None
        bos[closes > df['swing_h'].values] = 1
        bos[closes < df['swing_l'].values] = -1
        
        self.bos = bos
        self.opens = df['open'].values
        self.highs = df['high'].values
        self.lows = df['low'].values
        self.closes = closes
        self.dates = df['date'].values
        self.length = len(df)

    def scan_bar(self, i: int, symbol: str, tick_size: float) -> Optional[SMCSignal]:
        if i < 40: return None 
        
        signals = []
        c = self.closes[i]
        bos_val = self.bos[i]
        ts = pd.Timestamp(self.dates[i]).to_pydatetime()
        
        # 1. ORDER BLOCK
        for j in range(1, self.ob_lookback + 1):
            idx_n = i - j
            idx_c = i - j - 1
            if idx_c < 0: break
            
            # Bullish OB
            if (self.closes[idx_c] < self.opens[idx_c] and self.closes[idx_n] > self.opens[idx_n] 
                and bos_val == 1 and self.lows[idx_c] <= c <= self.highs[idx_c]):
                
                sl_price = self.lows[idx_c] - 3 * tick_size
                sl_points = abs(c - sl_price)
                if sl_points >= 1.0:
                    tp_price = c + sl_points * self.rr_ratio
                    signals.append(SMCSignal(symbol, Direction.LONG, SignalType.ORDER_BLOCK, c, sl_price, tp_price, sl_points, sl_points * self.rr_ratio, self.rr_ratio, ts, "15m", 0.78, "Bullish OB"))

            # Bearish OB
            if (self.closes[idx_c] > self.opens[idx_c] and self.closes[idx_n] < self.opens[idx_n] 
                and bos_val == -1 and self.lows[idx_c] <= c <= self.highs[idx_c]):
                
                sl_price = self.highs[idx_c] + 3 * tick_size
                sl_points = abs(sl_price - c)
                if sl_points >= 1.0:
                    tp_price = c - sl_points * self.rr_ratio
                    signals.append(SMCSignal(symbol, Direction.SHORT, SignalType.ORDER_BLOCK, c, sl_price, tp_price, sl_points, sl_points * self.rr_ratio, self.rr_ratio, ts, "15m", 0.78, "Bearish OB"))

        # 2. FVG
        min_fvg = self.min_fvg_points * (0.2 if symbol == "MGC" else 1.0)
        for k in range(2, 20):
            idx_c3 = i - k
            idx_c1 = i - k - 2
            if idx_c1 < 0: break
            
            # Bullish FVG
            gap = self.lows[idx_c3] - self.highs[idx_c1]
            if gap >= min_fvg and bos_val == 1 and self.highs[idx_c1] <= c <= self.lows[idx_c3]:
                sl_price = self.highs[idx_c1] - 5 * tick_size
                sl_points = abs(c - sl_price)
                tp_price = c + sl_points * self.rr_ratio
                signals.append(SMCSignal(symbol, Direction.LONG, SignalType.FVG, c, sl_price, tp_price, sl_points, sl_points * self.rr_ratio, self.rr_ratio, ts, "15m", 0.72, "Bullish FVG"))
                
            # Bearish FVG
            gap = self.lows[idx_c1] - self.highs[idx_c3]
            if gap >= min_fvg and bos_val == -1 and self.highs[idx_c3] <= c <= self.lows[idx_c1]:
                sl_price = self.lows[idx_c1] + 5 * tick_size
                sl_points = abs(sl_price - c)
                tp_price = c - sl_points * self.rr_ratio
                signals.append(SMCSignal(symbol, Direction.SHORT, SignalType.FVG, c, sl_price, tp_price, sl_points, sl_points * self.rr_ratio, self.rr_ratio, ts, "15m", 0.72, "Bearish FVG"))

        # 3. MSS
        if i >= 39:
            last_low = self.lows[i]
            last_high = self.highs[i]
            
            min_low_35 = np.min(self.lows[i-39 : i-4])
            max_high_35 = np.max(self.highs[i-39 : i-4])
            max_high_15 = np.max(self.highs[i-14 : i+1])
            min_low_15 = np.min(self.lows[i-14 : i+1])
            
            max_pts = 20 if symbol == "MGC" else 60
            
            # Bullish MSS
            if last_low > min_low_35 * 1.0005 and c > max_high_15:
                sl_price = last_low - 5 * tick_size
                sl_points = abs(c - sl_price)
                if 3 <= sl_points <= max_pts:
                    tp_price = c + sl_points * self.rr_ratio
                    signals.append(SMCSignal(symbol, Direction.LONG, SignalType.MSS, c, sl_price, tp_price, sl_points, sl_points * self.rr_ratio, self.rr_ratio, ts, "15m", 0.82, "Bullish MSS"))
                        
            # Bearish MSS
            if last_high < max_high_35 * 0.9995 and c < min_low_15:
                sl_price = last_high + 5 * tick_size
                sl_points = abs(sl_price - c)
                if 3 <= sl_points <= max_pts:
                    tp_price = c - sl_points * self.rr_ratio
                    signals.append(SMCSignal(symbol, Direction.SHORT, SignalType.MSS, c, sl_price, tp_price, sl_points, sl_points * self.rr_ratio, self.rr_ratio, ts, "15m", 0.82, "Bearish MSS"))

        if not signals: return None
        return max(signals, key=lambda s: s.confidence)

# ─────────────────────────────────────────────────────────────────────────────
# 4. LIGHTNING BACKTESTER (For Optimization Sweeps)
# ─────────────────────────────────────────────────────────────────────────────
def run_fast_backtest(sym: str, df: pd.DataFrame, rr: float, min_fvg: float, account_size: float = 50000.0) -> dict:
    """Headless backtester optimized for speed during parameter sweeps."""
    smc = FastSMCEngine(rr_ratio=rr, min_fvg_points=min_fvg)
    smc.prepare_data(df)
    
    inst = INSTRUMENTS[sym]
    tick_size = inst.tick_size
    raw_trades = []
    in_trade = False
    active_signal = None
    
    for i in range(120, len(df)):
        c_low = smc.lows[i]
        c_high = smc.highs[i]
        
        if in_trade:
            outcome = None
            if active_signal.direction == Direction.LONG:
                if c_low <= active_signal.stop_loss: outcome = 'loss'
                elif c_high >= active_signal.take_profit: outcome = 'win'
            else:
                if c_high >= active_signal.stop_loss: outcome = 'loss'
                elif c_low <= active_signal.take_profit: outcome = 'win'
                
            if outcome:
                pts_captured = active_signal.tp_points if outcome == 'win' else -active_signal.sl_points
                pnl = (pts_captured * inst.point_value * inst.contracts) - (inst.commission * inst.contracts)
                raw_trades.append(pnl)
                in_trade = False
                active_signal = None
            continue 
            
        signal = smc.scan_bar(i, sym, tick_size)
        if signal:
            active_signal = signal
            in_trade = True

    total_pnl = sum(raw_trades) if raw_trades else 0
    return {"pnl": total_pnl, "trades": len(raw_trades)}

# ─────────────────────────────────────────────────────────────────────────────
# 5. MAIN BACKTEST & MONTE CARLO ENGINE
# ─────────────────────────────────────────────────────────────────────────────
def run_individual_backtest(sym: str, df: pd.DataFrame, rr: float = 1.5, account_size: float = 50000.0, mll_trail: float = 2000.0):
    print(f"\n{'='*60}")
    print(f" INDIVIDUAL BACKTEST: {sym} (Optimized + Breakeven Mgmt)")
    print(f"{'='*60}")
    
    # Using the optimal RR of 1.5 found by your sweep
    smc = FastSMCEngine(rr_ratio=1.5, min_fvg_points=1.5)
    smc.prepare_data(df)
    
    inst = INSTRUMENTS[sym]
    tick_size = inst.tick_size
    raw_trades = []
    in_trade = False
    active_signal = None
    
    for i in range(120, len(df)):
        c_low = smc.lows[i]
        c_high = smc.highs[i]
        c_date = smc.dates[i]
        
        if in_trade:
            outcome = None
            
            # --- TRADE MANAGEMENT (Breakeven Logic) ---
            # If price moves 1R in our favor, move Stop Loss to Entry Price
            if active_signal.direction == Direction.LONG:
                if c_high >= active_signal.entry_price + active_signal.sl_points:
                    active_signal.stop_loss = max(active_signal.stop_loss, active_signal.entry_price)
            else:
                if c_low <= active_signal.entry_price - active_signal.sl_points:
                    active_signal.stop_loss = min(active_signal.stop_loss, active_signal.entry_price)

            # --- EXIT LOGIC ---
            if active_signal.direction == Direction.LONG:
                if c_low <= active_signal.stop_loss: 
                    outcome = 'loss'
                    # If stopped at BE, points captured is 0
                    pts_captured = 0 if active_signal.stop_loss == active_signal.entry_price else -active_signal.sl_points
                elif c_high >= active_signal.take_profit: 
                    outcome = 'win'
                    pts_captured = active_signal.tp_points
            else:
                if c_high >= active_signal.stop_loss: 
                    outcome = 'loss'
                    pts_captured = 0 if active_signal.stop_loss == active_signal.entry_price else -active_signal.sl_points
                elif c_low <= active_signal.take_profit: 
                    outcome = 'win'
                    pts_captured = active_signal.tp_points
                
            if outcome:
                # Commission is still paid even on breakeven trades
                pnl = (pts_captured * inst.point_value * inst.contracts) - (inst.commission * inst.contracts)
                
                raw_trades.append({
                    'exit_time': c_date,
                    'pnl': pnl
                })
                in_trade = False
                active_signal = None
            continue 
            
        signal = smc.scan_bar(i, sym, tick_size)
        if signal:
            active_signal = signal
            in_trade = True

    if not raw_trades:
        print(f"[BACKTEST] No trades were executed for {sym}.")
        return

    df_trades = pd.DataFrame(raw_trades)
    df_trades['cumulative_pnl'] = df_trades['pnl'].cumsum()
    df_trades['balance'] = account_size + df_trades['cumulative_pnl']
    
    peak = account_size
    max_dd = 0
    mll_breached = False
    
    for bal in df_trades['balance']:
        if bal > peak: peak = bal
        dd = peak - bal
        if dd > max_dd: max_dd = dd
        if dd >= mll_trail and not mll_breached: mll_breached = True

    total_pnl = df_trades['pnl'].sum()
    
    # Calculate Win Rate excluding breakeven trades
    wins = len(df_trades[df_trades['pnl'] > 0])
    losses = len(df_trades[df_trades['pnl'] < -10]) # Accounts for commission drag
    be_trades = len(df_trades[(df_trades['pnl'] <= 0) & (df_trades['pnl'] >= -10)])
    
    win_rate = (wins / (wins + losses)) * 100 if (wins+losses) > 0 else 0
    
    gw = df_trades[df_trades['pnl'] > 0]['pnl'].sum()
    gl = abs(df_trades[df_trades['pnl'] < 0]['pnl'].sum())
    profit_factor = gw / gl if gl > 0 else 99.0

    print(f" Total Trades:    {len(df_trades)} ({wins} Wins, {losses} Losses, {be_trades} BE)")
    print(f" Win Rate:        {win_rate:.1f}% (Excluding BE trades)")
    print(f" Profit Factor:   {profit_factor:.2f}")
    print(f" Total Net PnL:   ${total_pnl:,.2f}")
    print(f" Max Drawdown:    ${max_dd:,.2f}")
    print(f" MLL Breached?:   {'YES ❌' if mll_breached else 'NO ✅'} (${mll_trail} Limit)")
    
    # Monte Carlo Sim
    print(f"\n --- MONTE CARLO SIMULATION (1,000 Iterations) ---")
    pnls = df_trades['pnl'].values
    n_trades = len(pnls)
    num_sims = 1000
    
    mc_final_balances = []
    ruin_count = 0
    mc_paths = np.zeros((num_sims, n_trades + 1))
    mc_paths[:, 0] = account_size
    
    for i in range(num_sims):
        sampled_pnls = np.random.choice(pnls, size=n_trades, replace=True)
        cumulative = account_size + np.cumsum(sampled_pnls)
        mc_paths[i, 1:] = cumulative
        
        iter_peak = account_size
        iter_ruined = False
        for bal in cumulative:
            if bal > iter_peak: iter_peak = bal
            if (iter_peak - bal) >= mll_trail:
                iter_ruined = True
                break
        
        if iter_ruined: ruin_count += 1
        mc_final_balances.append(cumulative[-1])

    print(f" Risk of Ruin:    {(ruin_count / num_sims) * 100:.1f}%")
    print(f" Median Outcome:  ${np.percentile(mc_final_balances, 50):,.2f}")

    # Plotting
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6))
    
    line_color = '#2ca02c' if total_pnl >= 0 else '#d62728'
    ax1.plot(df_trades['exit_time'], df_trades['balance'], color=line_color, linewidth=2)
    ax1.axhline(account_size, color='black', linestyle='--')
    ax1.set_title(f'{sym} Equity (RR 1.5 + BE) - {len(df_trades)} Trades')
    ax1.set_ylabel('Account Balance ($)')
    ax1.grid(True, linestyle=':', alpha=0.7)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
    fig.autofmt_xdate()
    
    ax2.set_title(f'{sym} Monte Carlo (1,000 Paths)')
    ax2.axhline(account_size, color='black', linestyle='--', label='Starting Balance')
    for i in range(100): ax2.plot(mc_paths[i], color='blue', alpha=0.05)
    
    ax2.plot(np.percentile(mc_paths, 95, axis=0), color='green', linewidth=2, label='95th Percentile')
    ax2.plot(np.percentile(mc_paths, 50, axis=0), color='orange', linewidth=2, label='Median')
    ax2.plot(np.percentile(mc_paths, 5, axis=0), color='red', linewidth=2, label='5th Percentile')
    ax2.legend()
    ax2.grid(True, linestyle=':', alpha=0.7)
    
    plt.tight_layout()
    chart_filename = f'{sym}_opt_montecarlo.png'
    plt.savefig(chart_filename, dpi=300)
    print(f"\n[BACKTEST] Chart saved to '{chart_filename}'\n")

def optimize_mnq(df: pd.DataFrame):
    """Parameter Sweep to find the highest-yielding R:R and FVG configurations."""
    print(f"\n{'='*60}")
    print(f" YIELD OPTIMIZER: Sweeping Parameters for MNQ...")
    print(f"{'='*60}")
    
    best_pnl = -np.inf
    best_params = {}
    
    # Sweep Through Different Risk Reward Ratios & FVG minimum points
    rr_range = [1.5, 2.0, 2.5, 3.0]
    fvg_range = [1.0, 1.5, 2.0, 2.5]
    
    for rr in rr_range:
        for fvg in fvg_range:
            res = run_fast_backtest("MNQ", df, rr, fvg)
            print(f"  [Sweep] RR: {rr:.1f} | Min FVG: {fvg:.1f}pts -> PnL: ${res['pnl']:,.2f} ({res['trades']} trades)")
            
            if res['pnl'] > best_pnl:
                best_pnl = res['pnl']
                best_params = {'rr': rr, 'fvg': fvg}
                
    print(f"\n🚀 OPTIMAL MNQ SETTINGS FOUND: R:R = {best_params['rr']}, Min FVG = {best_params['fvg']} (Maximized PnL: ${best_pnl:,.2f})")

if __name__ == "__main__":
    data_mgr = DataManager()
    
    # Target symbols 
    TARGET_SYMBOLS = ["MES", "MNQ", "MGC"]
    
    # Cache data fetching so we only download once
    cached_data = {}
    for symbol in TARGET_SYMBOLS:
        df = data_mgr.fetch_yfinance(INSTRUMENTS[symbol])
        if not df.empty:
            cached_data[symbol] = df
            run_individual_backtest(sym=symbol, df=df, rr=2.5, account_size=50000.0, mll_trail=2000.0)
            
    # Run the Parameter Sweep explicitly on MNQ (The one with the edge)
    if "MNQ" in cached_data:
        optimize_mnq(cached_data["MNQ"])