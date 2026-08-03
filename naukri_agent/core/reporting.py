"""
Report Generation and Dataset Exporter.

Design decisions:

- **Decoupled Reporter.** Separates CSV/JSON dataset generation and reporting logic from
  orchestration flow, keeping the Orchestrator focused purely on pipeline coordination.
- **Unified CSV Formatter.** Uses DRY helper functions to write ranked job datasets
  (ranked_jobs.csv, selected_jobs.csv, rejected_jobs.csv) and apply outcome datasets
  (applied_jobs.csv, failed_jobs.csv).
- **Enriched Summary Output.** Generates an enriched `summary.json` incorporating profile,
  run_id, duration_seconds, dry_run flag, plan statistics, and application counts.
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

# Standard CSV Headers
RANKED_JOBS_CSV_HEADER = [
    "rank",
    "score",
    "job_id",
    "tab",
    "position",
    "company",
    "title",
    "url",
    "reasons",
]

OUTCOME_JOBS_CSV_HEADER = [
    "job_id",
    "status",
    "company",
    "title",
    "url",
    "detail",
    "reason",
    "attempts",
]


def _now_utc() -> datetime:
    """Centralized UTC timestamp generator."""
    return datetime.now(timezone.utc)


class ReportExporter:
    def __init__(self, output_dir: Path, run_id: str = "") -> None:
        self.output_dir = output_dir
        self.run_id = run_id
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def export_plan_reports(self, plan: ApplicationPlan, profile_name: str = "") -> None:
        """Export all Application Plan CSV files (ranked, selected, rejected)."""
        try:
            self._write_ranked_jobs_csv(self.output_dir / "ranked_jobs.csv", plan.ranked_jobs)
            self._write_ranked_jobs_csv(self.output_dir / "selected_jobs.csv", plan.selected_jobs)
            self._write_ranked_jobs_csv(self.output_dir / "rejected_jobs.csv", plan.rejected_jobs)
            log.info(
                "reporting.plan_reports_exported",
                output_dir=str(self.output_dir),
                run_id=self.run_id,
                profile=profile_name,
                selected=len(plan.selected_jobs),
                rejected=len(plan.rejected_jobs),
            )
        except Exception as exc:
            log.warning(
                "reporting.plan_export_failed",
                output_dir=str(self.output_dir),
                run_id=self.run_id,
                error=str(exc)[:200],
            )

    def export_outcome_reports(
        self,
        applied: list[tuple[Job, ApplyOutcome]],
        failed: list[tuple[Job, ApplyOutcome]],
        profile_name: str = "",
    ) -> None:
        """Export Apply execution outcome CSV files (applied_jobs, failed_jobs)."""
        try:
            self._write_outcomes_csv(self.output_dir / "applied_jobs.csv", applied)
            self._write_outcomes_csv(self.output_dir / "failed_jobs.csv", failed)
            log.info(
                "reporting.outcome_reports_exported",
                output_dir=str(self.output_dir),
                run_id=self.run_id,
                profile=profile_name,
                applied=len(applied),
                failed=len(failed),
            )
        except Exception as exc:
            log.warning(
                "reporting.outcome_export_failed",
                output_dir=str(self.output_dir),
                run_id=self.run_id,
                error=str(exc)[:200],
            )

    def export_summary_json(
        self,
        plan: ApplicationPlan,
        applied_count: int,
        failed_count: int,
        profile_name: str,
        dry_run: bool,
        duration_seconds: float = 0.0,
    ) -> None:
        """Export enriched summary.json report incorporating run metadata (P1-4)."""
        try:
            summary_data: dict[str, Any] = {
                "profile": profile_name,
                "run_id": self.run_id,
                "dry_run": dry_run,
                "duration_seconds": round(duration_seconds, 2),
                "generated_at": _now_utc().isoformat(),
                "plan_stats": {
                    "collected": plan.total_collected,
                    "rejected": len(plan.rejected_jobs),
                    "eligible": len(plan.eligible_jobs),
                    "selected": len(plan.selected_jobs),
                    "overflow": len(plan.overflow_jobs),
                    "rejection_reasons": plan.rejection_reasons,
                    "top_score": plan.selected_jobs[0].total_score if plan.selected_jobs else 0.0,
                    "cutoff_score": plan.selected_jobs[-1].total_score if plan.selected_jobs else 0.0,
                },
                "applied_count": applied_count,
                "failed_count": failed_count,
            }
            summary_file = self.output_dir / "summary.json"
            summary_file.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")
            log.info(
                "reporting.summary_json_exported",
                path=str(summary_file),
                run_id=self.run_id,
                profile=profile_name,
            )
        except Exception as exc:
            log.warning(
                "reporting.summary_json_export_failed",
                output_dir=str(self.output_dir),
                run_id=self.run_id,
                error=str(exc)[:200],
            )

    def _write_ranked_jobs_csv(self, path: Path, ranked_jobs: list[RankedJob]) -> None:
        """Unified writer for RankedJob collections (DRY P1-2)."""
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(RANKED_JOBS_CSV_HEADER)
            for rj in ranked_jobs:
                reasons_str = " | ".join(rj.breakdown.human_explanations)
                writer.writerow(
                    [
                        rj.rank or "",
                        f"{rj.total_score:.2f}",
                        rj.job.job_id,
                        rj.job.recommendation_tab or "",
                        rj.job.recommendation_position or "",
                        rj.job.company,
                        rj.job.title,
                        rj.job.url,
                        reasons_str,
                    ]
                )

    def _write_outcomes_csv(
        self, path: Path, items: list[tuple[Job, ApplyOutcome]]
    ) -> None:
        """Unified writer for Job + ApplyOutcome collections (DRY P1-2)."""
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(OUTCOME_JOBS_CSV_HEADER)
            for job, outcome in items:
                writer.writerow(
                    [
                        job.job_id,
                        outcome.status.value,
                        job.company,
                        job.title,
                        job.url,
                        outcome.detail or "",
                        outcome.reason.value if outcome.reason else "",
                        outcome.attempts,
                    ]
                )
