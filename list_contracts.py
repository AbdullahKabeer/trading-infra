import os, json
from urllib.request import Request, urlopen
from dotenv import load_dotenv

load_dotenv()
u = os.environ.get("PROJECT_X_USERNAME")
k = os.environ.get("PROJECT_X_API_KEY")

req = Request("https://api.topstepx.com/api/Auth/loginKey", data=json.dumps({"userName":u,"apiKey":k}).encode("utf-8"), headers={"Content-Type": "application/json"})
with urlopen(req) as res:
    token = json.loads(res.read())["token"]

req2 = Request("https://api.topstepx.com/api/Contract/available", data=json.dumps({"live": False}).encode("utf-8"), headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
with urlopen(req2) as res:
    contracts = json.loads(res.read())["contracts"]

for c in contracts:
    if "NQ" in c.get("id", "") or "NQ" in c.get("symbolId", "") or "Nasdaq" in c.get("description", ""):
        print(c.get("id"), " | ", c.get("symbolId"), " | ", c.get("description"))
