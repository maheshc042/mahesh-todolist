import re
with open('artifacts/2026-09-06/run-221/instahyre-search-results.html', 'r', encoding='utf-8', errors='ignore') as f:
    html = f.read()

pos = html.find('ng-repeat="pageNumber in pages"')
if pos != -1:
    print("Pagination container:")
    print(html[pos-300:pos+300])
