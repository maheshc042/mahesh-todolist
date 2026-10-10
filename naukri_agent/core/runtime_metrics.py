"""
Runtime Instrumentation & Performance Diagnostics.

Measures wall-clock time spent across all stages, recommendation collection tabs,
and individual job application steps using high-resolution time.perf_counter().
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class JobTiming:
    job_id: str
    title: str
    goto_s: float = 0.0
    readiness_s: float = 0.0
    btn_detect_s: float = 0.0
    click_s: float = 0.0
    q_detect_s: float = 0.0
    q_answer_s: float = 0.0
    verify_s: float = 0.0
    total_s: float = 0.0

    def summary_line(self, index: int) -> str:
        short_title = (self.title[:35] + "...") if len(self.title) > 38 else self.title
        return (
            f"  Job {index:<2} ({self.job_id:<14}) "
            f"goto:{self.goto_s:4.1f}s | ready:{self.readiness_s:4.1f}s | btn:{self.btn_detect_s:4.1f}s | "
            f"click:{self.click_s:4.1f}s | q_det:{self.q_detect_s:4.1f}s | q_ans:{self.q_answer_s:4.1f}s | "
            f"verify:{self.verify_s:4.1f}s => Total: {self.total_s:5.1f}s"
        )


@dataclass
class TabTiming:
    tab_name: str
    total_s: float = 0.0
    scroll_s: float = 0.0
    parse_s: float = 0.0
    dupe_check_s: float = 0.0
    jobs_found: int = 0


@dataclass
class RuntimeMetrics:
    browser_startup_s: float = 0.0
    login_s: float = 0.0
    resume_switch_s: float = 0.0

    # Recommendation Collection
    tab_timings: dict[str, TabTiming] = field(default_factory=dict)
    total_collection_s: float = 0.0

    # Core Pipeline
    ranking_s: float = 0.0
    planner_s: float = 0.0
    reporting_s: float = 0.0

    # Apply Engine
    job_timings: list[JobTiming] = field(default_factory=list)
    total_apply_phase_s: float = 0.0

    # Notifications & Global
    telegram_s: float = 0.0
    start_perf_counter: float = field(default_factory=time.perf_counter)

    def average_apply_time_s(self) -> float:
        if not self.job_timings:
            return 0.0
        return sum(jt.total_s for jt in self.job_timings) / len(self.job_timings)

    def total_runtime_s(self) -> float:
        return time.perf_counter() - self.start_perf_counter

    def format_duration(self, seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:5.2f}s"
        mins = int(seconds // 60)
        secs = seconds % 60
        return f"{mins:2d}m {secs:04.1f}s"

    def generate_report(self) -> str:
        tot_s = self.total_runtime_s()
        lines: list[str] = [
            "",
            "================ Runtime Summary ================",
            "",
            f"Browser Startup          {self.browser_startup_s:6.1f}s",
            f"Login                    {self.login_s:6.1f}s",
            f"Resume Switch            {self.resume_switch_s:6.1f}s",
            "",
            "Recommendation Collection",
        ]

        if self.tab_timings:
            for tab_name, tt in self.tab_timings.items():
                name_fmt = tab_name.title()
                lines.append(
                    f"  {name_fmt:<20} {tt.total_s:6.1f}s  "
                    f"(scroll: {tt.scroll_s:.1f}s, parse: {tt.parse_s:.1f}s, dupe: {tt.dupe_check_s:.1f}s, jobs: {tt.jobs_found})"
                )
            lines.append(f"  Total Collection     {self.total_collection_s:6.1f}s")
        else:
            lines.append(f"  Total Collection     {self.total_collection_s:6.1f}s")

        lines.extend([
            "",
            f"Ranking                  {self.ranking_s:6.2f}s",
            f"Planner                  {self.planner_s:6.2f}s",
            f"Reporting                {self.reporting_s:6.2f}s",
            "",
            "Apply Engine",
        ])

        if self.job_timings:
            for idx, jt in enumerate(self.job_timings, start=1):
                lines.append(jt.summary_line(idx))
            avg_apply = self.average_apply_time_s()
            lines.append("")
            lines.append(f"Average Apply Time       {avg_apply:6.1f}s")
            lines.append(f"Total Apply Phase        {self.total_apply_phase_s:6.1f}s")
        else:
            lines.append("  (No jobs applied in this run)")

        lines.extend([
            "",
            f"Telegram                 {self.telegram_s:6.1f}s",
            "",
            f"TOTAL                    {self.format_duration(tot_s)}",
            "",
            "=================================================",
            "",
        ])
        return "\n".join(lines)
