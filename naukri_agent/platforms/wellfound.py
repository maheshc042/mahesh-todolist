"""
Wellfound (AngelList) Platform Implementation.
"""
from __future__ import annotations

import re
import time
from typing import Callable

from playwright.async_api import Page

from ..browser.artifacts import ArtifactStore
from ..browser.resilience import (
    click_if_present,
    first_visible,
    human_pause,
    human_type,
    retry_async,
    safe_text,
    scroll_page,
)
from ..config import JobProfile, NaukriAccount, get_settings
from ..core.gemini_writer import GeminiWriter
from ..core.models import ApplicationStatus, ApplyOutcome, FilterDecision, Job, SkipReason, extract_description_metadata
from ..core.run_policy import RunPolicy
from ..logging_setup import get_logger
from .base import BaseJobPlatform

log = get_logger(__name__)


class WellfoundPlatform(BaseJobPlatform):
    def __init__(self, page: Page, account: NaukriAccount, artifacts: ArtifactStore, policy: RunPolicy):
        super().__init__(page, account.key, policy)
        self.account = account
        self.artifacts = artifacts

        # Initialize GeminiWriter for Wellfound pitches
        settings = get_settings()
        self.gemini = GeminiWriter(api_key=getattr(settings, "gemini_api_key", ""))

    @property
    def platform_name(self) -> str:
        return "wellfound"

    async def ensure_logged_in(self) -> bool:
        await self.page.goto("https://wellfound.com/jobs", wait_until="domcontentloaded")
        await human_pause(1500, 3000)

        # Check if already logged in (session restored from DB)
        if await first_visible(self.page, ["button[aria-label='User Menu']", "a[href*='/profile']", "text=Applied"]):
            log.info("wellfound.auth.session_reused")
            return True

        log.warning("wellfound.auth.manual_login_required")
        log.warning(
            "Cloudflare blocks automated logins on Wellfound. Please run the bot in --headed mode, log in manually, and the session will be saved to the database for future headless runs."
        )

        # Wait up to 60 seconds for the user to log in manually during a headed run
        try:
            await self.page.wait_for_selector("button[aria-label='User Menu'], a[href*='/profile']", timeout=60000)
            log.info("wellfound.auth.manual_login_success")
            return True
        except Exception:
            log.error("wellfound.auth.timeout")
            return False

    async def fetch_jobs(self, profile: JobProfile, exclude_job_ids: set[str]) -> list[Job]:
        log.info("wellfound.fetch.start", profile=profile.name)
        await self.page.goto("https://wellfound.com/jobs", wait_until="domcontentloaded")
        await human_pause(2000, 4000)

        # Trigger lazy loading (Wellfound is an infinite scroll feed)
        await scroll_page(self.page, steps=5, delay_s=0.8)

        cards = await self.page.locator("div[data-test='JobCard'], div[class*='styles_component']").all()
        jobs = []

        for index, card in enumerate(cards, start=1):
            try:
                title_el = await first_visible(card, ["a[data-test='JobCard-JobTitle']", "h2", "a[class*='title']"])
                company_el = await first_visible(
                    card, ["h2[data-test='JobCard-CompanyName']", "div[class*='company']"]
                )

                title = await safe_text(title_el)
                company = await safe_text(company_el)

                url = ""
                if title_el:
                    url = await title_el.get_attribute("href") or ""

                if url.startswith("/"):
                    url = f"https://wellfound.com{url}"

                # Extract ID from URL
                match = re.search(r"/jobs/(\d+)-", url)
                job_id = f"wellfound-{match.group(1)}" if match else f"wellfound-{Job.stable_id(url, title, company)}"

                if job_id in exclude_job_ids:
                    continue

                jobs.append(
                    Job(
                        job_id=job_id,
                        title=title,
                        company=company,
                        url=url,
                        recommendation_tab="default",
                        recommendation_position=index,
                    )
                )
            except Exception as exc:
                log.debug("wellfound.fetch.parse_error", error=str(exc))
                continue

        log.info("wellfound.fetch.done", count=len(jobs))
        return jobs

    async def apply_to_job(
        self,
        job: Job,
        profile_name: str,
        pre_submit_check: Callable[[Job], FilterDecision] | None = None,
    ) -> ApplyOutcome:
        self.require_mutation("application.apply_flow")
        log.info("wellfound.apply.start", job_id=job.job_id)

        async def navigate():
            await self.page.goto(job.url, wait_until="domcontentloaded")
            await human_pause(1000, 2000)

        try:
            await retry_async(navigate, attempts=2, label=f"wellfound-open:{job.job_id}")
        except Exception as exc:
            return ApplyOutcome(ApplicationStatus.FAILED, detail=f"Failed to load: {str(exc)}")

        # Check if already applied
        if await first_visible(self.page, ["text=Applied", "button:has-text('Applied')"]):
            return ApplyOutcome(ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)

        # Enrich job description to generate a better pitch
        desc_el = await first_visible(self.page, ["div[data-test='JobDescription']", "div[class*='description']"])
        if desc_el:
            job.description = await safe_text(desc_el)
            links, emails = extract_description_metadata(job.description)
            job.form_links = links
            job.recruiter_emails = emails


        if pre_submit_check:
            decision = pre_submit_check(job)
            if not decision.passed:
                return ApplyOutcome(ApplicationStatus.SKIPPED, reason=decision.reason, detail=decision.detail)

        apply_btn = await first_visible(
            self.page, ["button:has-text('Apply')", "button:has-text('Apply now')", "a:has-text('Apply')"]
        )
        if not apply_btn:
            return ApplyOutcome(ApplicationStatus.FAILED, detail="Apply button not found")

        await apply_btn.click()
        await human_pause(1000, 2000)

        if self.page.url.startswith("http") and "wellfound.com" not in self.page.url.lower():
            return ApplyOutcome(
                ApplicationStatus.EXTERNAL,
                external_url=self.page.url,
                detail="External company-site application is not automated",
            )

        # Handle Pitch Textarea ("What interests you about working for this company?")
        textarea = await first_visible(
            self.page,
            [
                "textarea[name='note']",
                "textarea[name='userNote']",
                "textarea[placeholder*='interests' i]",
                "textarea[placeholder*='note' i]",
                "textarea",
            ],
            timeout_ms=4000,
        )
        if textarea:
            log.info("wellfound.apply.generating_pitch")
            # Reuse Gemini cold email logic to write the Wellfound pitch
            pitch = self.gemini.generate_email_body(
                role_name=job.title,
                job_description=job.description or "",
                company_name=job.company,
            )

            if not pitch:
                return ApplyOutcome(
                    ApplicationStatus.NEEDS_REVIEW,
                    detail="Could not generate a verified pitch; application not submitted",
                )

            await human_type(textarea, pitch)
            await human_pause(500, 1000)

        # Submit Application
        submit_btn = await first_visible(
            self.page,
            [
                "button:has-text('Apply')",
                "button:has-text('Send application')",
                "button:has-text('Submit application')",
                "button[type='submit']",
            ],
            timeout_ms=4000,
        )
        if submit_btn:
            await submit_btn.click()
            await human_pause(1500, 3000)

            # Check for success
            if await first_visible(
                self.page, ["text=Application sent", "text=You applied", "button:has-text('Applied')", "text=Applied"]
            ):
                return ApplyOutcome(
                    ApplicationStatus.APPLIED,
                    confirmation_type="dom_marker",
                    confirmation_evidence="Wellfound application success marker observed",
                )

        return ApplyOutcome(ApplicationStatus.FAILED, detail="No success confirmation received")

