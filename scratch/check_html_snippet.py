import re

with open('scratch/gh_run_33730550784/artifacts/2026-09-03/run-211/081538_AI-Python-Engineer_reco-020926927973_no-confirmation.html', encoding='utf-8', errors='replace') as f:
    html = f.read()

idx = html.find('alt="success-icon"')
if idx != -1:
    snippet = html[max(0, idx-300):min(len(html), idx+600)]
    # clean HTML tags to see text
    text = re.sub(r'<[^>]+>', ' ', snippet)
    print("=== RAW SNIPPET ===")
    print(snippet)
    print("\n=== TEXT ===")
    print(" ".join(text.split()))
else:
    print("Not found")
