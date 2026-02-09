import yfinance as yf
import pandas as pd

TICKER = "NQ=F"
DATE = "2026-02-06"

def inspect_data():
    print(f"Fetching 1m data for {TICKER}...")
    try:
        df = yf.download(TICKER, start="2026-02-05", end="2026-02-08", interval="1m", progress=False)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Ensure timezone handling matches strategy.py
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df.index = df.index.tz_convert("US/Eastern")

        day_df = df[df.index.date.astype(str) == DATE]
        print(f"Total rows for {DATE}: {len(day_df)}")

        # Check 1: Set Difference
        expected_range = pd.date_range(start=f"{DATE} 09:30", end=f"{DATE} 12:30", freq="1min", tz="US/Eastern")
        missing_timestamps = expected_range.difference(day_df.index)
        print(f"Missing timestamps (Check 1): {len(missing_timestamps)}")
        if len(missing_timestamps) > 0:
            print(f"First 5 missing: {missing_timestamps[:5]}")

        # Check 2: between_time
        filtered_df = day_df.between_time("09:30", "12:30")
        print(f"Rows after between_time (Check 2): {len(filtered_df)}")

        # Identify discrepancy
        if len(missing_timestamps) == 0 and len(filtered_df) < len(expected_range):
             print("\nDiscrepancy found! 'difference' says 0 missing, but 'between_time' has fewer rows.")
             # Check what's in expected_range but NOT in filtered_df.index
             missing_in_filtered = expected_range.difference(filtered_df.index)
             print(f"Timestamps missing in filtered_df: {len(missing_in_filtered)}")
             print(f"First 5 missing in filtered_df: {missing_in_filtered[:5]}")
             
             # Check if these timestamps exist in day_df
             print("\nChecking if these timestamps exist in original day_df:")
             for ts in missing_in_filtered[:5]:
                 if ts in day_df.index:
                     print(f"{ts} exists in day_df")
                 else:
                     print(f"{ts} NOT in day_df")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    inspect_data()
