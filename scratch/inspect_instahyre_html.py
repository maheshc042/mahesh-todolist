import re

with open('artifacts/2026-09-06/run-221/instahyre-search-results.html', 'r', encoding='utf-8') as f:
    content = f.read()

print("HTML Length:", len(content))

# Look for employer-row occurrences
rows = list(re.finditer(r'<div[^>]*class="[^"]*employer-row[^"]*"[^>]*>', content))
print("Total employer-row matches in HTML:", len(rows))

for term in ['Altimate', 'Adobe', 'Purplle', 'eDAS', 'Juleo']:
    pos = content.find(term)
    if pos != -1:
        start = max(0, pos - 200)
        end = min(len(content), pos + 200)
        print(f"\n--- Snippet for {term} ---")
        print(content[start:end])
    else:
        print(f"\n--- {term} NOT FOUND in saved HTML ---")
