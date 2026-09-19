"""
Report Generation and Dataset Exporter.
"""
from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from .application_planner import ApplicationPlan
from .models import ApplyOutcome, Job
from .ranking import RankedJob

log = get_logger(__name__)

RANKED_JOBS_CSV_HEADER = ["rank", "score", "job_id", "tab", "position", "company", "title", "url", "applicants", "reasons"]
OUTCOME_JOBS_CSV_HEADER = ["job_id", "status", "company", "title", "url", "detail", "reason", "attempts"]

def _now_utc() -> datetime:
    return datetime.now(UTC)

class ReportExporter:
    def __init__(self, output_dir: Path, run_id: str = "") -> None:
        self.output_dir = output_dir
        self.run_id = run_id
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _names(self, base: str, platform: str = "") -> Path:
        """Per-platform filenames so phases stop overwriting each other."""
        slug = "".join(ch if ch.isalnum() else "_" for ch in (platform or "").lower()).strip("_")
        return self.output_dir / (f"{slug}_{base}" if slug else base)

    def export_plan_reports(self, plan: ApplicationPlan, collected_jobs: list[Job], profile_name: str = "", platform: str = "") -> None:
        try:
            with self._names("collected_jobs.csv", platform).open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["job_id", "tab", "position", "total_jobs_in_tab", "company", "title", "url", "applicants", "scraped_at"])
                for j in collected_jobs:
                    scraped = j.scraped_at.isoformat() if j.scraped_at else ""
                    w.writerow([j.job_id, j.recommendation_tab, j.recommendation_position or "", j.total_jobs_in_tab or "", j.company, j.title, j.url, j.applicant_count if j.applicant_count is not None else "", scraped])

            self._write_ranked_jobs_csv(self._names("ranked_jobs.csv", platform), plan.eligible_jobs)
            self._write_ranked_jobs_csv(self._names("selected_jobs.csv", platform), plan.selected_jobs)

            with self._names("rejected_jobs.csv", platform).open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["job_id", "reason", "detail", "company", "title", "url"])
                for rinfo in plan.rejected_jobs:
                    reason_str = rinfo.reason.value if rinfo.reason else "other"
                    w.writerow([rinfo.job.job_id, reason_str, rinfo.detail, rinfo.job.company, rinfo.job.title, rinfo.job.url])

        except Exception as exc:
            log.warning("reporting.plan_export_failed", error=str(exc)[:200])

    def export_outcome_reports(self, applied: list[tuple[Job, ApplyOutcome]], failed: list[tuple[Job, ApplyOutcome]], profile_name: str = "", platform: str = "") -> None:
        try:
            self._write_outcomes_csv(self._names("applied_jobs.csv", platform), applied)
            self._write_outcomes_csv(self._names("failed_jobs.csv", platform), failed)
        except Exception as exc:
            log.warning("reporting.outcome_export_failed", error=str(exc)[:200])

    def export_summary_json(self, plan: ApplicationPlan, applied_count: int, failed_count: int, profile_name: str, dry_run: bool, duration_seconds: float = 0.0, platform: str = "") -> None:
        try:
            summary_data: dict[str, Any] = {
                "profile": profile_name,
                "run_id": self.run_id,
                "dry_run": dry_run,
                "duration_seconds": round(duration_seconds, 2),
                "generated_at": _now_utc().isoformat(),
                "plan_stats": {
                    "collected": plan.stats.collected_count,
                    "rejected": len(plan.rejected_jobs),
                    "eligible": len(plan.eligible_jobs),
                    "selected": len(plan.selected_jobs),
                    "overflow": len(plan.overflow_jobs),
                    "rejection_reasons": plan.stats.rejection_reasons,
                    "top_score": plan.selected_jobs[0].score if plan.selected_jobs else 0.0,
                    "cutoff_score": plan.selected_jobs[-1].score if plan.selected_jobs else 0.0,
                },
                "applied_count": applied_count,
                "failed_count": failed_count,
            }
            summary_file = self._names("summary.json", platform)
            summary_file.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("reporting.summary_json_export_failed", error=str(exc)[:200])

    def _write_ranked_jobs_csv(self, path: Path, ranked_jobs: list[RankedJob]) -> None:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(RANKED_JOBS_CSV_HEADER)
            for rj in ranked_jobs:
                reasons_str = " | ".join(rj.reasons)
                writer.writerow([rj.rank or "", f"{rj.score:.2f}", rj.job.job_id, rj.job.recommendation_tab or "", rj.job.recommendation_position or "", rj.job.company, rj.job.title, rj.job.url, rj.job.applicant_count if rj.job.applicant_count is not None else "", reasons_str])

    def _write_outcomes_csv(self, path: Path, items: list[tuple[Job, ApplyOutcome]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(OUTCOME_JOBS_CSV_HEADER)
            for job, outcome in items:
                writer.writerow([job.job_id, outcome.status.value, job.company, job.title, job.url, outcome.detail or "", outcome.reason.value if outcome.reason else "", outcome.attempts])
