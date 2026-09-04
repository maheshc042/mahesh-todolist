import re
from pathlib import Path

for i in range(1, 9):
    p = Path(f'artifacts/2026-09-04/run-214/cutshort-thread-{i}.html')
    if not p.exists():
        continue
    content = p.read_text(encoding='utf-8')
    forms = re.findall(r'<form[\s\S]*?</form>', content)
    print(f'=== THREAD {i} (Forms: {len(forms)}) ===')
    for f in forms:
        btns = re.findall(r'<button[^>]*>[\s\S]*?</button>', f)
        for b in btns:
            print('  Button in Form:', b)
        inputs = re.findall(r'<textarea[^>]*>', f) + re.findall(r'<input[^>]*>', f)
        for inp in inputs:
            print('  Input in Form:', inp)

