"""
Application Planner.

Acts as the decision-making bridge between Job Discovery/Collection and the Apply Engine.

Pipeline:
  Collected Jobs (list[Job])
          ↓
     Hard Filter (HardFilter.evaluate)
          ↓
  Rejected Jobs & Eligible Jobs
          ↓
  Ranking Engine (RankingEngine.rank_jobs)
          ↓
  Top N Selection (Daily Cap Limit, Default = 35)
          ↓
  ApplicationPlan (Selected Jobs, Overflow Jobs, Rejected Jobs, Stats, Human Report)

Design principles:
- Pure Python: No Playwright, no async IO, no DB connection.
- Decoupled Execution: The planner ONLY decides; it never applies jobs.
- Explainable & Auditable: Produces structured stats and human-readable daily reports.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import FilterRules
from .models import FilterDecision, Job, SkipReason
from .ranking import CandidateProfile, HardFilter, RankedJob, RankingEngine, RankingWeights


@dataclass(slots=True)
class RejectedJobInfo:
    """Audit record for a job rejected during the Hard Filter phase."""

    job: Job
    reason: SkipReason | None
    detail: str


@dataclass(slots=True)
class ApplicationPlanStats:
    """Summary metrics for a generated ApplicationPlan."""

    collected_count: int
    rejected_count: int
    eligible_count: int
    selected_count: int
    overflow_count: int
    rejection_reasons: dict[str, int]
    top_score: float | None = None
    cutoff_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "collected": self.collected_count,
            "rejected": self.rejected_count,
            "eligible": self.eligible_count,
            "selected": self.selected_count,
            "overflow": self.overflow_count,
            "rejection_reasons": self.rejection_reasons,
            "top_score": self.top_score,
            "cutoff_score": self.cutoff_score,
        }


@dataclass(slots=True)
class ApplicationPlan:
    """
    Complete decision plan specifying which jobs to apply for today,
    which eligible jobs exceeded the daily cap, and audit details for rejected jobs.
    """

    selected_jobs: list[RankedJob]
    overflow_jobs: list[RankedJob]
    rejected_jobs: list[RejectedJobInfo]
    stats: ApplicationPlanStats
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def eligible_jobs(self) -> list[RankedJob]:
        """All eligible ranked jobs (selected + overflow)."""
        return self.selected_jobs + self.overflow_jobs

    def generate_report(self) -> str:
        """Generate a clean, human-readable daily application plan report."""
        s = self.stats
        lines: list[str] = [
            "==================================================",
            "           DAILY APPLICATION PLAN REPORT          ",
            "==================================================",
            f" Generated At: {self.generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f" Total Collected : {s.collected_count}",
            f" Hard Rejected   : {s.rejected_count}",
            f" Eligible Jobs   : {s.eligible_count}",
            f" Selected (Cap)  : {s.selected_count}",
            f" Overflow (Skip) : {s.overflow_count}",
            "--------------------------------------------------",
        ]

        if s.top_score is not None:
            cutoff = f"{s.cutoff_score:.1f}" if s.cutoff_score is not None else "N/A"
            lines.append(f" Top Score: {s.top_score:.1f}/100  |  Cutoff Score: {cutoff}/100")
            lines.append("--------------------------------------------------")

        if s.rejection_reasons:
            lines.append("Rejection Breakdown:")
            for reason, count in sorted(s.rejection_reasons.items(), key=lambda x: -x[1]):
                lines.append(f"  • {reason}: {count}")
            lines.append("--------------------------------------------------")

        if self.selected_jobs:
            lines.append(f"Top Selected Roles ({len(self.selected_jobs)}):")
            for idx, rjob in enumerate(self.selected_jobs[:10], start=1):
                j = rjob.job
                lines.append(
                    f" {idx:2d}. [{rjob.score:5.1f}/100] {j.title[:38]} @ {j.company[:25]}"
                )
                if rjob.reasons:
                    top_reasons = " | ".join(rjob.reasons[:2])
                    lines.append(f"     └─ {top_reasons}")
            if len(self.selected_jobs) > 10:
                lines.append(f"     ... and {len(self.selected_jobs) - 10} more selected jobs.")
        else:
            lines.append(" No jobs selected for application today.")

        lines.append("==================================================")
        return "\n".join(lines)


class ApplicationPlanner:
    """
    Pure Python Application Planner.

    Executes Hard Filtering, runs the Deterministic Ranking Engine, applies daily cap
    limits, and packages structured outputs for the downstream Apply Engine.
    """

    def __init__(
        self,
        candidate: CandidateProfile,
        rules: FilterRules | None = None,
        weights: RankingWeights | None = None,
        daily_limit: int = 35,
    ) -> None:
        self.candidate = candidate
        self.rules = rules
        self.weights = weights or RankingWeights()
        self.daily_limit = daily_limit
        self.hard_filter = HardFilter(rules) if rules else None
        self.ranking_engine = RankingEngine(candidate, rules=rules, weights=self.weights)

    def create_plan(self, jobs: list[Job], custom_daily_limit: int | None = None) -> ApplicationPlan:
        """
        Transform raw collected jobs into a structured, prioritized ApplicationPlan.
        """
        cap = custom_daily_limit if custom_daily_limit is not None else self.daily_limit
        rejected_list: list[RejectedJobInfo] = []
        eligible_raw: list[Job] = []
        rejection_reasons_count: dict[str, int] = {}

        # 1. Hard Filter Phase
        for job in jobs:
            if self.hard_filter is not None:
                decision = self.hard_filter.evaluate(job)
                if not decision.passed:
                    reason_key = decision.reason.value if decision.reason else "other"
                    rejection_reasons_count[reason_key] = rejection_reasons_count.get(reason_key, 0) + 1
                    rejected_list.append(
                        RejectedJobInfo(
                            job=job,
                            reason=decision.reason,
                            detail=decision.detail,
                        )
                    )
                    continue
            eligible_raw.append(job)

        # 2. Ranking Phase
        t_rk_0 = time.perf_counter()
        ranked_eligible = self.ranking_engine.rank_jobs(eligible_raw)
        self.last_ranking_time_s = time.perf_counter() - t_rk_0

        # 3. Partition into Selected vs Overflow based on Daily Cap Limit
        selected = ranked_eligible[:cap]
        overflow = ranked_eligible[cap:]

        # 4. Summary Statistics
        top_score = selected[0].score if selected else None
        cutoff_score = selected[-1].score if selected else None

        stats = ApplicationPlanStats(
            collected_count=len(jobs),
            rejected_count=len(rejected_list),
            eligible_count=len(ranked_eligible),
            selected_count=len(selected),
            overflow_count=len(overflow),
            rejection_reasons=rejection_reasons_count,
            top_score=top_score,
            cutoff_score=cutoff_score,
        )

        return ApplicationPlan(
            selected_jobs=selected,
            overflow_jobs=overflow,
            rejected_jobs=rejected_list,
            stats=stats,
        )
