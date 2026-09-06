import re

with open('artifacts/2026-09-06/run-221/cutshort-dashboard.html', 'r', encoding='utf-8', errors='ignore') as f:
    html = f.read()

pos = html.find('data-intercom-target="expRange-filter"')
if pos != -1:
    print("=== expRange-filter snippet ===")
    print(html[pos-100:pos+500])

pos2 = html.find('data-intercom-target="hiringActivityOnJob-filter"')
if pos2 != -1:
    print("\n=== hiringActivityOnJob-filter snippet ===")
    print(html[pos2-100:pos2+500])

pos3 = html.find('role="switch"')
if pos3 != -1:
    print("\n=== role=switch snippet ===")
    print(html[pos3-200:pos3+300])
