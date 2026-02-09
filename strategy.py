import yfinance as yf
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import Button
import matplotlib.dates as mdates
from matplotlib.collections import LineCollection

# --- CONFIGURATION ---
TICKER = "NQ=F"
SESSION_MINUTES = 180   # View first 3 hours (from session start)
HMA_PERIOD = 14         # Hull Moving Average Period

# Choose session:
SESSION_START = "09:30"
# If you want FULL RTH, set SESSION_END = "16:00"
# If you want FIRST 3 HOURS ONLY, set SESSION_END = "12:30"
SESSION_END = "12:30"   # <-- first 3 hours of RTH (09:30-12:30)

TICK_SIZE = 0.25

plt.style.use("dark_background")


class DPOCBacktester:
    def __init__(self):
        self.df = self.get_data()
        self.preprocess_data()

        if self.df.empty:
            print("No data found. Exiting.")
            return

        self.dates = sorted(list(set(self.df.index.date)))
        self.current_idx = 0

        # Setup Plot
        self.fig, self.ax = plt.subplots(figsize=(16, 9))
        plt.subplots_adjust(bottom=0.2)

        self.btn_next_ax = plt.axes([0.81, 0.05, 0.1, 0.075])
        self.btn_prev_ax = plt.axes([0.7, 0.05, 0.1, 0.075])
        self.btn_toggle_ax = plt.axes([0.59, 0.05, 0.1, 0.075])

        self.btn_next = Button(self.btn_next_ax, "Next Day >")
        self.btn_prev = Button(self.btn_prev_ax, "< Prev Day")
        self.btn_toggle = Button(self.btn_toggle_ax, "Toggle Ind.")

        self.btn_next.on_clicked(self.next_day)
        self.btn_prev.on_clicked(self.prev_day)
        self.btn_toggle.on_clicked(self.toggle_indicators)

        self.show_indicators = True
        self.render_day()
        plt.show()

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

    def calculate_tpo(self, df, tick_size=TICK_SIZE):
        """
        Calculates TPO Profile.
        Returns a dictionary {price_level: count_of_30m_blocks}
        """
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

    def get_data(self):
        print(f"Fetching 1m data for {TICKER} (Limit 7 days)...")
        try:
            df = yf.download(TICKER, period="7d", interval="1m", progress=False)

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # Only drop if essential price data is missing
            df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)

            # Ensure timezone is US/Eastern
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            df.index = df.index.tz_convert("US/Eastern")

            return df
        except Exception as e:
            print(f"Error fetching data: {e}")
            return pd.DataFrame()

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
                times.append(t); pocs.append(row["Close"]); vahs.append(row["Close"]); vals.append(row["Close"])
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

    def render_day(self):
        self.ax.clear()
        date = self.dates[self.current_idx]

        day_data = self.df[self.df.index.date == date]

        # Show full session (no time filtering)
        day_df = day_data.copy()

        if day_df.empty:
            self.ax.text(
                0.5, 0.5,
                f"No Data for {date}",
                transform=self.ax.transAxes,
                ha="center",
                color="white",
            )
            self.fig.canvas.draw()
            return

        # Use naive timestamps for matplotlib display stability
        plot_df = day_df.copy()
        plot_df.index = plot_df.index.tz_localize(None)

        # Candles
        up = plot_df[plot_df["Close"] >= plot_df["Open"]]
        down = plot_df[plot_df["Close"] < plot_df["Open"]]

        col_up = "#00ff00"
        col_down = "#ff0000"

        self.ax.vlines(up.index, up["Low"], up["High"], color=col_up, linewidth=1)
        self.ax.vlines(up.index, up["Open"], up["Close"], color=col_up, linewidth=3)

        self.ax.vlines(down.index, down["Low"], down["High"], color=col_down, linewidth=1)
        self.ax.vlines(down.index, down["Open"], down["Close"], color=col_down, linewidth=3)

        # Close line
        if self.show_indicators:
            self.ax.plot(plot_df.index, plot_df["Close"], color="#FF5500", linewidth=2.5, label="Close Price", alpha=1.0)

            # HMA colored
            if "HMA" in plot_df.columns:
                x = mdates.date2num(plot_df.index)
                y = plot_df["HMA"].values
                points = np.array([x, y]).T.reshape(-1, 1, 2)
                segments = np.concatenate([points[:-1], points[1:]], axis=1)

                diff = np.diff(y)
                colors = ["#00ffff" if d >= 0 else "#ff00ff" for d in diff]
                lc = LineCollection(segments, colors=colors, linewidth=2, label=f"HMA ({HMA_PERIOD})")
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

                # Vectorized rendering using barh
                prices = list(tpo_data.keys())
                counts = list(tpo_data.values())
                widths = [c * scale_factor for c in counts]
                
                self.ax.barh(prices, widths, height=TICK_SIZE, left=base_time, 
                             align='edge', color="#00ced1", alpha=0.4, edgecolor="none")

            # Developing POC / VA
            times, pocs, vahs, vals = self.calculate_developing_metrics(day_df)

            # Convert times to naive for step plotting
            times_naive = [t.tz_localize(None) for t in times]

            self.ax.step(times_naive, pocs, where="post", color="yellow", linewidth=2, label="POC")
            self.ax.step(times_naive, vahs, where="post", color="white", linewidth=1, linestyle="--", label="VAH")
            self.ax.step(times_naive, vals, where="post", color="white", linewidth=1, linestyle="--", label="VAL")

        # Session Separators and Labels
        # Times are naive because plot_df.index is naive
        # We use the date from self.dates[self.current_idx]
        
        session_configs = [
            ("Asia", pd.Timestamp(date).replace(hour=0, minute=0), pd.Timestamp(date).replace(hour=3, minute=0)),
            ("London", pd.Timestamp(date).replace(hour=3, minute=0), pd.Timestamp(date).replace(hour=9, minute=30)),
            ("NY", pd.Timestamp(date).replace(hour=9, minute=30), pd.Timestamp(date).replace(hour=16, minute=0)),
            ("Post", pd.Timestamp(date).replace(hour=16, minute=0), pd.Timestamp(date).replace(hour=23, minute=59))
        ]

        # Draw separators and labels
        y_lim = self.ax.get_ylim()
        y_top = y_lim[1] - (y_lim[1] - y_lim[0]) * 0.02 # Just below top

        for label, start, end in session_configs:
            # Draw line at start if it's within view
            if start >= plot_df.index[0] and start <= plot_df.index[-1]:
                self.ax.axvline(start, color="gray", linestyle="--", linewidth=1, alpha=0.5)
            
            # Place label roughly in the middle of the session, but ensure it's visible
            mid_point = start + (end - start) / 2
            
            # Simple check if midpoint is roughly in data range (not perfect but good enough)
            if start <= plot_df.index[-1] and end >= plot_df.index[0]:
                 self.ax.text(mid_point, y_top, label, color="gray", fontsize=12, 
                              ha="center", va="top", fontweight="bold", alpha=0.7)

        # Formatting
        self.ax.set_title(f"{TICKER} | {date} | Full Session", color="white")
        self.ax.legend(loc="upper right", facecolor="black", edgecolor="white")
        self.ax.grid(True, alpha=0.1, color="gray", linestyle="--")
        self.ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        self.ax.tick_params(colors="white")

        for spine in self.ax.spines.values():
            spine.set_color("white")

        plt.setp(self.ax.get_xticklabels(), rotation=45)
        self.fig.canvas.draw()

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


if __name__ == "__main__":
    DPOCBacktester()
