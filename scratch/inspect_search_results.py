with open('artifacts/2026-09-01/run-198/instahyre-search-results.html', encoding='utf-8') as f:
    content = f.read()

import re
print('Length of search results HTML:', len(content))
# Look for search heading, clear filters, reset, or url
matches = re.findall(r'class="[^"]*employer-row[^"]*"', content)
print(f'Employer rows in search results: {len(matches)}')
has_clear = "Clear" in content or "reset" in content.lower()
print('Has clear/reset:', has_clear)
# Look for search section state
print('Show filters in HTML:', 'show-filters' in content)
