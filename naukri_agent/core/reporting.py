"""
Report Generation and Dataset Exporter.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from .application_planner import ApplicationPlan
from .models import ApplyOutcome, Job
from .ranking import RankedJob

log = get_logger(__name__)

RANKED_JOBS_CSV_HEADER = ["rank", "score", "job_id", "tab", "position", "company", "title", "url", "reasons"]
OUTCOME_JOBS_CSV_HEADER = ["job_id", "status", "company", "title", "url", "detail", "reason", "attempts"]

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

class ReportExporter:
    def __init__(self, output_dir: Path, run_id: str = "") -> None:
        self.output_dir = output_dir
        self.run_id = run_id
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export_plan_reports(self, plan: ApplicationPlan, collected_jobs: list[Job], profile_name: str = "") -> None:
        try:
            with (self.output_dir / "collected_jobs.csv").open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["job_id", "tab", "position", "total_jobs_in_tab", "company", "title", "url", "scraped_at"])
                for j in collected_jobs:
                    w.writerow([j.job_id, j.recommendation_tab, j.recommendation_position or "", j.total_jobs_in_tab or "", j.company, j.title, j.url, j.scraped_at.isoformat()])

            self._write_ranked_jobs_csv(self.output_dir / "ranked_jobs.csv", plan.eligible_jobs)
            self._write_ranked_jobs_csv(self.output_dir / "selected_jobs.csv", plan.selected_jobs)

            with (self.output_dir / "rejected_jobs.csv").open("w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["job_id", "reason", "detail", "company", "title", "url"])
                for rinfo in plan.rejected_jobs:
                    reason_str = rinfo.reason.value if rinfo.reason else "other"
                    w.writerow([rinfo.job.job_id, reason_str, rinfo.detail, rinfo.job.company, rinfo.job.title, rinfo.job.url])

        except Exception as exc:
            log.warning("reporting.plan_export_failed", error=str(exc)[:200])

    def export_outcome_reports(self, applied: list[tuple[Job, ApplyOutcome]], failed: list[tuple[Job, ApplyOutcome]], profile_name: str = "") -> None:
        try:
            self._write_outcomes_csv(self.output_dir / "applied_jobs.csv", applied)
            self._write_outcomes_csv(self.output_dir / "failed_jobs.csv", failed)
        except Exception as exc:
            log.warning("reporting.outcome_export_failed", error=str(exc)[:200])

    def export_summary_json(self, plan: ApplicationPlan, applied_count: int, failed_count: int, profile_name: str, dry_run: bool, duration_seconds: float = 0.0) -> None:
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
            summary_file = self.output_dir / "summary.json"
            summary_file.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("reporting.summary_json_export_failed", error=str(exc)[:200])

    def _write_ranked_jobs_csv(self, path: Path, ranked_jobs: list[RankedJob]) -> None:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(RANKED_JOBS_CSV_HEADER)
            for rj in ranked_jobs:
                reasons_str = " | ".join(rj.reasons)
                writer.writerow([rj.rank or "", f"{rj.score:.2f}", rj.job.job_id, rj.job.recommendation_tab or "", rj.job.recommendation_position or "", rj.job.company, rj.job.title, rj.job.url, reasons_str])

    def _write_outcomes_csv(self, path: Path, items: list[tuple[Job, ApplyOutcome]]) -> None:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(OUTCOME_JOBS_CSV_HEADER)
            for job, outcome in items:
                writer.writerow([job.job_id, outcome.status.value, job.company, job.title, job.url, outcome.detail or "", outcome.reason.value if outcome.reason else "", outcome.attempts])
