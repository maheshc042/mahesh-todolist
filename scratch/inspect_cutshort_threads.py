import glob
import re

files = glob.glob('artifacts/**/cutshort-thread-*.html', recursive=True)
print(f'Found {len(files)} cutshort thread dumps')

for fpath in files:
    with open(fpath, encoding='utf-8') as f:
        content = f.read()
    print(f'=== {fpath} (len={len(content)}) ===')
    textareas = re.findall(r'<textarea[^>]*>', content)
    print('  Textareas:', len(textareas), textareas[:3])
    labels = re.findall(r'<label[^>]*>([^<]+)</label>', content)
    if labels:
        print('  Labels:', labels[:5])
    legends = re.findall(r'<legend[^>]*>([^<]+)</legend>', content)
    if legends:
        print('  Legends:', legends[:5])
    has_submit = 'type="submit"' in content or 'Submit' in content
    print('  Has submit:', has_submit)
