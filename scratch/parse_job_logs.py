import json
import os
import re

log_path = "scratch/gh_run_33730550784/logs/agent.log"

with open(log_path, encoding="utf-8", errors="replace") as f:
    events = [json.loads(line) for line in f if line.strip().startswith("{")]

# Let's find ranking and job processing logs
ranked_events = [e for e in events if e.get("event") in ["ranking.evaluated", "ranking.completed", "planner.plan_ready", "job.considered", "apply.attempt", "apply.success", "apply.failed", "apply.external", "apply.already_applied", "apply.needs_review"]]
print(f"Total events of interest: {len(ranked_events)}")

# Collect all job-specific logs
job_logs = {}
for e in events:
    jid = e.get("job_id")
    if jid:
        if jid not in job_logs:
            job_logs[jid] = []
        job_logs[jid].append(e)

print(f"Unique jobs with logs: {len(job_logs)}")

# Inspect the 45 considered jobs from Run 211
with open("scratch/check_jobs_detail.json", "w", encoding="utf-8") as out:
    json.dump(job_logs, out, indent=2, default=str)

print("Saved job logs to scratch/check_jobs_detail.json")
