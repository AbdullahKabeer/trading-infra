import os, json, urllib.request
from dotenv import load_dotenv

load_dotenv()
API = "https://api.topstepx.com/api"

def main():
    u = os.environ.get("PROJECT_X_USERNAME", "")
    k = os.environ.get("PROJECT_X_API_KEY", "")
    
    payload = json.dumps({"userName": u, "apiKey": k}).encode("utf-8")
    req = urllib.request.Request(f"{API}/Auth/loginKey", data=payload, headers={"Content-Type": "application/json"})
    
    with urllib.request.urlopen(req, timeout=30) as response:
        d = json.loads(response.read().decode("utf-8"))
        token = d["token"]
        
    for q in ["NQ", "NQM26", "ENQ"]:
        url = f"{API}/Contract/search"
        payload = json.dumps({"query": q}).encode("utf-8")
        try:
            r2 = urllib.request.Request(url, data=payload, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            with urllib.request.urlopen(r2, timeout=10) as r2_res:
                data = json.loads(r2_res.read().decode("utf-8"))
                if data:
                    print(f"--- QUERY: {q} ---")
                    print(json.dumps(data, indent=2))
        except Exception as e:
            print(f"Error on {q}: {e}")

if __name__ == "__main__":
    main()
