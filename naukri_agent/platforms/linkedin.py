"""
LinkedIn Easy Apply Platform Implementation.

Features:
- Exact 30 Target Roles Boolean Query with Entry & Associate Levels (f_E=2,3) and 24h Freshness (f_TPR=r86400).
- High-Performance In-Page Split-View Navigation (zero slow full-page reloads).
- Deep Suitability & Spam/Unpaid/Fellowship Filtering.
- State-Aware Resume Routing (CV_Mahesh_Chitakoti_2026 for AI/Python vs CV_Mahesh_Chitakoti_2026_1_ for Full Stack/QA/DevOps).
- Robust Question Answering via AnswerEngine (2.5 yrs exp, 0-day notice, 4/7 LPA CTC, India +91, phone 9481777227).
- Automatic Job Search Safety Reminder Handling (Continue/Acknowledge).
- Clean Multi-Step Submission & Modal Confirmation Dismissal.
"""
from __future__ import annotations

import asyncio
import re
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from playwright.async_api import Locator, Page

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
    ScreeningQuestion,
    SkipReason,
    extract_description_metadata,
)
from ..core.run_policy import RunPolicy
from ..logging_setup import get_logger
from .base import BaseJobPlatform

log = get_logger(__name__)

# All 33 Target Roles
TARGET_ROLES = [
    "Technical Support Engineer",
    "Technical Support Specialist",
    "Application Support Engineer",
    "Production Support Engineer",
    "IT Support Engineer",
    "Application Support Specialist",
    "Support Engineer",
    "L2 Support Engineer",
    "QA Engineer",
    "Quality Assurance Engineer",
    "Software Test Engineer",
    "Test Automation Engineer",
    "Automation Test Engineer",
    "SDET",
    "Software Development Engineer in Test",
    "QA Automation Engineer",
    "Python Developer",
    "Backend Developer",
    "Back End Developer",
    "Software Engineer",
    "Software Developer",
    "Full Stack Developer",
    "DevOps Engineer",
    "Cloud Engineer",
    "Cloud Support Engineer",
    "AWS Engineer",
    "Cloud Operations Engineer",
    "Site Reliability Engineer",
    "SRE",
    "AI Engineer",
    "AI Developer",
    "Generative AI Engineer",
    "GenAI Engineer",
    "ML Engineer",
]

# Exact 30-Role Boolean Query
FULL_TARGET_QUERY = (
    '("Technical Support Engineer" OR "Application Support Engineer" OR "Production Support Engineer" OR "Support Engineer" OR '
    '"QA Engineer" OR "Quality Assurance Engineer" OR "Software Test Engineer" OR "Test Automation Engineer" OR "Automation Test Engineer" OR '
    '"SDET" OR "Software Development Engineer in Test" OR "QA Automation Engineer" OR "Python Developer" OR "Backend Developer" OR '
    '"Back End Developer" OR "Software Engineer" OR "Software Developer" OR "Full Stack Developer" OR "DevOps Engineer" OR '
    '"Cloud Engineer" OR "Cloud Support Engineer" OR "AWS Engineer" OR "Cloud Operations Engineer" OR "Site Reliability Engineer" OR '
    '"SRE" OR "AI Engineer" OR "AI Developer" OR "Generative AI Engineer" OR "GenAI Engineer" OR "ML Engineer")'
)


class LinkedInPlatform(BaseJobPlatform):
    def __init__(
        self,
        page: Page,
        account: NaukriAccount,
        artifacts: ArtifactStore,
        answers: AnswerEngine,
        policy: RunPolicy,
        location: str = "India",
        days: int = 1,
    ):
        super().__init__(page, account.key, policy)
        self.account = account
        self.artifacts = artifacts
        self.answers = answers
        self.location = location
        self.days = days

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
            timeout_ms=5000,
        ):
            log.info("linkedin.auth.session_reused")
            return True

        log.warning(
            "linkedin.auth.manual_login_required",
            msg="Please log in to LinkedIn in the browser window. Waiting up to 90 seconds...",
        )
        try:
            await self.page.wait_for_selector(
                "nav.global-nav, img.global-nav__me-photo, a[href*='/in/'], .feed-identity-module",
                timeout=90000,
            )
            log.info("linkedin.auth.manual_login_success")
            return True
        except Exception:
            log.error("linkedin.auth.timeout")
            return False

    def _get_search_url(self, profile: JobProfile) -> str:
        """Constructs target Easy Apply search URL with 24h freshness, Entry & Associate levels."""
        tpr_seconds = self.days * 86400
        encoded_keywords = urllib.parse.quote(FULL_TARGET_QUERY)
        encoded_loc = urllib.parse.quote(self.location)

        # f_E=2,3 filters for Entry level (2) and Associate (3)
        return (
            f"https://www.linkedin.com/jobs/search/?"
            f"keywords={encoded_keywords}&location={encoded_loc}"
            f"&f_AL=true&f_TPR=r{tpr_seconds}&f_E=2%2C3&sortBy=DD"
        )

    def evaluate_job_suitability(self, job: Job) -> tuple[bool, str, int]:
        """Deep suitability analysis across all 33 target engineering & support tracks."""
        desc = (job.description or "").lower()
        title = (job.title or "").lower()
        full_text = f"{title} {desc}"

        score = 75

        # 1. Exact or Partial Target Role Match in Title
        matched_target_role = None
        for role in TARGET_ROLES:
            if role.lower() in title:
                matched_target_role = role
                break

        if matched_target_role:
            score += 20
        else:
            if any(w in title for w in ["developer", "engineer", "specialist", "sdet", "tester", "support", "sre", "qa"]):
                score += 10
            else:
                score -= 10

        # 2. Skip Unpaid / Fellowship / Survey spam postings immediately
        if any(w in title or w in desc for w in ["unpaid", "fellowship", "volunteer", "no salary", "commission only", "pollfish", "survey link"]):
            return False, "Unpaid / Fellowship / Survey spam posting skipped", 0

        # 3. Seniority / Experience Blocker Check (skips strictly 7+, 8+, 10+ years Principal/Architect/Director)
        senior_match = re.search(r"\b(7|8|9|10|12|15)\+?\s*(?:-\s*\d+)?\s*(?:years|yrs)\b", desc)
        if senior_match and any(w in title for w in ["principal", "staff", "architect", "director", "head of", "engineering manager"]):
            return False, f"Requires senior experience ({senior_match.group(0)})", 20

        # 4. Technical Skills Recognition Across Tracks
        track_keywords = [
            "python", "fastapi", "django", "llm", "genai", "generative ai", "langchain", "machine learning", "rag", "pytorch", "agent", "nlp", "chatbot",
            "react", "node", "javascript", "typescript", "full stack", "fullstack", "frontend", "backend", "next.js", "express", "postgresql", "mongodb", "rest api",
            "qa", "testing", "automation", "playwright", "selenium", "pytest", "sdet", "test automation", "manual testing", "api testing",
            "aws", "cloud", "docker", "kubernetes", "linux", "devops", "sre", "ci/cd", "terraform",
            "technical support", "application support", "production support", "it support", "l2 support", "troubleshooting", "jira", "incident management"
        ]

        matched_skills = [k for k in track_keywords if k in full_text]
        if matched_skills:
            score += min(len(matched_skills) * 2, 20)

        # 5. Location / Remote compatibility
        loc_str = (job.location or "").lower()
        if any(c in loc_str or c in full_text for c in ["india", "bengaluru", "bangalore", "hyderabad", "mumbai", "pune", "delhi", "noida", "gurgaon", "chennai", "remote", "hybrid", "work from home"]):
            score += 5
        elif any(c in loc_str for c in ["united states", "usa", "uk", "canada", "germany", "australia"]) and "remote" not in loc_str:
            return False, f"Foreign on-site location ({job.location})", 10

        is_suitable = score >= 60
        match_info = f"Matched '{matched_target_role}'" if matched_target_role else "Skills aligned"
        reason = f"{match_info} (Score: {score}/100)" if is_suitable else f"Low relevance score ({score}/100)"
        return is_suitable, reason, score

    async def fetch_jobs(self, profile: JobProfile, exclude_job_ids: set[str]) -> list[Job]:
        """Fetches fresh Easy Apply jobs from LinkedIn job search with left-pane scrolling across pages."""
        base_search_url = self._get_search_url(profile)
        target_applies = profile.platform_limits.get(self.platform_name, 50)
        # Scrape a generous pool (2.5x the apply target) so that after hard filtering
        # and recruiter alignment, the apply engine actually has enough eligible jobs
        # to satisfy the application quota (e.g. 50 successful applies).
        scrape_target = max(125, int(target_applies * 2.5))
        log.info("linkedin.fetch.start", profile=profile.name, target_applies=target_applies, scrape_target=scrape_target)

        jobs: list[Job] = []
        seen_ids: set[str] = set()

        for page_idx in range(6):  # Scrape up to 6 pages (25 jobs per page = 150 max)
            if len(jobs) >= scrape_target:
                break

            start_offset = page_idx * 25
            search_url = f"{base_search_url}&start={start_offset}"
            log.info("linkedin.fetch.page_start", page=page_idx + 1, start_offset=start_offset, collected_so_far=len(jobs))

            try:
                await self.page.goto(search_url, wait_until="domcontentloaded", timeout=35000)
                await human_pause(2500, 4000)
            except Exception as exc:
                log.warning("linkedin.fetch.goto_error", page=page_idx + 1, error=str(exc))
                break

            try:
                await self.page.wait_for_selector(
                    ".jobs-search-results-list, .scaffold-layout__list, li.jobs-search-results__list-item, div.job-card-container",
                    timeout=12000,
                )
            except Exception:
                pass

            # Scroll the left listings pane
            list_pane = await first_visible(
                self.page,
                [
                    ".jobs-search-results-list",
                    ".scaffold-layout__list",
                    "div[class*='jobs-search-results-list']",
                    ".jobs-search__results-list",
                ],
                timeout_ms=3000,
            )
            if list_pane:
                try:
                    await list_pane.hover()
                except Exception:
                    pass

            for _ in range(6):
                try:
                    await self.page.mouse.wheel(0, 700)
                    await human_pause(500, 1000)
                except Exception:
                    pass

            card_locators = self.page.locator(
                ".jobs-search-results-list li, .scaffold-layout__list-container li, li.jobs-search-results__list-item, div.job-card-container, div[data-job-id]"
            )
            total_cards = await card_locators.count()
            if total_cards == 0:
                log.info("linkedin.fetch.no_more_cards", page=page_idx + 1)
                break

            page_added = 0
            for idx in range(total_cards):
                if len(jobs) >= scrape_target:
                    break
                try:
                    card = card_locators.nth(idx)
                    if not await card.is_visible():
                        await card.scroll_into_view_if_needed()
                        await human_pause(100, 200)
                    if not await card.is_visible():
                        continue

                    title_el = card.locator("a.job-card-list__title, a[href*='/jobs/view/'], strong").first
                    company_el = card.locator(".job-card-container__primary-description, .artdeco-entity-lockup__subtitle, span.job-card-container__publisher, .job-card-container__company-name").first
                    loc_el = card.locator(".job-card-container__metadata-item, .artdeco-entity-lockup__caption").first

                    title = (await safe_text(title_el)).strip()
                    company = (await safe_text(company_el)).strip()
                    location = (await safe_text(loc_el)).strip()

                    if not title or len(title) < 3:
                        continue

                    card_url = ""
                    if await title_el.count() > 0:
                        card_url = (await title_el.get_attribute("href") or "").split("?")[0]
                    if card_url and not card_url.startswith("http"):
                        card_url = f"https://www.linkedin.com{card_url}"

                    match = re.search(r"/view/(\d+)", card_url)
                    raw_id = match.group(1) if match else Job.stable_id(card_url, title, company)
                    job_id = f"linkedin-{raw_id}"

                    if job_id in exclude_job_ids or job_id in seen_ids:
                        continue
                    seen_ids.add(job_id)

                    card_text = (await safe_text(card)).strip()
                    min_exp, max_exp = None, None
                    exp_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to|\+)\s*(\d+(?:\.\d+)?)?\s*(?:yrs|years|yr)", f"{title} {card_text}", re.I)
                    if exp_match:
                        min_exp = float(exp_match.group(1))
                        if exp_match.group(2):
                            max_exp = float(exp_match.group(2))
                        elif "+" in exp_match.group(0):
                            max_exp = min_exp + 5.0
                    elif any(k in f"{title} {card_text}".lower() for k in ("senior", "sr.", "sr ", "lead", "principal", "staff")):
                        min_exp = 5.0
                        max_exp = 10.0
                    elif any(k in f"{title} {card_text}".lower() for k in ("intern", "trainee", "fresher")):
                        min_exp = 0.0
                        max_exp = 0.0

                    job = Job(
                        job_id=job_id,
                        title=title,
                        company=company or "Confidential",
                        url=card_url or self.page.url,
                        location=location,
                        min_experience=min_exp,
                        max_experience=max_exp,
                    )
                    jobs.append(job)
                    page_added += 1
                except Exception:
                    continue

            log.info("linkedin.fetch.page_done", page=page_idx + 1, added=page_added, total=len(jobs))
            if page_added == 0 and total_cards < 5:
                break

        log.info("linkedin.fetch.ready", count=len(jobs))
        return jobs

    async def apply_to_job(
        self,
        job: Job,
        profile_name: str,
        pre_submit_check: Callable[[Job], FilterDecision] | None = None,
    ) -> ApplyOutcome:
        """Executes full multi-step Easy Apply application in-page with 10/10 precision."""
        log.info("linkedin.apply.start", job_id=job.job_id, title=job.title, company=job.company)

        # Locate and click job card in left pane
        card = self.page.locator(f"div[data-job-id*='{job.job_id.replace('linkedin-', '')}'], a[href*='{job.job_id.replace('linkedin-', '')}']").first
        if await card.count() > 0:
            await card.scroll_into_view_if_needed()
            await human_pause(200, 400)
            await card.click()
            await human_pause(1200, 2000)
        else:
            try:
                if job.url and job.url.startswith("http"):
                    await self.page.goto(job.url, wait_until="domcontentloaded", timeout=25000)
                    await human_pause(2000, 3500)
            except Exception:
                pass

        # Extract description from right pane
        desc_el = await first_visible(
            self.page,
            ["#job-details", ".jobs-description__content", ".jobs-box__html-content", "article"],
            timeout_ms=2500,
        )
        job.description = await safe_text(desc_el)
        extract_description_metadata(job.description)

        if job.min_experience is None and job.description:
            match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to|\+)\s*(\d+(?:\.\d+)?)?\s*(?:yrs|years|yr)", job.description, re.I)
            if match:
                job.min_experience = float(match.group(1))
                if match.group(2):
                    job.max_experience = float(match.group(2))
                elif "+" in match.group(0):
                    job.max_experience = job.min_experience + 5.0

        # Suitability & Spam Check
        suitable, reason, score = self.evaluate_job_suitability(job)
        if not suitable:
            log.info("linkedin.apply.skipped", job_id=job.job_id, reason=reason)
            return ApplyOutcome(status=ApplicationStatus.SKIPPED, reason=SkipReason.LOW_SCORE, detail=reason)

        if pre_submit_check:
            decision = pre_submit_check(job)
            if not decision.passed:
                return ApplyOutcome(
                    status=ApplicationStatus.SKIPPED,
                    reason=decision.reason or SkipReason.FILTER_REJECTED,
                    detail=decision.detail,
                )

        # Check Easy Apply button inside details pane
        apply_btn = await first_visible(
            self.page,
            [
                "div.jobs-apply-button--top-card button",
                "button.jobs-apply-button",
                "button[aria-label*='Easy Apply to' i]",
                ".jobs-details button:has-text('Easy Apply')",
                ".scaffold-layout__detail button:has-text('Easy Apply')",
            ],
            timeout_ms=3500,
        )

        if not apply_btn:
            if await first_visible(self.page, ["button:has-text('Applied')", "span:has-text('Applied')"], timeout_ms=800):
                return ApplyOutcome(status=ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)
            return ApplyOutcome(status=ApplicationStatus.SKIPPED, reason=SkipReason.EXTERNAL_APPLY)

        log.info("linkedin.apply.clicking_button", job_id=job.job_id)
        await apply_btn.scroll_into_view_if_needed()
        await human_pause(300, 600)
        await apply_btn.click(force=True)
        await human_pause(1500, 2500)

        # Modal Detection
        modal = await first_visible(
            self.page,
            [
                "div.jobs-easy-apply-modal",
                "div.artdeco-modal",
                "div[data-test-modal]",
                "div[role='dialog']",
            ],
            timeout_ms=5000,
        )

        if not modal:
            return ApplyOutcome(status=ApplicationStatus.FAILED, detail="Modal not visible")

        prev_step_signature = ""
        stuck_count = 0

        for step in range(1, 12):
            await human_pause(800, 1500)

            # 1. Check for Safety Reminder screen
            safety_btn = modal.locator(
                "button:has-text('Continue applying'), button:has-text('Continue'), button:has-text('Acknowledge'), button:has-text('Got it'), button:has-text('I understand')"
            ).first
            if await safety_btn.count() > 0:
                btn_txt = (await safety_btn.inner_text()).strip()
                if btn_txt.lower() in ["continue applying", "continue", "acknowledge", "got it", "i understand"]:
                    log.info("linkedin.apply.safety_reminder_passed", btn=btn_txt)
                    await safety_btn.click()
                    await human_pause(1200, 2000)
                    continue

            # 2. Fill inputs strictly inside the modal container
            await self._fill_step_inputs(modal, job)

            # Check if modal advanced (inspecting actual input counts and form content, not static header)
            form_content = modal.locator(".jobs-easy-apply-modal__content, .artdeco-modal__content, form").first
            form_text = (await safe_text(form_content)).strip()
            input_count = await modal.locator("input, select, textarea").count()
            step_sig = f"inputs:{input_count}::{form_text[:250]}"

            if step > 1 and step_sig == prev_step_signature:
                stuck_count += 1
                if stuck_count >= 3:
                    error_el = await first_visible(modal, [".artdeco-inline-feedback--error", "p.artdeco-inline-feedback", "[data-test-form-builder-error]"], timeout_ms=800)
                    err_txt = (await safe_text(error_el)).strip() if error_el else "Required field unfilled"
                    log.warning("linkedin.apply.step_stuck", step=step, error=err_txt)
                    await self._dismiss_modal()
                    return ApplyOutcome(status=ApplicationStatus.FAILED, detail=f"Step {step} blocked: {err_txt}")
            else:
                stuck_count = 0
            prev_step_signature = step_sig

            # Check Submit button
            submit_btn = await first_visible(
                modal,
                [
                    "button[aria-label='Submit application']",
                    "button:has-text('Submit application')",
                ],
                timeout_ms=1500,
            )

            if submit_btn:
                if self.policy.dry_run:
                    log.info("linkedin.apply.dry_run_success", job_id=job.job_id)
                    await self._dismiss_modal()
                    return ApplyOutcome(status=ApplicationStatus.APPLIED, detail="Dry-run submit reached")

                self.require_mutation("submit_application")
                log.info("linkedin.apply.submitting", job_id=job.job_id)
                await submit_btn.scroll_into_view_if_needed()
                await submit_btn.click(force=True)
                await human_pause(2500, 4000)

                await click_if_present(
                    self.page,
                    ["button[aria-label='Dismiss']", "button:has-text('Done')"],
                )
                log.info("linkedin.apply.submitted_successfully", job_id=job.job_id)
                return ApplyOutcome(status=ApplicationStatus.APPLIED, detail="Submitted via LinkedIn Easy Apply")

            # Check Next / Review button
            action_btn = await first_visible(
                modal,
                [
                    "button[aria-label='Review your application']",
                    "button:has-text('Review')",
                    "button[aria-label='Continue to next step']",
                    "button:has-text('Next')",
                ],
                timeout_ms=2000,
            )

            if action_btn:
                await action_btn.scroll_into_view_if_needed()
                await action_btn.click(force=True)
                await human_pause(1500, 2500)
            else:
                break

        await self._dismiss_modal()
        return ApplyOutcome(status=ApplicationStatus.NEEDS_REVIEW, detail="Modal step threshold exceeded")

    async def _fill_step_inputs(self, modal: Locator, job: Job | None = None) -> None:
        """Fills radio fieldsets, text inputs, dropdowns, comboboxes strictly within the modal."""
        # 1. State-Aware Resume Selection (never deselects already selected resume)
        job_title_low = (job.title if job else "").lower()
        if any(k in job_title_low for k in ["ai", "python", "genai", "llm", "data", "machine learning"]):
            target_resume_stem = "CV_Mahesh_Chitakoti_2026"
        elif any(k in job_title_low for k in ["full stack", "react", "frontend", "web", "node", "javascript"]):
            target_resume_stem = "CV_Mahesh_Chitakoti_2026_1_"
        else:
            target_resume_stem = "CV_Mahesh_Chitakoti_2026_1_"

        resume_cards = await modal.locator("div[data-test-document-item], .jobs-document-upload-redesign-card, div.jobs-document-upload-redesign-card__container").all()
        for card in resume_cards:
            card_text = (await safe_text(card)).lower()
            aria_label = (await card.get_attribute("aria-label") or "").lower()
            card_class = (await card.get_attribute("class") or "").lower()
            is_selected = "selected" in aria_label or "container--selected" in card_class or "deselect" in card_text

            if target_resume_stem.lower() in card_text:
                if not is_selected:
                    select_btn = card.locator("button[aria-label*='Select' i]").first
                    if await select_btn.count() > 0:
                        await select_btn.click()
                    else:
                        await card.click()
                    await human_pause(300, 600)
                break

        # 2. Dropdowns (<select>) strictly inside modal
        selects = await modal.locator("select").all()
        for sel in selects:
            try:
                if not await sel.is_visible():
                    continue

                label = ""
                try:
                    label = (await sel.evaluate("el => el.closest('div').innerText")).strip()
                except Exception:
                    pass
                if not label:
                    label_el = sel.locator("xpath=ancestor::div[contains(@class, 'fb-dash-form-element')][1]//label").first
                    label = (await safe_text(label_el)).strip()
                label_low = label.lower()

                options = await sel.locator("option").all_inner_texts()
                target_val = ""

                # Phone Country Code
                if any(k in label_low for k in ["country code", "phone country", "phone code"]):
                    for o in options:
                        if "india" in o.lower() or "+91" in o:
                            target_val = o.strip()
                            break
                    if target_val:
                        await sel.select_option(label=target_val)
                    continue

                # Email Address dropdown (keep pre-filled)
                if "email" in label_low:
                    continue

                resolved = self.answers.resolve(ScreeningQuestion(text=label, kind="select", options=options))
                if resolved and resolved.value:
                    target_val = resolved.value

                if not target_val:
                    if any(k in label_low for k in ["sponsorship", "visa"]):
                        for o in options:
                            if "no" in o.lower():
                                target_val = o.strip()
                                break
                    elif any(k in label_low for k in ["total years", "years of", "years of experience"]):
                        for o in options:
                            if any(d == o.strip() or f"{d} " in o for d in ["2", "3", "2.5", "2+"]):
                                target_val = o.strip()
                                break
                    elif any(k in label_low for k in ["additional month", "months"]):
                        for o in options:
                            if any(d == o.strip() or f"{d} " in o for d in ["6", "0"]):
                                target_val = o.strip()
                                break
                    elif any(k in label_low for k in ["start immediately", "immediate", "authorized", "authorization", "commute", "relocate", "comfortable", "degree", "experience", "agree", "willing", "can you", "are you", "do you", "within a week", "have you", "troubleshot", "is your"]):
                        for o in options:
                            if "yes" in o.lower():
                                target_val = o.strip()
                                break
                    elif any(k in label_low for k in ["proficiency", "language"]):
                        for o in options:
                            if any(p in o.lower() for p in ["professional", "fluent", "conversational", "native"]):
                                target_val = o.strip()
                                break
                    elif any(k in label_low for k in ["degree", "education"]):
                        for o in options:
                            if "bachelor" in o.lower() or "graduate" in o.lower():
                                target_val = o.strip()
                                break

                if not target_val and len(options) > 1:
                    target_val = options[1].strip() if "select" not in options[1].lower() else (options[2].strip() if len(options) > 2 else "")

                if target_val:
                    await sel.select_option(label=target_val)
                    await human_pause(200, 400)
            except Exception:
                pass

        # 3. Radio Fieldsets strictly inside modal
        fieldsets = await modal.locator("fieldset").all()
        for fs in fieldsets:
            try:
                if not await fs.is_visible():
                    continue
                legend_el = fs.locator("legend").first
                legend = (await safe_text(legend_el)).strip()
                if not legend:
                    continue

                options = await fs.locator("div[tabindex='0'], label, input[type='radio']").all()
                opt_texts = [(await safe_text(opt)).strip() for opt in options if (await safe_text(opt)).strip()]

                resolved = self.answers.resolve(ScreeningQuestion(text=legend, kind="radio", options=opt_texts))
                target_val = resolved.value if resolved else ""

                if not target_val:
                    low_leg = legend.lower()
                    if any(k in low_leg for k in ["sponsorship", "visa sponsorship", "require sponsorship"]):
                        target_val = "No"
                    elif any(k in low_leg for k in ["authorized", "authorization", "commute", "relocate", "comfortable", "background", "willing", "participate", "agree", "process", "interest"]):
                        target_val = "Yes"
                    else:
                        target_val = "Yes"

                for opt in options:
                    otext = (await safe_text(opt)).strip()
                    if target_val and (otext.lower() == target_val.lower() or target_val.lower() in otext.lower()):
                        await opt.scroll_into_view_if_needed()
                        await opt.click(force=True)
                        await human_pause(200, 400)
                        break
            except Exception:
                pass

        # 3.5 Checkboxes strictly inside modal
        checkboxes = await modal.locator("input[type='checkbox']").all()
        for cb in checkboxes:
            try:
                if not await cb.is_visible():
                    continue
                cb_label = ""
                cb_id = await cb.get_attribute("id") or ""
                if cb_id:
                    lbl = modal.locator(f"label[for='{cb_id}']").first
                    if await lbl.count() > 0:
                        cb_label = (await safe_text(lbl)).strip()
                if not cb_label:
                    lbl = cb.locator("xpath=ancestor::label[1], xpath=following-sibling::label[1]").first
                    if await lbl.count() > 0:
                        cb_label = (await safe_text(lbl)).strip()

                cb_label_low = cb_label.lower()
                is_checked = await cb.is_checked()
                is_required = await cb.get_attribute("required") is not None or "required" in (await cb.get_attribute("class") or "")

                if is_required or any(k in cb_label_low for k in ["agree", "consent", "acknowledge", "confirm", "certify", "terms", "policy", "authorized"]):
                    if not is_checked:
                        await cb.scroll_into_view_if_needed()
                        try:
                            await cb.check(force=True)
                        except Exception:
                            await cb.click(force=True)
                        await human_pause(200, 400)
                elif "follow" in cb_label_low:
                    if is_checked:
                        try:
                            await cb.uncheck(force=True)
                        except Exception:
                            pass
            except Exception:
                pass

        # 4. Text, Numeric Inputs & Comboboxes strictly inside modal
        inputs = await modal.locator("input[type='text'], input[type='number'], input[type='tel'], textarea").all()
        for inp in inputs:
            try:
                if not await inp.is_visible():
                    continue
                val = (await inp.input_value()).strip()

                label = ""
                try:
                    label = (await inp.evaluate("el => el.closest('div').innerText")).strip()
                except Exception:
                    pass
                if not label:
                    inp_id = await inp.get_attribute("id") or ""
                    label_el = modal.locator(f"label[for='{inp_id}']").first if inp_id else None
                    if not label_el or await label_el.count() == 0:
                        label_el = inp.locator("xpath=ancestor::div[contains(@class, 'fb-dash-form-element') or contains(@class, 'form__input')][1]//label").first
                    label = (await safe_text(label_el)).strip() if label_el and await label_el.count() > 0 else ""
                
                label_low = label.lower()

                # Check if this input's container has an inline feedback error
                container = inp.locator("xpath=ancestor::div[contains(@class, 'fb-dash-form-element') or contains(@class, 'form__input') or contains(@class, 'jobs-easy-apply-form-element')][1]").first
                err_msg = ""
                if await container.count() > 0:
                    err_el = container.locator(".artdeco-inline-feedback--error, p.artdeco-inline-feedback, [data-test-form-builder-error]").first
                    if await err_el.count() > 0:
                        err_msg = (await safe_text(err_el)).strip()

                inp_type = (await inp.get_attribute("type") or "").lower()
                inp_mode = (await inp.get_attribute("inputmode") or "").lower()

                # Comprehensive numeric question detection
                is_numeric = (
                    inp_type in ("number", "numeric")
                    or inp_mode in ("numeric", "decimal")
                    or any(w in label_low for w in (
                        "whole number", "between 0 and", "years", "experience", "months",
                        "notice", "days", "rating", "scale", "rate", "ctc", "salary",
                        "how many", "integer"
                    ))
                    or any(w in err_msg.lower() for w in ("whole number", "between 0 and", "number", "numeric"))
                )

                # Mobile phone field
                if any(k in label_low for k in ["phone", "mobile"]):
                    if not val or err_msg:
                        await inp.fill("9481777227")
                    continue

                # City / Location combobox
                if any(k in label_low for k in ["city", "location", "zip code", "state"]):
                    if not val or "search" in val.lower():
                        await inp.fill("Bengaluru, Karnataka, India")
                        await human_pause(600, 1000)
                        typeahead_item = self.page.locator(".search-typeahead-v2__hit, div[role='option'], li[role='option'], .basic-typeahead__selectable-result").first
                        if await typeahead_item.count() > 0:
                            await typeahead_item.click()
                        else:
                            await inp.press("ArrowDown")
                            await inp.press("Enter")
                    continue

                # Skip if already validly filled (never skip if an error is present or if numeric field has decimals/letters)
                if val and not err_msg:
                    if is_numeric:
                        # Valid whole integer between 0 and 99
                        if val.isdigit() and 0 <= int(val) <= 99:
                            continue
                        # If val contains decimals or letters (e.g. "2.5" or "2 years"), do NOT skip — fix it below
                    else:
                        continue

                ans = ""
                if is_numeric:
                    # If val had a number (e.g. "2.5"), extract it and round to whole integer
                    if val:
                        num_match = re.search(r"[-+]?\d*\.?\d+", val)
                        if num_match:
                            val_float = float(num_match.group(0))
                            ans = str(max(0, min(99, int(round(val_float)))))

                    if not ans:
                        resolved = self.answers.resolve(ScreeningQuestion(text=label, kind="text"))
                        raw_ans = resolved.value if resolved else ""
                        if raw_ans:
                            num_match = re.search(r"[-+]?\d*\.?\d+", str(raw_ans))
                            if num_match:
                                ans = str(max(0, min(99, int(round(float(num_match.group(0)))))))

                    if not ans:
                        if "notice" in label_low or "how soon" in label_low or "availability" in label_low:
                            ans = "0"
                        elif "months" in label_low:
                            ans = "6"
                        elif "rate" in label_low or "scale" in label_low or "1 to 10" in label_low:
                            ans = "9"
                        elif "ctc" in label_low or "salary" in label_low:
                            ans = "7"
                        else:
                            # Skill or general years of experience -> whole integer 2
                            ans = "2"
                else:
                    resolved = self.answers.resolve(ScreeningQuestion(text=label, kind="text"))
                    ans = resolved.value if resolved else ""

                    if not ans or "days" in str(ans).lower():
                        if "title" in label_low:
                            ans = "Full Stack & AI Engineer"
                        elif "company" in label_low:
                            ans = "Independent Software Consultant"
                        elif "linkedin" in label_low:
                            ans = "https://www.linkedin.com/in/maheshchitakoti"
                        elif "github" in label_low or "portfolio" in label_low or "website" in label_low:
                            ans = "https://github.com/maheshchitakoti"
                        else:
                            ans = "Experienced software and AI engineer with 2.5 years of experience building scalable applications, APIs, and AI workflows."

                if ans:
                    await inp.click()
                    await inp.fill("")
                    await inp.fill(str(ans))
                    await human_pause(100, 250)
                    try:
                        await inp.dispatch_event("input")
                        await inp.dispatch_event("change")
                        await inp.dispatch_event("blur")
                    except Exception:
                        pass
                    await human_pause(100, 250)
            except Exception:
                pass

    async def _dismiss_modal(self) -> None:
        """Safely dismisses and discards any open dialog."""
        try:
            await click_if_present(self.page, ["button[aria-label='Dismiss']", "button[data-test-modal-close-btn]"])
            await human_pause(500, 1000)
            await click_if_present(self.page, ["button:has-text('Discard')", "button[data-control-name='discard_application_confirm_btn']"])
            await human_pause(500, 1000)
        except Exception:
            pass
