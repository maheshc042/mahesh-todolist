with open('artifacts/2026-09-04/run-214/cutshort-thread-5.html', encoding='utf-8') as f:
    content = f.read()

import re

# Find form or container of questionnaire
forms = re.findall(r'<form[\s\S]*?</form>', content)
print('Forms found:', len(forms))
if forms:
    print('Form snippet (first 1000 chars):')
    print(forms[0][:1000])
    print('...')
    print('Form submit buttons:')
    submits = re.findall(r'<button[^>]*>[\s\S]*?</button>', forms[0])
    for s in submits:
        if 'submit' in s.lower() or 'button' in s.lower():
            print('  Btn:', s[:200])
else:
    # Look for questionnaire container
    print('No <form> tag, searching for fieldset parent')
    m = re.search(r'(<div[^>]*class="[^"]*questionnaire[^"]*"[\s\S]*?)(?:<div class="sc-)', content)
    if m:
        print('Found questionnaire div:', m.group(1)[:500])
