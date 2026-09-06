import re

with open('artifacts/2026-09-06/run-221/cutshort-dashboard.html', 'r', encoding='utf-8', errors='ignore') as f:
    c = f.read()

print("HTML size:", len(c))
# Check for any URLs with find-jobs or jobs
job_urls = set(re.findall(r'href=["\']([^"\']*(?:job|filter|search)[^"\']*)["\']', c))
print("Matching links:")
for u in sorted(job_urls)[:20]:
    print("  ", u)

# Search for "recommended" toggle text or switch
matches = [m.start() for m in re.finditer(r'recommend', c, re.I)]
print(f"\nRecommend occurrences: {len(matches)}")
for pos in matches[:5]:
    print("---", c[max(0, pos-100):min(len(c), pos+150)])
