"""
Easy Apply engine.

The apply flow is a decision tree, not a linear script, because a Naukri job
detail page can be in six different states when it loads:

    already applied            -> record ALREADY_APPLIED, no click
    external only              -> record EXTERNAL, no click  (hard requirement)
    apply -> instant success   -> record APPLIED
    apply -> chatbot drawer    -> answer questions, then verify
    apply -> new tab opens     -> external in disguise, close tab, EXTERNAL
    apply -> error/unknown     -> screenshot + HTML, retry or FAILED

Design decisions:

- **External detection happens BEFORE clicking anything.** Naukri renders
  `#company-site-button` instead of `#apply-button` for off-platform postings.
  Checking first means we never open a third-party tab, which is both the user's
  explicit requirement and the main source of hung runs.
- **Popup guard.** Even "Easy Apply" buttons sometimes open a new tab (recruiter
  misconfiguration). We register a `page.on("popup")` handler that closes any new
  tab immediately and flags the job external, instead of leaving orphan pages
  that leak memory across a long run.
- **Event-based post-click reaction.** Replaced fixed sleeps with `_wait_for_post_apply_event()`,
  racing success banners, chatbot drawers, popup flags, and error toasts.
- **Success is verified, never assumed.** A click landing without an exception
  proves nothing. We require one of: success banner, "already applied" marker,
  or the chatbot reporting completion. Anything else is FAILED with artifacts.
- **Retries are scoped to navigation/parse, not to the click.** Re-clicking Apply
  risks duplicate submissions, so `retry_async` wraps only the idempotent
  navigate+inspect phase; the apply click itself runs exactly once per attempt
  and re-checks applied-state on entry.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from playwright.async_api import Page, TimeoutError as PWTimeoutError

from ..browser.artifacts import ArtifactStore
from ..browser.resilience import (
    dismiss_overlays,
    first_visible,
    human_pause,
    retry_async,
    safe_text,
)
from ..core.answers import AnswerEngine
from ..core.models import (
    ApplicationStatus,
    ApplyOutcome,
    FilterDecision,
    Job,
    SkipReason,
)
from ..core.runtime_metrics import JobTiming, RuntimeMetrics
from ..logging_setup import get_logger
from . import selectors as S
from .chatbot import ChatbotHandler

log = get_logger(__name__)


class ApplyEngine:
    def __init__(
        self,
        page: Page,
        answers: AnswerEngine,
        artifacts: ArtifactStore,
        *,
        dry_run: bool = False,
        max_questions: int = 15,
        nav_timeout_ms: int = 45_000,
        attempts: int = 2,
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        self.page = page
        self.answers = answers
        self.artifacts = artifacts
        self.dry_run = dry_run
        self.max_questions = max_questions
        self.nav_timeout_ms = nav_timeout_ms
        self.attempts = attempts
        self.metrics = metrics
        self._popup_opened = False
        self._popup_tasks: set[asyncio.Task] = set()
        self.page.on("popup", self._on_popup)

    def set_answers(self, answers: AnswerEngine) -> None:
        """
        Swap the knowledge base when the run moves to the next profile.

        The engine is created ONCE per run and reused: constructing one per
        profile registered a new `popup` listener on the same page every time,
        so a stray tab fired N handlers and the listeners leaked for the whole
        run.
        """
        self.answers = answers

    def close(self) -> None:
        """Detach the popup listener; safe to call twice."""
        try:
            self.page.remove_listener("popup", self._on_popup)
        except Exception:
            pass

    # ------------------------------------------------------------ popup guard
    def _on_popup(self, popup: Page) -> None:
        """Close stray tabs synchronously-ish; never let them accumulate."""
        self._popup_opened = True
        log.warning("apply.popup_blocked", url=popup.url[:200])
        # Keep a strong reference: bare create_task() lets the GC collect the
        # task mid-flight and the tab is then never closed.
        task = asyncio.create_task(self._close_popup(popup))
        self._popup_tasks.add(task)
        task.add_done_callback(self._popup_tasks.discard)

    async def _close_popup(self, popup: Page) -> None:
        try:
            await popup.close()
        except Exception:
            pass

    # ------------------------------------------------------------- inspection
    async def _open_job(self, job: Job) -> None:
        await self.page.goto(job.url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
        await dismiss_overlays(self.page)
        await human_pause(500, 1_300)

    async def _state(self) -> str:
        """
        Classify the loaded detail page into one actionable state.
        """
        if await first_visible(self.page, S.JD_ALREADY_APPLIED, timeout_ms=4_500):
            return "already_applied"

        # Stable IDs Naukri has shipped for years: exactly one is rendered.
        easy_by_id = await first_visible(self.page, ["button#apply-button"], timeout_ms=6_000)
        if easy_by_id is not None:
            return "easy_apply"
        external_by_id = await first_visible(
            self.page, ["button#company-site-button"], timeout_ms=1_500
        )
        if external_by_id is not None:
            return "external"

        # IDs gone (redesign): fall back to text, external FIRST so the
        # more-specific "Apply on company site" wins over a bare "Apply".
        if await first_visible(self.page, S.JD_COMPANY_SITE_BUTTON, timeout_ms=2_000):
            return "external"
        if await first_visible(self.page, S.JD_APPLY_BUTTON, timeout_ms=4_000):
            return "easy_apply"
        return "unknown"

    async def enrich(self, job: Job) -> None:
        """
        Pull the full JD text so description-level keyword filters can run.
        Search cards only carry a truncated snippet.
        """
        description = await safe_text(
            await first_visible(self.page, S.JD_DESCRIPTION, timeout_ms=6_000)
        )
        if description:
            job.description = description[:12_000]

    # ------------------------------------------------------------------ apply
    async def apply(
        self,
        job: Job,
        profile: str,
        pre_submit_check: Callable[[Job], FilterDecision] | None = None,
    ) -> ApplyOutcome:
        """
        Full flow for a single job. Never raises; always returns an outcome.
        """
        t_job_start = time.perf_counter()
        jt = JobTiming(job_id=job.job_id, title=job.title)
        self._popup_opened = False
        attempt_used = 0

        async def navigate_and_classify() -> str:
            nonlocal attempt_used
            attempt_used += 1

            t_goto_0 = time.perf_counter()
            await self._open_job(job)
            jt.goto_s += time.perf_counter() - t_goto_0

            t_ready_0 = time.perf_counter()
            await self.enrich(job)
            state = await self._state()
            jt.readiness_s += time.perf_counter() - t_ready_0

            if state == "unknown":
                # Could be a soft 5xx or a partially hydrated page: worth a retry.
                raise PWTimeoutError("apply button not resolvable")
            return state

        try:
            state = await retry_async(
                navigate_and_classify,
                attempts=self.attempts,
                label=f"open-job:{job.job_id}",
                on_retry=lambda attempt, exc: self.artifacts.capture_failure(
                    self.page, f"open-retry{attempt}", profile, job.job_id
                ),
            )
        except Exception as exc:
            shot = await self.artifacts.capture_failure(self.page, "open-failed", profile, job.job_id)
            log.error("apply.open_failed", job_id=job.job_id, profile=profile, attempt=attempt_used, error=str(exc)[:250])
            jt.total_s = time.perf_counter() - t_job_start
            if self.metrics:
                self.metrics.job_timings.append(jt)
            return ApplyOutcome(
                status=ApplicationStatus.FAILED,
                detail=f"could not classify job page: {str(exc)[:180]}",
                screenshot_path=shot,
                attempts=attempt_used,
            )

        if state == "already_applied":
            log.info("apply.already_applied", job_id=job.job_id, profile=profile, attempt=attempt_used, title=job.title[:80])
            jt.total_s = time.perf_counter() - t_job_start
            if self.metrics:
                self.metrics.job_timings.append(jt)
            return ApplyOutcome(
                status=ApplicationStatus.ALREADY_APPLIED,
                reason=SkipReason.ALREADY_APPLIED,
                detail="Naukri reports this application already exists",
                attempts=attempt_used,
            )

        if state == "external":
            log.info("apply.external_skip", job_id=job.job_id, profile=profile, attempt=attempt_used, company=job.company[:60])
            jt.total_s = time.perf_counter() - t_job_start
            if self.metrics:
                self.metrics.job_timings.append(jt)
            return ApplyOutcome(
                status=ApplicationStatus.EXTERNAL,
                reason=SkipReason.EXTERNAL_APPLY,
                detail="apply-on-company-site only",
                attempts=attempt_used,
            )

        if pre_submit_check is not None:
            decision = pre_submit_check(job)
            if not decision.passed:
                log.info(
                    "apply.blocked_by_jd_filter",
                    job_id=job.job_id,
                    profile=profile,
                    attempt=attempt_used,
                    title=job.title[:70],
                    detail=decision.detail,
                )
                jt.total_s = time.perf_counter() - t_job_start
                if self.metrics:
                    self.metrics.job_timings.append(jt)
                return ApplyOutcome(
                    status=ApplicationStatus.SKIPPED,
                    reason=decision.reason or SkipReason.FILTER_DESCRIPTION,
                    detail=decision.detail or "blocked by job-description filter",
                    attempts=attempt_used,
                )

        if self.dry_run:
            log.info("apply.dry_run", job_id=job.job_id, profile=profile, attempt=attempt_used, title=job.title[:80])
            jt.total_s = time.perf_counter() - t_job_start
            if self.metrics:
                self.metrics.job_timings.append(jt)
            return ApplyOutcome(
                status=ApplicationStatus.SKIPPED,
                reason=SkipReason.DRY_RUN,
                detail="dry_run enabled: Easy Apply available but not submitted",
                attempts=attempt_used,
            )

        outcome = await self._submit(job, profile, attempt_used, jt)
        jt.total_s = time.perf_counter() - t_job_start
        if self.metrics:
            self.metrics.job_timings.append(jt)
        return outcome

    async def _wait_for_post_apply_event(self) -> str:
        """Race-free event-driven wait for post-apply UI reaction (P1-1 & P1-2)."""
        for _ in range(25):  # Max 3.75s event-driven poll (150ms interval)
            if self._popup_opened:
                return "popup"
            if await first_visible(self.page, S.APPLY_SUCCESS, timeout_ms=50):
                return "success"
            if await first_visible(self.page, S.CHATBOT_DRAWER, timeout_ms=50):
                return "chatbot"
            if await first_visible(self.page, S.APPLY_ERROR_TOAST, timeout_ms=50):
                return "toast"
            if await first_visible(self.page, S.JD_ALREADY_APPLIED, timeout_ms=50):
                return "already_applied"
            await asyncio.sleep(0.15)
        return "timeout"

    async def _submit(self, job: Job, profile: str, attempts: int, jt: JobTiming) -> ApplyOutcome:
        t_btn_0 = time.perf_counter()
        apply_btn = await first_visible(
            self.page, ["button#apply-button", *S.JD_APPLY_BUTTON], timeout_ms=8_000
        )
        jt.btn_detect_s += time.perf_counter() - t_btn_0
        if apply_btn is None:
            shot = await self.artifacts.capture_failure(self.page, "apply-btn-gone", profile, job.job_id)
            return ApplyOutcome(
                status=ApplicationStatus.FAILED,
                detail="apply button vanished before click",
                screenshot_path=shot,
                attempts=attempts,
            )

        t_click_0 = time.perf_counter()
        try:
            await apply_btn.scroll_into_view_if_needed(timeout=5_000)
            await human_pause(250, 700)
            await apply_btn.click(timeout=10_000)
        except Exception as exc:
            shot = await self.artifacts.capture_failure(self.page, "apply-click", profile, job.job_id)
            log.error("apply.click_failed", job_id=job.job_id, profile=profile, attempt=attempts, error=str(exc)[:200])
            jt.click_s += time.perf_counter() - t_click_0
            return ApplyOutcome(
                status=ApplicationStatus.FAILED,
                detail=f"apply click failed: {str(exc)[:180]}",
                screenshot_path=shot,
                attempts=attempts,
            )
        jt.click_s += time.perf_counter() - t_click_0

        # Race-free event-driven wait (P1-1 & P1-2)
        t_qdet_0 = time.perf_counter()
        event_type = await self._wait_for_post_apply_event()
        jt.q_detect_s += time.perf_counter() - t_qdet_0

        if event_type == "popup" or self._popup_opened:
            return ApplyOutcome(
                status=ApplicationStatus.EXTERNAL,
                reason=SkipReason.EXTERNAL_APPLY,
                detail="apply opened a third-party tab; treated as external",
                attempts=attempts,
            )

        # Fast path: immediate confirmation
        if event_type in ("success", "already_applied") or await first_visible(self.page, S.APPLY_SUCCESS, timeout_ms=2_000):
            log.info("apply.success_immediate", job_id=job.job_id, profile=profile, attempt=attempts, title=job.title[:80])
            return ApplyOutcome(status=ApplicationStatus.APPLIED, attempts=attempts)

        # Questions path
        chatbot = ChatbotHandler(self.page, self.answers, max_questions=self.max_questions)
        if event_type == "chatbot" or await chatbot.is_open(timeout_ms=3_000):
            log.info("apply.chatbot_opened", job_id=job.job_id, profile=profile, attempt=attempts)
            t_qans_0 = time.perf_counter()
            result = await chatbot.run()
            jt.q_answer_s += time.perf_counter() - t_qans_0

            if result.unanswered:
                shot = await self.artifacts.screenshot(self.page, "unanswered", profile, job.job_id)
                log.warning(
                    "apply.needs_review",
                    job_id=job.job_id,
                    profile=profile,
                    attempt=attempts,
                    unanswered=len(result.unanswered),
                )
                return ApplyOutcome(
                    status=ApplicationStatus.NEEDS_REVIEW,
                    reason=SkipReason.UNANSWERED_QUESTION,
                    detail="screening question not in knowledge base; queued for review",
                    screenshot_path=shot,
                    questions_answered=result.answered,
                    unanswered_questions=[
                        {"text": q.text, "kind": q.kind, "options": q.options}
                        for q in result.unanswered
                    ],
                    attempts=attempts,
                )

            if result.error:
                shot = await self.artifacts.capture_failure(self.page, "chatbot-error", profile, job.job_id)
                return ApplyOutcome(
                    status=ApplicationStatus.FAILED,
                    detail=f"chatbot: {result.error}",
                    screenshot_path=shot,
                    questions_answered=result.answered,
                    attempts=attempts,
                )

            # Check for explicit error toast
            toast = await safe_text(await first_visible(self.page, S.APPLY_ERROR_TOAST, timeout_ms=1_500))
            if toast:
                shot = await self.artifacts.capture_failure(self.page, "apply-rejected", profile, job.job_id)
                log.warning("apply.rejected_by_naukri", job_id=job.job_id, profile=profile, attempt=attempts, detail=toast[:160])
                return ApplyOutcome(
                    status=ApplicationStatus.FAILED,
                    detail=f"Naukri rejected application: {toast[:160]}",
                    screenshot_path=shot,
                    questions_answered=result.answered,
                    attempts=attempts,
                )

            log.info(
                "apply.success_after_chatbot",
                job_id=job.job_id,
                profile=profile,
                attempt=attempts,
                answered=result.answered,
            )
            return ApplyOutcome(
                status=ApplicationStatus.APPLIED,
                questions_answered=result.answered,
                attempts=attempts,
            )

        # Check for explicit error toast
        toast = await safe_text(await first_visible(self.page, S.APPLY_ERROR_TOAST, timeout_ms=1_500))
        if toast:
            shot = await self.artifacts.capture_failure(self.page, "apply-error-toast", profile, job.job_id)
            return ApplyOutcome(
                status=ApplicationStatus.FAILED,
                detail=f"Naukri toast error: {toast[:160]}",
                screenshot_path=shot,
                attempts=attempts,
            )

        t_ver_0 = time.perf_counter()
        verified = await self._verify_applied(job)
        jt.verify_s += time.perf_counter() - t_ver_0
        if verified:
            log.info("apply.success_verified_on_reload", job_id=job.job_id, profile=profile, attempt=attempts)
            return ApplyOutcome(status=ApplicationStatus.APPLIED, attempts=attempts)

        shot = await self.artifacts.capture_failure(self.page, "no-confirmation", profile, job.job_id)
        log.error("apply.no_confirmation", job_id=job.job_id, profile=profile, attempt=attempts, toast=toast[:150])
        return ApplyOutcome(
            status=ApplicationStatus.FAILED,
            detail="no success banner, no chatbot, no applied marker",
            screenshot_path=shot,
            attempts=attempts,
        )

    async def _verify_applied(self, job: Job) -> bool:
        """
        Ground truth: reload the JD and look for the applied marker.
        """
        try:
            if await first_visible(self.page, S.JD_ALREADY_APPLIED + S.APPLY_SUCCESS, timeout_ms=3_000):
                return True
            await self.page.goto(
                job.url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms
            )
            await dismiss_overlays(self.page)
            if await first_visible(self.page, S.JD_ALREADY_APPLIED, timeout_ms=8_000):
                return True
            return await first_visible(self.page, S.APPLY_SUCCESS, timeout_ms=3_000) is not None
        except Exception as exc:
            log.warning("apply.verify_failed", job_id=job.job_id, error=str(exc)[:180])
            # Conservative verification fallback (P2-4 Fix)
            return False
