import yfinance as yf
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import Button
import matplotlib.dates as mdates
from matplotlib.collections import LineCollection

# --- CONFIGURATION ---
TICKER = "NQ=F"
HMA_PERIOD = 14
DAY_ROLLOVER_HOUR_ET = 18  # 6pm ET futures trading-day rollover
TICK_SIZE = 0.25

plt.style.use("dark_background")


class DPOCBacktester:
    def __init__(self):
        self.df = self.get_data()
        self.preprocess_data()

        if self.df.empty:
            print("No data found. Exiting.")
            return

        # trade_date is a python date
        self.dates = sorted(self.df["trade_date"].unique())
        self.current_idx = 0

        # Setup Plot
        self.fig, self.ax = plt.subplots(figsize=(16, 9))
        plt.subplots_adjust(bottom=0.2)

        self.btn_next_ax = plt.axes([0.81, 0.05, 0.1, 0.075])
        self.btn_prev_ax = plt.axes([0.70, 0.05, 0.1, 0.075])
        self.btn_toggle_ax = plt.axes([0.59, 0.05, 0.1, 0.075])
        self.btn_pnl_ax = plt.axes([0.48, 0.05, 0.1, 0.075])

        self.btn_next = Button(self.btn_next_ax, "Next Day >")
        self.btn_prev = Button(self.btn_prev_ax, "< Prev Day")
        self.btn_toggle = Button(self.btn_toggle_ax, "Toggle Ind.")
        self.btn_pnl = Button(self.btn_pnl_ax, "PnL Colors")

        self.btn_next.on_clicked(self.next_day)
        self.btn_prev.on_clicked(self.prev_day)
        self.btn_toggle.on_clicked(self.toggle_indicators)
        self.btn_pnl.on_clicked(self.toggle_pnl_colors)

        self.show_indicators = True
        self.use_pnl_colors = False

        # Print all trade summaries at startup
        self.print_all_summaries()

        self.render_day()
        plt.show()

    # ---------------------------
    # Indicators
    # ---------------------------
    def preprocess_data(self):
        self.df["HMA"] = self.calculate_hma(self.df["Close"], HMA_PERIOD)

    def calculate_hma(self, series, period):
        def wma(s, n):
            weights = np.arange(1, n + 1)
            return s.rolling(n).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

        half_length = max(int(period / 2), 1)
        sqrt_length = max(int(np.sqrt(period)), 1)

        wmaf = wma(series, half_length)
        wmas = wma(series, period)
        raw_hma = 2 * wmaf - wmas
        return wma(raw_hma, sqrt_length)

    # ---------------------------
    # TPO / Developing Metrics
    # ---------------------------
    def calculate_tpo(self, df, tick_size=TICK_SIZE):
        tpo_counts = {}
        resampled = df.resample("30min").agg({"High": "max", "Low": "min"}).dropna()

        for _, row in resampled.iterrows():
            high = row["High"]
            low = row["Low"]

            start_tick = int(np.floor(low / tick_size))
            end_tick = int(np.ceil(high / tick_size))

            for t in range(start_tick, end_tick + 1):
                price = t * tick_size
                tpo_counts[price] = tpo_counts.get(price, 0) + 1

        return tpo_counts

    def calculate_developing_metrics(self, session_data):
        times, pocs, vahs, vals = [], [], [], []

        tpo_counts = {}
        tick_size = TICK_SIZE

        current_period_idx = -1
        touched_in_period = set()

        if session_data.empty:
            return [], [], [], []

        session_start = session_data.index[0]

        for t, row in session_data.iterrows():
            minutes_since_start = (t - session_start).total_seconds() / 60
            period_idx = int(minutes_since_start // 30)

            if period_idx != current_period_idx:
                current_period_idx = period_idx
                touched_in_period = set()

            l_tick = int(np.floor(row["Low"] / tick_size))
            h_tick = int(np.ceil(row["High"] / tick_size))

            for tick in range(l_tick, h_tick + 1):
                p = tick * tick_size
                if p not in touched_in_period:
                    tpo_counts[p] = tpo_counts.get(p, 0) + 1
                    touched_in_period.add(p)

            if not tpo_counts:
                times.append(t)
                pocs.append(row["Close"])
                vahs.append(row["Close"])
                vals.append(row["Close"])
                continue

            current_poc = max(tpo_counts, key=tpo_counts.get)

            sorted_prices = sorted(tpo_counts.keys())
            total_tpo = sum(tpo_counts.values())
            target_tpo = total_tpo * 0.70

            current_tpo_sum = tpo_counts[current_poc]
            poc_idx = sorted_prices.index(current_poc)
            upper_idx = poc_idx
            lower_idx = poc_idx

            while current_tpo_sum < target_tpo:
                can_go_up = upper_idx < len(sorted_prices) - 1
                can_go_down = lower_idx > 0

                next_up_val = tpo_counts[sorted_prices[upper_idx + 1]] if can_go_up else 0
                next_down_val = tpo_counts[sorted_prices[lower_idx - 1]] if can_go_down else 0

                if not can_go_up and not can_go_down:
                    break

                if next_up_val > next_down_val:
                    upper_idx += 1
                    current_tpo_sum += next_up_val
                elif next_down_val > next_up_val:
                    lower_idx -= 1
                    current_tpo_sum += next_down_val
                else:
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

    # ---------------------------
    # Trades (same logic you had)
    # ---------------------------
    def calculate_trades(self, df, pocs, vahs, vals):
        trades = []
        if "HMA" not in df.columns:
            return trades

        hma = df["HMA"].values
        close = df["Close"].values
        high = df["High"].values
        low = df["Low"].values
        open_ = df["Open"].values

        hma_diff = np.diff(hma, prepend=hma[0])

        current_trade = None
        
        # Chop guard state tracking
        consecutive_losses = 0
        last_loss_time = None

        for i in range(1, len(df)):
            if i >= len(pocs):
                break

            t = df.index[i]
            c = close[i]
            h = high[i]
            l = low[i]

            poc = pocs[i]
            vah = vahs[i]
            val = vals[i]

            cur_slope = hma_diff[i]
            prev_slope = hma_diff[i - 1]

            is_up = cur_slope > 0
            is_down = cur_slope < 0

            # --- EXIT ---
            if current_trade:
                exit_signal = False
                exit_reason = ""
                exit_price = c

                # Dynamic stop loss based on value area width, CAPPED at 30 pts
                stop_loss_pts = min(30, max(15, 0.5 * (vah - val)))
                
                # HARD TREND INVALIDATION - Check FIRST (most important exit)
                if current_trade["label"] == "Trend A":
                    if current_trade["type"] == "LONG":
                        # Exit LONG if HMA turns negative OR close < POC
                        if cur_slope <= 0 or c < poc:
                            exit_signal = True
                            exit_price = c
                            exit_reason = "Trend Invalidation"
                    else:  # SHORT
                        # Exit SHORT if HMA turns positive OR close > POC
                        if cur_slope >= 0 or c > poc:
                            exit_signal = True
                            exit_price = c
                            exit_reason = "Trend Invalidation"
                
                # Stop loss check (only if trend still valid)
                if not exit_signal:
                    if current_trade["type"] == "LONG":
                        if l <= current_trade["entry_price"] - stop_loss_pts:
                            exit_signal = True
                            exit_price = current_trade["entry_price"] - stop_loss_pts
                            exit_reason = "Stop Loss"
                    else:  # SHORT
                        if h >= current_trade["entry_price"] + stop_loss_pts:
                            exit_signal = True
                            exit_price = current_trade["entry_price"] + stop_loss_pts
                            exit_reason = "Stop Loss"

                # Target check (only if trend still valid and not stopped out)
                if not exit_signal:
                    if current_trade["label"] == "Trend A":
                        if current_trade["type"] == "LONG":
                            target = vah
                            if h >= target and target > current_trade["entry_price"]:
                                exit_signal = True
                                exit_price = target
                                exit_reason = "Target (VAH)"
                        else:
                            target = val
                            if l <= target and target < current_trade["entry_price"]:
                                exit_signal = True
                                exit_price = target
                                exit_reason = "Target (VAL)"
                    else:  # Fade B
                        target = poc
                        if current_trade["type"] == "LONG":
                            if h >= target and target > current_trade["entry_price"]:
                                exit_signal = True
                                exit_price = target
                                exit_reason = "Target (POC)"
                        else:
                            if l <= target and target < current_trade["entry_price"]:
                                exit_signal = True
                                exit_price = target
                                exit_reason = "Target (POC)"

                if exit_signal:
                    pnl = (exit_price - current_trade["entry_price"]) if current_trade["type"] == "LONG" else (current_trade["entry_price"] - exit_price)
                    
                    # Track consecutive losses for chop guard
                    if exit_reason == "Stop Loss":
                        consecutive_losses += 1
                        last_loss_time = t
                    else:  # Any win resets the counter
                        consecutive_losses = 0
                    
                    trades.append(
                        {
                            "type": current_trade["type"],
                            "entry_time": current_trade["entry_time"],
                            "entry_price": current_trade["entry_price"],
                            "exit_time": t,
                            "exit_price": exit_price,
                            "pnl": pnl,
                            "label": current_trade["label"],
                            "exit_reason": exit_reason,
                        }
                    )
                    current_trade = None

                continue

            # --- ENTRY ---
            
            # FIX #2: Skip Post session (16:00-17:00 ET)
            hour_float = t.hour + t.minute / 60.0
            if 16.0 <= hour_float < 17.0:
                continue
            
            # FIX #3: Chop guard - cooldown after consecutive losses
            if consecutive_losses >= 2 and last_loss_time:
                time_since_loss = (t - last_loss_time).total_seconds()
                if time_since_loss < 3600:  # 60 minutes = 3600 seconds
                    continue
            
            is_green = c > open_[i]
            is_red = c < open_[i]

            signal_type = None
            label = ""

            # Trend A
            if is_up and c > poc:
                touched_poc = (l <= poc <= h)
                touched_val = (l <= val <= h)
                if (touched_poc or touched_val) and is_green:
                    signal_type = "LONG"
                    label = "Trend A"

            elif is_down and c < poc:
                touched_poc = (l <= poc <= h)
                touched_vah = (l <= vah <= h)
                if (touched_poc or touched_vah) and is_red:
                    signal_type = "SHORT"
                    label = "Trend A"


            # Fade B (DISABLED FOR NOW)
            # if not signal_type:
            #     if (l <= val <= h) and is_green and is_up:
            #         signal_type = "LONG"
            #         label = "Fade B"
            #     elif (l <= vah <= h) and is_red and is_down:
            #         signal_type = "SHORT"
            #         label = "Fade B"


            # FIX #1: Minimum reward filter
            if signal_type:
                # Calculate potential reward
                if signal_type == "LONG":
                    reward = vah - c
                else:  # SHORT
                    reward = c - val
                
                # Calculate what the stop loss would be
                potential_stop = max(15, 0.5 * (vah - val))
                
                # Require reward >= stop_loss OR reward >= 15
                if reward < potential_stop and reward < 15:
                    signal_type = None  # Skip this trade - insufficient reward
            
            if signal_type:
                current_trade = {"type": signal_type, "entry_price": c, "entry_time": t, "label": label}

        return trades

    def print_all_summaries(self):
        """Print trade summaries for all trading days"""
        print("\n" + "=" * 100)
        print("GENERATING ALL TRADE SUMMARIES")
        print("=" * 100)
        
        all_trades = []  # Collect all trades for CSV export
        
        for date in self.dates:
            day_df = self.df[self.df["trade_date"] == date].copy()
            
            if day_df.empty:
                continue
            
            # Enforce 24h window
            anchor_et = pd.Timestamp(date) + pd.Timedelta(hours=DAY_ROLLOVER_HOUR_ET)
            start_et = anchor_et.tz_localize("US/Eastern")
            end_et = (anchor_et + pd.Timedelta(hours=24)).tz_localize("US/Eastern")
            day_df = day_df[(day_df.index >= start_et) & (day_df.index < end_et)].copy()
            
            if day_df.empty:
                continue
            
            # Calculate metrics and trades
            times, pocs, vahs, vals = self.calculate_developing_metrics(day_df)
            trades = self.calculate_trades(day_df, pocs, vahs, vals)
            
            if not trades:
                continue
            
            print(f"\n--- Trade Summary for {date} ---")
            print(f"{'Strategy':<10} {'Session':<8} {'Type':<8} {'Time In Trade':<15} {'PnL (pts)':<12} {'Entry':<10} {'Exit':<10} {'Reason'}")
            print("-" * 100)
            
            total_pnl = 0.0
            for tr in trades:
                t_entry = tr["entry_time"]
                t_exit = tr["exit_time"]
                
                # Session classification
                h = t_entry.hour
                m = t_entry.minute
                tf = h + m / 60.0
                if tf >= 18.0 or tf < 3.0:
                    session_name = "Asia"
                elif 3.0 <= tf < 9.5:
                    session_name = "London"
                elif 9.5 <= tf < 16.0:
                    session_name = "NY"
                elif 16.0 <= tf < 17.0:
                    session_name = "Post"
                else:
                    session_name = "Closed"
                
                entry_price = tr["entry_price"]
                exit_price = tr["exit_price"]
                pnl = tr["pnl"]
                label = tr["label"]
                type_ = tr["type"]
                reason = tr["exit_reason"]
                duration = t_exit.tz_localize(None) - t_entry.tz_localize(None)
                
                print(f"{label:<10} {session_name:<8} {type_:<8} {str(duration):<15} {pnl:<+12.2f} {entry_price:<10.2f} {exit_price:<10.2f} {reason}")
                total_pnl += pnl
                
                # Collect for CSV
                all_trades.append({
                    "Trade_Date": date,
                    "Entry_Time": t_entry,
                    "Exit_Time": t_exit,
                    "Session": session_name,
                    "Strategy": label,
                    "Type": type_,
                    "Entry_Price": entry_price,
                    "Exit_Price": exit_price,
                    "PnL_pts": pnl,
                    "Duration": str(duration),
                    "Exit_Reason": reason
                })
            
            print("-" * 100)
            print(f"Total Daily PnL: {total_pnl:+.2f} pts")
            print("=" * 100)
        
        # Export to CSV
        if all_trades:
            csv_filename = f"trades_{TICKER.replace('=', '')}.csv"
            trades_df = pd.DataFrame(all_trades)
            trades_df.to_csv(csv_filename, index=False)
            print(f"\n✅ Exported {len(all_trades)} trades to {csv_filename}")
        
        print("\n" + "=" * 100)
        print("ALL SUMMARIES GENERATED - Now showing interactive chart")
        print("=" * 100 + "\n")

    # ---------------------------
    # Rendering
    # ---------------------------
    def render_day(self):
        self.ax.clear()
        date = self.dates[self.current_idx]  # python date (trade day)

        # Pull the entire futures trading-day by trade_date
        day_df = self.df[self.df["trade_date"] == date].copy()
        if day_df.empty:
            self.ax.text(0.5, 0.5, f"No Data for {date}", transform=self.ax.transAxes, ha="center", color="white")
            self.fig.canvas.draw()
            return

        # Hard-enforce the exact 24h window for this trade_date: 18:00 -> next 18:00
        anchor_et = pd.Timestamp(date) + pd.Timedelta(hours=DAY_ROLLOVER_HOUR_ET)  # naive
        start_et = anchor_et.tz_localize("US/Eastern")
        end_et = (anchor_et + pd.Timedelta(hours=24)).tz_localize("US/Eastern")
        day_df = day_df[(day_df.index >= start_et) & (day_df.index < end_et)].copy()

        if day_df.empty:
            self.ax.text(0.5, 0.5, f"No Data in trade-day window for {date}", transform=self.ax.transAxes, ha="center", color="white")
            self.fig.canvas.draw()
            return

        # For matplotlib stability, use naive timestamps for plotting
        plot_df = day_df.copy()
        plot_df.index = plot_df.index.tz_localize(None)

        # Candles
        up = plot_df[plot_df["Close"] >= plot_df["Open"]]
        down = plot_df[plot_df["Close"] < plot_df["Open"]]

        self.ax.vlines(up.index, up["Low"], up["High"], color="#00ff00", linewidth=1)
        self.ax.vlines(up.index, up["Open"], up["Close"], color="#00ff00", linewidth=3)

        self.ax.vlines(down.index, down["Low"], down["High"], color="#ff0000", linewidth=1)
        self.ax.vlines(down.index, down["Open"], down["Close"], color="#ff0000", linewidth=3)

        if self.show_indicators:
            # Close line
            self.ax.plot(plot_df.index, plot_df["Close"], color="#FF5500", linewidth=2.5, label="Close Price", alpha=1.0)

            # HMA colored
            if "HMA" in plot_df.columns:
                x = mdates.date2num(plot_df.index)
                y = plot_df["HMA"].values
                pts = np.array([x, y]).T.reshape(-1, 1, 2)
                segs = np.concatenate([pts[:-1], pts[1:]], axis=1)
                dy = np.diff(y)
                colors = ["#00ffff" if d >= 0 else "#ff00ff" for d in dy]
                lc = LineCollection(segs, colors=colors, linewidth=2, label=f"HMA ({HMA_PERIOD})")
                self.ax.add_collection(lc)
                self.ax.plot([], [], color="#00ffff", label="HMA Up")
                self.ax.plot([], [], color="#ff00ff", label="HMA Down")

            # TPO blocks
            tpo_data = self.calculate_tpo(day_df)
            if tpo_data:
                max_tpo = max(tpo_data.values())
                time_span = mdates.date2num(plot_df.index[-1]) - mdates.date2num(plot_df.index[0])
                scale_factor = (time_span * 0.4) / max_tpo
                base_time = mdates.date2num(plot_df.index[0])

                prices = list(tpo_data.keys())
                widths = [c * scale_factor for c in tpo_data.values()]
                self.ax.barh(
                    prices,
                    widths,
                    height=TICK_SIZE,
                    left=base_time,
                    align="edge",
                    color="#00ced1",
                    alpha=0.4,
                    edgecolor="none",
                )

            # Developing POC / VA
            times, pocs, vahs, vals = self.calculate_developing_metrics(day_df)
            times_naive = [t.tz_localize(None) for t in times]

            self.ax.step(times_naive, pocs, where="post", color="yellow", linewidth=2, label="POC")
            self.ax.step(times_naive, vahs, where="post", color="white", linewidth=1, linestyle="--", label="VAH")
            self.ax.step(times_naive, vals, where="post", color="white", linewidth=1, linestyle="--", label="VAL")

            # Trades + printed summary
            trades = self.calculate_trades(day_df, pocs, vahs, vals)

            print(f"\n--- Trade Summary for {date} ---")
            print(f"{'Strategy':<10} {'Session':<8} {'Type':<8} {'Time In Trade':<15} {'PnL (pts)':<12} {'Entry':<10} {'Exit':<10} {'Reason'}")
            print("-" * 100)

            total_pnl = 0.0
            for tr in trades:
                t_entry = tr["entry_time"].tz_localize(None)
                t_exit = tr["exit_time"].tz_localize(None)

                # Session buckets (ET) that MATCH the separator blocks:
                # Asia:   18:00–03:00
                # London: 03:00–09:30
                # NY:     09:30–16:00
                # Post:   16:00–17:00
                # Closed: 17:00–18:00
                h = t_entry.hour
                m = t_entry.minute
                tf = h + m / 60.0
                if tf >= 18.0 or tf < 3.0:
                    session_name = "Asia"
                elif 3.0 <= tf < 9.5:
                    session_name = "London"
                elif 9.5 <= tf < 16.0:
                    session_name = "NY"
                elif 16.0 <= tf < 17.0:
                    session_name = "Post"
                else:
                    session_name = "Closed"

                entry_price = tr["entry_price"]
                exit_price = tr["exit_price"]
                pnl = tr["pnl"]
                label = tr["label"]
                type_ = tr["type"]
                reason = tr["exit_reason"]
                duration = t_exit - t_entry

                print(f"{label:<10} {session_name:<8} {type_:<8} {str(duration):<15} {pnl:<+12.2f} {entry_price:<10.2f} {exit_price:<10.2f} {reason}")
                total_pnl += pnl

                # Highlight
                if self.use_pnl_colors:
                    color = "#ff69b4" if pnl > 0 else "#ff8c00"
                    alpha = 0.10
                else:
                    color = "#00ff00" if type_ == "LONG" else "#ff0000"
                    alpha = 0.10

                self.ax.axvspan(t_entry, t_exit, color=color, alpha=alpha)

                # Annotation
                t_start_num = mdates.date2num(t_entry)
                t_end_num = mdates.date2num(t_exit)
                width = t_end_num - t_start_num
                mid_time_num = t_start_num + width / 2

                text_y = max(entry_price, exit_price) + 2 if type_ == "LONG" else min(entry_price, exit_price) - 2
                va = "bottom" if type_ == "LONG" else "top"
                outcome = "WIN" if pnl > 0 else "LOSS"
                pnl_str = f"{pnl:+.2f} pts"

                self.ax.text(
                    mid_time_num,
                    text_y,
                    f"{type_} {label}\n{outcome}\n{pnl_str}",
                    color="white",
                    fontsize=9,
                    ha="center",
                    va=va,
                    fontweight="bold",
                    bbox=dict(facecolor="black", alpha=0.6, edgecolor=color),
                )

            print("-" * 90)
            print(f"Total Daily PnL: {total_pnl:+.2f} pts")
            print("=" * 90)

        # ---------------------------
        # Session separators/labels (MATCH trade-day anchor)
        # ---------------------------
        anchor = pd.Timestamp(date) + pd.Timedelta(hours=DAY_ROLLOVER_HOUR_ET)  # naive (18:00 of calendar date)
        session_configs = [
            ("Asia",   anchor,                          anchor + pd.Timedelta(hours=9)),        # 18:00–03:00
            ("London", anchor + pd.Timedelta(hours=9),  anchor + pd.Timedelta(hours=15.5)),     # 03:00–09:30
            ("NY",     anchor + pd.Timedelta(hours=15.5), anchor + pd.Timedelta(hours=22)),     # 09:30–16:00
            ("Post",   anchor + pd.Timedelta(hours=22), anchor + pd.Timedelta(hours=23)),       # 16:00–17:00
            ("Closed", anchor + pd.Timedelta(hours=23), anchor + pd.Timedelta(hours=24)),       # 17:00–18:00
        ]

        y0, y1 = self.ax.get_ylim()
        y_top = y1 - (y1 - y0) * 0.02

        for label, start, end in session_configs:
            if start >= plot_df.index[0] and start <= plot_df.index[-1]:
                self.ax.axvline(start, color="gray", linestyle="--", linewidth=1, alpha=0.5)

            mid = start + (end - start) / 2
            if start <= plot_df.index[-1] and end >= plot_df.index[0]:
                self.ax.text(mid, y_top, label, color="gray", fontsize=12, ha="center", va="top", fontweight="bold", alpha=0.7)

        # Formatting
        self.ax.set_title(f"{TICKER} | Trade Day: {date} (18:00 ET rollover)", color="white")
        self.ax.legend(loc="upper right", facecolor="black", edgecolor="white")
        self.ax.grid(True, alpha=0.1, color="gray", linestyle="--")
        self.ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        self.ax.tick_params(colors="white")
        for spine in self.ax.spines.values():
            spine.set_color("white")
        plt.setp(self.ax.get_xticklabels(), rotation=45)

        self.fig.canvas.draw()

    # ---------------------------
    # Buttons
    # ---------------------------
    def next_day(self, event=None):
        if self.current_idx < len(self.dates) - 1:
            self.current_idx += 1
            self.render_day()

    def prev_day(self, event=None):
        if self.current_idx > 0:
            self.current_idx -= 1
            self.render_day()

    def toggle_indicators(self, event=None):
        self.show_indicators = not self.show_indicators
        self.render_day()

    def toggle_pnl_colors(self, event=None):
        self.use_pnl_colors = not self.use_pnl_colors
        self.render_day()

    # ---------------------------
    # Data
    # ---------------------------
    def get_data(self):
        print(f"Fetching 1m data for {TICKER} (Limit 7 days)...")
        try:
            df = yf.download(TICKER, period="7d", interval="1m", progress=False)

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)

            # Ensure timezone is US/Eastern
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            df.index = df.index.tz_convert("US/Eastern")

            # Futures trade_date rolls at 18:00 ET:
            # subtract 18 hours so anything between 18:00–23:59 maps to SAME trade_date,
            # and 00:00–17:59 maps to that same trade_date too.
            df["trade_date"] = (df.index - pd.Timedelta(hours=DAY_ROLLOVER_HOUR_ET)).date

            print(f"DEBUG: Rows={len(df)}  Range(ET)={df.index.min()} -> {df.index.max()}")
            print(f"DEBUG: trade_date span={min(df['trade_date'])} -> {max(df['trade_date'])}")

            return df

        except Exception as e:
            print(f"Error fetching data: {e}")
            return pd.DataFrame()


if __name__ == "__main__":
    DPOCBacktester()
