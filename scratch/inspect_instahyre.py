import re

with open('artifacts/2026-09-04/run-214/instahyre-feed.html', encoding='utf-8') as f:
    content = f.read()

matches = re.findall(r'class="[^"]*employer-row[^"]*"', content)
print(f'Employer rows in run-214: {len(matches)}')
names = re.findall(r'class="company-name"[^>]*>([^<]+)<', content)
print('Company names:', names)

# Check search section
has_search_section = "job-search-heading" in content or "Search other jobs" in content
print('Has search section:', has_search_section)
