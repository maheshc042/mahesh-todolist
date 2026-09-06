with open('artifacts/2026-09-06/run-221/cutshort-dashboard.html', 'r', encoding='utf-8', errors='ignore') as f:
    c = f.read()

import re
matches = [m.start() for m in re.finditer(r'filter|experience|active|skills', c, re.I)]
print("Total matches:", len(matches))
found_snippets = set()
for pos in matches:
    snip = c[max(0, pos-60):min(len(c), pos+80)].replace('\n', ' ')
    if any(k in snip.lower() for k in ['button', 'input', 'select', 'dropdown', 'toggle', 'switch', 'checkbox']):
        found_snippets.add(snip)

for s in list(found_snippets)[:20]:
    print("->", s)
