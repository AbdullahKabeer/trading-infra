"""
Raw TopstepX API test — no SDK, just direct HTTP calls.
This isolates whether the problem is the SDK or your credentials.
"""

import asyncio
import os
import httpx

API_URL = "https://api.topstepx.com/api"

async def main():
    username = os.environ.get("PROJECT_X_USERNAME", "")
    api_key = os.environ.get("PROJECT_X_API_KEY", "")
    
    if not username or not api_key:
        print("Set env vars first:")
        print("  export PROJECT_X_API_KEY='your_key'")
        print("  export PROJECT_X_USERNAME='your_username'")
        return
    
    print(f"Username: {username}")
    print(f"API Key: {api_key[:8]}...{api_key[-4:]}")
    print(f"API URL: {API_URL}")
    print()
    
    async with httpx.AsyncClient(timeout=30) as http:
        # Step 1: Auth
        print("1. Authenticating...")
        r = await http.post(f"{API_URL}/Auth/loginKey", json={
            "userName": username,
            "apiKey": api_key
        })
        print(f"   Status: {r.status_code}")
        auth = r.json()
        print(f"   Response: {auth}")
        
        if not auth.get("success") and auth.get("token") is None:
            # Try checking if token is at a different key
            print(f"\n   Full response keys: {list(auth.keys())}")
            if "errorMessage" in auth:
                print(f"   Error: {auth['errorMessage']}")
            print("\n   Auth failed. Check your username and API key.")
            return
        
        token = auth.get("token", auth.get("sessionToken", ""))
        if not token:
            print(f"   No token found in response. Keys: {list(auth.keys())}")
            return
        
        print(f"   Token: {token[:20]}...")
        headers = {"Authorization": f"Bearer {token}"}
        
        # Step 2: List accounts
        print("\n2. Listing accounts...")
        r = await http.post(f"{API_URL}/Account/search", 
                           json={"onlyActiveAccounts": True},
                           headers=headers)
        print(f"   Status: {r.status_code}")
        accts = r.json()
        print(f"   Response: {accts}")
        
        if "accounts" in accts:
            for a in accts["accounts"]:
                print(f"\n   Account: {a.get('name')} (ID: {a.get('id')}) CanTrade: {a.get('canTrade')}")
        
        # Step 3: List contracts (find ES)
        print("\n3. Searching for ES contract...")
        r = await http.post(f"{API_URL}/Contract/available",
                           json={"live": False},
                           headers=headers)
        contracts = r.json()
        if "contracts" in contracts:
            es_contracts = [c for c in contracts["contracts"] 
                          if "ES" in c.get("name","").upper() or "E-MINI S&P" in c.get("description","").upper()
                          or "EP" in c.get("symbolId","").upper() or "ES" in c.get("symbolId","").upper()]
            print(f"   Found {len(es_contracts)} ES-related contracts:")
            for c in es_contracts[:5]:
                print(f"     ID: {c.get('id')}  Name: {c.get('name')}  Desc: {c.get('description')}")
                print(f"     TickSize: {c.get('tickSize')}  TickValue: {c.get('tickValue')}  Symbol: {c.get('symbolId')}")
        else:
            print(f"   Response: {str(contracts)[:500]}")

if __name__ == "__main__":
    asyncio.run(main())