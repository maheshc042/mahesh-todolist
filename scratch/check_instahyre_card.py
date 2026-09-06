with open('artifacts/2026-09-06/run-221/instahyre-search-results.html', 'r', encoding='utf-8') as f:
    content = f.read()

pos = content.find('Altimate AI - Backend Engineer')
# look backwards for the start of the card container
start_div = content.rfind('<div class="employer-row', 0, pos)
print("Distance to employer-row:", pos - start_div if start_div != -1 else "NOT FOUND")
if start_div != -1:
    print("employer-row tag:", content[start_div:start_div+150])
else:
    # What is the outer div?
    start_any = content.rfind('<div id="opportunity-', 0, pos)
    print("opportunity- div:", start_any)
    if start_any != -1:
        print(content[start_any:start_any+150])
