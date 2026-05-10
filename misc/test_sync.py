import urllib.request
import json
from datetime import datetime, timezone, time as dtime
import pytz
import os
from dotenv import load_dotenv
load_dotenv()
ET = pytz.timezone('America/New_York')

import httpx, asyncio
async def test():
    c = httpx.AsyncClient()
    u = os.environ.get("PROJECT_X_USERNAME")
    k = os.environ.get("PROJECT_X_API_KEY")
    r = await c.post("https://api.topstepx.com/api/Auth/loginKey",json={"username":u,"key":k,"applicationId":1})
    print("REST:", r.status_code, r.text[:100])
    token = r.json()["token"]
    
    url = "https://api.topstepx.com/api/History/retrieveBars"
    day = datetime.strptime("2026-03-04", "%Y-%m-%d").date()
    rth_start = ET.localize(datetime.combine(day, dtime(9,30)))
    rth_end = ET.localize(datetime.combine(day, dtime(16,0)))
    start_utc = rth_start.astimezone(timezone.utc)
    end_utc = rth_end.astimezone(timezone.utc)
    
    payload = json.dumps({
        "contractId": "CON.F.US.EP.M26", "live": False,
        "startTime": start_utc.isoformat(), 
        "endTime": end_utc.isoformat(),
        "unit": 2, "unitNumber": 1, "limit": 500, "includePartialBar": False
    }).encode("utf-8")
    
    print("PAYLOAD:", payload)
    
    req = urllib.request.Request(url, data=payload, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            print("SUCCESS:", response.read().decode("utf-8")[:100])
    except urllib.error.HTTPError as e:
        print("HTTP_ERROR:", e.code, e.read().decode('utf-8')[:200])

asyncio.run(test())
