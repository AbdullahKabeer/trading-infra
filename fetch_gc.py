import os, json, urllib.request, time
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Load credentials
load_dotenv()

API = "https://api.topstepx.com/api"
DATA_DIR = "gc_sessions"
DAYS_BACK = 150

import pytz
# Gold usually has different trading hours, but we'll use same timezone handling.
# RTH for Gold is typically 08:20 to 13:30 ET, but let's grab the whole liquid session
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

def fetch_gc(token, date_obj):
    d_str = date_obj.strftime("%Y-%m-%d")
    out_path = os.path.join(DATA_DIR, f"{d_str}.json")
    if os.path.exists(out_path):
        return  # Already cached
    
    # Let's grab standard US session broadly, or standard GC RTH (08:20 - 13:30)
    # Using 08:00 to 14:00 to capture the core liquidity period for Gold
    rth_start = ET.localize(datetime.combine(date_obj, datetime.strptime("08:00", "%H:%M").time()))
    rth_end = ET.localize(datetime.combine(date_obj, datetime.strptime("14:00", "%H:%M").time()))
    
    start_utc = rth_start.astimezone(timezone.utc)
    end_utc = rth_end.astimezone(timezone.utc)
    
    cid = "CON.F.US.GCE.M26"
    
    url = f"{API}/History/retrieveBars"
    payload = json.dumps({
        "contractId": cid, 
        "live": False, 
        "startTime": start_utc.isoformat(), 
        "endTime": end_utc.isoformat(), 
        "unit": 2, # minute bars
        "unitNumber": 1, 
        "limit": 1000, 
        "includePartialBar": False
    }).encode("utf-8")
    
    req = urllib.request.Request(url, data=payload, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            bars = json.loads(response.read().decode("utf-8")).get("bars", [])
    except Exception as e:
        print(f"Failed to fetch {d_str}: {e}")
        return
        
    if not bars:
        return
        
    bars.reverse()
    
    session_data = {
        "date": d_str,
        "bars": [{"ts": b.get("t"), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]} for b in bars],
        "vwaps": []
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
    
    print(f"Fetching GCE sessions from {current_date} to {today}...")
    
    while current_date <= today:
        if current_date.weekday() < 5:  # Mon-Fri only
            fetch_gc(token, current_date)
            time.sleep(0.5)
        current_date += timedelta(days=1)
        
    print("Done fetching GCE data.")

if __name__ == "__main__":
    main()
