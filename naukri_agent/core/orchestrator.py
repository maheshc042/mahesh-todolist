"""
Run orchestrator.

Owns the whole lifecycle of one run:

    open DB -> start run row -> launch browser -> login
      for each profile (priority order):
          swap resume -> for each search: scrape -> filter -> apply -> persist
      -> finish run row -> notify -> tear down

Design decisions:

- **One browser, one page, sequential applies.** Naukri aggressively throttles
  parallel sessions from one account, and concurrent applies make the "already
  applied" check racy. Throughput is not the goal; not getting banned is.
- **Safety valves compose.** A run stops early on: daily cap reached, per-profile
  cap reached, N consecutive failures (default 5 → likely a markup change or a
  block), or the global wall-clock timeout. Each one is recorded distinctly so
  the summary explains itself.
- **Every outcome is persisted, including skips.** That is what makes the next
  run cheap: `known_job_ids()` means a job is evaluated once per dedupe window.
- **Failures never escape a job.** `_process_job` catches everything, screenshots
  it, records FAILED and moves on. Only fatal errors (bad credentials, captcha)
  abort the run.
- **Session recovery mid-run.** If a page shows logged-out markers we
  re-authenticate once and retry that job instead of losing the remaining work.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..browser.artifacts import ArtifactStore
from ..browser.manager import BrowserManager
from ..browser.resilience import FatalAgentError, first_visible
from ..config import AgentConfig, FilterRules, JobProfile, NaukriAccount, Settings, PROJECT_ROOT
from ..core.answers import AnswerEngine
from ..core.application_planner import ApplicationPlan, ApplicationPlanner
from ..core.cold_email import ColdEmailer
from ..core.reporting import ReportExporter
from ..core.filters import FilterEngine
from ..core.models import (
    ApplicationStatus,
    ApplyOutcome,
    Job,
    RunStats,
    RunStatus,
    SkipReason,
)
from ..core.ranking import CandidateProfile, RankedJob, RankingWeights
from ..core.runtime_metrics import RuntimeMetrics
from ..db.repository import Repository
from ..logging_setup import bind_context, clear_context, get_logger
from ..naukri import selectors as S
from ..naukri.apply import ApplyEngine
from ..naukri.auth import NaukriAuth
from ..naukri.naukri_api import NaukriApiClient, extract_naukri_token
from ..naukri.profile import ProfileRefresher
from ..naukri.resume import ResumeManager
from ..naukri.search import JobSearcher
from ..notify.notifier import build_notifier, format_run_summary

log = get_logger(__name__)


class StopRun(Exception):
    """Internal signal: a safety valve tripped, unwind cleanly."""


class _CapReached(Exception):
    """Internal signal: this profile hit its per-run cap; move to the next one."""




class Orchestrator:
    def __init__(
        self,
        config: AgentConfig,
        settings: Settings,
        *,
        mode: str = "manual",
        only_profiles: list[str] | None = None,
        dry_run: bool = False,
        account: str | None = None,
    ) -> None:
        self.config = config
        self.settings = settings
        self.mode = mode
        self.only_profiles = only_profiles
        self.dry_run = dry_run or settings.dry_run
        # One Orchestrator instance == one Naukri account == one browser session.
        # Accounts are never mixed inside a run: the resume lives on the account,
        # and the daily cap is a per-account budget.
        self.account_key = (account or settings.default_account or "primary").strip().lower()
        self.account: NaukriAccount | None = None

        self.stats = RunStats()
        self.metrics = RuntimeMetrics()
        self.run_id: int | None = None
        self.repo: Repository | None = None
        self.applier: ApplyEngine | None = None
        self.api_client: NaukriApiClient | None = None
        self.consecutive_failures = 0
        self.applied_today = 0
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self.started_at = time.monotonic()
        self.notifier = build_notifier(
            telegram_enabled=config.notifications.telegram_enabled,
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )

    # --------------------------------------------------------------- helpers
    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    def _check_global_limits(self) -> None:
        if self.elapsed_s > self.config.run.run_timeout_minutes * 60:
            raise StopRun(f"run timeout of {self.config.run.run_timeout_minutes} minutes reached")
        if self.consecutive_failures >= self.config.run.max_consecutive_failures:
            raise StopRun(
                f"{self.consecutive_failures} consecutive failures — aborting to avoid a ban"
            )
        if self.applied_today >= self.config.run.daily_application_cap:
            raise StopRun(f"daily cap of {self.config.run.daily_application_cap} reached")

    async def _pace(self) -> None:
        """Randomised gap between applications; the single most important
        anti-detection measure in the whole agent."""
        delay = random.uniform(
            self.config.run.min_delay_between_applies_s,
            self.config.run.max_delay_between_applies_s,
        )
        log.debug("orchestrator.pacing", seconds=round(delay, 1))
        await asyncio.sleep(delay)

    async def _build_answer_engine(self, profile: JobProfile) -> AnswerEngine:
        assert self.repo is not None
        # YAML seeds are synced into Postgres so human-resolved answers and
        # config answers live in one ordered knowledge base.
        await self.repo.seed_answer_kb(self.config.answers, profile=None)
        await self.repo.seed_answer_kb(profile.answers, profile=profile.name)
        kb = await self.repo.load_answer_kb(profile.name)
        return AnswerEngine(
            kb=kb,
            profile_answers=profile.answers,
            strict=self.config.run.strict_answers,
            # Skill -> years map: answers the open-ended "how many years of X?"
            # family that no finite question table can cover.
            experience=self.config.experience_for(profile),
        )

    # ------------------------------------------------------------------- main
    async def run(self) -> RunStats:
        # Resolves + validates the credentials for THIS account only, so a run
        # targeting `secondary` fails loudly instead of silently using account 1.
        self.account = self.settings.validate_for_run(self.account_key)
        self.repo = await Repository.create()
        try:
            await self.repo.prune_stale_data()
        except Exception as exc:
            log.debug("db.prune_ignored", error=str(exc))
        profiles = self.config.active_profiles(self.only_profiles, account=self.account_key)
        profile_names = [p.name for p in profiles]
        if not profile_names:
            raise RuntimeError(
                f"No enabled profiles matched the selection for account '{self.account_key}'"
            )

        self.run_id = await self.repo.start_run(self.mode, profile_names, account=self.account_key)
        bind_context(run_id=self.run_id, account=self.account_key)
        self.applied_today = await self.repo.applied_today(account=self.account_key)
        log.info(
            "run.start",
            mode=self.mode,
            account=self.account_key,
            email=self.account.masked_email,
            profiles=profile_names,
            dry_run=self.dry_run,
            applied_today=self.applied_today,
        )

        status = RunStatus.SUCCESS
        fatal_error: str | None = None
        artifacts = ArtifactStore(self.settings.artifacts_dir, self.run_id)

        try:
            t_b_0 = time.perf_counter()
            async with BrowserManager(
                self.config.browser,
                self.repo,
                session_key=self.account.session_key,
            ) as browser:
                self.metrics.browser_startup_s = time.perf_counter() - t_b_0
                auth = NaukriAuth(
                    browser,
                    self.account.email,
                    self.account.password,
                    artifacts,
                )
                t_login_0 = time.perf_counter()
                page = await auth.ensure_logged_in()
                self.metrics.login_s = time.perf_counter() - t_login_0

                # Extract session token for the match score API pre-filter.
                # Best-effort: if extraction fails, the API client stays None
                # and all jobs go through the normal Playwright flow.
                ms_cfg = self.config.match_score_prefilter
                if ms_cfg.enabled:
                    token = await extract_naukri_token(page)
                    if token:
                        self.api_client = NaukriApiClient(
                            token,
                            timeout_s=ms_cfg.request_timeout_s,
                            max_concurrent=ms_cfg.max_concurrent,
                        )
                        await self.api_client.__aenter__()
                        log.info("match_score.api_client_ready")
                    else:
                        log.info("match_score.no_token_skipping_api")

                # Ranking first, applying second: the profile touch is quota-free
                # and worthless if the run later aborts on a cap.
                t_ref_0 = time.perf_counter()
                await self._refresh_profile(page)
                self.metrics.resume_switch_s = time.perf_counter() - t_ref_0

                for profile in profiles:
                    try:
                        await self._run_profile(profile, page, auth, artifacts)
                    except StopRun as stop:
                        log.warning("run.stopped_early", reason=str(stop))
                        self.stats.errors.append(f"stopped early: {stop}")
                        status = RunStatus.PARTIAL
                        break
        except FatalAgentError as exc:
            fatal_error = str(exc)
            status = RunStatus.FAILED
            log.error("run.fatal", error=fatal_error)
        except Exception as exc:  # unexpected: still record and notify
            fatal_error = f"{type(exc).__name__}: {exc}"
            status = RunStatus.FAILED
            log.exception("run.crashed")
        finally:
            if self._background_tasks:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
            # Detach the popup listener before the page dies.
            if self.applier is not None:
                self.applier.close()
                self.applier = None
            if self.api_client is not None:
                await self.api_client.__aexit__(None, None, None)
                self.api_client = None
            if self.stats.failed and status == RunStatus.SUCCESS:
                status = RunStatus.PARTIAL
            if self.repo is not None and self.run_id is not None:
                await self.repo.finish_run(self.run_id, status, self.stats, fatal_error)
            await self._notify(status, fatal_error)

            # Print & Log structured Runtime Summary report
            summary_report = self.metrics.generate_report()
            print(summary_report)
            log.info("runtime.summary_report", report=summary_report)

            clear_context()

        log.info("run.finished", status=status.value, **self.stats.as_dict()["per_profile"])
        return self.stats

    # ------------------------------------------------------- profile refresh
    async def _refresh_profile(self, page) -> None:
        """
        Move the profile's "last updated" timestamp to today.

        Naukri's recruiter-side search ranks by profile freshness, so this is the
        cheapest high-value action in the run — and unlike applying it has no
        daily quota. It is never fatal: losing the ranking boost for one day is
        not a reason to skip 25 applications.

        The `min_hours_between` gate is evaluated in SQL against
        `profile_updates`, so several runs a day (or a container restart) still
        produce exactly one profile edit; repeated edits inside a day is exactly
        the pattern Naukri's abuse heuristics look for.
        """
        assert self.repo is not None
        settings = self.config.profile_refresh
        if not settings.enabled:
            return

        if not await self.repo.profile_refresh_due(self.account_key, settings.min_hours_between):
            last = await self.repo.last_profile_refresh(self.account_key)
            log.info(
                "profile.refresh_not_due",
                account=self.account_key,
                hours_ago=round(float((last or {}).get("hours_ago") or 0), 1),
                min_hours_between=settings.min_hours_between,
            )
            return

        if self.dry_run:
            log.info("profile.refresh_skipped_dry_run", account=self.account_key)
            return

        refresher = ProfileRefresher(
            page,
            self.account_key,
            strategies=settings.strategies,
            headline_variants=settings.headline_variants,
            resume_dir=self.settings.resume_dir,
            resume_file=self.config.resume_for(self.account_key),
            verify=settings.verify,
        )
        try:
            result = await refresher.refresh()
        except FatalAgentError:
            raise
        except Exception as exc:
            log.warning("profile.refresh_crashed", error=str(exc)[:250])
            await self.repo.record_profile_refresh(
                self.account_key,
                self.run_id,
                ok=False,
                detail=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
            return

        await self.repo.record_profile_refresh(
            self.account_key,
            self.run_id,
            ok=result.ok,
            strategy=result.strategy,
            detail=result.detail,
            headline_before=result.before,
            headline_after=result.after,
            last_updated_text=result.last_updated,
        )
        await self.repo.log_event(
            self.run_id,
            "profile.refresh",
            level="info" if result.ok else "warning",
            account=self.account_key,
            payload=result.as_dict(),
        )
        if not result.ok:
            # Surfaced in the run summary: a silently stale profile is the
            # failure mode the user would never notice on their own.
            self.stats.errors.append(f"profile refresh failed: {result.detail[:120]}")

    # --------------------------------------------------------------- profile
    async def _run_profile(
        self,
        profile: JobProfile,
        page,
        auth: NaukriAuth,
        artifacts: ArtifactStore,
    ) -> None:
        assert self.repo is not None
        bind_context(profile=profile.name)
        log.info(
            "profile.start",
            profile=profile.name,
            account=profile.account,
            recommended=profile.use_recommended,
        )

        answers = await self._build_answer_engine(profile)
        searcher = JobSearcher(
            page,
            self.config.browser.min_action_delay_ms,
            self.config.browser.max_action_delay_ms,
            metrics=self.metrics,
        )

        # The apply engine is created ONCE per run and its knowledge base swapped
        # per profile. Building one per profile registered a fresh `popup`
        # listener on the same page every time, leaking handlers for the run.
        applier = None
        if not self.dry_run:
            if self.applier is None:
                self.applier = ApplyEngine(
                    page,
                    answers,
                    artifacts,
                    dry_run=False,
                    nav_timeout_ms=self.config.browser.navigation_timeout_ms,
                    attempts=self.config.run.max_retries_per_job,
                    metrics=self.metrics,
                )
            else:
                self.applier.set_answers(answers)
            applier = self.applier

        known = await self.repo.known_job_ids(
            profile.name,
            self.config.run.dedupe_window_days,
            account=self.account_key,
        )
        applied_this_profile = 0

        def remaining() -> int:
            return profile.max_applications_per_run - applied_this_profile

        # Step 1: Collect Recommendation Jobs
        collected_jobs: list[Job] = []
        if profile.use_recommended and self.config.recommended.enabled:
            collected_jobs = await searcher.search_recommended(
                self.config.recommended,
                exclude_job_ids=known,
            )
            if collected_jobs:
                self.stats.bump(profile.name, "scraped", len(collected_jobs))

        if not collected_jobs:
            log.info("search.recommended_empty", profile=profile.name)
            return

        # =========================================================
        # STEP 1.5: Inject API Match Scores (Fast Pre-filter)
        # =========================================================
        if self.api_client and collected_jobs:
            log.info("match_score.fetching_batch", count=len(collected_jobs))
            job_ids = [j.job_id.replace("reco-", "") for j in collected_jobs]
            scores = await self.api_client.batch_match_scores(job_ids)

            api_filtered_jobs = []
            for job in collected_jobs:
                raw_id = job.job_id.replace("reco-", "")
                score_result = scores.get(raw_id)

                # Fail-open: If API fails or score is good, keep it.
                # Only drop if the API explicitly says keyskills_score == 0.
                if score_result is None or score_result.is_worth_applying:
                    api_filtered_jobs.append(job)
                else:
                    self.stats.bump(profile.name, "filtered_out")
                    log.debug("match_score.dropped", job_id=job.job_id)

            log.info("match_score.batch_complete", before=len(collected_jobs), after=len(api_filtered_jobs))
            collected_jobs = api_filtered_jobs

            if not collected_jobs:
                return
        # =========================================================

        # Step 2: Generate ApplicationPlan via ApplicationPlanner
        t_plan_0 = time.perf_counter()
        candidate = profile.to_candidate_profile(self.config)
        planner = ApplicationPlanner(
            candidate=candidate,
            rules=profile.filters_for("recommended"),
            daily_limit=profile.max_applications_per_run,
        )
        plan = planner.create_plan(collected_jobs)
        t_plan_1 = time.perf_counter()
        self.metrics.planner_s += (t_plan_1 - t_plan_0)
        self.metrics.ranking_s += getattr(planner, "last_ranking_time_s", 0.0)

        # Step 3: Export Plan CSV Reports & Enriched Summary
        profile_start_time = time.monotonic()
        t_exp_0 = time.perf_counter()
        analysis_dir = PROJECT_ROOT / "analysis"
        exporter = ReportExporter(analysis_dir, run_id=self.run_id)
        exporter.export_plan_reports(plan, collected_jobs, profile_name=profile.name)
        self.metrics.reporting_s += time.perf_counter() - t_exp_0

        # Step 4: Print Concise Summary & Full Application Plan Report
        s = plan.stats
        report_text = plan.generate_report()
        log.info(
            "plan.summary",
            collected=s.collected_count,
            rejected=s.rejected_count,
            eligible=s.eligible_count,
            selected=s.selected_count,
            dry_run=self.dry_run,
        )
        print(report_text)

        # Step 5: Dry Run Gate — Stop before ApplyEngine
        if self.dry_run:
            log.info("dry_run.complete", selected_count=len(plan.selected_jobs))
            duration = time.monotonic() - profile_start_time
            exporter.export_summary_json(
                plan,
                applied_count=0,
                failed_count=0,
                profile_name=profile.name,
                dry_run=True,
                duration_seconds=duration,
            )
            return

        # Step 6: Pass ONLY Selected Jobs to ApplyEngine (Live Run)
        applied_outcomes: list[tuple[Job, ApplyOutcome]] = []
        failed_outcomes: list[tuple[Job, ApplyOutcome]] = []
        filters = FilterEngine(profile.filters_for("recommended"))

        t_apply_loop_0 = time.perf_counter()
        candidate_queue = plan.selected_jobs + plan.overflow_jobs
        try:
            for rjob in candidate_queue:
                self._check_global_limits()
                if remaining() <= 0:
                    log.info("profile.cap_reached", profile=profile.name, cap=profile.max_applications_per_run)
                    break

                job = rjob.job
                known.add(job.job_id)

                outcome = await self._process_job(
                    job, profile, filters, applier, page, auth, artifacts
                )

                if outcome.status == ApplicationStatus.APPLIED:
                    applied_this_profile += 1
                    self.applied_today += 1
                    applied_outcomes.append((job, outcome))
                    await self._pace()
                else:
                    failed_outcomes.append((job, outcome))
        except _CapReached:
            pass
        finally:
            self.metrics.total_apply_phase_s += time.perf_counter() - t_apply_loop_0

        # Step 7: Export Outcome Reports & Enriched Summary JSON
        duration = time.monotonic() - profile_start_time
        t_exp_out_0 = time.perf_counter()
        exporter.export_outcome_reports(applied_outcomes, failed_outcomes, profile_name=profile.name)
        exporter.export_summary_json(
            plan,
            applied_count=len(applied_outcomes),
            failed_count=len(failed_outcomes),
            profile_name=profile.name,
            dry_run=False,
            duration_seconds=duration,
        )
        self.metrics.reporting_s += time.perf_counter() - t_exp_out_0
        log.info("profile.done", profile=profile.name, applied=applied_this_profile)

    # ------------------------------------------------------------------- job
    async def _process_job(
        self,
        job: Job,
        profile: JobProfile,
        filters: FilterEngine,
        applier: ApplyEngine,
        page,
        auth: NaukriAuth,
        artifacts: ArtifactStore,
    ) -> ApplyOutcome:
        assert self.repo is not None
        bind_context(job_id=job.job_id)
        self.stats.bump(profile.name, "considered")

        # Phase 1: free, card-level filtering.
        decision = filters.evaluate_card(job)
        if not decision.passed:
            outcome = ApplyOutcome(
                status=ApplicationStatus.SKIPPED,
                reason=decision.reason,
                detail=decision.detail,
            )
            self.stats.bump(profile.name, "filtered_out")
            if decision.reason == SkipReason.BANGALORE_WALKIN_ALERT:
                self.stats.walkin_alerts.append(
                    {
                        "title": job.title,
                        "company": job.company,
                        "location": job.location,
                        "url": job.url,
                        "profile": profile.name,
                    }
                )
                log.info("job.bangalore_walkin_alert", title=job.title[:70], company=job.company)
            else:
                log.info(
                    "job.filtered",
                    title=job.title[:70],
                    reason=decision.reason.value if decision.reason else "?",
                    detail=decision.detail,
                )
            await self.repo.record_outcome(
                job, profile.name, self.run_id, outcome, account=self.account_key
            )
            return outcome

        try:
            # Phase 2 runs INSIDE apply(): the engine loads the job page, enriches
            # the description, then calls this gate before clicking Apply. The old
            # order filtered after submitting, which only logged a regret.
            outcome = await applier.apply(
                job, profile.name, pre_submit_check=filters.evaluate_detail
            )

            # Session may have silently expired mid-flow.
            if outcome.status == ApplicationStatus.FAILED and await first_visible(
                page, S.LOGGED_OUT_MARKERS, timeout_ms=2_000
            ):
                log.warning("job.session_lost_retrying", job_id=job.job_id)
                await auth.reauthenticate(page)
                outcome = await applier.apply(
                    job, profile.name, pre_submit_check=filters.evaluate_detail
                )

        except FatalAgentError:
            raise
        except Exception as exc:
            shot = await artifacts.capture_failure(page, "job-crash", profile.name, job.job_id)
            log.exception("job.unexpected_error", job_id=job.job_id)
            outcome = ApplyOutcome(
                status=ApplicationStatus.FAILED,
                detail=f"{type(exc).__name__}: {str(exc)[:200]}",
                screenshot_path=shot,
            )

        await self._record(job, profile, outcome)
        return outcome

    async def _record(self, job: Job, profile: JobProfile, outcome: ApplyOutcome) -> None:
        assert self.repo is not None

        if outcome.status == ApplicationStatus.APPLIED:
            self.stats.bump(profile.name, "applied")
            self.stats.applied_jobs.append(
                {
                    "title": job.title,
                    "company": job.company,
                    "url": job.url,
                    "profile": profile.name,
                    "account": self.account_key,
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "form_links": getattr(job, "form_links", []),
                    "recruiter_emails": getattr(job, "recruiter_emails", []),
                }
            )
            self.consecutive_failures = 0
        elif outcome.status == ApplicationStatus.FAILED:
            self.stats.bump(profile.name, "failed")
            self.consecutive_failures += 1
            self.stats.errors.append(f"{job.title[:40]}: {outcome.detail[:120]}")
        elif outcome.status == ApplicationStatus.EXTERNAL or outcome.reason == SkipReason.EXTERNAL_APPLY:
            self.stats.bump(profile.name, "external")
            self.consecutive_failures = 0
            self.stats.external_jobs.append(
                {
                    "title": job.title,
                    "company": job.company,
                    "url": job.url,
                    "profile": profile.name,
                    "form_links": getattr(job, "form_links", []),
                    "recruiter_emails": getattr(job, "recruiter_emails", []),
                }
            )
        elif outcome.status == ApplicationStatus.ALREADY_APPLIED:
            self.stats.bump(profile.name, "already_applied")
            self.consecutive_failures = 0
        elif outcome.status == ApplicationStatus.NEEDS_REVIEW:
            self.stats.bump(profile.name, "needs_review")
            self.consecutive_failures = 0
            for question in outcome.unanswered_questions:
                await self.repo.queue_question_for_review(
                    profile=profile.name,
                    question=question["text"],
                    kind=question.get("kind", "unknown"),
                    options=question.get("options", []),
                    job_id=job.job_id,
                    screenshot_path=outcome.screenshot_path,
                )
        else:
            self.stats.bump(profile.name, "filtered_out")

        await self.repo.record_outcome(
            job, profile.name, self.run_id, outcome, account=self.account_key
        )
        task = asyncio.create_task(self._dispatch_recruiter_emails(job, profile, outcome))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        await self.repo.log_event(
            self.run_id,
            f"apply.{outcome.status.value}",
            level="error" if outcome.status == ApplicationStatus.FAILED else "info",
            job_id=job.job_id,
            profile=profile.name,
            payload={
                "account": self.account_key,
                "title": job.title,
                "company": job.company,
                "reason": outcome.reason.value if outcome.reason else None,
                "detail": outcome.detail[:500],
                "questions_answered": outcome.questions_answered,
            },
        )

    async def _dispatch_recruiter_emails(self, job: Job, profile: JobProfile, outcome: ApplyOutcome) -> None:
        """Dispatches Gemini-tailored cold emails to HR emails extracted from job descriptions."""
        # PREVENT SPAM LOOPHOLE: Do not email if we skipped/rejected the job!
        if outcome.status == ApplicationStatus.SKIPPED:
            return

        recruiter_emails = getattr(job, "recruiter_emails", [])
        if not recruiter_emails:
            return

        gmail_user = os.getenv("GMAIL_USER", "").strip()
        gmail_pass = os.getenv("GMAIL_APP_PASSWORD", os.getenv("GMAIL_APP_PASS", "")).strip()
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()

        if not gmail_user or not gmail_pass:
            return

        mailer = ColdEmailer(sender_email=gmail_user, app_password=gmail_pass, gemini_api_key=gemini_key)

        # Dynamically route the correct PDF based on the active profile track
        if "Full Stack" in profile.name:
            resume_name = "Mahesh_Chitakoti_FullStack_Engineer.pdf"
        else:
            resume_name = "Mahesh_Chitakoti_AI_Engineer.pdf"

        resume_path = PROJECT_ROOT / "resumes" / resume_name

        if not resume_path.exists():
            log.error("cold_email.resume_missing", path=str(resume_path))
            return

        role_name = job.title or profile.name

        for target_email in recruiter_emails:
            clean_email = target_email.strip().lower()
            if await self.repo.has_emailed(clean_email):
                continue

            success = await mailer.send_application_async(
                target_email=clean_email,
                role_name=role_name,
                resume_path=resume_path,
                job_description=job.description or "",
                company_name=job.company or "",
            )
            if success:
                await self.repo.record_contacted_recruiter(clean_email, role_name, job.description or "", job.url or "")
                log.info("naukri.cold_email_sent", to=clean_email, job_id=job.job_id, role=role_name)

    # ---------------------------------------------------------------- notify
    async def _notify(self, status: RunStatus, error: str | None) -> None:
        notifications = self.config.notifications
        is_error = status == RunStatus.FAILED
        if is_error and not notifications.notify_on_failure:
            return
        if not is_error and not notifications.notify_on_success:
            return

        body = format_run_summary(
            self.stats,
            duration_s=self.elapsed_s,
            run_id=self.run_id,
            include_job_list=notifications.include_job_list,
            max_jobs=notifications.max_jobs_in_message,
            error=error,
        )
        title = (
            f"Naukri agent [{self.account_key}] {status.value} — "
            f"{self.stats.applied} applied"
        )
        t_notif_0 = time.perf_counter()
        await self.notifier.send(title, body, is_error=is_error)
        self.metrics.telegram_s += time.perf_counter() - t_notif_0
