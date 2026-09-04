import json
import re
import csv

# 1. Parse the 45 jobs from runtime summary in agent.log
log_path = "scratch/gh_run_33730550784/logs/agent.log"
with open(log_path, encoding="utf-8", errors="replace") as f:
    text = f.read()

# find Apply Engine block
apply_lines = re.findall(r"Job (\d+)\s+\((reco-[\w\d]+)\)\s+goto:.*?=> Total:\s+([\d\.]+s)", text)
print(f"Parsed {len(apply_lines)} jobs from Apply Engine report:")

# Load DB applications
with open("scratch/run_211_applications_full.json", encoding="utf-8") as f:
    db_apps = {row["job_id"]: row for row in json.load(f)}

# Load ranked jobs
ranked_map = {}
try:
    with open("scratch/gh_run_33730550784/analysis/ranked_jobs.csv", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            ranked_map[row["job_id"]] = row
except Exception as e:
    print("ranked_jobs.csv:", e)

# Load selected jobs
selected_map = {}
try:
    with open("scratch/gh_run_33730550784/analysis/selected_jobs.csv", encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            selected_map[row["job_id"]] = row
except Exception as e:
    print("selected_jobs.csv:", e)

# Load HTML verification map
html_success_map = {
    "reco-020926927973": True,
    "reco-020926036168": True,
    "reco-080726015528": True,
    "reco-020926025539": True,
    "reco-020926928154": True,
    "reco-020926915993": True,
    "reco-210426045216": True,
    "reco-010926035891": True,
    "reco-020926013547": True,
    "reco-020926920887": True,
    "reco-160626031119": True,
    "reco-020926927265": True,
}

full_report_items = []
for order, jid, dur in apply_lines:
    db = db_apps.get(jid, {})
    rk = ranked_map.get(jid, {}) or selected_map.get(jid, {})
    
    title = db.get("title") or rk.get("title") or "Unknown"
    company = db.get("company") or rk.get("company") or "Unknown"
    loc = db.get("location") or "N/A"
    exp = db.get("experience_text") or f"{db.get('min_experience')}-{db.get('max_experience')} yrs"
    tags = db.get("tags") or []
    status = db.get("status") or "failed (unrecorded in app table)"
    reason = db.get("reason")
    detail = db.get("detail")
    score = rk.get("score") or "N/A"
    reasons = rk.get("reasons") or "N/A"
    is_false_failure = html_success_map.get(jid, False)
    
    full_report_items.append({
        "order": int(order),
        "job_id": jid,
        "duration": dur,
        "title": title,
        "company": company,
        "location": loc,
        "experience": exp,
        "tags": tags,
        "status": status,
        "reason": reason,
        "detail": detail,
        "score": score,
        "reasons": reasons,
        "is_false_failure": is_false_failure,
        "actual_outcome": "applied (confirmed on page)" if is_false_failure else (status if status != "failed" else "failed / error")
    })

with open("scratch/evaluated_45_jobs.json", "w", encoding="utf-8") as f:
    json.dump(full_report_items, f, indent=2)

print(f"Successfully processed {len(full_report_items)} jobs into scratch/evaluated_45_jobs.json")
