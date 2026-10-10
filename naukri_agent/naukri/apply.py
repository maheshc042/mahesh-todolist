"""
Easy Apply engine.
Production-Optimized: High-throughput, fail-fast execution.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PWTimeoutError

from ..browser.artifacts import ArtifactStore
from ..browser.resilience import (
    dismiss_overlays,
    first_visible,
    human_pause,
    retry_async,
    safe_text,
)
from ..core.answers import AnswerEngine
from ..core.models import ApplicationStatus, ApplyOutcome, FilterDecision, Job, SkipReason
from ..core.run_policy import RunPolicy, SideEffectBlocked
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
        policy: RunPolicy,
        max_questions: int = 15,
        nav_timeout_ms: int = 20_000,
        attempts: int = 1,
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        self.page = page
        self.answers = answers
        self.artifacts = artifacts
        self.policy = policy
        self.max_questions = max_questions
        self.nav_timeout_ms = nav_timeout_ms
        self.attempts = attempts
        self.metrics = metrics
        self._popup_opened = False
        self._last_popup_url: str | None = None
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
            # Stash first: the URL is the employer's actual ATS destination,
            # which the Sidekick handoff requires (raw naukri.com listing
            # links are rejected downstream).
            if popup.url and popup.url.lower().startswith("http"):
                self._last_popup_url = popup.url
        except Exception:
            pass
        try:
            await popup.close()
        except Exception:
            pass

    @staticmethod
    def _is_company_url(url: str | None) -> str | None:
        """Normalized employer link, or None for listing/blank URLs."""
        if not url:
            return None
        clean = url.strip()
        if not clean.lower().startswith("http"):
            return None
        if "naukri.com" in clean.lower():
            return None
        return clean

    async def _company_site_url(self) -> str | None:
        """Resolved employer ATS link behind 'Apply on company site'.

        Anchor variant carries the href directly; the button variant only
        link-outs, so clicking it submits nothing — capture where it leads
        (popup, else same-tab URL) and return it. Never raises.
        """
        for sel in ("a#company-site-button", "a:text-is('Apply on company site')"):
            try:
                anchor = self.page.locator(sel).first
                if await anchor.count() > 0:
                    found = self._is_company_url(await anchor.get_attribute("href"))
                    if found:
                        return found
            except Exception:
                continue
        try:
            btn = await first_visible(
                self.page, ["button#company-site-button", *S.JD_COMPANY_SITE_BUTTON],
                timeout_ms=2_000,
            )
        except Exception:
            btn = None
        if btn is None:
            return None
        try:
            async with self.page.expect_popup(timeout=3_000) as pop_info:
                await btn.click(timeout=3_000)
            try:
                popup = await pop_info.value
            except Exception:
                popup = None
            if popup is not None:
                found = self._is_company_url(popup.url)
                try:
                    await popup.close()
                except Exception:
                    pass
                if found:
                    return found
        except Exception:
            pass
        try:
            return self._is_company_url(self.page.url)
        except Exception:
            return None

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

        # Walk-in pages carry no apply machinery at all (run 498: classified
        # as FAILED instead of skipped). Detect explicitly so they record as
        # clean skips under the skip_walkin policy, never failures.
        if await self._is_walkin_page():
            return "walkin"

        return "unknown"

    async def _is_walkin_page(self) -> bool:
        """True when the loaded job page is a walk-in posting.

        Conservative: requires a walk-in marker PLUS venue/interview-day
        evidence, so ordinary postings mentioning "walk in" in prose never
        match. Never raises.
        """
        try:
            title = ((await self.page.title()) or "").lower()
            if "walk-in" in title or "walkin" in title:
                return True
            body_text = ""
            try:
                body_text = ((await self.page.locator("body").first.inner_text()) or "").lower()
            except Exception:
                body_text = ""
            if ("walk-in interview" in body_text or "walk in interview" in body_text
                    or "walk-in drive" in body_text):
                if any(k in body_text for k in ("venue", "reporting time", "reporting date",
                                                "interview date", "interview venue", "f2f",
                                                "face to face", "bring along", "carry your resume")):
                    return True
        except Exception:
            pass
        return False

    async def enrich(self, job: Job) -> None:
        description = await safe_text(await first_visible(self.page, S.JD_DESCRIPTION, timeout_ms=3_000))
        if description:
            job.description = description[:12_000]
            form_links = re.findall(
                r"(https?://(?:forms\.gle|docs\.google\.com/forms|forms\.office\.com|typeform\.com)[^\s\"'>]+)",
                description,
            )
            if form_links:
                job.form_links = list(dict.fromkeys(form_links))
                log.info("job.form_links_found", job_id=job.job_id, count=len(job.form_links))

            # Extract Recruiter Emails
            raw_emails = re.findall(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)", description)
            if raw_emails:
                ignored_prefixes = ("info@", "support@", "sales@", "contact@", "help@", "admin@", "query@", "feedback@", "hrintern@", "careers@", "jobs@")
                valid_emails = [
                    e.lower().strip(".")
                    for e in raw_emails
                    if not e.lower().startswith(ignored_prefixes)
                    and not e.lower().endswith(("naukri.com", "naukrigulf.com", "example.com", "yopmail.com"))
                ]
                if valid_emails:
                    job.recruiter_emails = list(dict.fromkeys(valid_emails))
                    log.info("job.recruiter_emails_found", job_id=job.job_id, count=len(job.recruiter_emails))

    async def apply(self, job: Job, profile: str, pre_submit_check: Callable[[Job], FilterDecision] | None = None) -> ApplyOutcome:
        t_job_start = time.perf_counter()
        jt = JobTiming(job_id=job.job_id, title=job.title)
        self._popup_opened = False
        self._last_popup_url = None
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
                # Job-title walk-in marker (run 526: "Walk-in || Python
                # Software Developer" failed classification — the TAB title
                # lacked the marker even though the posting title carries
                # it). A walk-in marker in the job title itself is
                # unambiguous (no prose-confusion risk), so trust it here.
                job_title_low = (job.title or "").lower()
                if "walk-in" in job_title_low or "walkin" in job_title_low:
                    log.info("naukri.apply.walkin_by_title", job_id=job.job_id,
                             title=job.title[:70])
                    return "walkin"
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

        if state == "walkin":
            jt.total_s = time.perf_counter() - t_job_start
            if self.metrics:
                self.metrics.job_timings.append(jt)
            log.info("naukri.apply.walkin_skipped", job_id=job.job_id, title=job.title[:60])
            return ApplyOutcome(status=ApplicationStatus.SKIPPED, reason=SkipReason.WALKIN, detail="Walk-in posting: no online apply path under skip_walkin policy", attempts=attempt_used)

        if state == "external":
            jt.total_s = time.perf_counter() - t_job_start
            if self.metrics:
                self.metrics.job_timings.append(jt)
            ats_url = await self._company_site_url()
            log.info("naukri.apply.external_resolved", job_id=job.job_id,
                     resolved=bool(ats_url))
            return ApplyOutcome(status=ApplicationStatus.EXTERNAL, reason=SkipReason.EXTERNAL_APPLY, detail="apply-on-company-site only", attempts=attempt_used, external_url=ats_url)

        if pre_submit_check is not None:
            decision = pre_submit_check(job)
            if not decision.passed:
                jt.total_s = time.perf_counter() - t_job_start
                if self.metrics:
                    self.metrics.job_timings.append(jt)
                return ApplyOutcome(status=ApplicationStatus.SKIPPED, reason=decision.reason or SkipReason.FILTER_DESCRIPTION, detail=decision.detail, attempts=attempt_used)

        if not self.policy.may_mutate:
            jt.total_s = time.perf_counter() - t_job_start
            if self.metrics:
                self.metrics.job_timings.append(jt)
            return ApplyOutcome(
                status=ApplicationStatus.SKIPPED,
                reason=SkipReason.DRY_RUN,
                detail="submission blocked by run policy",
                attempts=attempt_used,
            )

        outcome = await self._submit(job, profile, attempt_used, jt)
        jt.total_s = time.perf_counter() - t_job_start
        if self.metrics:
            self.metrics.job_timings.append(jt)
        return outcome

    async def _submit_button_state(self) -> tuple:
        """Snapshot (url, present, enabled, text) of the job Apply button.

        Never raises. Tuple compare after the click tells whether the click
        did anything at all.
        """
        try:
            url = self.page.url or ""
        except Exception:
            url = ""
        try:
            btn = self.page.locator("button#apply-button").first
            if await btn.count() == 0:
                return (url, False, None, None)
            try:
                text = ((await btn.inner_text(timeout=1_500)) or "").strip().lower() or None
            except Exception:
                text = None
            try:
                enabled = await btn.is_enabled()
            except Exception:
                enabled = None
            return (url, True, enabled, text)
        except Exception:
            return (url, False, None, None)

    async def _submit_effect_seen(self, pre: tuple) -> bool:
        """True when the page observably reacted to the Apply click (pure
        observation). A submitted flow always disturbs something — popup,
        navigation, drawer, toast, or the button itself. Never raises."""
        pre_url, pre_present, pre_enabled, pre_text = pre
        try:
            if self._popup_opened:
                return True
            if (self.page.url or "") != (pre_url or ""):
                return True
            if await self._fast_check(S.CHATBOT_DRAWER):
                return True
            if await self._fast_check(S.APPLY_SUCCESS):
                return True
            if await self._fast_check(S.APPLY_ERROR_TOAST):
                return True
            if await self._fast_check(S.JD_ALREADY_APPLIED):
                return True
            post = await self._submit_button_state()
            _, post_present, post_enabled, post_text = post
            if pre_present and not post_present:
                return True
            if pre_enabled is True and post_enabled is False:
                return True
            if pre_text is not None and post_text != pre_text:
                return True
        except Exception:
            pass
        return False

    async def _fast_check(self, selectors: list[str]) -> bool:
        """Fast, non-blocking check to see if any selector is visible right now in 0ms."""
        for selector in selectors:
            try:
                loc = self.page.locator(selector).first
                if await loc.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _wait_for_post_apply_event(self) -> str:
        extended_success = S.APPLY_SUCCESS + [
            "div:has-text('Application sent')",
            "div:has-text('Successfully applied')",
            "div.acp-header-container:has-text('Applied to')",
        ]
        for _ in range(100):  # ~15s resilient SLA poll for enterprise network latency
            if self._popup_opened:
                return "popup"
            try:
                title = await self.page.title()
                if "Apply Confirmation" in title or "/apply/confirmation" in self.page.url:
                    return "success"
            except Exception:
                pass
            if await self._fast_check(S.CHATBOT_DRAWER):
                return "chatbot"
            if await self._fast_check(S.APPLY_ERROR_TOAST):
                return "toast"
            if await self._fast_check(S.JD_ALREADY_APPLIED):
                return "already_applied"
            if await self._fast_check(extended_success):
                return "success"
            await asyncio.sleep(0.15)
        return "timeout"

    async def _submit(self, job: Job, profile: str, attempts: int, jt: JobTiming) -> ApplyOutcome:
        t_btn_0 = time.perf_counter()
        apply_btn = await first_visible(self.page, ["button#apply-button", *S.JD_APPLY_BUTTON], timeout_ms=4_000)
        if apply_btn is None:
            # Transient render race (React re-hydration, late overlay): one cheap
            # re-resolve before giving up. Pre-click, so zero double-apply risk.
            await dismiss_overlays(self.page)
            await human_pause(500, 1_000)
            apply_btn = await first_visible(self.page, ["button#apply-button", *S.JD_APPLY_BUTTON], timeout_ms=4_000)
        jt.btn_detect_s += time.perf_counter() - t_btn_0

        if apply_btn is None:
            shot = await self.artifacts.capture_failure(self.page, "apply-btn-gone", profile, job.job_id)
            return ApplyOutcome(status=ApplicationStatus.SKIPPED, reason=SkipReason.STALE_JOB, detail="Apply button vanished (filled/expired between collect and apply)", screenshot_path=shot, attempts=attempts)

        t_click_0 = time.perf_counter()
        pre_click_state = await self._submit_button_state()
        # Pre-click micro-retry: a transient overlay or React re-render can break
        # exactly one click attempt. Retrying here is safe — nothing has been
        # submitted yet. Post-click outcomes below are deliberately never retried.
        click_exc: Exception | None = None
        for click_attempt in (1, 2):
            try:
                # Defense in depth: enforce immediately before the irreversible click,
                # even if a future caller bypasses apply() or policy wiring regresses.
                self.policy.require_mutation("naukri.application.submit")
            
                await asyncio.sleep(1.5)  # Wait for React hydration / event listeners to attach

                # Try to click all visible apply buttons in case the first is a dummy/sticky header
                clicked = False
                for btn in await self.page.locator("button#apply-button").all():
                    if await btn.is_visible():
                        try:
                            await btn.scroll_into_view_if_needed(timeout=2_000)
                            try:
                                await btn.click(timeout=3_000, delay=50, force=True)
                            except Exception:
                                # Fallback to pure JS click
                                await btn.evaluate("node => node.click()")
                            clicked = True
                        except Exception:
                            pass

                if not clicked:
                    try:
                        await apply_btn.scroll_into_view_if_needed(timeout=2_000)
                    except Exception:
                        pass
                    try:
                        await apply_btn.click(timeout=3_000, delay=50, force=True)
                    except Exception:
                        await apply_btn.evaluate("node => node.click()")

                click_exc = None
                break
            except SideEffectBlocked as exc:
                jt.click_s += time.perf_counter() - t_click_0
                return ApplyOutcome(
                    status=ApplicationStatus.SKIPPED,
                    reason=SkipReason.DRY_RUN,
                    detail=f"submission blocked by run policy: {str(exc)[:100]}",
                    attempts=attempts,
                )
            except Exception as exc:
                click_exc = exc
                if click_attempt == 1:
                    # One transient failure: clear overlays, re-resolve the button
                    # (the old locator may be detached after a re-render), retry once.
                    await dismiss_overlays(self.page)
                    await human_pause(500, 1_000)
                    fresh_btn = await first_visible(self.page, ["button#apply-button", *S.JD_APPLY_BUTTON], timeout_ms=4_000)
                    if fresh_btn is not None:
                        apply_btn = fresh_btn
                    continue
        if click_exc is not None:
            shot = await self.artifacts.capture_failure(self.page, "apply-click", profile, job.job_id)
            jt.click_s += time.perf_counter() - t_click_0
            return ApplyOutcome(status=ApplicationStatus.FAILED, detail=f"Click failed after 2 attempts: {str(click_exc)[:100]}", screenshot_path=shot, attempts=attempts)

        # Post-click effect check (run 512: 8 clicks with zero page reaction —
        # React swallowed them on detached nodes, then every downstream check
        # confirmed nothing). Re-click ONCE only when the button is provably
        # untouched (same text, still enabled, same URL, no popup/drawer).
        # Double-submit risk is nil: a fired submit always disturbs something.
        await asyncio.sleep(2.5)
        if not await self._submit_effect_seen(pre_click_state):
            log.info("naukri.apply.no_click_effect_reclicking", job_id=job.job_id)
            try:
                fresh = await first_visible(self.page, ["button#apply-button", *S.JD_APPLY_BUTTON], timeout_ms=4_000)
                if fresh is not None:
                    try:
                        await fresh.click(force=True, timeout=3_000)
                    except Exception:
                        try:
                            await fresh.evaluate("node => node.click()")
                        except Exception:
                            pass
                    await asyncio.sleep(2.5)
            except Exception:
                pass
        jt.click_s += time.perf_counter() - t_click_0

        t_qdet_0 = time.perf_counter()
        event_type = await self._wait_for_post_apply_event()
        jt.q_detect_s += time.perf_counter() - t_qdet_0

        if event_type == "popup" or self._popup_opened:
            return ApplyOutcome(status=ApplicationStatus.EXTERNAL, reason=SkipReason.EXTERNAL_APPLY, detail="Third-party tab opened", attempts=attempts, external_url=self._is_company_url(self._last_popup_url))

        if event_type == "already_applied" or await self._fast_check(S.JD_ALREADY_APPLIED):
            return ApplyOutcome(
                status=ApplicationStatus.ALREADY_APPLIED,
                reason=SkipReason.ALREADY_APPLIED,
                detail="Already applied marker observed on page",
                attempts=attempts,
            )

        # Instant check for genuine success banner
        if event_type == "success" or await self._fast_check(S.APPLY_SUCCESS):
            return ApplyOutcome(
                status=ApplicationStatus.APPLIED,
                detail="Naukri submission confirmation observed",
                attempts=attempts,
                confirmation_type=event_type if event_type != "timeout" else "dom_marker",
                confirmation_evidence="Naukri ACP confirmation or success marker observed",
            )

        chatbot = ChatbotHandler(
            self.page,
            self.answers,
            self.policy,
            max_questions=self.max_questions,
        )
        if event_type == "chatbot" or await self._fast_check(S.CHATBOT_DRAWER):
            t_qans_0 = time.perf_counter()
            result = await chatbot.run()
            jt.q_answer_s += time.perf_counter() - t_qans_0

            outcome = await self._handle_chatbot_result(job, profile, attempts, result)
            if outcome is not None:
                return outcome

            shot = await self.artifacts.capture_failure(
                self.page,
                "chatbot-no-confirmation",
                profile,
                job.job_id,
            )
            return ApplyOutcome(
                status=ApplicationStatus.FAILED,
                detail="Screening answers sent but no submission confirmation observed",
                screenshot_path=shot,
                questions_answered=result.answered,
                attempts=attempts,
            )

        toast = await safe_text(await first_visible(self.page, S.APPLY_ERROR_TOAST, timeout_ms=1_000))
        if toast:
            shot = await self.artifacts.capture_failure(self.page, "apply-error-toast", profile, job.job_id)
            return ApplyOutcome(status=ApplicationStatus.FAILED, detail=f"Toast error: {toast[:100]}", screenshot_path=shot, attempts=attempts)

        # Fail-Fast Verification: Check page title and DOM confirmation markers
        t_ver_0 = time.perf_counter()
        is_success = False
        try:
            cur_title = await self.page.title()
            if "Apply Confirmation" in cur_title or "/apply/confirmation" in self.page.url:
                is_success = True
        except Exception:
            pass
        if not is_success:
            is_success = await first_visible(self.page, S.APPLY_SUCCESS, timeout_ms=3_000) is not None
        jt.verify_s += time.perf_counter() - t_ver_0

        if is_success:
            return ApplyOutcome(
                status=ApplicationStatus.APPLIED,
                detail="Naukri submission confirmation observed",
                attempts=attempts,
                confirmation_type="dom_marker",
                confirmation_evidence="Naukri success or ACP confirmation marker observed after submission",
            )

        if await first_visible(self.page, S.JD_ALREADY_APPLIED, timeout_ms=1_000) is not None:
            return ApplyOutcome(
                status=ApplicationStatus.ALREADY_APPLIED,
                reason=SkipReason.ALREADY_APPLIED,
                detail="Already applied marker observed after submission check",
                attempts=attempts,
            )

        # Verify pass (runs 488/490: 6 straight "no confirmation" fails on the
        # secondary account): the submit click already fired, so a stuck drawer
        # DOM proves nothing. One fresh reload + marker re-check separates a
        # genuinely lost submit from a confirmation the stale page never
        # rendered. A reload is a plain GET — it can never double-submit.
        verdict = await self._verify_post_submit(job)
        if verdict == "applied":
            return ApplyOutcome(
                status=ApplicationStatus.APPLIED,
                detail="Naukri submission confirmation observed on verify pass",
                attempts=attempts,
                confirmation_type="verify_pass",
                confirmation_evidence="Success marker observed after job page reload",
            )
        if verdict == "already_applied":
            return ApplyOutcome(
                status=ApplicationStatus.ALREADY_APPLIED,
                reason=SkipReason.ALREADY_APPLIED,
                detail="Already applied marker observed on verify pass",
                attempts=attempts,
            )

        # Late-drawer recovery: the screening chatbot can hydrate AFTER the
        # 15s post-click event poll on slow renders. Answering it now still
        # completes the apply — declaring the submit lost without this check
        # is what manufactured part of the "No success confirmation" backlog.
        # Read-only probe first: no drawer, no extra work.
        if await self._fast_check(S.CHATBOT_DRAWER):
            log.info("naukri.apply.late_drawer_recovery", job_id=job.job_id, title=job.title[:60])
            t_late_0 = time.perf_counter()
            late_chatbot = ChatbotHandler(
                self.page,
                self.answers,
                self.policy,
                max_questions=self.max_questions,
            )
            late_result = await late_chatbot.run()
            jt.q_answer_s += time.perf_counter() - t_late_0
            late_outcome = await self._handle_chatbot_result(job, profile, attempts, late_result)
            if late_outcome is not None:
                return late_outcome

        shot = await self.artifacts.capture_failure(self.page, "no-confirmation", profile, job.job_id)
        return ApplyOutcome(status=ApplicationStatus.FAILED, detail="No success confirmation within SLA", screenshot_path=shot, attempts=attempts)

    async def _handle_chatbot_result(self, job: Job, profile: str, attempts: int, result) -> ApplyOutcome | None:
        """Map a finished screening run to an outcome.

        Returns None when answers went in but nothing confirms submission —
        the caller decides the fallback (main branch fails closed with its
        specific message; late-drawer recovery keeps hunting). Never raises.
        """
        if result.unfit:
            unfit_reason = getattr(result, "unfit_reason", None) or SkipReason.FILTER_EXPERIENCE
            log.info("naukri.apply.unfit_skip", job_id=job.job_id,
                     reason=unfit_reason.value, detail=result.unfit[:150])
            detail = result.unfit[:200] if unfit_reason != SkipReason.FILTER_EXPERIENCE else f"Tenure shortfall: {result.unfit[:200]}"
            return ApplyOutcome(status=ApplicationStatus.SKIPPED, reason=unfit_reason, detail=detail, questions_answered=result.answered, attempts=attempts)

        if result.unanswered:
            shot = await self.artifacts.screenshot(self.page, "unanswered", profile, job.job_id)
            detail = "Unanswered screening question"
            if result.error:
                detail += f" (submit also failed: {result.error[:150]})"
            return ApplyOutcome(status=ApplicationStatus.NEEDS_REVIEW, reason=SkipReason.UNANSWERED_QUESTION, detail=detail, screenshot_path=shot, questions_answered=result.answered, unanswered_questions=list(result.unanswered), attempts=attempts)

        is_page_confirmed = (
            await self._fast_check(S.APPLY_SUCCESS)
            or "applied" in self.page.url.lower()
            or "confirmation" in (await self.page.title()).lower()
            or await self.page.locator("meta[name='atdlayout'][content='jobapplied'], meta[atdlayout='jobapplied']").count() > 0
        )

        if result.error:
            if is_page_confirmed:
                return ApplyOutcome(
                    status=ApplicationStatus.APPLIED,
                    detail="Naukri submission confirmation observed",
                    questions_answered=result.answered,
                    attempts=attempts,
                    confirmation_type="confirmation_page",
                    confirmation_evidence="Apply Confirmation page or jobapplied layout observed",
                )
            shot = await self.artifacts.capture_failure(self.page, "chatbot-error", profile, job.job_id)
            return ApplyOutcome(status=ApplicationStatus.FAILED, detail=f"Chatbot error: {result.error}", screenshot_path=shot, questions_answered=result.answered, attempts=attempts)

        toast = await safe_text(await first_visible(self.page, S.APPLY_ERROR_TOAST, timeout_ms=1_000))
        if toast:
            shot = await self.artifacts.capture_failure(self.page, "apply-rejected", profile, job.job_id)
            return ApplyOutcome(status=ApplicationStatus.FAILED, detail=f"Naukri rejection: {toast[:100]}", screenshot_path=shot, questions_answered=result.answered, attempts=attempts)

        if await self._fast_check(S.JD_ALREADY_APPLIED):
            return ApplyOutcome(
                status=ApplicationStatus.ALREADY_APPLIED,
                reason=SkipReason.ALREADY_APPLIED,
                detail="Already applied marker observed after screening",
                questions_answered=result.answered,
                attempts=attempts,
            )

        confirmed = result.completed or is_page_confirmed
        if confirmed:
            return ApplyOutcome(
                status=ApplicationStatus.APPLIED,
                detail="Naukri submission confirmation observed",
                questions_answered=result.answered,
                attempts=attempts,
                confirmation_type="chatbot_completed" if result.completed else "dom_marker",
                confirmation_evidence="Screening flow completed or Naukri success marker observed",
            )
        return None

    async def _verify_post_submit(self, job: Job) -> str:
        """Reload the job page and re-check confirmation markers.

        Returns 'applied' | 'already_applied' | 'unknown'. Pure observation —
        markers must be present; absence is never treated as success.
        """
        try:
            await self.page.goto(job.url, wait_until="domcontentloaded", timeout=self.nav_timeout_ms)
        except Exception as exc:
            log.debug("apply.verify_goto_failed", job_id=job.job_id, error=str(exc)[:120])
            return "unknown"
        await human_pause(1000, 2000)
        try:
            title = await self.page.title()
            if "Apply Confirmation" in (title or "") or "/apply/confirmation" in (self.page.url or ""):
                return "applied"
        except Exception:
            pass
        try:
            if await first_visible(self.page, S.APPLY_SUCCESS, timeout_ms=4_000) is not None:
                return "applied"
        except Exception:
            pass
        try:
            if await first_visible(self.page, S.JD_ALREADY_APPLIED, timeout_ms=2_000) is not None:
                return "already_applied"
        except Exception:
            pass
        return "unknown"
