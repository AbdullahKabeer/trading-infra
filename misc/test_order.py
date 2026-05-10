import os
import sys
import asyncio
import httpx
from datetime import datetime

API_URL = "https://api.topstepx.com/api"

async def main():
    print("="*60)
    print("  TOPSTEP TEST - DEEP LIMIT ORDER")
    print("="*60)

    username = os.environ.get("PROJECT_X_USERNAME", "")
    api_key = os.environ.get("PROJECT_X_API_KEY", "")
    
    if not username or not api_key:
        print("ERROR: Set env vars first:")
        print("  export PROJECT_X_API_KEY='...'")
        print("  export PROJECT_X_USERNAME='...'")
        return
        
    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. Authenticate
        print("1. Authenticating...")
        r = await client.post(f"{API_URL}/Auth/loginKey", json={"userName": username, "apiKey": api_key})
        auth = r.json()
        token = auth.get("token")
        if not token:
            print(f"Auth failed: {auth}")
            return
        headers = {"Authorization": f"Bearer {token}"}
        print("  ✓ Success\n")

        # 2. Find Practice Account
        print("2. Finding Practice Account...")
        r = await client.post(f"{API_URL}/Account/search", json={"onlyActiveAccounts": True}, headers=headers)
        accts = r.json().get("accounts", [])
        prac = [a for a in accts if "PRAC" in a.get("name", "").upper() and a.get("canTrade")]
        if not prac:
            prac = [a for a in accts if a.get("canTrade")]
        if not prac:
            print("No active practice accounts found.")
            return
        account_id = prac[0]["id"]
        print(f"  ✓ {prac[0]['name']} (ID: {account_id})\n")

        # 3. Find ES Contract
        print("3. Finding ES Contract...")
        r = await client.post(f"{API_URL}/Contract/available", json={"live": False}, headers=headers)
        contracts = r.json().get("contracts", [])
        es = [c for c in contracts if c.get("symbolId") == "F.US.EP" or c.get("description", "").startswith("E-Mini S&P")]
        if not es:
            print("No ES contract found.")
            return
        contract_id = es[0]["id"]
        print(f"  ✓ {es[0]['name']} (ID: {contract_id})\n")

        # 4. Place limit order at absurdly low price
        test_price = 2000.00
        print("4. Placing BUY Limit Order...")
        payload = {
            "accountId": account_id,
            "contractId": contract_id,
            "type": 1,          # 1 = Limit Order
            "side": 0,          # 0 = Buy
            "size": 1,          # 1 contract
            "limitPrice": test_price,
            "customTag": f"TEST_LIMIT_{int(datetime.now().timestamp())}"
        }
        
        print(f"  Payload: {payload}")
        r = await client.post(f"{API_URL}/Order/place", json=payload, headers=headers)
        
        try:
            res = r.json()
            if res.get("success"):
                print(f"\n  [SUCCESS] Order Accepted!")
                print("  Go check your TopstepX platform -> Orders tab.")
                print(f"  You should see an open BUY Limit order for 1 ES at ${test_price}")
            else:
                print(f"\n  [FAILED] {res}")
        except Exception as e:
            print(f"\n  [FAILED] Could not parse response: {r.text}")

if __name__ == "__main__":
    asyncio.run(main())
