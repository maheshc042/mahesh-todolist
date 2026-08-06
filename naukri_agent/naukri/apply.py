"""
Easy Apply engine.
Production-Optimized: High-throughput, fail-fast execution.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from playwright.async_api import Page, TimeoutError as PWTimeoutError

from ..browser.artifacts import ArtifactStore
from ..browser.resilience import dismiss_overlays, first_visible, human_pause, retry_async, safe_text
from ..core.answers import AnswerEngine
from ..core.models import ApplicationStatus, ApplyOutcome, FilterDecision, Job, SkipReason
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
        nav_timeout_ms: int = 20_000,
        attempts: int = 1,
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
        self.answers = answers

    def close(self) -> None:
        try:
            self.page.remove_listener("popup", self._on_popup)
        except Exception:
            pass

    def _on_popup(self, popup: Page) -> None:
        self._popup_opened = True
        task = asyncio.create_task(self._close_popup(popup))
        self._popup_tasks.add(task)
        task.add_done_callback(self._popup_tasks.discard)

    async def _close_popup(self, popup: Page) -> None:
        try:
            await popup.close()
        except Exception:
            pass

    async def _open_job(self, job: Job) -> None:
        await self.page.goto(job.url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
        await dismiss_overlays(self.page)
        await human_pause(200, 500)

    async def _state(self) -> str:
        if await first_visible(self.page, S.JD_ALREADY_APPLIED, timeout_ms=3_000):
            return "already_applied"

        easy_by_id = await first_visible(self.page, ["button#apply-button"], timeout_ms=4_000)
        if easy_by_id is not None:
            return "easy_apply"

        external_by_id = await first_visible(self.page, ["button#company-site-button"], timeout_ms=1_000)
        if external_by_id is not None:
            return "external"

        if await first_visible(self.page, S.JD_COMPANY_SITE_BUTTON, timeout_ms=1_000):
            return "external"
        if await first_visible(self.page, S.JD_APPLY_BUTTON, timeout_ms=2_000):
            return "easy_apply"

        return "unknown"

    async def enrich(self, job: Job) -> None:
        description = await safe_text(await first_visible(self.page, S.JD_DESCRIPTION, timeout_ms=3_000))
        if description:
            job.description = description[:12_000]

    async def apply(self, job: Job, profile: str, pre_submit_check: Callable[[Job], FilterDecision] | None = None) -> ApplyOutcome:
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
                raise PWTimeoutError("Apply button not resolvable within SLA")
            return state

        try:
            state = await retry_async(
                navigate_and_classify,
                attempts=self.attempts,
                label=f"open-job:{job.job_id}",
                on_retry=lambda attempt, exc: self.artifacts.capture_failure(self.page, f"open-retry{attempt}", profile, job.job_id),
            )
        except Exception as exc:
            shot = await self.artifacts.capture_failure(self.page, "open-failed", profile, job.job_id)
            jt.total_s = time.perf_counter() - t_job_start
            if self.metrics:
                self.metrics.job_timings.append(jt)
            return ApplyOutcome(status=ApplicationStatus.FAILED, detail=f"Page classification failed: {str(exc)[:100]}", screenshot_path=shot, attempts=attempt_used)

        if state == "already_applied":
            jt.total_s = time.perf_counter() - t_job_start
            if self.metrics:
                self.metrics.job_timings.append(jt)
            return ApplyOutcome(status=ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED, detail="Naukri reports this application already exists", attempts=attempt_used)

        if state == "external":
            jt.total_s = time.perf_counter() - t_job_start
            if self.metrics:
                self.metrics.job_timings.append(jt)
            return ApplyOutcome(status=ApplicationStatus.EXTERNAL, reason=SkipReason.EXTERNAL_APPLY, detail="apply-on-company-site only", attempts=attempt_used)

        if pre_submit_check is not None:
            decision = pre_submit_check(job)
            if not decision.passed:
                jt.total_s = time.perf_counter() - t_job_start
                if self.metrics:
                    self.metrics.job_timings.append(jt)
                return ApplyOutcome(status=ApplicationStatus.SKIPPED, reason=decision.reason or SkipReason.FILTER_DESCRIPTION, detail=decision.detail, attempts=attempt_used)

        if self.dry_run:
            jt.total_s = time.perf_counter() - t_job_start
            if self.metrics:
                self.metrics.job_timings.append(jt)
            return ApplyOutcome(status=ApplicationStatus.SKIPPED, reason=SkipReason.DRY_RUN, detail="dry_run enabled", attempts=attempt_used)

        outcome = await self._submit(job, profile, attempt_used, jt)
        jt.total_s = time.perf_counter() - t_job_start
        if self.metrics:
            self.metrics.job_timings.append(jt)
        return outcome

    async def _wait_for_post_apply_event(self) -> str:
        for _ in range(30):  # 4.5s strict SLA poll
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
        apply_btn = await first_visible(self.page, ["button#apply-button", *S.JD_APPLY_BUTTON], timeout_ms=4_000)
        jt.btn_detect_s += time.perf_counter() - t_btn_0

        if apply_btn is None:
            shot = await self.artifacts.capture_failure(self.page, "apply-btn-gone", profile, job.job_id)
            return ApplyOutcome(status=ApplicationStatus.FAILED, detail="Apply button vanished", screenshot_path=shot, attempts=attempts)

        t_click_0 = time.perf_counter()
        try:
            await apply_btn.scroll_into_view_if_needed(timeout=2_000)
            await apply_btn.click(timeout=5_000)
        except Exception as exc:
            shot = await self.artifacts.capture_failure(self.page, "apply-click", profile, job.job_id)
            jt.click_s += time.perf_counter() - t_click_0
            return ApplyOutcome(status=ApplicationStatus.FAILED, detail=f"Click failed: {str(exc)[:100]}", screenshot_path=shot, attempts=attempts)
        jt.click_s += time.perf_counter() - t_click_0

        t_qdet_0 = time.perf_counter()
        event_type = await self._wait_for_post_apply_event()
        jt.q_detect_s += time.perf_counter() - t_qdet_0

        if event_type == "popup" or self._popup_opened:
            return ApplyOutcome(status=ApplicationStatus.EXTERNAL, reason=SkipReason.EXTERNAL_APPLY, detail="Third-party tab opened", attempts=attempts)

        # Wait up to 6s for the success banner
        if event_type in ("success", "already_applied") or await first_visible(self.page, S.APPLY_SUCCESS, timeout_ms=6_000):
            return ApplyOutcome(status=ApplicationStatus.APPLIED, attempts=attempts)

        chatbot = ChatbotHandler(self.page, self.answers, max_questions=self.max_questions)
        if event_type == "chatbot" or await chatbot.is_open(timeout_ms=2_000):
            t_qans_0 = time.perf_counter()
            result = await chatbot.run()
            jt.q_answer_s += time.perf_counter() - t_qans_0

            if result.unanswered:
                shot = await self.artifacts.screenshot(self.page, "unanswered", profile, job.job_id)
                return ApplyOutcome(status=ApplicationStatus.NEEDS_REVIEW, reason=SkipReason.UNANSWERED_QUESTION, detail="Unanswered screening question", screenshot_path=shot, questions_answered=result.answered, attempts=attempts)

            if result.error:
                shot = await self.artifacts.capture_failure(self.page, "chatbot-error", profile, job.job_id)
                return ApplyOutcome(status=ApplicationStatus.FAILED, detail=f"Chatbot error: {result.error}", screenshot_path=shot, questions_answered=result.answered, attempts=attempts)

            toast = await safe_text(await first_visible(self.page, S.APPLY_ERROR_TOAST, timeout_ms=1_000))
            if toast:
                shot = await self.artifacts.capture_failure(self.page, "apply-rejected", profile, job.job_id)
                return ApplyOutcome(status=ApplicationStatus.FAILED, detail=f"Naukri rejection: {toast[:100]}", screenshot_path=shot, questions_answered=result.answered, attempts=attempts)

            return ApplyOutcome(status=ApplicationStatus.APPLIED, questions_answered=result.answered, attempts=attempts)

        toast = await safe_text(await first_visible(self.page, S.APPLY_ERROR_TOAST, timeout_ms=1_000))
        if toast:
            shot = await self.artifacts.capture_failure(self.page, "apply-error-toast", profile, job.job_id)
            return ApplyOutcome(status=ApplicationStatus.FAILED, detail=f"Toast error: {toast[:100]}", screenshot_path=shot, attempts=attempts)

        # Fail-Fast Verification: Instead of a massive 60s page reload, we just check the DOM. 
        # If Naukri didn't render the success banner in time, we drop the job and grab the next one.
        t_ver_0 = time.perf_counter()
        is_success = await first_visible(self.page, S.JD_ALREADY_APPLIED + S.APPLY_SUCCESS, timeout_ms=3_000) is not None
        jt.verify_s += time.perf_counter() - t_ver_0

        if is_success:
            return ApplyOutcome(status=ApplicationStatus.APPLIED, attempts=attempts)

        shot = await self.artifacts.capture_failure(self.page, "no-confirmation", profile, job.job_id)
        return ApplyOutcome(status=ApplicationStatus.FAILED, detail="No success confirmation within SLA", screenshot_path=shot, attempts=attempts)
