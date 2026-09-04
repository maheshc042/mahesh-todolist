import csv
import json
import re

# Load selected jobs
selected_jobs = {}
try:
    with open("scratch/gh_run_33730550784/analysis/selected_jobs.csv", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            selected_jobs[row["job_id"]] = row
except Exception as e:
    print("Error reading selected_jobs.csv:", e)

# Load ranked jobs
ranked_jobs = {}
try:
    with open("scratch/gh_run_33730550784/analysis/ranked_jobs.csv", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            ranked_jobs[row["job_id"]] = row
except Exception as e:
    print("Error reading ranked_jobs.csv:", e)

# Load log events from Run 211
with open("scratch/gh_run_33730550784/logs/agent.log", encoding="utf-8", errors="replace") as f:
    events = [json.loads(l) for l in f if l.strip().startswith("{")]

# Map of job outcomes
outcomes = {}
for e in events:
    jid = e.get("job_id")
    if jid:
        if jid not in outcomes:
            outcomes[jid] = {"events": []}
        outcomes[jid]["events"].append(e)

# Summary of 45 considered jobs from Run 211
# Let's extract the exact list of 45 considered jobs in chronological order
considered_order = []
for e in events:
    jid = e.get("job_id")
    if jid and jid.startswith("reco-") and jid not in considered_order:
        considered_order.append(jid)

print(f"Total considered jobs identified: {len(considered_order)}")

output_data = []
for idx, jid in enumerate(considered_order, 1):
    rj = ranked_jobs.get(jid, {})
    sj = selected_jobs.get(jid, {})
    
    # Gather events
    jevs = outcomes.get(jid, {}).get("events", [])
    ev_names = [x.get("event") for x in jevs]
    
    output_data.append({
        "order": idx,
        "job_id": jid,
        "title": rj.get("title") or sj.get("title"),
        "company": rj.get("company") or sj.get("company"),
        "score": rj.get("score") or sj.get("score"),
        "rank": rj.get("rank") or sj.get("rank"),
        "reasons": rj.get("reasons") or sj.get("reasons"),
        "url": rj.get("url") or sj.get("url"),
        "event_names": ev_names,
    })

with open("scratch/considered_jobs_45.json", "w", encoding="utf-8") as out:
    json.dump(output_data, out, indent=2)

print("Saved considered_jobs_45.json")
