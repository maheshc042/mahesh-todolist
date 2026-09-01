"""
LinkedIn Easy Apply Platform Implementation.

Automates:
- Logging in and reusing stored session cookies.
- Searching for fresh Easy Apply job postings (f_AL=true, f_TPR=r86400).
- Scraping job listings, titles, companies, locations, and descriptions.
- Multi-step Easy Apply modal handling (contact info, resume attachment, screening questions, review, and submission).
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Callable

from playwright.async_api import Page

from ..browser.artifacts import ArtifactStore
from ..browser.resilience import (
    click_if_present,
    first_visible,
    human_pause,
    human_type,
    safe_text,
    scroll_page,
)
from ..config import AgentConfig, JobProfile, NaukriAccount, get_settings
from ..core.answers import AnswerEngine
from ..core.models import (
    ApplicationStatus,
    ApplyOutcome,
    FilterDecision,
    Job,
    SkipReason,
    extract_description_metadata,
)
from ..core.run_policy import RunPolicy
from ..logging_setup import get_logger
from .base import BaseJobPlatform

log = get_logger(__name__)


class LinkedInPlatform(BaseJobPlatform):
    def __init__(
        self,
        page: Page,
        account: NaukriAccount,
        artifacts: ArtifactStore,
        answers: AnswerEngine,
        policy: RunPolicy,
    ):
        super().__init__(page, account.key, policy)
        self.account = account
        self.artifacts = artifacts
        self.answers = answers

    @property
    def platform_name(self) -> str:
        return "linkedin"

    async def ensure_logged_in(self) -> bool:
        """Checks if session is active or allows manual login in headed mode."""
        log.info("linkedin.auth.check")
        try:
            await self.page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=25000)
            await human_pause(2000, 3500)
        except Exception as exc:
            log.warning("linkedin.auth.nav_failed", error=str(exc))

        if await first_visible(
            self.page,
            [
                "nav.global-nav",
                "img.global-nav__me-photo",
                "a[href*='/in/']",
                ".feed-identity-module",
                "button[aria-label*='Account']",
            ],
        ):
            log.info("linkedin.auth.session_reused")
            return True

        log.warning(
            "linkedin.auth.manual_login_required",
            msg="Please log in to LinkedIn in the browser window. Waiting up to 60 seconds...",
        )
        try:
            await self.page.wait_for_selector(
                "nav.global-nav, img.global-nav__me-photo, a[href*='/in/'], .feed-identity-module",
                timeout=60000,
            )
            log.info("linkedin.auth.manual_login_success")
            return True
        except Exception:
            log.error("linkedin.auth.timeout")
            return False

    def _get_search_url(self, profile: JobProfile) -> str:
        """Constructs target Easy Apply search URL."""
        if "ai" in profile.name.lower() or "python" in profile.name.lower():
            keywords = "AI Engineer OR Python Developer OR GenAI OR LLM"
        else:
            keywords = "Full Stack Developer OR React Developer OR MERN Stack"

        encoded = keywords.replace(" ", "%20").replace('"', "%22")
        return (
            f"https://www.linkedin.com/jobs/search/?"
            f"keywords={encoded}&f_AL=true&f_TPR=r86400&location=India&sortBy=DD"
        )

    async def fetch_jobs(self, profile: JobProfile, exclude_job_ids: set[str]) -> list[Job]:
        """Fetches fresh Easy Apply jobs from LinkedIn job search."""
        search_url = self._get_search_url(profile)
        log.info("linkedin.fetch.start", profile=profile.name, url=search_url)

        try:
            await self.page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            await human_pause(2500, 4500)
        except Exception as exc:
            log.warning("linkedin.fetch.goto_error", error=str(exc))
            return []

        # Scroll the jobs list pane to trigger dynamic loading
        for _ in range(6):
            await self.page.mouse.wheel(0, 800)
            await human_pause(1000, 1800)

        card_selectors = [
            "li.jobs-search-results__list-item",
            "div.job-card-container",
            "li[data-occludable-job-id]",
            "div[data-job-id]",
        ]

        cards = await self.page.locator(
            ", ".join(card_selectors)
        ).all()

        log.info("linkedin.fetch.cards_found", count=len(cards))
        jobs: list[Job] = []

        for card in cards:
            try:
                title_el = await first_visible(
                    card,
                    [
                        "a.job-card-list__title",
                        ".job-card-container__link",
                        "a[href*='/jobs/view/']",
                        "strong",
                    ],
                )
                company_el = await first_visible(
                    card,
                    [
                        ".job-card-container__primary-description",
                        ".artdeco-entity-lockup__subtitle",
                        "span.job-card-container__publisher",
                    ],
                )
                loc_el = await first_visible(
                    card,
                    [
                        ".job-card-container__metadata-item",
                        ".artdeco-entity-lockup__caption",
                    ],
                )

                title = (await safe_text(title_el)).strip()
                company = (await safe_text(company_el)).strip()
                location = (await safe_text(loc_el)).strip()

                if not title:
                    continue

                url = ""
                if title_el:
                    url = (await title_el.get_attribute("href") or "").split("?")[0]
                if url and not url.startswith("http"):
                    url = f"https://www.linkedin.com{url}"

                # Extract Job ID
                card_id_attr = (
                    await card.get_attribute("data-occludable-job-id")
                    or await card.get_attribute("data-job-id")
                    or ""
                )
                if not card_id_attr:
                    match = re.search(r"/view/(\d+)", url)
                    card_id_attr = match.group(1) if match else Job.stable_id(url, title, company)

                job_id = f"linkedin-{card_id_attr}"

                if job_id in exclude_job_ids:
                    continue

                job = Job(
                    job_id=job_id,
                    title=title,
                    company=company or "Confidential",
                    url=url or search_url,
                    location=location,
                )
                jobs.append(job)

            except Exception as exc:
                log.debug("linkedin.card_parse_error", error=str(exc))
                continue

        log.info("linkedin.fetch.success", valid_jobs=len(jobs))
        return jobs

    async def apply_to_job(
        self,
        job: Job,
        profile_name: str,
        pre_submit_check: Callable[[Job], FilterDecision] | None = None,
    ) -> ApplyOutcome:
        """Executes full LinkedIn Easy Apply dialog automation."""
        log.info("linkedin.apply.start", job_id=job.job_id, title=job.title, company=job.company)

        try:
            if job.url and job.url.startswith("http"):
                await self.page.goto(job.url, wait_until="domcontentloaded", timeout=25000)
                await human_pause(1800, 3000)
        except Exception as exc:
            log.warning("linkedin.apply.nav_failed", error=str(exc))

        # Extract Job Description text for parsing
        desc_el = await first_visible(
            self.page,
            [
                "#job-details",
                ".jobs-description__content",
                ".jobs-box__html-content",
                "article",
            ],
        )
        job.description = await safe_text(desc_el)
        extract_description_metadata(job)

        if pre_submit_check:
            decision = pre_submit_check(job)
            if not decision.passed:
                log.info("linkedin.apply.rejected_by_filter", reason=decision.reason, detail=decision.detail)
                return ApplyOutcome(
                    status=ApplicationStatus.SKIPPED,
                    reason=decision.reason,
                    detail=decision.detail,
                )

        # Look for Easy Apply Button
        apply_btn = await first_visible(
            self.page,
            [
                "button.jobs-apply-button",
                "button[aria-label*='Easy Apply']",
                "button:has-text('Easy Apply')",
            ],
        )

        if not apply_btn:
            log.info("linkedin.apply.no_easy_apply_button", job_id=job.job_id)
            return ApplyOutcome(
                status=ApplicationStatus.EXTERNAL,
                reason=SkipReason.EXTERNAL_APPLY,
                detail="External apply only / No Easy Apply button found",
            )

        # Mutation check before clicking apply
        self.require_mutation("apply")

        await apply_btn.click()
        await human_pause(1500, 2500)

        # Multi-step Easy Apply Modal Loop
        modal_selector = "div.jobs-easy-apply-modal, div[role='dialog']:has(button[aria-label*='Dismiss'])"
        modal = await first_visible(self.page, [modal_selector, "div[role='dialog']"])

        if not modal:
            log.warning("linkedin.apply.modal_not_found")
            return ApplyOutcome(
                status=ApplicationStatus.FAILED,
                detail="Easy apply dialog did not appear",
            )

        # Handle modal steps (up to 8 steps)
        for step in range(1, 9):
            await human_pause(1200, 2000)

            # Check if modal is completed or submit button is ready
            submit_btn = await first_visible(
                self.page,
                [
                    "button[aria-label='Submit application']",
                    "button:has-text('Submit application')",
                ],
            )

            if submit_btn:
                log.info("linkedin.apply.submitting", step=step)
                await submit_btn.click()
                await human_pause(2000, 3500)

                # Close post-apply confirmation dialog if present
                await click_if_present(
                    self.page,
                    [
                        "button[aria-label='Dismiss']",
                        "button:has-text('Done')",
                        "button[data-control-name='save_application_dismiss_icon']",
                    ],
                )
                log.info("linkedin.apply.success", job_id=job.job_id)
                return ApplyOutcome(
                    status=ApplicationStatus.APPLIED,
                    detail="Submitted via LinkedIn Easy Apply",
                )

            # Answer any visible screening questions / form inputs on current page
            await self._fill_step_inputs(profile_name)

            # Check for Next / Review button
            next_btn = await first_visible(
                self.page,
                [
                    "button[aria-label='Continue to next step']",
                    "button:has-text('Next')",
                    "button[aria-label='Review your application']",
                    "button:has-text('Review')",
                ],
            )

            if next_btn:
                await next_btn.click()
                await human_pause(1500, 2500)
            else:
                log.debug("linkedin.apply.no_next_button", step=step)
                break

        # Fallback check after steps
        final_submit = await first_visible(
            self.page,
            ["button:has-text('Submit application')", "button[aria-label='Submit application']"],
        )
        if final_submit:
            await final_submit.click()
            await human_pause(2000, 3000)
            await click_if_present(self.page, ["button:has-text('Done')", "button[aria-label='Dismiss']"])
            return ApplyOutcome(
                status=ApplicationStatus.APPLIED,
                detail="Submitted via LinkedIn Easy Apply",
            )

        # If stuck on complex unhandled modal, dismiss it cleanly
        await click_if_present(self.page, ["button[aria-label='Dismiss']"])
        await human_pause(500, 1000)
        await click_if_present(self.page, ["button:has-text('Discard')"])

        return ApplyOutcome(
            status=ApplicationStatus.NEEDS_REVIEW,
            detail="Easy apply required custom inputs that exceeded automated step handling",
        )

    async def _fill_step_inputs(self, profile_name: str) -> None:
        """Fills radio chips, dropdowns, text inputs, textareas, and resume attachments on current Easy Apply step."""
        try:
            # 1. Resume selection or upload
            resume_path = (
                Path("resumes/CV_Mahesh_Chitakoti_2026.pdf")
                if "ai" in profile_name.lower() or "python" in profile_name.lower()
                else Path("resumes/CV_Mahesh_Chitakoti_2026_1_.pdf")
            )
            file_input = self.page.locator("input[type='file']").first
            if await file_input.is_visible() and resume_path.exists():
                try:
                    await file_input.set_input_files(str(resume_path.resolve()))
                    log.info("linkedin.apply.resume_uploaded", path=resume_path.name)
                    await human_pause(1000, 2000)
                except Exception as exc:
                    log.debug("linkedin.resume_upload_error", error=str(exc))

            # 2. Radio fieldsets (Yes/No, screening questions)
            radios = await self.page.locator("fieldset").all()
            for fs in radios:
                legend_el = fs.locator("legend").first
                legend = (await safe_text(legend_el)).strip()
                legend_low = legend.lower()

                # Try matching AnswerEngine first
                ans = self.answers.answer(legend) if self.answers else None
                if ans:
                    opt = fs.locator(f"label:has-text('{ans}'), input[value='{ans}']").first
                    if await opt.is_visible():
                        await opt.click()
                        continue

                if "authorized" in legend_low or ("sponsorship" not in legend_low and "yes" in legend_low):
                    yes_opt = fs.locator("label:has-text('Yes'), input[value='Yes'], label:has-text('yes')").first
                    if await yes_opt.is_visible():
                        await yes_opt.click()
                elif "sponsorship" in legend_low:
                    no_opt = fs.locator("label:has-text('No'), input[value='No'], label:has-text('no')").first
                    if await no_opt.is_visible():
                        await no_opt.click()
                else:
                    # Default to Yes if available
                    yes_fallback = fs.locator("label:has-text('Yes'), input[value='Yes']").first
                    if await yes_fallback.is_visible():
                        await yes_fallback.click()

            # 3. Dropdowns (<select> elements)
            selects = await self.page.locator("select").all()
            for sel in selects:
                val = await sel.input_value()
                if not val or val == "Select an option" or val == "0":
                    label_el = sel.locator("xpath=preceding::label[1]").first
                    label = (await safe_text(label_el)).lower()
                    ans = self.answers.answer(label) if self.answers else None
                    if ans:
                        try:
                            await sel.select_option(label=ans)
                            continue
                        except Exception:
                            pass

                    options = await sel.locator("option").all_inner_texts()
                    if len(options) > 1:
                        # Select first non-empty option or matching keyword
                        best_opt = None
                        for opt_text in options[1:]:
                            low_opt = opt_text.lower()
                            if any(k in low_opt for k in ["yes", "immediate", "bachelor", "professional", "native", "2", "3"]):
                                best_opt = opt_text
                                break
                        if not best_opt and len(options) > 1:
                            best_opt = options[1]
                        if best_opt:
                            try:
                                await sel.select_option(label=best_opt.strip())
                            except Exception:
                                pass

            # 4. Text & Numeric inputs
            inputs = await self.page.locator("input[type='text'], input[type='number'], textarea").all()
            for inp in inputs:
                val = await inp.input_value()
                if not val:
                    label_el = inp.locator("xpath=preceding::label[1]").first
                    label = (await safe_text(label_el)).strip()
                    ans = self.answers.answer(label) if self.answers else None
                    if ans:
                        await inp.fill(ans)
                        continue

                    label_low = label.lower()
                    if "experience" in label_low or "years" in label_low:
                        await inp.fill("2.6")
                    elif "notice" in label_low:
                        await inp.fill("0")
                    elif "current" in label_low and ("ctc" in label_low or "salary" in label_low):
                        await inp.fill("4")
                    elif "expected" in label_low and ("ctc" in label_low or "salary" in label_low):
                        await inp.fill("7")
                    elif "city" in label_low or "location" in label_low:
                        await inp.fill("Bengaluru")
                    elif "phone" in label_low or "mobile" in label_low:
                        pass  # Preserve pre-filled account phone

        except Exception as exc:
            log.debug("linkedin.step_fill_error", error=str(exc))
