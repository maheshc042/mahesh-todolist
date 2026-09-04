with open('artifacts/2026-09-04/run-214/cutshort-thread-2.html', encoding='utf-8') as f:
    content = f.read()

import re

forms = re.findall(r'<form[\s\S]*?</form>', content)
print('Forms in thread 2:', len(forms))
if forms:
    print(forms[0][:1500])
    submits = re.findall(r'<button[^>]*>[\s\S]*?</button>', forms[0])
    print('Submit buttons in thread 2:', submits)
