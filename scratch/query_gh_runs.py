import requests
import subprocess
import json

# Get git credential
p = subprocess.Popen(['git', 'credential', 'fill'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
out, _ = p.communicate("protocol=https\nhost=github.com\n")
token = None
for line in out.splitlines():
    if line.startswith("password="):
        token = line.split("=", 1)[1].strip()

print(f"Token found: {bool(token)}")

headers = {
    "Accept": "application/vnd.github.v3+json",
    "Authorization": f"Bearer {token}"
}

url = "https://api.github.com/repos/maheshchichkoti/naukri-auto-apply-agent/actions/runs?per_page=15"
resp = requests.get(url, headers=headers)
print(f"Status: {resp.status_code}")
if resp.status_code == 200:
    data = resp.json()
    print(f"Total runs: {data.get('total_count')}")
    for r in data.get("workflow_runs", []):
        print(f"Run {r['id']} | Event: {r['event']} | Status: {r['status']} | Conclusion: {r['conclusion']} | Created: {r['created_at']} | Head: {r['head_commit']['message'][:50]}")
        # print jobs url
else:
    print(resp.text[:300])
