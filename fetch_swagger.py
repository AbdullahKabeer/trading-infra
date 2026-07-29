import sys, json, urllib.request

try:
    with urllib.request.urlopen("https://api.topstepx.com/swagger/v1/swagger.json") as res:
        data = json.loads(res.read().decode("utf-8"))
        paths = data.get("paths", {})
        for path in paths.keys():
            path_l = path.lower()
            if "market" in path_l or "product" in path_l or "instrument" in path_l or "contract" in path_l:
                print(path)
except Exception as e:
    print(e)
