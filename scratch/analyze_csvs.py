import csv
import json

files = ['ranked_jobs.csv', 'selected_jobs.csv', 'rejected_jobs.csv', 'applied_jobs.csv', 'failed_jobs.csv']
for name in files:
    path = f'scratch/gh_run_33730550784/analysis/{name}'
    try:
        with open(path, mode='r', encoding='utf-8', errors='replace') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            print(f"=== {name} (Total rows: {len(rows)}) ===")
            if rows:
                print("  Columns:", list(rows[0].keys()))
                print("  Sample row 1:", json.dumps(rows[0], indent=2))
    except Exception as e:
        print(f"{name}: error {e}")
