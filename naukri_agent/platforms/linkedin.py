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

import re
import urllib.parse
from collections.abc import Callable

from playwright.async_api import Locator, Page

from ..browser.artifacts import ArtifactStore
from ..browser.resilience import (
    click_if_present,
    first_visible,
    human_pause,
    safe_text,
)
from ..config import AgentConfig, JobProfile, NaukriAccount
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

# High-Relevance Target Roles strictly aligned with candidate profiles (AI/Python & Full Stack/QA)
TARGET_ROLES = [
    "AI Engineer",
    "Generative AI Engineer",
    "GenAI Engineer",
    "AI Developer",
    "ML Engineer",
    "Machine Learning Engineer",
    "LLM Engineer",
    "Python AI Developer",
    "Python Developer",
    "Backend Developer",
    "Back End Developer",
    "Software Engineer",
    "Software Developer",
    "Full Stack Developer",
    "Full Stack Engineer",
    "Frontend Developer",
    "React Developer",
    "Node.js Developer",
    "Web Developer",
    "SDET",
    "Software Development Engineer in Test",
    "QA Automation Engineer",
    "Test Automation Engineer",
    "QA Engineer",
    "Software Test Engineer",
]

# High-Precision Boolean Queries per Track
AI_TARGET_QUERY = (
    '("AI Engineer" OR "Generative AI Engineer" OR "GenAI Engineer" OR "AI Developer" OR '
    '"ML Engineer" OR "Machine Learning Engineer" OR "LLM Engineer" OR "Python Developer" OR '
    '"Python AI Developer" OR "AI Software Engineer" OR "MLOps Engineer")'
)

FULLSTACK_TARGET_QUERY = (
    '("Full Stack Developer" OR "Full Stack Engineer" OR "Software Engineer" OR "Software Developer" OR '
    '"Frontend Developer" OR "React Developer" OR "Node.js Developer" OR "Backend Developer" OR '
    '"SDET" OR "QA Automation Engineer" OR "DevOps Engineer" OR "Web Developer")'
)

FULL_TARGET_QUERY = (
    '("AI Engineer" OR "Generative AI Engineer" OR "GenAI Engineer" OR "AI Developer" OR "ML Engineer" OR '
    '"Python Developer" OR "Backend Developer" OR "Software Engineer" OR "Software Developer" OR '
    '"Full Stack Developer" OR "Full Stack Engineer" OR "React Developer" OR "Node.js Developer" OR '
    '"SDET" OR "QA Automation Engineer" OR "Test Automation Engineer" OR "QA Engineer")'
)
MODAL_CONTAINER_SELECTORS = [
    "dialog:has(button[aria-label*='Dismiss' i])",
    "dialog",
    "div.jobs-easy-apply-modal",
    "div[data-view-name*='easy-apply']",
    "div[data-test-modal]",
    "div[role='dialog']:has(button[aria-label*='Dismiss' i])",
    "button[aria-label*='Dismiss' i]",
]


class LinkedInPlatform(BaseJobPlatform):
    def __init__(
        self,
        page: Page,
        account: NaukriAccount,
        artifacts: ArtifactStore,
        answers: AnswerEngine,
        policy: RunPolicy,
        config: AgentConfig | None = None,
        location: str = "India",
        days: int = 1,
    ):
        super().__init__(page, account.key, policy)
        self.account = account
        self.artifacts = artifacts
        self.answers = answers
        self.config = config or AgentConfig.load()
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

        is_headless = getattr(getattr(self.config, "browser", None), "headless", True)

        # Check for immediate Cloud IP challenge / checkpoint
        cur_url = self.page.url.lower()
        if "checkpoint" in cur_url or "challenge" in cur_url or "security-check" in cur_url:
            log.warning("linkedin.auth.checkpoint_detected", url=cur_url)
            if is_headless:
                log.error(
                    "linkedin.auth.checkpoint_in_headless",
                    msg="LinkedIn security checkpoint encountered in headless CI; skipping LinkedIn gracefully without stalling.",
                )
                return False

        # In headless CI mode, fail-fast without hanging for 90 seconds
        if is_headless:
            log.warning(
                "linkedin.auth.headless_session_missing",
                msg="No active LinkedIn session in Postgres. Run 'python -m naukri_agent login --platform linkedin' locally to save session.",
            )
            return False

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
        """Constructs target Easy Apply search URL tailored to candidate profile with 24h freshness."""
        tpr_seconds = self.days * 86400
        prof_name = (profile.name or "").lower()
        if any(k in prof_name for k in ["ai", "python", "machine learning", "ml"]):
            query = AI_TARGET_QUERY
        elif any(k in prof_name for k in ["full stack", "web", "frontend", "react"]):
            query = FULLSTACK_TARGET_QUERY
        else:
            query = FULL_TARGET_QUERY

        encoded_keywords = urllib.parse.quote(query)
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

        # 1. Block Non-Relevant Stacks, Senior Leadership & Non-Dev Roles in Title
        blocked_title_keywords = [
            "lead", "principal", "architect", "manager", "director", "head of", "intern", "internship",
            "java", "spring boot", "spring-boot", ".net", "dotnet", "dot net", "c#", "php", "wordpress",
            "business analyst", "erp", "sap", "salesforce", "sales", "bpo", "gis", "oac", "oracle dba",
            "mainframe", "teradata", "snowflake", "etl", "aem"
        ]
        for blocked_kw in blocked_title_keywords:
            if re.search(rf"\b{re.escape(blocked_kw)}\b", title):
                return False, f"Excluded role or tech stack ('{blocked_kw}')", 0

        # 1.5 Exact or Partial Target Role Match in Title
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
                score -= 15

        # 2. Skip Unpaid / Fellowship / Survey spam postings immediately
        if any(w in title or w in desc for w in ["unpaid", "fellowship", "volunteer", "no salary", "commission only", "pollfish", "survey link"]):
            return False, "Unpaid / Fellowship / Survey spam posting skipped", 0

        # 3. Seniority / Experience Blocker Check (skips strictly 7+, 8+, 10+ years Principal/Architect/Director)
        senior_match = re.search(r"\b(6|7|8|9|10|12|15)\+?\s*(?:-\s*\d+)?\s*(?:years|yrs)\b", desc)
        if senior_match and any(w in title for w in ["principal", "staff", "architect", "director", "head of", "engineering manager", "senior", "sr."]):
            return False, f"Requires senior experience ({senior_match.group(0)})", 20

        # 4. Location / Remote compatibility (Filter foreign on-site roles strictly)
        loc_str = (job.location or "").lower()
        is_foreign = any(c in loc_str for c in ["united states", "usa", "u.s.", "uk", "united kingdom", "canada", "germany", "australia", "austin", "texas", "california", "london"])
        is_india = any(c in loc_str for c in ["india", "bengaluru", "bangalore", "hyderabad", "mumbai", "pune", "delhi", "noida", "gurgaon", "chennai", "kolkata", "ahmedabad"])
        loc_remote = any(k in loc_str or k in title for k in ["remote", "work from home", "wfh", "anywhere"])

        if is_foreign and not is_india and not loc_remote:
            return False, f"Foreign on-site location ({job.location})", 10

        if is_india or loc_remote:
            score += 5

        # 5. Technical Skills Recognition Across Tracks
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
            target_page_num = page_idx + 1
            log.info("linkedin.fetch.page_start", page=target_page_num, start_offset=start_offset, collected_so_far=len(jobs))

            if page_idx == 0:
                try:
                    await self.page.goto(base_search_url, wait_until="domcontentloaded", timeout=35000)
                    await human_pause(2500, 4000)
                except Exception as exc:
                    log.warning("linkedin.fetch.goto_error", page=1, error=str(exc))
                    break
            else:
                page_clicked = False
                try:
                    # Scroll down to pagination control
                    pag_el = await first_visible(
                        self.page,
                        [
                            "ul.artdeco-pagination__pages",
                            ".jobs-search-pagination",
                            "div[class*='pagination']",
                        ],
                        timeout_ms=3000,
                    )
                    if pag_el:
                        await pag_el.scroll_into_view_if_needed()
                        await human_pause(500, 1000)

                    target_page_btn = await first_visible(
                        self.page,
                        [
                            f"button[aria-label='Page {target_page_num}']",
                            f"button[aria-label*='Page {target_page_num}']",
                            f"li[data-test-pagination-page-btn='{target_page_num}'] button",
                            f"ul.artdeco-pagination__pages button:has-text('{target_page_num}')",
                        ],
                        timeout_ms=3000,
                    )
                    if target_page_btn:
                        await target_page_btn.scroll_into_view_if_needed()
                        await human_pause(300, 600)
                        await target_page_btn.click(force=True)
                        log.info("linkedin.fetch.clicked_pagination_button", page=target_page_num)
                        await human_pause(2500, 4000)
                        page_clicked = True
                except Exception as exc:
                    log.debug("linkedin.fetch.in_page_pagination_failed", page=target_page_num, error=str(exc))

                if not page_clicked:
                    search_url = f"{base_search_url}&start={start_offset}"
                    try:
                        await self.page.goto(search_url, wait_until="domcontentloaded", timeout=25000)
                        await human_pause(2500, 4000)
                    except Exception as exc:
                        log.warning("linkedin.fetch.goto_error", page=target_page_num, error=str(exc))
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
                    title = re.sub(r"\s+with verification\b", "", title, flags=re.IGNORECASE).strip()
                    half_len = len(title) // 2
                    if half_len > 3 and title[:half_len].strip() == title[half_len:].strip():
                        title = title[:half_len].strip()
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
                    exp_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to|\+)\s*(\d+(?:\.\d+)?)?\s*(?:yrs|years|yr)", f"{title} {card_text}", re.IGNORECASE)
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
                        platform="linkedin",
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
            match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to|\+)\s*(\d+(?:\.\d+)?)?\s*(?:yrs|years|yr)", job.description, re.IGNORECASE)
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
            return ApplyOutcome(status=ApplicationStatus.SKIPPED, reason=SkipReason.LOW_MATCH_SCORE, detail=reason)

        if pre_submit_check:
            decision = pre_submit_check(job)
            if not decision.passed:
                return ApplyOutcome(
                    status=ApplicationStatus.SKIPPED,
                    reason=decision.reason or SkipReason.FILTER_REJECTED,
                    detail=decision.detail,
                )

        # Check Easy Apply button inside details pane or full page
        apply_btn = await first_visible(
            self.page,
            [
                "button:has-text('Easy Apply')",
                "button[aria-label*='Easy Apply' i]",
                "div.jobs-apply-button--top-card button",
                "button.jobs-apply-button",
                "button[aria-label*='Easy Apply to' i]",
                ".jobs-details button:has-text('Easy Apply')",
                ".scaffold-layout__detail button:has-text('Easy Apply')",
            ],
            timeout_ms=3500,
        )

        if not apply_btn:
            if await first_visible(self.page, ["button:has-text('Applied')", "span:has-text('Applied')", "span:has-text('Application submitted')", "div:has-text('Application submitted')"], timeout_ms=800):
                return ApplyOutcome(status=ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)
            return ApplyOutcome(status=ApplicationStatus.SKIPPED, reason=SkipReason.EXTERNAL_APPLY)

        log.info("linkedin.apply.clicking_button", job_id=job.job_id)
        await apply_btn.scroll_into_view_if_needed()
        await human_pause(400, 700)
        try:
            await apply_btn.click()
        except Exception:
            try:
                await apply_btn.click(force=True)
            except Exception:
                try:
                    await apply_btn.evaluate("node => node.click()")
                except Exception:
                    pass
        await human_pause(1800, 2800)

        # Handle any immediate Safety Reminder or intermediate screen
        safety_btn = await first_visible(
            self.page,
            [
                "button:has-text('Continue applying')",
                "button:has-text('Continue')",
                "button:has-text('Acknowledge')",
                "button:has-text('Got it')",
                "button:has-text('I understand')",
            ],
            timeout_ms=1500,
        )
        if safety_btn:
            try:
                await safety_btn.click()
                log.info("linkedin.apply.safety_reminder_passed")
                await human_pause(1200, 2000)
            except Exception:
                pass

        # Modal Detection (supporting modern CSS module hashed containers, slide-out drawers, and legacy modals)
        dismiss_btn = await first_visible(
            self.page,
            ["button[aria-label*='Dismiss' i]", "button[data-test-modal-close-btn]"],
            timeout_ms=5000,
        )
        if dismiss_btn:
            modal = self.page.locator(
                "dialog[open]:has(button[aria-label*='Dismiss' i]), "
                "dialog:has(button[aria-label*='Dismiss' i]), "
                "div[role='dialog']:has(button[aria-label*='Dismiss' i]), "
                "dialog[open], "
                "div.jobs-easy-apply-modal:visible, "
                "div[data-test-modal]:visible, "
                "div[data-view-name*='easy-apply']:visible"
            ).first
        else:
            modal = await first_visible(
                self.page,
                [
                    "dialog[open]:has(button[aria-label*='Dismiss' i])",
                    "dialog:has(button[aria-label*='Dismiss' i])",
                    "dialog[open]",
                    "dialog:visible",
                    "div.jobs-easy-apply-modal",
                    "div[data-view-name*='easy-apply']",
                    "div.artdeco-modal[role='dialog']",
                    "div[role='dialog']:visible",
                ],
                timeout_ms=3500,
            )

        if not modal or not await modal.is_visible():
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

            # Check Submit button immediately (before stuck checks or advancing)
            submit_loc = self.page.locator("button:has-text('Submit application'), button[aria-label*='Submit application' i]").first
            if await submit_loc.count() > 0:
                try:
                    await submit_loc.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                if self.policy.dry_run:
                    log.info("linkedin.apply.dry_run_success", job_id=job.job_id)
                    await self._dismiss_modal()
                    return ApplyOutcome(status=ApplicationStatus.APPLIED, detail="Dry-run submit reached")

                self.require_mutation("submit_application")
                log.info("linkedin.apply.submitting", job_id=job.job_id)
                await submit_loc.click(force=True)
                await human_pause(2500, 4000)

                # Check for explicit submission confirmation
                confirmation = await first_visible(
                    self.page,
                    [
                        "div[data-test-modal]:has-text('Application sent')",
                        "div[role='dialog']:has-text('Application sent')",
                        "h3:has-text('Application sent')",
                        "h2:has-text('Application sent')",
                        "span:has-text('Application sent')",
                        "div:has-text('Your application was sent')",
                        "button:has-text('Done')",
                        "button[aria-label='Done']",
                    ],
                    timeout_ms=5000,
                )

                # Click Done/Dismiss if present
                await click_if_present(
                    self.page,
                    ["button:has-text('Done')", "button[aria-label='Done']", "button[aria-label='Dismiss']"],
                )
                await human_pause(1000, 2000)

                modal_still_open = await first_visible(self.page, MODAL_CONTAINER_SELECTORS, timeout_ms=1000)
                submitted_badge = await first_visible(
                    self.page,
                    [
                        "span:has-text('Application submitted')",
                        "div:has-text('Application submitted')",
                        "button:has-text('Applied')",
                        "span:has-text('Applied')",
                        "div:has-text('Your application was sent')",
                        "div[data-test-modal]:has-text('Application sent')",
                    ],
                    timeout_ms=1500,
                )
                if confirmation or submitted_badge or not modal_still_open:
                    log.info("linkedin.apply.submitted_successfully", job_id=job.job_id)
                    return ApplyOutcome(
                        status=ApplicationStatus.APPLIED,
                        detail="Submitted via LinkedIn Easy Apply",
                        confirmation_type="dom_marker",
                        confirmation_evidence="LinkedIn post-submit confirmation or modal completion observed",
                    )

                error_el = await first_visible(modal, [".artdeco-inline-feedback--error", "p.artdeco-inline-feedback", "[data-test-form-builder-error]"], timeout_ms=1000)
                err_txt = (await safe_text(error_el)).strip() if error_el else "Submission unconfirmed; modal remained open"
                log.warning("linkedin.apply.submit_unconfirmed", job_id=job.job_id, error=err_txt)
                await self._dismiss_modal()
                return ApplyOutcome(status=ApplicationStatus.FAILED, detail=f"Submission failed: {err_txt}")

            # Check if modal advanced (inspecting actual input counts and modal body text)
            form_text = (await safe_text(modal)).strip()
            input_count = await modal.locator("input, select, textarea").count()
            step_sig = f"inputs:{input_count}::{form_text[:250]}"

            if step > 1 and step_sig == prev_step_signature:
                stuck_count += 1
                log.info("linkedin.apply.step_unchanged", step=step, count=stuck_count)
                # Auto-recover any unresolved required fields on this step
                await self._recover_step_errors(modal)
                if stuck_count >= 3:
                    error_el = await first_visible(modal, [".artdeco-inline-feedback--error", "p.artdeco-inline-feedback", "[data-test-form-builder-error]"], timeout_ms=800)
                    err_txt = (await safe_text(error_el)).strip() if error_el else "Required field unfilled"
                    log.warning("linkedin.apply.step_stuck", step=step, error=err_txt)
                    await self._dismiss_modal()
                    return ApplyOutcome(status=ApplicationStatus.FAILED, detail=f"Step {step} blocked: {err_txt}")
            else:
                stuck_count = 0
            prev_step_signature = step_sig

            # Check Next / Review button (within modal or page)
            action_btn = await first_visible(
                self.page,
                [
                    "button[aria-label*='Review your application' i]",
                    "button:has-text('Review')",
                    "button[aria-label*='Continue to next step' i]",
                    "button:has-text('Next')",
                ],
                timeout_ms=1500,
            )

            if action_btn:
                try:
                    await action_btn.scroll_into_view_if_needed(timeout=1000)
                except Exception:
                    pass
                try:
                    await action_btn.click(force=True, timeout=3000)
                    await human_pause(1500, 2500)
                except Exception as exc:
                    log.warning("linkedin.apply.action_btn_click_failed", error=str(exc))
                    # Attempt error auto-recovery and retry click once
                    await self._recover_step_errors(modal)
                    try:
                        await action_btn.click(force=True, timeout=2000)
                        await human_pause(1500, 2500)
                    except Exception:
                        pass
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
            target_resume_file = "resumes/CV_Mahesh_Chitakoti_2026.pdf"
        elif any(k in job_title_low for k in ["full stack", "react", "frontend", "web", "node", "javascript"]):
            target_resume_stem = "CV_Mahesh_Chitakoti_2026_1_"
            target_resume_file = "resumes/CV_Mahesh_Chitakoti_2026_1_.pdf"
        else:
            target_resume_stem = "CV_Mahesh_Chitakoti_2026_1_"
            target_resume_file = "resumes/CV_Mahesh_Chitakoti_2026_1_.pdf"

        resume_cards = await modal.locator("div[data-test-document-item], .jobs-document-upload-redesign-card, div.jobs-document-upload-redesign-card__container").all()
        card_selected = False
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
                card_selected = True
                break

        # Fallback: if no matching card was found/selected, check for file upload input
        if not card_selected:
            file_inp = modal.locator("input[type='file']").first
            if await file_inp.count() > 0:
                try:
                    from ..config import PROJECT_ROOT
                    abs_resume = PROJECT_ROOT / target_resume_file
                    if abs_resume.exists():
                        await file_inp.set_input_files(str(abs_resume))
                        await human_pause(1000, 2000)
                        log.info("linkedin.apply.resume_uploaded_from_disk", file=target_resume_file)
                except Exception as exc:
                    log.warning("linkedin.apply.resume_upload_failed", error=str(exc))

        # 2. Dropdowns (<select>) strictly inside modal
        selects = await modal.locator("select").all()
        for sel in selects:
            try:
                if not await sel.is_visible():
                    continue

                label = ""
                inp_id = await sel.get_attribute("id") or ""
                if inp_id:
                    label_el = modal.locator(f"label[for='{inp_id}']").first
                    if await label_el.count() > 0:
                        label = (await safe_text(label_el)).strip()
                if not label:
                    label_el = sel.locator("xpath=ancestor::div[contains(@class, 'fb-dash-form-element') or contains(@class, 'form__input') or contains(@class, 'jobs-easy-apply-form-element')][1]//label").first
                    if await label_el.count() > 0:
                        label = (await safe_text(label_el)).strip()
                if not label:
                    try:
                        label = (await sel.evaluate("el => el.closest('div').innerText")).strip()
                    except Exception:
                        pass
                label_low = label.lower()

                # Guard against footer language selector
                is_footer = await sel.evaluate("el => !!el.closest('footer') || !!el.closest('#compactfooter-language')")
                if is_footer:
                    continue

                options = await sel.locator("option").all_inner_texts()
                opt_joined = " ".join(options).lower()
                if any(k in opt_joined for k in ["deutsch", "español", "français", "bangla", "العربية", "čeština"]):
                    continue
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

                # Email Address dropdown
                is_email_select = (
                    "email" in label_low
                    or any("@" in o for o in options)
                    or await sel.evaluate("el => !!(el.id && el.id.toLowerCase().includes('email')) || !!(el.name && el.name.toLowerCase().includes('email'))")
                )
                if is_email_select:
                    target_email = (self.config.applicant_email or "maheshchitkoti@gmail.com") if self.config else "maheshchitkoti@gmail.com"
                    chosen_opt = ""
                    for o in options:
                        if target_email.lower() in o.lower():
                            chosen_opt = o.strip()
                            break
                    if chosen_opt:
                        await sel.select_option(label=chosen_opt)
                        log.info("linkedin.apply.email_selected", email=chosen_opt)
                    elif options:
                        log.info("linkedin.apply.email_default_kept", email=options[0].strip())
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
                    elif any(k in label_low for k in ["disability", "handicap"]):
                        for o in options:
                            if any(w in o.lower() for w in ["no", "not have", "decline", "prefer not"]):
                                target_val = o.strip()
                                break
                    elif any(k in label_low for k in ["gender", "sex"]):
                        for o in options:
                            if "male" in o.lower() and "female" not in o.lower():
                                target_val = o.strip()
                                break
                    elif any(k in label_low for k in ["veteran", "military"]):
                        for o in options:
                            if any(w in o.lower() for w in ["not", "no", "decline", "prefer not"]):
                                target_val = o.strip()
                                break

                if not target_val and len(options) > 1:
                    target_val = options[1].strip() if "select" not in options[1].lower() else (options[2].strip() if len(options) > 2 else "")

                if target_val:
                    try:
                        await sel.select_option(label=target_val)
                    except Exception:
                        try:
                            # Fallback: substring matching in evaluate
                            await sel.evaluate(
                                f"""(el) => {{
                                    const match = Array.from(el.options).find(o => o.text.toLowerCase().includes('{target_val.lower()}'));
                                    if (match) {{
                                        el.value = match.value;
                                        el.dispatchEvent(new Event('change', {{bubbles: true}}));
                                    }}
                                }}"""
                            )
                        except Exception:
                            pass
                    await human_pause(200, 400)
            except Exception:
                pass

        # 3. Radio Fieldsets & Groups strictly inside modal
        radio_groups = await modal.locator(
            "fieldset, div[role='radiogroup'], div[data-test-form-builder-radio-button-form-component], div.fb-dash-form-element:has(input[type='radio'])"
        ).all()
        for rg in radio_groups:
            try:
                if not await rg.is_visible():
                    continue

                # Check if already answered
                already_checked = await rg.locator("input[type='radio']:checked, div[role='radio'][aria-checked='true']").count() > 0
                if already_checked:
                    continue

                q_text = ""
                legend_el = rg.locator("legend").first
                if await legend_el.count() > 0:
                    q_text = (await safe_text(legend_el)).strip()

                first_opt = rg.locator("div[role='radio']").first
                if not q_text and await first_opt.count() > 0:
                    q_text = (await first_opt.get_attribute("aria-label") or "").strip()

                if not q_text:
                    head_el = rg.locator("[data-test-form-builder-radio-button-form-component__title], p, span, h3, h4").first
                    if await head_el.count() > 0:
                        q_text = (await safe_text(head_el)).strip()

                options = await rg.locator("div[role='radio'], label:has(input[type='radio']), input[type='radio']").all()
                opt_texts = [(await safe_text(opt)).strip() for opt in options if (await safe_text(opt)).strip()]

                resolved = self.answers.resolve(ScreeningQuestion(text=q_text, kind="radio", options=opt_texts)) if q_text else None
                target_val = resolved.value if resolved else ""

                if not target_val:
                    low_q = q_text.lower()
                    if any(k in low_q for k in ["sponsorship", "visa sponsorship", "require sponsorship"]):
                        target_val = "No"
                    elif any(k in low_q for k in ["disability", "handicap", "impairment"]):
                        target_val = "No"
                    elif any(k in low_q for k in ["veteran", "military"]):
                        target_val = "No"
                    elif any(k in low_q for k in ["gender", "sex"]):
                        target_val = "Male"
                    elif any(k in low_q for k in ["authorized", "authorization", "commute", "relocate", "comfortable", "background", "willing", "participate", "agree", "process", "interest", "onsite", "hybrid", "remote", "budget", "salary", "lpa", "join", "immediate"]):
                        target_val = "Yes"
                    else:
                        target_val = "Yes"

                matched_radio = False
                for opt in options:
                    otext = (await safe_text(opt)).strip()
                    aria_lbl = (await opt.get_attribute("aria-label") or "").strip()
                    if target_val and (target_val.lower() in otext.lower() or target_val.lower() in aria_lbl.lower()):
                        try:
                            await opt.scroll_into_view_if_needed(timeout=1000)
                            await opt.click(force=True, timeout=1500)
                        except Exception:
                            pass
                        inp = opt.locator("input[type='radio']").first
                        if await inp.count() > 0:
                            await inp.evaluate("""el => {
                                el.checked = true;
                                el.dispatchEvent(new Event('change', {bubbles: true}));
                                el.dispatchEvent(new Event('input', {bubbles: true}));
                                el.click();
                            }""")
                        await human_pause(200, 400)
                        matched_radio = True
                        break

                if not matched_radio and options:
                    # Robust fallback: click first option or option containing decline / prefer not
                    chosen_opt = None
                    for opt in options:
                        otext = (await safe_text(opt)).strip().lower()
                        if any(d in otext for d in ["prefer not", "decline", "not wish", "no", "yes"]):
                            chosen_opt = opt
                            break
                    if not chosen_opt:
                        chosen_opt = options[0]
                    try:
                        await chosen_opt.scroll_into_view_if_needed(timeout=1000)
                        await chosen_opt.click(force=True, timeout=1500)
                    except Exception:
                        pass
                    inp = chosen_opt.locator("input[type='radio']").first
                    if await inp.count() > 0:
                        await inp.evaluate("""el => {
                            el.checked = true;
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.click();
                        }""")
                    await human_pause(200, 400)
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
        inputs = await modal.locator(
            "input:not([type='radio']):not([type='checkbox']):not([type='hidden']):not([type='file']):not([type='submit']):not([type='button']), textarea"
        ).all()
        for inp in inputs:
            try:
                if not await inp.is_visible():
                    continue
                val = (await inp.input_value()).strip()

                aria_label = (await inp.get_attribute("aria-label") or "").strip()
                label = aria_label
                if not label:
                    inp_id = await inp.get_attribute("id") or ""
                    if inp_id:
                        label_el = modal.locator(f"label[for='{inp_id}']").first
                        if await label_el.count() > 0:
                            label = (await safe_text(label_el)).strip()
                if not label:
                    label_el = inp.locator("xpath=ancestor::div[contains(@class, 'fb-dash-form-element') or contains(@class, 'form__input') or contains(@class, 'jobs-easy-apply-form-element')][1]//label").first
                    if await label_el.count() > 0:
                        label = (await safe_text(label_el)).strip()
                if not label:
                    try:
                        label = (await inp.evaluate("el => el.closest('div').innerText")).strip()
                    except Exception:
                        pass
                
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
                if any(w in label_low for w in ("last working", "lwd", "date of", "joining date", "start date")):
                    is_numeric = False

                # Mobile phone field
                is_phone_field = (
                    inp_type == "tel"
                    or any(k in label_low for k in ["phone", "mobile", "contact number", "cell", "telephone"])
                    or any(k in aria_label.lower() for k in ["phone", "mobile", "contact number"])
                    or await inp.evaluate("el => !!(el.id && el.id.toLowerCase().includes('phone')) || !!(el.name && el.name.toLowerCase().includes('phone')) || !!(el.autocomplete && el.autocomplete.toLowerCase().includes('tel'))")
                )
                if is_phone_field:
                    phone_val = (self.config.applicant_phone or "9481777227") if self.config else "9481777227"
                    clean_val = re.sub(r"\D", "", val)
                    clean_target = re.sub(r"\D", "", phone_val)
                    if clean_val != clean_target or err_msg:
                        await inp.scroll_into_view_if_needed()
                        await inp.click()
                        await inp.press("ControlOrMeta+A")
                        await inp.press("Backspace")
                        await inp.fill("")
                        await inp.fill(phone_val)
                        await human_pause(100, 250)
                        try:
                            await inp.dispatch_event("input")
                            await inp.dispatch_event("change")
                            await inp.dispatch_event("blur")
                        except Exception:
                            pass
                        # Verify if properly updated
                        new_val = (await inp.input_value()).strip()
                        if re.sub(r"\D", "", new_val) != clean_target:
                            await inp.click()
                            await inp.press("ControlOrMeta+A")
                            await inp.press("Backspace")
                            await inp.press_sequentially(phone_val, delay=25)
                            await inp.dispatch_event("input")
                            await inp.dispatch_event("change")
                            await inp.dispatch_event("blur")
                        log.info("linkedin.apply.phone_updated", old=val, new=phone_val)
                    continue

                # Email field if input instead of select
                is_email_field = (
                    inp_type == "email"
                    or ("email" in label_low and "subscribe" not in label_low)
                    or "email" in aria_label.lower()
                    or await inp.evaluate("el => !!(el.id && el.id.toLowerCase().includes('email')) || !!(el.name && el.name.toLowerCase().includes('email'))")
                )
                if is_email_field:
                    target_email = (self.config.applicant_email or "maheshchitkoti@gmail.com") if self.config else "maheshchitkoti@gmail.com"
                    if not val or err_msg or ("@" not in val):
                        await inp.scroll_into_view_if_needed()
                        await inp.click()
                        await inp.fill("")
                        await inp.fill(target_email)
                        await human_pause(100, 250)
                        try:
                            await inp.dispatch_event("input")
                            await inp.dispatch_event("change")
                            await inp.dispatch_event("blur")
                        except Exception:
                            pass
                        log.info("linkedin.apply.email_filled", email=target_email)
                    continue

                # City / Location combobox
                if any(k in label_low for k in ["city", "location", "zip code", "state"]):
                    if not val or "search" in val.lower():
                        loc_val = (self.config.applicant_location or "Bengaluru, Karnataka, India") if self.config else "Bengaluru, Karnataka, India"
                        await inp.fill(loc_val)
                        await human_pause(600, 1000)
                        typeahead_item = self.page.locator(".search-typeahead-v2__hit, div[role='option'], li[role='option'], .basic-typeahead__selectable-result").first
                        if await typeahead_item.count() > 0:
                            await typeahead_item.click()
                        else:
                            await inp.press("ArrowDown")
                            await inp.press("Enter")
                    continue

                # Skip unlabeled inputs that are not phone or email
                if not label and not is_phone_field and not is_email_field:
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
                            ans = str(max(0, min(99, round(val_float))))

                    if not ans:
                        resolved = self.answers.resolve(ScreeningQuestion(text=label, kind="text"))
                        raw_ans = resolved.value if resolved else ""
                        if raw_ans:
                            num_match = re.search(r"[-+]?\d*\.?\d+", str(raw_ans))
                            if num_match:
                                ans = str(max(0, min(99, round(float(num_match.group(0))))))

                    if not ans:
                        if any(k in label_low for k in ["notice", "how soon", "availability", "immediate", "serving"]):
                            ans = "0"
                        elif "months" in label_low:
                            ans = "6"
                        elif "rate" in label_low or "scale" in label_low or "1 to 10" in label_low:
                            ans = "9"
                        elif "ctc" in label_low or "salary" in label_low or "compensation" in label_low:
                            min_attr = await inp.get_attribute("min")
                            if min_attr and float(min_attr) >= 100:
                                ans = "650000" if float(min_attr) > 10000 else "650"
                            elif "lakh" in label_low or "lpa" in label_low:
                                ans = "7"
                            elif "thousand" in label_low:
                                ans = "650"
                            elif any(w in label_low for w in ["inr", "annual", "per year", "per annum", "/year"]):
                                ans = "650000"
                            else:
                                ans = "650000" if (min_attr and float(min_attr) > 1000) else "7"
                        else:
                            # Skill or general years of experience -> whole integer 2
                            ans = "2"
                else:
                    resolved = self.answers.resolve(ScreeningQuestion(text=label, kind="text"))
                    ans = resolved.value if resolved else ""

                    if inp_type == "date" or any(k in label_low for k in ["start date", "joining date", "available date", "date of", "last working", "lwd", "relieving"]):
                        import datetime
                        ans = datetime.date.today().strftime("%Y-%m-%d")
                    elif any(k in label_low for k in ["notice", "immediate", "serving", "availability"]):
                        ans = "0"
                    elif any(k in label_low for k in ["ctc", "salary", "compensation", "package"]):
                        min_attr = await inp.get_attribute("min")
                        if min_attr and float(min_attr) >= 100:
                            ans = "650000" if float(min_attr) > 10000 else "650"
                        elif "lakh" in label_low or "lpa" in label_low:
                            ans = "7"
                        elif "thousand" in label_low:
                            ans = "650"
                        elif any(w in label_low for w in ["inr", "annual", "per year", "per annum", "/year"]):
                            ans = "650000"
                        else:
                            ans = "650000" if (min_attr and float(min_attr) > 1000) else "7"
                    elif not ans or "days" in str(ans).lower():
                        if "title" in label_low:
                            ans = "Full Stack & AI Engineer"
                        elif "company" in label_low:
                            ans = "Independent Software Consultant"
                        elif "linkedin" in label_low:
                            ans = (self.config.applicant_linkedin or "https://www.linkedin.com/in/maheshchitakoti") if self.config else "https://www.linkedin.com/in/maheshchitakoti"
                        elif "github" in label_low or "portfolio" in label_low or "website" in label_low:
                            if "github" in label_low and self.config and self.config.applicant_github:
                                ans = self.config.applicant_github
                            elif self.config and self.config.applicant_portfolio:
                                ans = self.config.applicant_portfolio
                            else:
                                ans = "https://github.com/maheshchitakoti"
                        else:
                            applicant_name = (self.config.applicant_name or "Mahesh Chitakoti") if self.config else "Mahesh Chitakoti"
                            ans = f"Experienced software and AI engineer ({applicant_name}) with 2.5 years of experience building scalable applications, APIs, and AI workflows."

                if ans:
                    try:
                        await inp.click(timeout=1500, force=True)
                    except Exception:
                        pass
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

        # 5. Auto-recover any remaining inline validation feedback errors on the step
        await self._recover_step_errors(modal)

    async def _recover_step_errors(self, modal: Locator) -> None:
        """Inspects inline feedback errors on the step and auto-populates unfilled controls."""
        try:
            errors = await modal.locator(".artdeco-inline-feedback--error, p.artdeco-inline-feedback, [data-test-form-builder-error]").all()
            for err in errors:
                try:
                    if not await err.is_visible():
                        continue
                    container = err.locator("xpath=ancestor::div[contains(@class, 'fb-dash-form-element') or contains(@class, 'form__input') or contains(@class, 'jobs-easy-apply-form-element') or @data-test-form-builder-radio-button-form-component][1]").first
                    target_scope = container if await container.count() > 0 else modal

                    # 1. Radio group in error container
                    radios = await target_scope.locator("input[type='radio']").all()
                    if radios:
                        first_radio = radios[0]
                        await first_radio.evaluate("""el => {
                            el.checked = true;
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.click();
                        }""")
                        log.info("linkedin.apply.error_recovered_radio")
                        continue

                    # 2. Text/Number input in error container
                    inputs = await target_scope.locator("input:not([type='radio']):not([type='checkbox']):not([type='hidden']):not([type='file']), textarea").all()
                    for inp in inputs:
                        val = (await inp.input_value()).strip()
                        err_text_low = (await safe_text(err)).lower()
                        if not val or err:
                            new_val = "2"
                            if "larger than" in err_text_low or "greater than" in err_text_low:
                                num_match = re.search(r"(?:larger|greater)\s+than\s+(\d+)", err_text_low)
                                if num_match:
                                    min_val = int(num_match.group(1))
                                    new_val = str(min_val + 10) if min_val < 1000 else str(min_val + 50000)
                                else:
                                    new_val = "500000"
                            elif "between 0 and 99" in err_text_low or "between 0 and" in err_text_low:
                                new_val = "2"
                            elif "decimal" in err_text_low:
                                new_val = "2.5"
                            elif "email" in err_text_low:
                                new_val = "maheshchitkoti@gmail.com"
                            elif "phone" in err_text_low or "mobile" in err_text_low:
                                new_val = "9481777227"
                            elif "date" in err_text_low or (await inp.get_attribute("type") == "date"):
                                import datetime
                                new_val = datetime.date.today().strftime("%Y-%m-%d")

                            await inp.fill("")
                            await inp.fill(new_val)
                            await inp.dispatch_event("input")
                            await inp.dispatch_event("change")
                            await inp.dispatch_event("blur")
                            log.info("linkedin.apply.error_recovered_input", val=new_val)

                    # 3. Select dropdown in error container
                    selects = await target_scope.locator("select").all()
                    for sel in selects:
                        options = await sel.locator("option").all_inner_texts()
                        if len(options) > 1:
                            target = options[1].strip() if "select" not in options[1].lower() else (options[2].strip() if len(options) > 2 else "")
                            if target:
                                try:
                                    await sel.select_option(label=target)
                                except Exception:
                                    await sel.evaluate(f"el => {{ el.selectedIndex = 1; el.dispatchEvent(new Event('change', {{bubbles: true}})); }}")
                                log.info("linkedin.apply.error_recovered_select", label=target)

                    # 4. Checkbox in error container
                    checkboxes = await target_scope.locator("input[type='checkbox']").all()
                    for cb in checkboxes:
                        if not await cb.is_checked():
                            await cb.evaluate("""el => {
                                el.checked = true;
                                el.dispatchEvent(new Event('change', {bubbles: true}));
                                el.dispatchEvent(new Event('input', {bubbles: true}));
                                el.click();
                            }""")
                            log.info("linkedin.apply.error_recovered_checkbox")
                except Exception:
                    pass
        except Exception as exc:
            log.debug("linkedin.apply.error_recovery_failed", error=str(exc))

    async def _dismiss_modal(self) -> None:
        """Safely dismisses and discards any open dialog."""
        try:
            await click_if_present(self.page, ["button[aria-label='Dismiss']", "button[data-test-modal-close-btn]", "button[aria-label='Close']"])
            await human_pause(500, 1000)
            await click_if_present(self.page, [
                "button:has-text('Discard')",
                "button[data-control-name='discard_application_confirm_btn']",
                "button[data-test-dialog-primary-btn]",
                "button:has-text('Discard draft')",
            ])
            await human_pause(500, 1000)
        except Exception:
            pass
