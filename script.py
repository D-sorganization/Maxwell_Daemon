import json

with open("issues.json") as f:
    issues = json.load(f)
for i in issues:
    if "pull_request" not in i:
        print(f"#{i['number']} - {i['title']}")
