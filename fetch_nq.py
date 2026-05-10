import os, json, urllib.request, time
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Load credentials
load_dotenv()

API = "https://api.topstepx.com/api"
DATA_DIR = "nq_sessions"
DAYS_BACK = 150

import pytz
ET = pytz.timezone("America/New_York")

def auth():
    u = os.environ.get("PROJECT_X_USERNAME", "")
    k = os.environ.get("PROJECT_X_API_KEY", "")
    if not u or not k:
        print("Missing TopStepX credentials in .env")
        return None
    
    payload = json.dumps({"userName": u, "apiKey": k}).encode("utf-8")
    req = urllib.request.Request(f"{API}/Auth/loginKey", data=payload, headers={"Content-Type": "application/json"})
    
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            d = json.loads(response.read().decode("utf-8"))
            if d.get("success") and d.get("token"):
                print("Authenticated successfully.")
                return d["token"]
            print(f"Auth failed: {d}")
            return None
    except Exception as e:
        print(f"Auth request failed: {e}")
        return None

def fetch_nq(token, date_obj):
    d_str = date_obj.strftime("%Y-%m-%d")
    out_path = os.path.join(DATA_DIR, f"{d_str}.json")
    if os.path.exists(out_path):
        return  # Already cached
    
    rth_start = ET.localize(datetime.combine(date_obj, datetime.strptime("09:30", "%H:%M").time()))
    rth_end = ET.localize(datetime.combine(date_obj, datetime.strptime("16:00", "%H:%M").time()))
    
    start_utc = rth_start.astimezone(timezone.utc)
    end_utc = rth_end.astimezone(timezone.utc)
    
    def _get(cid):
        url = f"{API}/History/retrieveBars"
        payload = json.dumps({
            "contractId": cid, 
            "live": False, 
            "startTime": start_utc.isoformat(), 
            "endTime": end_utc.isoformat(), 
            "unit": 2, 
            "unitNumber": 1, 
            "limit": 500, 
            "includePartialBar": False
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return json.loads(response.read().decode("utf-8")).get("bars", [])
        except:
            return []

    # Map the correct contract for the NQ front month
    # Z25 = Dec 2025, H26 = Mar 2026, M26 = Jun 2026
    c_date = date_obj
    if c_date < datetime.strptime("2025-12-12", "%Y-%m-%d").date():
        cid = "CON.F.US.NQ.Z25"
    elif c_date < datetime.strptime("2026-03-12", "%Y-%m-%d").date():
        cid = "CON.F.US.NQ.H26"
    else:
        cid = "CON.F.US.NQ.M26"
        
    bars = _get(cid)
    if not bars:
        # Fallback to TSX alternative tickers if NQ doesn't match
        alt_ids = [cid.replace(".NQ.", ".ENQ."), cid.replace(".NQ.", "")]
        for alt in alt_ids:
            bars = _get(alt)
            if bars: break
    
    if not bars:
        return
        
    bars.reverse()
    
    # Minimal extraction that matches combine_live processing structure
    session_data = {
        "date": d_str,
        "bars": [{"ts": b.get("t"), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]} for b in bars],
        "vwaps": [] # Let the bot recalculate during backtest
    }
    
    with open(out_path, "w") as f:
        json.dump(session_data, f)
    print(f"  [+] Saved {d_str} ({len(bars)} bars)")

def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    token = auth()
    if not token: return
    
    today = datetime.now(ET).date()
    current_date = today - timedelta(days=DAYS_BACK)
    
    print(f"Fetching NQ RTH sessions from {current_date} to {today}...")
    
    while current_date <= today:
        if current_date.weekday() < 5:  # Mon-Fri only
            fetch_nq(token, current_date)
            time.sleep(0.1) # Be nice to their API
        current_date += timedelta(days=1)
        
    print("Done fetching NQ data.")

if __name__ == "__main__":
    main()
