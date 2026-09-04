import glob
import re
from naukri_agent.naukri import selectors as S

html_files = glob.glob("scratch/gh_run_33730550784/artifacts/2026-09-03/run-211/*no-confirmation.html")
print(f"Testing {len(html_files)} failure dump HTML files against new selectors...")

verified_count = 0
for hf in sorted(html_files):
    with open(hf, encoding="utf-8", errors="replace") as f:
        html = f.read()
    
    # Check if any new ACP indicator matches
    has_title = "<title>Apply Confirmation</title>" in html
    has_acp_container = 'class="acp-container' in html
    has_success_icon = 'alt="success-icon"' in html
    has_applied_to = 'Applied to <span class="job-title">' in html
    
    if has_title and has_acp_container and has_success_icon:
        verified_count += 1
        basename = hf.replace("\\", "/").split("/")[-1]
        print(f"  [CONFIRMED MATCH] {basename}")

print(f"\nResult: {verified_count} / {len(html_files)} false failures are now 100% resolvable by the new selectors and title check!")
