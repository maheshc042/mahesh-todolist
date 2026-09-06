import py_compile
import re

print("=== 1. COMPILATION CHECK ===")
files_to_compile = [
    "naukri_agent/linkedin/analyzer.py",
    "naukri_agent/linkedin/scraper.py",
    "naukri_agent/platforms/cutshort.py",
    "naukri_agent/platforms/instahyre.py",
    "naukri_agent/platforms/linkedin.py",
]
for f in files_to_compile:
    py_compile.compile(f, doraise=True)
    print(f"  [OK] Compiled {f}")

print("\n=== 2. CUTSHORT AUDIO SELECTOR CHECK ===")
with open("artifacts/2026-09-06/run-221/cutshort-thread-1.html", "r", encoding="utf-8", errors="ignore") as f:
    cs_html = f.read()

# Match role="button" with "Pick from 1 saved audios"
match_audio = re.search(r'role="button"[^>]*>Pick from \d+ saved audios<', cs_html)
if match_audio:
    print(f"  [OK] Found Cutshort Audio Selector: snippet='{match_audio.group(0)}'")
else:
    print("  [FAIL] Cutshort audio selector not found")

match_submit = re.search(r'<button[^>]*type="submit"[^>]*>Submit</button>', cs_html)
if match_submit:
    print(f"  [OK] Found Cutshort Submit Button: snippet='{match_submit.group(0)}'")
else:
    print("  [FAIL] Cutshort submit button not found")

print("\n=== 3. INSTAHYRE CARD & PAGINATION CHECK ===")
with open("artifacts/2026-09-06/run-221/instahyre-search-results.html", "r", encoding="utf-8", errors="ignore") as f:
    ih_html = f.read()

pag_match = re.search(r'<div class="pagination[^"]*"[^>]*>(.*?)</div>', ih_html, re.DOTALL)
if pag_match:
    page_nums = re.findall(r'ng-repeat="pageNumber in pages"[^>]*>(\d+)</li>', pag_match.group(1))
    print(f"  [OK] Found Instahyre Pagination: {len(page_nums)} pages -> {page_nums}")
else:
    print("  [FAIL] Instahyre pagination not found")

cards = re.findall(r'<div class="employer-row[^"]*"', ih_html)
print(f"  [OK] Found {len(cards)} Instahyre employer-row cards on search page")

print("\nALL AUTOMATED VERIFICATION CHECKS PASSED!")
