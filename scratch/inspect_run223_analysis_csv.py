import csv
import json

print("=== 1. PLANNER STATS FROM SUMMARY ===")
with open('analysis/summary.json') as f:
    s = json.load(f)
print(json.dumps(s.get('plan_stats', {}), indent=2))

print("\n=== 2. REJECTED JOBS SAMPLE BY REASON ===")
with open('analysis/rejected_jobs.csv', encoding='utf-8', errors='ignore') as f:
    r = list(csv.DictReader(f))
    print(f"Total rejected jobs: {len(r)}")
    by_reason = {}
    for row in r:
        by_reason.setdefault(row['reason'], []).append(row)
    for rsn, items in by_reason.items():
        print(f"\nReason: {rsn} ({len(items)} jobs)")
        for item in items[:4]:
            print(f"  - {item.get('title')} @ {item.get('company')} | exp: {item.get('experience_text')} | detail: {item.get('detail')}")

print("\n=== 3. TOP RANKED SELECTED JOBS ===")
with open('analysis/selected_jobs.csv', encoding='utf-8', errors='ignore') as f:
    sel = list(csv.DictReader(f))
    print(f"Total selected jobs: {len(sel)}")
    for item in sel[:10]:
        print(f"  Score: {item.get('score')} | {item.get('title')} @ {item.get('company')} | Loc: {item.get('location')} | Exp: {item.get('experience_text')}")

print("\n=== 4. FAILED JOBS ===")
with open('analysis/failed_jobs.csv', encoding='utf-8', errors='ignore') as f:
    fl = list(csv.DictReader(f))
    print(f"Total failed jobs: {len(fl)}")
    for item in fl:
        print(f"  [{item.get('job_id')}] {item.get('title')} @ {item.get('company')} | Error: {item.get('detail')}")
