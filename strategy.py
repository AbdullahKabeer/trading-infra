import yfinance as yf
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import Button
import matplotlib.dates as mdates
from matplotlib.collections import LineCollection

# --- CONFIGURATION ---
TICKER = "ES=F"
SESSION_MINUTES = 180   # View first 3 hours
ENTRY_THRESHOLD = 15.0  # Points away from POC to trigger "Fade"
TP_BUFFER = 2.0         # Take profit X points before POC
STOP_LOSS = 20.0        # Hard Stop
HMA_PERIOD = 14         # Hull Moving Average Period

# Set Style
plt.style.use('dark_background')

class DPOCBacktester:
    def __init__(self):
        self.df = self.get_data()
        self.preprocess_data() # Calculate indicators upfront
        if self.df.empty:
            print("No data found. Exiting.")
            return
        self.dates = sorted(list(set(self.df.index.date)))
        self.current_idx = 0
        
        # Setup Plot
        self.fig, self.ax = plt.subplots(figsize=(16, 9))
        plt.subplots_adjust(bottom=0.2) # Make room for buttons
        
        self.btn_next_ax = plt.axes([0.81, 0.05, 0.1, 0.075])
        self.btn_prev_ax = plt.axes([0.7, 0.05, 0.1, 0.075])
        
        self.btn_next = Button(self.btn_next_ax, 'Next Day >')
        self.btn_prev = Button(self.btn_prev_ax, '< Prev Day')
        
        self.btn_next.on_clicked(self.next_day)
        self.btn_prev.on_clicked(self.prev_day)
        
        self.render_day()
        plt.show()

    def preprocess_data(self):
        # Calculate HMA for the entire dataset
        self.df['HMA'] = self.calculate_hma(self.df['Close'], HMA_PERIOD)

    def calculate_hma(self, series, period):
        """
        Calculate Hull Moving Average (HMA)
        Formula: WMA(2 * WMA(n/2) - WMA(n)), sqrt(n)
        """
        def wma(s, n):
            weights = np.arange(1, n + 1)
            return s.rolling(n).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

        half_length = int(period / 2)
        sqrt_length = int(np.sqrt(period))
        
        wmaf = wma(series, half_length)
        wmas = wma(series, period)
        
        raw_hma = 2 * wmaf - wmas
        return wma(raw_hma, sqrt_length)

    def calculate_tpo(self, df, tick_size=0.25):
        """
        Calculates TPO Profile.
        Returns a dictionary {price_level: count_of_30m_blocks}
        """
        tpo_counts = {}
        
        # Resample to 30m blocks to determine TPO
        # We need High and Low for each 30m block
        resampled = df.resample('30min').agg({'High': 'max', 'Low': 'min'}).dropna()
        
        for idx, row in resampled.iterrows():
            high = row['High']
            low = row['Low']
            
            # Helper to round to nearest tick
            start_tick = int(np.floor(low / tick_size))
            end_tick = int(np.ceil(high / tick_size))
            
            for t in range(start_tick, end_tick + 1):
                price = t * tick_size
                tpo_counts[price] = tpo_counts.get(price, 0) + 1
                
        return tpo_counts

    def get_data(self):
        print(f"Fetching 1m data for {TICKER} (Limit 7 days)...")
        # Increase period to 7d for better coverage
        try:
            df = yf.download(TICKER, period="7d", interval="1m", progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df.dropna(inplace=True)
            
            # Localize/Convert to US/Eastern
            if df.index.tz is None:
                df.index = df.index.tz_localize('UTC')
            
            # Convert to Eastern
            df.index = df.index.tz_convert('US/Eastern')
            
            return df
        except Exception as e:
            print(f"Error fetching data: {e}")
            return pd.DataFrame()

    def calculate_developing_metrics(self, session_data):
        """
        Calculates Developing TPO POC and Value Area (VAH, VAL).
        Returns: (times, pocs, vahs, vals)
        """
        times = []
        pocs = []
        vahs = []
        vals = []
        
        # TPO Tracking
        tpo_counts = {}      # {price: total_tpo_count}
        tick_size = 0.25
        
        # Logic to track "touched this 30m period"
        current_period_idx = -1
        touched_in_period = set()
        
        if session_data.empty:
            return [], [], [], []
            
        session_start = session_data.index[0]
        
        for t, row in session_data.iterrows():
            # Determine which 30m period we are in
            minutes_since_start = (t - session_start).total_seconds() / 60
            period_idx = int(minutes_since_start // 30)
            
            # If new period, reset the "touched" tracker
            if period_idx != current_period_idx:
                current_period_idx = period_idx
                touched_in_period = set()
                
            # Process Price Range of this candle
            l_tick = int(row['Low'] / tick_size)
            h_tick = int(row['High'] / tick_size)
            
            for tick in range(l_tick, h_tick + 1):
                p = tick * tick_size
                if p not in touched_in_period:
                    tpo_counts[p] = tpo_counts.get(p, 0) + 1
                    touched_in_period.add(p)
            
            if not tpo_counts:
                times.append(t); pocs.append(row['Close']); vahs.append(row['Close']); vals.append(row['Close'])
                continue

            # 1. Find POC
            current_poc = max(tpo_counts, key=tpo_counts.get)
            
            # 2. Calculate Value Area (70%)
            sorted_prices = sorted(tpo_counts.keys())
            total_tpo = sum(tpo_counts.values())
            target_tpo = total_tpo * 0.70
            
            # Start at POC
            current_tpo_sum = tpo_counts[current_poc]
            poc_idx = sorted_prices.index(current_poc)
            upper_idx = poc_idx
            lower_idx = poc_idx
            
            # Expand until target reached
            while current_tpo_sum < target_tpo:
                # Check neighbors
                can_go_up = upper_idx < len(sorted_prices) - 1
                can_go_down = lower_idx > 0
                
                next_up_val = tpo_counts[sorted_prices[upper_idx + 1]] if can_go_up else 0
                next_down_val = tpo_counts[sorted_prices[lower_idx - 1]] if can_go_down else 0
                
                # If we can't go anywhere, break
                if not can_go_up and not can_go_down:
                    break
                    
                # Take the side with higher volume (standard TPO rule: if equal, take both or expanding side. simplified: take larger)
                # If equal, usually we take both, but let's just create a slight bias or check both
                if next_up_val > next_down_val:
                    upper_idx += 1
                    current_tpo_sum += next_up_val
                elif next_down_val > next_up_val:
                    lower_idx -= 1
                    current_tpo_sum += next_down_val
                else:
                    # Balance expansion
                    if can_go_up:
                        upper_idx += 1
                        current_tpo_sum += next_up_val
                    if can_go_down:
                        lower_idx -= 1
                        current_tpo_sum += next_down_val
            
            times.append(t)
            pocs.append(current_poc)
            vahs.append(sorted_prices[upper_idx])
            vals.append(sorted_prices[lower_idx])
            
        return times, pocs, vahs, vals

    def simulate_trades(self, session_data, pocs, vahs, vals):
        trades = []
        in_trade = False
        entry_price = 0
        stop_price = 0
        target_price = 0
        direction = 0 # 1 long, -1 short
        type_str = ""
        
        # HMA and slope
        hma = session_data['HMA'] if 'HMA' in session_data.columns else pd.Series(index=session_data.index, data=0)
        hma_diff = hma.diff()
        
        # Iterate
        for i in range(len(session_data)):
            if i < 15: continue # Let indicators stabilize
            
            row = session_data.iloc[i]
            time = session_data.index[i]
            close = row['Close']
            high = row['High']
            low = row['Low']
            
            curr_poc = pocs[i]
            curr_vah = vahs[i]
            curr_val = vals[i]
            curr_hma = hma.iloc[i]
            curr_slope = hma_diff.iloc[i]
            prev_slope = hma_diff.iloc[i-1] if i > 0 else 0
            
            # HMA Trend
            hma_up = curr_slope > 0
            hma_down = curr_slope < 0
            
            if not in_trade:
                # --- STRATEGY TYPE A: Trend Continuation ---
                # Long: Price > POC, HMA Up, Pullback to POC/VAL
                if close > curr_poc and hma_up:
                    # Check Pullback near POC or VAL (within buffer)
                    near_poc = abs(low - curr_poc) <= TP_BUFFER
                    near_val = abs(low - curr_val) <= TP_BUFFER
                    
                    if (near_poc or near_val) and close > row['Open']: # Bullish candle close
                        direction = 1
                        entry_price = close
                        stop_price = min(low, curr_val) - 2.0 # Below wick or VAL
                        target_price = curr_vah
                        type_str = "Type A (Trend)"
                        in_trade = True
                        trades.append({'type': 'Long', 'entry_time': time, 'entry_price': entry_price, 'status': 'Open', 'desc': type_str})
                        continue

                # Short: Price < POC, HMA Down, Pullback to POC/VAH
                elif close < curr_poc and hma_down:
                    # Check Pullback near POC or VAH
                    near_poc = abs(high - curr_poc) <= TP_BUFFER
                    near_vah = abs(high - curr_vah) <= TP_BUFFER
                    
                    if (near_poc or near_vah) and close < row['Open']: # Bearish candle close
                        direction = -1
                        entry_price = close
                        stop_price = max(high, curr_vah) + 2.0
                        target_price = curr_val
                        type_str = "Type A (Trend)"
                        in_trade = True
                        trades.append({'type': 'Short', 'entry_time': time, 'entry_price': entry_price, 'status': 'Open', 'desc': type_str})
                        continue

                # --- STRATEGY TYPE B: Value Rejection (Fade) ---
                # Long Fade at VAL: Down trend hitting VAL, HMA turning up/flat
                if not hma_up and low <= curr_val: # Hit VAL
                     # Reversal sign: Bullish close + HMA slope improving (getting less negative or positive)
                     if close > row['Open'] and curr_slope > prev_slope:
                        direction = 1
                        entry_price = close
                        stop_price = curr_val - 3.0
                        target_price = curr_poc
                        type_str = "Type B (Fade)"
                        in_trade = True
                        trades.append({'type': 'Long', 'entry_time': time, 'entry_price': entry_price, 'status': 'Open', 'desc': type_str})
                        continue
                
                # Short Fade at VAH: Up trend hitting VAH, HMA turning down/flat
                if not hma_down and high >= curr_vah:
                    if close < row['Open'] and curr_slope < prev_slope:
                        direction = -1
                        entry_price = close
                        stop_price = curr_vah + 3.0
                        target_price = curr_poc
                        type_str = "Type B (Fade)"
                        in_trade = True
                        trades.append({'type': 'Short', 'entry_time': time, 'entry_price': entry_price, 'status': 'Open', 'desc': type_str})
                        continue

            else:
                # Manage Trade
                trade = trades[-1]
                exit_triggered = False
                pnl = 0
                
                # Check Stop
                if direction == 1 and low <= stop_price:
                    exit_triggered = True; pnl = stop_price - entry_price; trade['result'] = 'Loss'; trade['exit_price'] = stop_price
                elif direction == -1 and high >= stop_price:
                    exit_triggered = True; pnl = entry_price - stop_price; trade['result'] = 'Loss'; trade['exit_price'] = stop_price
                
                # Check Target
                elif direction == 1 and high >= target_price:
                    exit_triggered = True; pnl = target_price - entry_price; trade['result'] = 'Win'; trade['exit_price'] = target_price
                elif direction == -1 and low <= target_price:
                    exit_triggered = True; pnl = entry_price - target_price; trade['result'] = 'Win'; trade['exit_price'] = target_price
                
                if exit_triggered:
                    trade['exit_time'] = time
                    trade['pnl'] = pnl * 20 # ES Multiplier $50/pt, let's just log points or cash? NQ is $20. ES is $50. User said ES=F. 
                    # Simpler to just keep points for now.
                    trade['pnl_pts'] = pnl
                    trade['status'] = 'Closed'
                    in_trade = False

        return trades

    def render_day(self):
        self.ax.clear()
        date = self.dates[self.current_idx]
        
        # Get Session Data (00:00 - 03:00 ET)
        # Filter for the specific date
        day_data = self.df[self.df.index.date == date]
        
        # Robust filtering for 3 hour session
        day_df = day_data.between_time('00:00', '03:00').copy()

        if day_df.empty:
            self.ax.text(0.5, 0.5, f"No Data between 00:00-03:00 for {date}", transform=self.ax.transAxes, ha='center', color='white')
            self.fig.canvas.draw()
            return

        # 1. Plot Candlesticks (Custom for Dark Mode)
        up = day_df[day_df['Close'] >= day_df['Open']]
        down = day_df[day_df['Close'] < day_df['Open']]
        
        # Colors based on image (Teal/Cyan theme? or standard)
        # Standard: Green/Red. Image had teal TPO. Let's use bright colors vs dark bg.
        col_up = '#00ff00'
        col_down = '#ff0000'
        
        self.ax.vlines(up.index, up['Low'], up['High'], color=col_up, linewidth=1)
        self.ax.vlines(up.index, up['Open'], up['Close'], color=col_up, linewidth=3)
        
        self.ax.vlines(down.index, down['Low'], down['High'], color=col_down, linewidth=1)
        self.ax.vlines(down.index, down['Open'], down['Close'], color=col_down, linewidth=3)
        
        # 1b. Plot Close Price Line
        self.ax.plot(day_df.index, day_df['Close'], color='#FF5500', linewidth=2.5, label='Close Price', alpha=1.0)

        # 2. Plot HMA (Color Coded)
        if 'HMA' in day_df.columns:
            # Prepare data for LineCollection
            # Convert dates to matplotlib numbers
            x = mdates.date2num(day_df.index)
            y = day_df['HMA'].values
            
            # Create segments: (x1, y1) to (x2, y2)
            points = np.array([x, y]).T.reshape(-1, 1, 2)
            segments = np.concatenate([points[:-1], points[1:]], axis=1)
            
            # Determine colors based on direction
            diff = np.diff(y)
            # Distinct colors: Cyan (Up), Magenta (Down) to contrast with Green/Red candles
            colors = ['#00ffff' if d >= 0 else '#ff00ff' for d in diff]
            
            lc = LineCollection(segments, colors=colors, linewidth=2, label=f'HMA ({HMA_PERIOD})')
            self.ax.add_collection(lc)
            
            # Legend hack
            self.ax.plot([], [], color='#00ffff', label='HMA Up')
            self.ax.plot([], [], color='#ff00ff', label='HMA Down')

        # 3. Plot TPO Profile (Teal blocks)
        tpo_data = self.calculate_tpo(day_df)
        
        if tpo_data:
            max_tpo = max(tpo_data.values())
            time_span = mdates.date2num(day_df.index[-1]) - mdates.date2num(day_df.index[0])
            scale_factor = (time_span * 0.4) / max_tpo # Use 40% of screen
            
            base_time = mdates.date2num(day_df.index[0])
            
            for price, count in tpo_data.items():
                width = count * scale_factor
                self.ax.fill_betweenx([price, price + 0.25], base_time, base_time + width, color='#00ced1', alpha=0.4, edgecolor='none')

        # 4. Plot Developing POC and Value Area
        times, pocs, vahs, vals = self.calculate_developing_metrics(day_df)
        
        # POC to Yellow for distinctness
        self.ax.step(times, pocs, where='post', color='yellow', linewidth=2, label='POC', linestyle='-')
        self.ax.step(times, vahs, where='post', color='white', linewidth=1, linestyle='--', label='VAH')
        self.ax.step(times, vals, where='post', color='white', linewidth=1, linestyle='--', label='VAL')
        
        # 5. Simulate and Highlight Trades (No symbols, just background)
        trades = self.simulate_trades(day_df, pocs, vahs, vals)
        total_pnl = 0
        
        for t in trades:
            if t['status'] == 'Closed':
                total_pnl += t['pnl_pts']
                # Highlight Section
                # Green background for Long, Red for Short
                color = 'green' if t['type'] == 'Long' else 'red'
                
                # Use axvspan for the duration of the trade
                # Connect entry_time to exit_time
                self.ax.axvspan(t['entry_time'], t['exit_time'], color=color, alpha=0.15)
                
                # Optional: Thin line connecting price for precision viewing
                self.ax.plot([t['entry_time'], t['exit_time']], [t['entry_price'], t['exit_price']], color=color, linestyle=':', alpha=0.5, linewidth=0.5)

                # Add Text Annotations
                # Entry Price
                self.ax.text(t['entry_time'], t['entry_price'], f"{t['entry_price']:.2f}", color='white', fontsize=8, ha='right', va='center', fontweight='bold')
                
                # Exit Price
                self.ax.text(t['exit_time'], t['exit_price'], f"{t['exit_price']:.2f}", color='white', fontsize=8, ha='left', va='center', fontweight='bold')
                
                # PnL / Result (Centered in span)
                mid_time = t['entry_time'] + (t['exit_time'] - t['entry_time']) / 2
                mid_price = (t['entry_price'] + t['exit_price']) / 2
                pnl_text = f"{t['result'].upper()}\n{t['pnl_pts']:.2f} pts"
                self.ax.text(mid_time, mid_price, pnl_text, color='white', fontsize=9, ha='center', va='center', fontweight='bold', bbox=dict(facecolor=color, alpha=0.5, edgecolor='none'))

        # Formatting
        self.ax.set_title(f"Session: {date} | Total PnL: {total_pnl:.2f} pts | Trades: {len(trades)}", color='white')
        self.ax.legend(loc='upper right', facecolor='black', edgecolor='white')
        self.ax.grid(True, alpha=0.1, color='gray', linestyle='--')
        self.ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        self.ax.tick_params(colors='white')
        for spine in self.ax.spines.values():
            spine.set_color('white')
            
        plt.setp(self.ax.get_xticklabels(), rotation=45)
        
        self.fig.canvas.draw()

    def next_day(self, event):
        if self.current_idx < len(self.dates) - 1:
            self.current_idx += 1
            self.render_day()

    def prev_day(self, event):
        if self.current_idx > 0:
            self.current_idx -= 1
            self.render_day()

if __name__ == "__main__":
    DPOCBacktester()
