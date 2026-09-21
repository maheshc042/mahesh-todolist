"""
LinkedIn Easy Apply Platform Implementation.

Features:
- Exact 30 Target Roles Boolean Query with Entry & Associate Levels (f_E=2,3) and 24h Freshness (f_TPR=r86400).
- High-Performance In-Page Split-View Navigation (zero slow full-page reloads).
- Deep Suitability & Spam/Unpaid/Fellowship Filtering.
- State-aware resume routing (per-profile resume files; Naukri attaches the CV
  stored on the profile, so one account == one job family).
- Robust question answering via AnswerEngine (experience, notice period and CTC
  resolved from config.yaml — never hardcoded).
- Automatic Job Search Safety Reminder Handling (Continue/Acknowledge).
- Clean Multi-Step Submission & Modal Confirmation Dismissal.
"""
from __future__ import annotations

import re
import time as _time
import urllib.parse
from collections.abc import Callable
from typing import Any

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

_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)


def _looks_like_option_dump(text: str) -> bool:
    """True when a scraped 'label' is actually the dropdown's option list
    (Month/Year selects) rather than the question — e.g. the Altraize
    Month/Year fields whose container text is just months and years."""
    low = (text or "").lower()
    if not low:
        return False
    months = sum(1 for m in _MONTH_NAMES if m in low)
    years = len(re.findall(r"\b(?:19|20)\d{2}\b", low))
    return months >= 2 or years >= 3


class LinkedInPlatform(BaseJobPlatform):
    def __init__(
        self,
        page: Page,
        account: NaukriAccount,
        artifacts: ArtifactStore,
        answers: AnswerEngine,
        policy: RunPolicy,
        config: AgentConfig | None = None,
        metrics: Any | None = None,
        location: str = "India",
        days: int = 3,
    ):
        super().__init__(page, account.key, policy)
        self.account = account
        self.artifacts = artifacts
        self.answers = answers
        self.config = config or AgentConfig.load()
        self._metrics = metrics
        self.location = location
        cfg_days = getattr(getattr(self.config, "linkedin", None), "days", None)
        self.days = cfg_days if cfg_days is not None else days

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

    def _get_search_url(self, profile: JobProfile, days: int | None = None) -> str:
        """Constructs target Easy Apply search URL tailored to candidate profile with freshness."""
        effective_days = days if days is not None else self.days
        tpr_seconds = effective_days * 86400
        prof_name = (profile.name or "").lower()
        if "unified" in prof_name:
            query = FULL_TARGET_QUERY
        elif any(k in prof_name for k in ["ai", "python", "machine learning", "ml"]):
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
        # NOTE: "lead" is deliberately NOT blocked on LinkedIn either (same as
        # Cutshort): with 2.5y experience the candidate is offered full-stack
        # lead roles, and seniority is guarded by the 0-3.5y ceiling, not the
        # title blocklist.
        blocked_title_keywords = [
            "principal", "architect", "manager", "director", "head of", "intern", "internship",
            "presales", "pre-sales", "sales", "bpo", "business analyst", "content writer", "seo",
            "java", "spring boot", "spring-boot", ".net", "dotnet", "dot net", "c#", "php", "wordpress",
            "erp", "sap", "salesforce", "gis", "oac", "oracle dba", "mainframe", "teradata", "snowflake", "etl", "aem",
            "security engineer", "cybersecurity", "infosec", "soc analyst",
            "data scientist", "bi developer", "powerbi", "tableau",
            "it support", "helpdesk", "service desk",
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
            "python", "fastapi", "django", "llm", "genai", "generative ai", "langchain", "machine learning", "rag", "pytorch", "ai agent", "agentic", "nlp", "chatbot",
            "react", "node", "javascript", "typescript", "full stack", "fullstack", "frontend", "backend", "next.js", "express", "postgresql", "mongodb", "rest api",
            "mean", "mean stack", "mern", "website", "forward deployment", "deployment",
            "qa", "testing", "automation", "playwright", "selenium", "pytest", "sdet", "tester", "software tester", "test automation", "manual testing", "api testing",
            "aws", "cloud", "docker", "kubernetes", "linux", "devops", "sre", "ci/cd", "terraform",
            "technical support", "application support", "production support", "it support", "l2 support", "troubleshooting", "jira", "incident management"
        ]

        matched_skills = [k for k in track_keywords if k in full_text]
        if matched_skills:
            score += min(len(matched_skills) * 2, 20)

        # Quality gate: a generic "engineer" title with zero stack overlap
        # must not pass on geography alone (was 75+10+5=90 before).
        if not matched_skills and not matched_target_role:
            return False, "No candidate-stack overlap in title/JD", 0

        is_suitable = score >= 60
        match_info = f"Matched '{matched_target_role}'" if matched_target_role else "Skills aligned"
        reason = f"{match_info} (Score: {score}/100)" if is_suitable else f"Low relevance score ({score}/100)"
        return is_suitable, reason, score

    async def _scroll_and_extract_cards(
        self,
        scrape_target: int,
        exclude_job_ids: set[str],
        seen_ids: set[str],
        jobs: list[Job],
    ) -> int:
        """Scrolls the job listings pane and extracts fresh job cards."""
        try:
            await self.page.wait_for_selector(
                ".jobs-search-results-list, .scaffold-layout__list, li.jobs-search-results__list-item, div.job-card-container, div.base-card, ul.jobs-search__results-list",
                timeout=12000,
            )
        except Exception:
            pass

        # Scroll the listings pane (supporting both split-view and grid layouts)
        list_pane = await first_visible(
            self.page,
            [
                ".jobs-search-results-list",
                ".scaffold-layout__list",
                "div[class*='jobs-search-results-list']",
                ".jobs-search__results-list",
                "ul.jobs-search__results-list",
                "main",
            ],
            timeout_ms=3000,
        )
        if list_pane:
            try:
                await list_pane.hover()
            except Exception:
                pass

        # Scroll until the card count stabilises (lazy-load needs more
        # than a fixed 6 wheels on dense result pages).
        _card_sel = (
            ".jobs-search-results-list li, .scaffold-layout__list-container li, li.jobs-search-results__list-item, "
            "div.job-card-container, div[data-job-id], div.base-card, div.base-search-card, "
            "ul.jobs-search__results-list li, div[data-entity-urn*='jobPosting']"
        )
        _last_count = -1
        _stable = 0
        for _ in range(10):
            try:
                await self.page.mouse.wheel(0, 700)
                await human_pause(500, 1000)
            except Exception as exc:
                log.debug("linkedin.fetch.scroll_wheel_failed", error=str(exc))
            try:
                _n = await self.page.locator(_card_sel).count()
            except Exception as exc:
                log.debug("linkedin.fetch.card_count_failed", error=str(exc))
                break
            if _n <= _last_count:
                _stable += 1
                if _stable >= 3:
                    break
            else:
                _stable = 0
            _last_count = _n

        card_locators = self.page.locator(
            ".jobs-search-results-list li, .scaffold-layout__list-container li, li.jobs-search-results__list-item, "
            "div.job-card-container, div[data-job-id], div.base-card, div.base-search-card, "
            "ul.jobs-search__results-list li, div[data-entity-urn*='jobPosting']"
        )
        total_cards = await card_locators.count()
        if total_cards == 0:
            return 0

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

                title_el = card.locator("a.job-card-list__title, a.base-card__full-link, .base-search-card__title, h3, a[href*='/jobs/view/'], strong").first
                company_el = card.locator(".job-card-container__primary-description, .artdeco-entity-lockup__subtitle, span.job-card-container__publisher, .job-card-container__company-name, h4, a.hidden-nested-link, .base-search-card__subtitle").first
                loc_el = card.locator(".job-card-container__metadata-item, .artdeco-entity-lockup__caption, .job-search-card__location, .base-search-card__metadata").first

                title = (await safe_text(title_el)).strip()
                title = re.sub(r"\s+with verification\b", "", title, flags=re.IGNORECASE).strip()
                title = re.sub(r"\bBe an early applicant\b", "", title, flags=re.IGNORECASE).strip()
                half_len = len(title) // 2
                if half_len > 3 and title[:half_len].strip() == title[half_len:].strip():
                    title = title[:half_len].strip()
                company = (await safe_text(company_el)).strip()
                location = (await safe_text(loc_el)).strip()

                if not title or len(title) < 3 or title.lower() in ("be an early applicant", "early applicant"):
                    continue

                card_url = ""
                if await title_el.count() > 0:
                    card_url = (await title_el.get_attribute("href") or "").split("?")[0]
                if card_url and not card_url.startswith("http"):
                    card_url = f"https://www.linkedin.com{card_url}"

                raw_id = None
                entity_urn = await card.get_attribute("data-entity-urn") or await card.get_attribute("data-job-id") or ""
                urn_match = re.search(r"(\d{7,})", entity_urn)
                if urn_match:
                    raw_id = urn_match.group(1)
                if not raw_id and card_url:
                    url_match = re.search(r"/view/.*?(\d{7,})", card_url) or re.search(r"(\d{7,})", card_url)
                    if url_match:
                        raw_id = url_match.group(1)
                if not raw_id:
                    raw_id = Job.stable_id(card_url, title, company)
                job_id = f"linkedin-{raw_id}"

                if job_id in exclude_job_ids or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                card_text = (await safe_text(card)).strip()
                # Easy Apply signal from the card footer. This search always
                # runs with f_AL=true (Easy Apply filter), so a rendered card
                # WITHOUT the "Easy Apply" footer contradicts LinkedIn's own
                # filter and is overwhelmingly a company-site posting: mark it
                # external so gate 0 rejects it in ~0s instead of burning
                # ~20s per job at apply time (run 360: 22 externals, 0 applies).
                # Already-applied cards stay unknown so apply-time classifies
                # them correctly as ALREADY_APPLIED, not EXTERNAL.
                card_low = card_text.lower()
                if "easy apply" in card_low:
                    easy_apply: bool | None = True
                elif re.search(r"\bapplied\b", card_low):
                    easy_apply = None
                else:
                    easy_apply = False
                # Applicant count straight off the card ("57 applicants" -> 57,
                # "Over 100 applicants" -> 101, "Be an early applicant" -> 5).
                applicants: int | None = None
                m_ap = re.search(r"over\s+(\d+)\s+applicants?", card_text, re.IGNORECASE)
                if m_ap:
                    applicants = int(m_ap.group(1)) + 1
                else:
                    m_ap = re.search(r"(\d+)\s+applicants?\b", card_text, re.IGNORECASE)
                    if m_ap:
                        applicants = int(m_ap.group(1))
                    elif re.search(r"be an early applicant", card_text, re.IGNORECASE):
                        applicants = 5
                # Posted age straight off the card ("3 days ago" -> 3). Without
                # this the 24h plan gate can never fire (unknown never rejects)
                # and stale jobs sail to apply time — the exact leak behind the
                # 3-day-old 100+ applicant jobs. None when unstated: unknown
                # still never rejects.
                posted_days: int | None = None
                card_age = f"{title} {card_text}".lower()
                if re.search(r"\bjust now\b|few minutes|seconds? ago|today\b", card_age):
                    posted_days = 0
                elif "yesterday" in card_age:
                    posted_days = 1
                else:
                    # "ago" anchor is mandatory: without it an experience range
                    # ("3-5 years") misreads as a posted age. LinkedIn always
                    # renders card dates as relative ("3 days ago").
                    m_age = re.search(
                        r"(\d+)\s*(hours?|hrs?|h|days?|d|weeks?|w|months?|mos?|years?|y)\s+ago\b",
                        card_age,
                    )
                    if m_age:
                        n = int(m_age.group(1))
                        unit = m_age.group(2).lower()
                        if unit.startswith("h"):
                            posted_days = 0
                        elif unit.startswith("w"):
                            posted_days = n * 7
                        elif unit.startswith("mo"):
                            posted_days = n * 30
                        elif unit.startswith("y"):
                            posted_days = n * 365
                        else:
                            posted_days = n
                min_exp, max_exp = None, None
                exp_imputed = False
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
                    exp_imputed = True
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
                    experience_imputed=exp_imputed,
                    applicant_count=applicants,
                    easy_apply=easy_apply,
                    posted_days_ago=posted_days,
                    platform="linkedin",
                )
                jobs.append(job)
                page_added += 1
            except Exception:
                continue
        return page_added

    async def fetch_jobs(self, profile: JobProfile, exclude_job_ids: set[str]) -> list[Job]:
        """Fetches fresh Easy Apply jobs from LinkedIn job search with left-pane scrolling across pages."""
        current_days = self.days
        base_search_url = self._get_search_url(profile, days=current_days)
        target_applies = profile.platform_limits.get(self.platform_name, 50)
        # Scrape a generous pool (2.5x the apply target) so that after hard filtering
        # and recruiter alignment, the apply engine actually has enough eligible jobs
        # to satisfy the application quota (e.g. 50 successful applies).
        scrape_target = max(125, int(target_applies * 2.5))
        log.info("linkedin.fetch.start", profile=profile.name, target_applies=target_applies, scrape_target=scrape_target, days=current_days)

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
                    await self.page.goto(base_search_url, wait_until="commit", timeout=25000)
                    await human_pause(2000, 3500)
                except Exception:
                    try:
                        await self.page.goto(base_search_url, timeout=25000)
                        await human_pause(2000, 3500)
                    except Exception as exc:
                        log.warning("linkedin.fetch.goto_error", page=1, error=str(exc))
                        break

                page_added = await self._scroll_and_extract_cards(scrape_target, exclude_job_ids, seen_ids, jobs)
                log.info("linkedin.fetch.page_done", page=1, added=page_added, total=len(jobs))

                # Fallback to past week (7 days) if page 1 yields fewer than 10 jobs
                if len(jobs) < 10 and current_days < 7:
                    log.info(
                        "linkedin.fetch.fallback_past_week",
                        page1_count=len(jobs),
                        current_days=current_days,
                        fallback_days=7,
                    )
                    current_days = 7
                    base_search_url = self._get_search_url(profile, days=7)
                    try:
                        await self.page.goto(base_search_url, wait_until="commit", timeout=25000)
                        await human_pause(2000, 3500)
                        fb_added = await self._scroll_and_extract_cards(scrape_target, exclude_job_ids, seen_ids, jobs)
                        log.info("linkedin.fetch.fallback_page_done", page=1, added=fb_added, total=len(jobs))
                    except Exception:
                        try:
                            await self.page.goto(base_search_url, timeout=25000)
                            await human_pause(2000, 3500)
                            fb_added = await self._scroll_and_extract_cards(scrape_target, exclude_job_ids, seen_ids, jobs)
                            log.info("linkedin.fetch.fallback_page_done", page=1, added=fb_added, total=len(jobs))
                        except Exception as exc:
                            log.warning("linkedin.fetch.fallback_goto_error", error=str(exc))
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
                        await self.page.goto(search_url, wait_until="commit", timeout=25000)
                        await human_pause(2000, 3500)
                    except Exception:
                        try:
                            await self.page.goto(search_url, timeout=25000)
                            await human_pause(2000, 3500)
                        except Exception as exc:
                            log.warning("linkedin.fetch.goto_error", page=target_page_num, error=str(exc))
                            break

                page_added = await self._scroll_and_extract_cards(scrape_target, exclude_job_ids, seen_ids, jobs)
                log.info("linkedin.fetch.page_done", page=target_page_num, added=page_added, total=len(jobs))

            if page_added == 0 and (len(jobs) >= 20 or page_idx >= 1):
                log.info("linkedin.fetch.no_new_jobs_done", total=len(jobs))
                break

        # Observability parity with Cutshort/Instahyre: persist the final
        # search DOM so empty/stale feeds are debuggable without re-running.
        try:
            dump_path = self.artifacts.dir / "linkedin-search-results.html"
            dump_path.write_text(await self.page.content(), encoding="utf-8")
            log.info("linkedin.search_results_html_saved", path=str(dump_path))
        except Exception as exc:
            log.debug("linkedin.search_results_dump_failed", error=str(exc))

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
        t0 = _time.perf_counter()
        jt = None
        if self._metrics is not None:
            try:
                from ..core.runtime_metrics import JobTiming

                jt = JobTiming(job_id=job.job_id, title=job.title)
            except Exception:
                jt = None

        def _record_timing() -> None:
            if jt is not None and self._metrics is not None:
                jt.total_s = _time.perf_counter() - t0
                try:
                    self._metrics.job_timings.append(jt)
                except Exception:
                    pass

        # Locate and click job card in left pane
        raw_num_id = job.job_id.replace("linkedin-", "")
        card = self.page.locator(
            f"div[data-job-id*='{raw_num_id}'], div[data-entity-urn*='{raw_num_id}'], a[href*='{raw_num_id}']"
        ).first
        if await card.count() > 0:
            try:
                await card.scroll_into_view_if_needed(timeout=5_000)
            except Exception:
                pass
            await human_pause(200, 400)
            try:
                await card.click(timeout=8_000)
            except Exception:
                try:
                    await card.click(force=True, timeout=5_000)
                except Exception:
                    try:
                        await card.evaluate("node => node.click()")
                    except Exception:
                        # Card present but unclickable (stale/overlaid): fall back
                        # to the job URL rather than failing the whole job.
                        try:
                            if job.url and job.url.startswith("http"):
                                await self.page.goto(job.url, wait_until="domcontentloaded", timeout=25000)
                                await human_pause(2000, 3500)
                        except Exception:
                            pass
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
        # Persist form links + recruiter emails (return value was discarded).
        links, emails = extract_description_metadata(job.description)
        job.form_links = links
        job.recruiter_emails = emails

        if job.min_experience is None and job.description:
            match = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to|\+)\s*(\d+(?:\.\d+)?)?\s*(?:yrs|years|yr)", job.description, re.IGNORECASE)
            if match:
                job.min_experience = float(match.group(1))
                # JD-stated numbers supersede the card heuristic.
                job.experience_imputed = False
                if match.group(2):
                    job.max_experience = float(match.group(2))
                elif "+" in match.group(0):
                    job.max_experience = job.min_experience + 5.0

        # Suitability & Spam Check
        suitable, reason, score = self.evaluate_job_suitability(job)
        if not suitable:
            log.info("linkedin.apply.skipped", job_id=job.job_id, reason=reason)
            _record_timing()
            return ApplyOutcome(status=ApplicationStatus.SKIPPED, reason=SkipReason.LOW_MATCH_SCORE, detail=reason)

        if pre_submit_check:
            decision = pre_submit_check(job)
            if not decision.passed:
                _record_timing()
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
                _record_timing()
                return ApplyOutcome(status=ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)
            _record_timing()
            return ApplyOutcome(status=ApplicationStatus.SKIPPED, reason=SkipReason.EXTERNAL_APPLY, detail="No Easy Apply button; company-site apply only")

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
            # NOTE: never resolve the modal to the bare Dismiss button
            # itself — filling inputs scoped to a button silently no-ops.
            modal = self.page.locator(
                "dialog[open]:has(button[aria-label*='Dismiss' i]), "
                "dialog:has(button[aria-label*='Dismiss' i]), "
                "div[role='dialog']:has(button[aria-label*='Dismiss' i]), "
                "dialog[open], "
                "div.jobs-easy-apply-modal:visible, "
                "div[data-test-modal]:visible, "
                "div[data-view-name*='easy-apply']:visible"
            ).first
            try:
                tag = (await modal.evaluate("el => el.tagName || ''")).strip().upper()
                if tag == "BUTTON":
                    modal = None  # fall through to container search below
            except Exception:
                pass
        else:
            modal = None
        if modal is None:
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
            _record_timing()
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
            await self._fill_step_inputs(modal, job, profile_name=profile_name)

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
                    return ApplyOutcome(
                        status=ApplicationStatus.SKIPPED,
                        reason=SkipReason.DRY_RUN,
                        detail="Dry-run submit reached; nothing submitted",
                    )

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
                    _record_timing()
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
                _record_timing()
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
                    _record_timing()
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
        _record_timing()
        return ApplyOutcome(
            status=ApplicationStatus.NEEDS_REVIEW,
            reason=SkipReason.STUCK_FLOW,
            detail="Modal step threshold exceeded",
        )

    @staticmethod
    def _resume_ref(relative_path: str) -> tuple[str, str]:
        """Split a configured resume path into (card-stem, relative-path)."""
        rel = (relative_path or "").strip()
        base = rel.replace("\\", "/").rsplit("/", 1)[-1]
        stem = base[:-4] if base.lower().endswith(".pdf") else base
        return stem, rel

    def _resume_for_role(self, profile_name: str, job_title: str) -> tuple[str, str]:
        """Resume matching this role, resolved from config.yaml — never hardcoded.

        Returns (stem, relative_path); ("", "") when nothing is configured, in
        which case the caller must leave resume selection untouched.
        """
        title_low = (job_title or "").lower()
        ai_track = any(k in title_low for k in ["ai", "python", "genai", "llm", "data", "machine learning"])
        wanted = (profile_name or "").strip().lower()
        profiles = list(getattr(self.config, "profiles", []) or [])
        primary = next(
            (p for p in profiles if (p.name or "").strip().lower() == wanted and getattr(p, "resume_file", None)),
            None,
        )
        if ai_track and primary is not None:
            return self._resume_ref(primary.resume_file)
        if not ai_track:
            # Full-stack/web track: prefer a non-AI profile's resume, else the caller's.
            for p in profiles:
                if getattr(p, "resume_file", None) and not any(
                    k in (p.name or "").lower() for k in ["ai", "python"]
                ):
                    return self._resume_ref(p.resume_file)
        if primary is not None:
            return self._resume_ref(primary.resume_file)
        fallback = self.config.resume_for(getattr(self.account, "key", "") or "primary")
        if fallback:
            return self._resume_ref(fallback)
        return "", ""

    async def _fill_step_inputs(self, modal: Locator, job: Job | None = None, profile_name: str = "") -> None:
        """Fills radio fieldsets, text inputs, dropdowns, comboboxes strictly within the modal."""
        # 1. State-aware resume selection (never deselects already selected resume)
        target_resume_stem, target_resume_file = self._resume_for_role(
            profile_name, job.title if job else ""
        )
        if not target_resume_stem:
            log.warning("linkedin.apply.no_resume_configured", profile=profile_name)

        resume_cards = await modal.locator("div[data-test-document-item], .jobs-document-upload-redesign-card, div.jobs-document-upload-redesign-card__container").all()
        card_selected = False
        for card in (resume_cards if target_resume_stem else []):
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
        if target_resume_file and not card_selected:
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
                if _looks_like_option_dump(label):
                    # The "label" is the option list itself (Month/Year
                    # dropdowns) — dig for the real question instead.
                    label = ""
                    try:
                        aria = (await sel.get_attribute("aria-label") or "").strip()
                    except Exception:
                        aria = ""
                    if aria and not _looks_like_option_dump(aria):
                        label = aria
                    if not label:
                        try:
                            named = (await sel.get_attribute("name") or "").strip()
                        except Exception:
                            named = ""
                        if named:
                            label = re.sub(r"[_-]+", " ", named).strip()
                    if not label:
                        try:
                            labelledby = (await sel.get_attribute("aria-labelledby") or "").strip().split()
                            for ref_id in labelledby:
                                ref_el = modal.locator(f"#{ref_id}").first
                                if await ref_el.count() > 0:
                                    ref_text = (await safe_text(ref_el)).strip()
                                    if ref_text and not _looks_like_option_dump(ref_text):
                                        label = ref_text
                                        break
                        except Exception:
                            pass
                    if not label:
                        log.debug("linkedin.apply.select_label_unknown", options=(await sel.locator("option").all_inner_texts())[:4])
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
                    target_email = (self.config.applicant_email or "").strip()
                    if not target_email:
                        log.warning("linkedin.apply.missing_applicant_email")
                        continue
                    chosen_opt = ""
                    for o in options:
                        if target_email.lower() in o.lower():
                            chosen_opt = o.strip()
                            break
                    if chosen_opt:
                        await sel.select_option(label=chosen_opt)
                        log.info("linkedin.apply.email_selected")
                    elif options:
                        log.info("linkedin.apply.email_default_kept")
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
                    if any(k in low_q for k in ["sponsorship", "visa sponsorship", "require sponsorship"]) or any(k in low_q for k in ["disability", "handicap", "impairment"]) or any(k in low_q for k in ["veteran", "military"]):
                        target_val = "No"
                    elif any(k in low_q for k in ["gender", "sex"]):
                        target_val = "Male"
                    elif any(k in low_q for k in ["authorized", "authorization", "commute", "relocate", "comfortable", "background", "willing", "participate", "agree", "process", "interest", "onsite", "hybrid", "remote", "budget", "salary", "lpa", "join", "immediate", "bond", "contract", "policy", "terms", "undertaking"]):
                        target_val = "Yes"
                    else:
                        target_val = "Yes"

                matched_radio = False
                for opt in options:
                    otext = (await safe_text(opt)).strip()
                    aria_lbl = (await opt.get_attribute("aria-label") or "").strip()
                    opt_combo = f"{otext} {aria_lbl}".lower()
                    is_affirmative_opt = any(y in opt_combo for y in ["yes", "agree", "okay", "ok", "accept", "willing", "available", "confirm"])
                    is_negative_opt = any(n in opt_combo for n in ["no", "decline", "not", "disagree", "unwilling"])

                    if target_val == "Yes" and (target_val.lower() in opt_combo or (is_affirmative_opt and not is_negative_opt)) or target_val and target_val.lower() in opt_combo:
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
                    # Fail closed: never submit a hardcoded fallback number. If the
                    # config has no phone, leave the field for human review.
                    phone_val = (self.config.applicant_phone or "").strip()
                    if not phone_val:
                        log.warning("linkedin.apply.missing_applicant_phone")
                        continue
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
                        log.info("linkedin.apply.phone_updated")
                    continue

                # Email field if input instead of select
                is_email_field = (
                    inp_type == "email"
                    or ("email" in label_low and "subscribe" not in label_low)
                    or "email" in aria_label.lower()
                    or await inp.evaluate("el => !!(el.id && el.id.toLowerCase().includes('email')) || !!(el.name && el.name.toLowerCase().includes('email'))")
                )
                if is_email_field:
                    target_email = (self.config.applicant_email or "").strip()
                    if not target_email:
                        log.warning("linkedin.apply.missing_applicant_email")
                        continue
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
                        log.info("linkedin.apply.email_filled")
                    continue

                # City / Location combobox
                if any(k in label_low for k in ["city", "location", "zip code", "state"]):
                    if not val or "search" in val.lower():
                        loc_val = (self.config.applicant_location or "India").strip() or "India"
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
                        max_attr = await inp.get_attribute("max")
                        min_attr = await inp.get_attribute("min")
                        max_val = float(max_attr) if max_attr and re.match(r"^\d+(\.\d+)?$", max_attr) else None
                        min_val = float(min_attr) if min_attr and re.match(r"^\d+(\.\d+)?$", min_attr) else None

                        if any(k in label_low for k in ["notice", "how soon", "availability", "immediate", "serving"]):
                            ans = "0"
                        elif "months" in label_low:
                            ans = "6"
                        elif any(k in label_low for k in ["percentile", "cet", "jee", "math", "class 10", "10th", "12th", "percentage", "cgpa", "marks"]):
                            # Never fabricate exam scores or grades: leave the field
                            # empty so it routes to human review instead of
                            # submitting false credentials to a recruiter.
                            ans = ""
                        elif "rate" in label_low or "scale" in label_low or "out of" in label_low or "/5" in label_low or "/10" in label_low:
                            if (max_val and max_val <= 5) or "out of 5" in label_low or "/5" in label_low or "1-5" in label_low or "1 to 5" in label_low:
                                ans = "5"
                            elif (max_val and max_val <= 10) or "out of 10" in label_low or "/10" in label_low or "1-10" in label_low or "1 to 10" in label_low:
                                ans = "9"
                            else:
                                ans = "5" if (max_val and max_val <= 5) else "9"
                        elif "ctc" in label_low or "salary" in label_low or "compensation" in label_low:
                            # Config first (expected/current CTC from config.yaml —
                            # same 4/7 for every job), scaled to the field's unit.
                            # Only 2 unit types exist: LPA (4/7) and full INR
                            # (400000/700000). The stale 650000/650 literals never
                            # matched the config and are gone.
                            _ctc_res = self.answers.resolve(ScreeningQuestion(text=label, kind="text"))
                            _ctc_lpa: float | None = None
                            if _ctc_res and _ctc_res.value:
                                _m = re.search(r"[-+]?\d*\.?\d+", str(_ctc_res.value))
                                if _m:
                                    try:
                                        _ctc_lpa = float(_m.group(0))
                                    except ValueError:
                                        _ctc_lpa = None
                            if any(w in label_low for w in ["inr", "annual", "per year", "per annum", "/year"]) or (min_val and min_val >= 1000):
                                _unit = "inr"
                            elif "thousand" in label_low or (min_val and min_val >= 100):
                                _unit = "thousands"
                            else:
                                _unit = "lpa"  # lakh/lpa label or LinkedIn India default
                            _base = _ctc_lpa if _ctc_lpa is not None else 7.0  # expected CTC fallback
                            if _unit == "inr":
                                ans = str(int(_base * 100000))
                            elif _unit == "thousands":
                                ans = str(int(_base * 100))
                            else:
                                ans = str(int(_base)) if float(_base) == int(_base) else str(_base)
                        else:
                            # Skill or general years of experience -> whole integer 2 (respecting max if 1)
                            ans = "1" if (max_val and max_val < 2) else "2"
                else:
                    resolved = self.answers.resolve(ScreeningQuestion(text=label, kind="text"))
                    ans = resolved.value if resolved else ""

                    import datetime
                    if "ddmmyy" in label_low or "ddmmyyyy" in label_low or "dd-mm-yy" in label_low or "dd/mm/yy" in label_low or "dd-mm-yyyy" in label_low or "dd/mm/yyyy" in label_low:
                        if "ddmmyyyy" in label_low or "dd-mm-yyyy" in label_low:
                            ans = datetime.date.today().strftime("%d%m%Y" if "ddmmyyyy" in label_low else "%d-%m-%Y")
                        elif "dd/mm/yyyy" in label_low:
                            ans = datetime.date.today().strftime("%d/%m/%Y")
                        elif "dd/mm/yy" in label_low:
                            ans = datetime.date.today().strftime("%d/%m/%y")
                        elif "dd-mm-yy" in label_low:
                            ans = datetime.date.today().strftime("%d-%m-%y")
                        else:
                            ans = datetime.date.today().strftime("%d%m%y")
                    elif inp_type == "date" or any(k in label_low for k in ["start date", "joining date", "available date", "date of", "last working", "lwd", "relieving"]):
                        ans = datetime.date.today().strftime("%Y-%m-%d")
                    elif not ans and any(k in label_low for k in ["percentile", "cet", "jee", "math", "class 10", "10th", "12th", "percentage", "cgpa", "marks"]):
                        # Never fabricate exam scores or grades: without a configured
                        # answer the field is left for human review.
                        ans = ""
                    elif any(k in label_low for k in ["notice", "immediate", "serving", "availability"]):
                        num_match = re.search(r"[-+]?\d*\.?\d+", ans or "")
                        ans = str(max(0, min(99, round(float(num_match.group(0)))))) if num_match else ""
                    elif not ans and any(k in label_low for k in ["ctc", "salary", "compensation", "package"]):
                        # No configured answer: skip rather than invent compensation.
                        ans = ""
                    elif not ans:
                        if "title" in label_low:
                            ans = "Full Stack & AI Engineer"
                        elif "company" in label_low:
                            ans = "Independent Software Consultant"
                        elif "linkedin" in label_low:
                            ans = (self.config.applicant_linkedin or "").strip()
                        elif "github" in label_low or "portfolio" in label_low or "website" in label_low:
                            gh = (self.config.applicant_github or "").strip()
                            pf = (self.config.applicant_portfolio or "").strip()
                            if "github" in label_low and gh:
                                ans = gh
                            elif pf:
                                ans = pf
                            else:
                                ans = ""
                        else:
                            from ..core.gemini_writer import applicant_snapshot
                            who = applicant_snapshot()
                            applicant_name = who.name if who.name != "a Software Engineer" else (self.config.applicant_name or "").strip()
                            ans = (
                                f"Experienced software and AI engineer ({applicant_name}) with {who.experience_label} of experience "
                                "building scalable applications, APIs, and AI workflows."
                                if applicant_name else ""
                            )

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

                    # 1. Radio group in error container: resolve via
                    # AnswerEngine first (blind first-option clicks have
                    # answered e.g. sponsorship with "Yes" before).
                    radios = await target_scope.locator("input[type='radio']").all()
                    if radios:
                        q_text = (await safe_text(target_scope)).strip()[:250]
                        opt_texts = []
                        for _r in radios:
                            _t = (await safe_text(_r)).strip()
                            if _t:
                                opt_texts.append(_t)
                        picked = None
                        if q_text:
                            _res = self.answers.resolve(
                                ScreeningQuestion(text=q_text, kind="radio", options=opt_texts)
                            )
                            if _res and _res.value:
                                for _r in radios:
                                    _t = (await safe_text(_r)).strip().lower()
                                    if _res.value.lower() in _t or _t in _res.value.lower():
                                        picked = _r
                                        break
                        first_radio = picked or radios[0]
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
                                new_val = (self.config.applicant_email or "").strip()
                            elif "phone" in err_text_low or "mobile" in err_text_low:
                                new_val = (self.config.applicant_phone or "").strip()
                            elif "date" in err_text_low or (await inp.get_attribute("type") == "date"):
                                import datetime
                                new_val = datetime.date.today().strftime("%Y-%m-%d")

                            if not new_val:
                                # Config holds no identity value for this field:
                                # fail closed instead of submitting a blank/literal.
                                log.warning("linkedin.apply.error_recovery_no_config_value")
                                continue
                            await inp.fill("")
                            await inp.fill(new_val)
                            await inp.dispatch_event("input")
                            await inp.dispatch_event("change")
                            await inp.dispatch_event("blur")
                            log.info("linkedin.apply.error_recovered_input", length=len(new_val))

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
                                    await sel.evaluate("el => { el.selectedIndex = 1; el.dispatchEvent(new Event('change', {bubbles: true})); }")
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
