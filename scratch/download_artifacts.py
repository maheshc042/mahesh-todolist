import requests
import subprocess
import json
import zipfile
import io
import os

p = subprocess.Popen(['git', 'credential', 'fill'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
out, _ = p.communicate("protocol=https\nhost=github.com\n")
token = None
for line in out.splitlines():
    if line.startswith("password="):
        token = line.split("=", 1)[1].strip()

headers = {
    "Accept": "application/vnd.github.v3+json",
    "Authorization": f"Bearer {token}"
}

run_id = 33730550784
url = f"https://api.github.com/repos/maheshchichkoti/naukri-auto-apply-agent/actions/runs/{run_id}/artifacts"
resp = requests.get(url, headers=headers)
print("Artifacts status:", resp.status_code)
artifacts = resp.json().get("artifacts", [])
print(f"Found {len(artifacts)} artifacts:")
for a in artifacts:
    print(f"  Artifact: {a['name']} | Size: {a['size_in_bytes']} | ID: {a['id']} | URL: {a['archive_download_url']}")
    
    # download artifact
    dl_resp = requests.get(a['archive_download_url'], headers=headers)
    if dl_resp.status_code == 200:
        os.makedirs(f"scratch/gh_run_{run_id}", exist_ok=True)
        z = zipfile.ZipFile(io.BytesIO(dl_resp.content))
        z.extractall(f"scratch/gh_run_{run_id}")
        print(f"Extracted to scratch/gh_run_{run_id}: {z.namelist()[:20]}")
    else:
        print(f"Failed to download: {dl_resp.status_code}")

