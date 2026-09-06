import re

with open('artifacts/2026-09-06/run-221/cutshort-dashboard.html', 'r', encoding='utf-8', errors='ignore') as f:
    c = f.read()

links = re.findall(r'<a[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>', c, re.DOTALL)
for href, text in links:
    clean_t = re.sub(r'<[^>]+>', '', text).strip()
    if clean_t and any(k in clean_t.lower() for k in ['job', 'search', 'filter', 'recommend', 'view']):
        print(f"{clean_t} -> {href}")
