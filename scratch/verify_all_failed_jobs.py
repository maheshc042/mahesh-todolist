import glob
import re

html_files = glob.glob("scratch/gh_run_33730550784/artifacts/2026-09-03/run-211/*.html")

print(f"Checking all {len(html_files)} HTML artifacts:")
for hf in sorted(html_files):
    with open(hf, encoding="utf-8", errors="replace") as f:
        html = f.read()
    
    basename = hf.split("\\")[-1]
    is_acp = "acp-container" in html or "alt=\"success-icon\"" in html or "<title>Apply Confirmation</title>" in html
    applied_to_match = re.search(r'Applied to <span class="job-title">"(.*?)"</span>', html)
    applied_to = applied_to_match.group(1) if applied_to_match else None
    
    if is_acp:
        print(f"[ACTUAL SUCCESS / FALSE NEGATIVE] {basename} -> Applied to '{applied_to}'")
    else:
        title_match = re.search(r"<title>(.*?)</title>", html)
        t = title_match.group(1) if title_match else "No title"
        print(f"[REAL FAILURE / BLOCKED] {basename} -> Title: {t}")
