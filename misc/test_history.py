"""
Test historical bar fetch from TopstepX.
Pulls last 5 days of 1-min ES bars during RTH.
"""
import asyncio, os, json
from datetime import datetime, timezone, timedelta
import httpx

API = "https://api.topstepx.com/api"
CONTRACT = "CON.F.US.EP.M26"

async def main():
    u = os.environ.get("PROJECT_X_USERNAME", "")
    k = os.environ.get("PROJECT_X_API_KEY", "")

    async with httpx.AsyncClient(timeout=60) as http:
        # Auth
        r = await http.post(f"{API}/Auth/loginKey", json={"userName": u, "apiKey": k})
        token = r.json().get("token")
        if not token:
            print("Auth failed"); return
        headers = {"Authorization": f"Bearer {token}"}
        print(f"✓ Authenticated\n")

        # Try fetching 1-min bars for yesterday RTH
        # RTH = 9:30 AM - 4:00 PM ET = 13:30 - 20:00 UTC (EST) or 13:30 - 20:00 (EDT)
        now = datetime.now(timezone.utc)
        
        # Go back 1 day
        yesterday = now - timedelta(days=1)
        start = yesterday.replace(hour=13, minute=30, second=0, microsecond=0)  # 9:30 AM ET approx
        end = yesterday.replace(hour=20, minute=0, second=0, microsecond=0)    # 4:00 PM ET approx

        print(f"Fetching: {start.isoformat()} to {end.isoformat()}")
        print(f"Contract: {CONTRACT}")

        # Try with string contractId first
        payload = {
            "contractId": CONTRACT,
            "live": False,
            "startTime": start.isoformat(),
            "endTime": end.isoformat(),
            "unit": 2,       # Minute
            "unitNumber": 1,  # 1-minute bars
            "limit": 500,
            "includePartialBar": False,
        }

        print(f"\nPayload: {json.dumps(payload, indent=2)}")

        r = await http.post(f"{API}/History/retrieveBars", json=payload, headers=headers)
        print(f"\nStatus: {r.status_code}")
        
        data = r.json()
        print(f"Success: {data.get('success')}")
        print(f"Error: {data.get('errorCode')} - {data.get('errorMessage')}")
        
        bars = data.get("bars", [])
        print(f"Bars returned: {len(bars)}")

        if bars:
            print(f"\nFirst bar: {json.dumps(bars[0], indent=2)}")
            print(f"Last bar:  {json.dumps(bars[-1], indent=2)}")
            
            # Show first 5 bars
            print(f"\nFirst 5:")
            for b in bars[:5]:
                print(f"  {b}")
        else:
            # Try with integer contractId
            print("\nString contractId returned no bars. Trying integer...")
            # Maybe we need to look up the numeric ID
            payload2 = {**payload, "contractId": 1}  # dummy
            r2 = await http.post(f"{API}/History/retrieveBars", json=payload2, headers=headers)
            print(f"Status: {r2.status_code}")
            print(f"Response: {json.dumps(r2.json(), indent=2)[:500]}")

        # Also try fetching today
        print(f"\n{'='*50}")
        today_start = now.replace(hour=13, minute=30, second=0, microsecond=0)
        if now.hour < 13:
            today_start -= timedelta(days=1)

        payload3 = {
            "contractId": CONTRACT,
            "live": False,
            "startTime": today_start.isoformat(),
            "endTime": now.isoformat(),
            "unit": 2,
            "unitNumber": 1,
            "limit": 500,
            "includePartialBar": True,
        }
        print(f"\nFetching today: {today_start.isoformat()} to {now.isoformat()}")
        r3 = await http.post(f"{API}/History/retrieveBars", json=payload3, headers=headers)
        d3 = r3.json()
        bars3 = d3.get("bars", [])
        print(f"Today bars: {len(bars3)}")
        if bars3:
            print(f"First: {json.dumps(bars3[0], indent=2)}")
            print(f"Last:  {json.dumps(bars3[-1], indent=2)}")

if __name__ == "__main__":
    asyncio.run(main())