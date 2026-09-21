"""
Wellfound (AngelList) Platform Implementation.
"""
from __future__ import annotations
import asyncio

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from playwright.async_api import Page

from ..browser.artifacts import ArtifactStore
from ..browser.resilience import (
    first_visible,
    human_pause,
    human_type,
    retry_async,
    safe_text,
)
from ..config import JobProfile, NaukriAccount, get_settings
from ..core.gemini_writer import GeminiWriter
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
from ..naukri.parser import parse_salary_lpa
from .base import BaseJobPlatform

log = get_logger(__name__)

# Wellfound strategy: startups hire fast and leave stale posts up. Anything
# older than a week is treated as dead — past that, the job is already old.
MAX_POSTED_DAYS = 7

_AGO_RE = re.compile(
    r"(\d+)\s*(hours?|hrs?|h|days?|d|weeks?|w|months?|mos?|years?|y)\s*ago|\bjust now\b|\btoday\b|\byesterday\b",
    re.IGNORECASE,
)
_CARD_EXP_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:-|to|\+)\s*(\d+(?:\.\d+)?)?\s*(?:yrs|years|yr)\b", re.IGNORECASE
)
_SINGLE_EXP_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\+\s*(?:yrs|years|yr)\b", re.IGNORECASE)

# (label, pattern): labels surface in skip reasons, patterns do the matching.
# User-first policy: this prefilter mirrors the base profiles, exactly like
# the plan-stage FilterRules do. QA/testing, data-analyst, fresher and
# lead/senior/sr titles are NOT blocked here — relevance is decided by the
# title allowlist and seniority by the experience gates (unknown never
# rejects; the detail page re-checks). Only off-stack families stay blocked.
_BLOCKED_TITLES: tuple[tuple[str, str], ...] = (
    ("java", r"\bjava\b"), ("spring", r"\bspring\b"), (".net", r"\.net\b"),
    ("dotnet", r"\bdotnet\b"), ("php", r"\bphp\b"), ("wordpress", r"\bwordpress\b"),
    ("salesforce", r"\bsalesforce\b"), ("sap", r"\bsap\b"), ("oracle", r"\boracle\b"),
    ("mainframe", r"\bmainframe\b"), ("principal", r"\bprincipal\b"),
    ("staff", r"\bstaff\b"), ("architect", r"\barchitect\b"), ("manager", r"\bmanager\b"),
    ("director", r"\bdirector\b"), ("head of", r"\bhead of\b"), ("intern", r"\bintern\b"),
    ("trainee", r"\btrainee\b"), ("sales", r"\bsales\b"),
    ("presales", r"\bpresales\b"), ("recruiter", r"\brecruiter\b"), ("hr", r"\bhr\b"),
    ("accountant", r"\baccountant\b"), ("designer", r"\bdesigner\b"),
    ("writer", r"\bwriter\b"), ("marketing", r"\bmarketing\b"),
    ("security engineer", r"\bsecurity engineer\b"), ("cybersecurity", r"\bcybersecurity\b"),
    ("infosec", r"\binfosec\b"), ("data scientist", r"\bdata scientist\b"),
    ("it support", r"\bit support\b"),
    ("helpdesk", r"\bhelpdesk\b"), ("service desk", r"\bservice desk\b"),
)
_BLOCKED_TITLE_RES = tuple((label, re.compile(pat, re.IGNORECASE)) for label, pat in _BLOCKED_TITLES)
_SPAM_RES = ("unpaid", "no salary", "without salary", "volunteer", "commission only", "survey")
_FOREIGN_ONSITE = (
    "united states", "usa", "u.s.", "uk", "united kingdom", "canada", "germany",
    "france", "australia", "singapore", "austin", "texas", "california", "london",
    "new york", "seattle", "toronto", "berlin",
)
_INDIA_MARKERS = (
    "india", "bengaluru", "bangalore", "hyderabad", "mumbai", "pune", "delhi",
    "noida", "gurgaon", "gurugram", "chennai", "kolkata", "ahmedabad", "kochi",
    "remote", "work from home", "wfh", "anywhere", "distributed",
)


def _parse_posted_days(text: str) -> int | None:
    """'3d ago' -> 3, '2 weeks ago' -> 14, 'just now' -> 0. None when unstated."""
    if not text:
        return None
    low = text.lower()
    if any(k in low for k in ("just now", "few minutes", "today", "hour")):
        return 0
    if "yesterday" in low:
        return 1
    m = _AGO_RE.search(low)
    if not m or not m.group(1):
        return None
    n = int(m.group(1))
    unit = (m.group(2) or "day").lower()
    if unit.startswith("h"):
        return 0
    if unit.startswith("w"):
        return n * 7
    if unit.startswith("mo"):
        return n * 30
    if unit.startswith("y"):
        return n * 365
    return n


def _parse_card_experience(text: str) -> tuple[float | None, float | None]:
    """Experience range only when adjacent to a year token ('1-3 years', '5+ yrs')."""
    if not text:
        return None, None
    m = _CARD_EXP_RE.search(text)
    if m:
        lo = float(m.group(1))
        hi = float(m.group(2)) if m.group(2) else (None if "+" in (m.group(0) or "") else lo)
        return lo, hi
    return None, None


def _wellfound_suitable(title: str, description: str, location: str) -> tuple[bool, str]:
    """Wellfound-specific pre-screen: spam, wrong stack/track, foreign on-site."""
    low_title = (title or "").lower()
    low_text = f"{low_title} {(description or '').lower()}"
    for label, pat in _BLOCKED_TITLE_RES:
        if pat.search(low_title):
            return False, f"Blocked title/stack ('{label}')"
    if any(k in low_text for k in _SPAM_RES):
        return False, "Unpaid/volunteer posting"
    loc = (location or "").lower()
    if any(k in loc for k in _FOREIGN_ONSITE) and not any(
        k in loc or k in low_title for k in _INDIA_MARKERS
    ):
        return False, f"Foreign on-site ({location})"
    return True, ""


def _stack_hit(term: str, haystack: str) -> bool:
    """Token-aware match so 'c' never matches 'react' and 'go' never matches 'django'."""
    t = (term or "").strip().lower()
    if not t:
        return False
    left = r"\b" if re.match(r"^\w", t) else r"(?<!\w)"
    right = r"\b" if re.search(r"\w$", t) else r"(?!\w)"
    return re.search(left + re.escape(t) + right, haystack) is not None


# Feed chrome that leaks into card text but is never a company name (run 308:
# "Save Learn more" and "Apply on Wellfound  Save Learn more" were persisted
# as the company for 15 of 40 collected rows).
_CHROME_COMPANIES = frozenset({
    "save learn more",
    "apply on wellfound save learn more",
    "apply on wellfound",
    "save",
    "learn more",
})

# Markers where the real title ends inside a Wellfound card blob. Card link
# text arrives as one unbroken string, e.g.
# "Senior AI Full Stack Engineer (React + Node) Remote onlyEverywhere
#  RECRUITER RECENTLY ACTIVE POSTED YESTERDAY" — everything from the first
# marker on is location/salary/recruiter chrome, never the title.
_TITLE_CUT_RES = tuple(
    re.compile(pat, re.IGNORECASE)
    for pat in (
        r"onsite or remote",
        r"remote only",
        r"remote\s*\(",
        r"in office",
        r"in-office",
        r"recruiter",
        r"posted",
        r"no equity",
        r"•",
        r"\$",
        r"₹",
        r"apply on wellfound",
        r"save learn more",
    )
)

# Markers where the location segment ends (salary/recruiter/posted tail).
_LOCATION_CUT_RES = tuple(
    re.compile(pat, re.IGNORECASE)
    for pat in (
        r"recruiter",
        r"posted",
        r"no equity",
        r"•",
        r"\$",
        r"₹",
    )
)

_LOCATION_HINTS = (
    "remote", "onsite", "on-site", "hybrid", "in office", "india",
    "bengaluru", "bangalore", "hyderabad", "mumbai", "pune", "delhi",
    "noida", "gurgaon", "gurugram", "chennai", "kolkata", "tokyo", "seoul",
)

# Link texts that are feed navigation, never a job title.
_CHROME_TITLES = frozenset({
    "home", "jobs", "apply", "save", "learn more", "apply on wellfound",
})


def _clean_wellfound_title(raw: str) -> str:
    """Cut location/salary/recruiter chrome off a card blob, keeping the title."""
    text = (raw or "").strip()
    if not text:
        return ""
    cut = len(text)
    for rgx in _TITLE_CUT_RES:
        m = rgx.search(text)
        if m:
            cut = min(cut, m.start())
    text = text[:cut].strip()
    return re.sub(r"[\s—–\-|,.(]+$", "", text).strip()


def _clean_wellfound_company(raw: str) -> str:
    """Drop feed-chrome company strings; '' means fall back to 'Confidential'."""
    text = (raw or "").strip()
    if not text:
        return ""
    # Collapse internal runs: the feed emits "Apply on Wellfound  Save Learn
    # more" (double space), which a literal set lookup would miss.
    if " ".join(text.lower().split()) in _CHROME_COMPANIES:
        return ""
    return text


def _clean_wellfound_location(raw: str) -> str:
    """Keep the location segment, cut the salary/recruiter/posted tail."""
    text = (raw or "").strip()
    if not text:
        return ""
    if not any(h in text.lower() for h in _LOCATION_HINTS):
        return ""
    cut = len(text)
    for rgx in _LOCATION_CUT_RES:
        m = rgx.search(text)
        if m:
            cut = min(cut, m.start())
    return re.sub(r"\s+", " ", text[:cut]).strip(" ,•|-")


class WellfoundPlatform(BaseJobPlatform):
    def __init__(self, page: Page, account: NaukriAccount, artifacts: ArtifactStore, policy: RunPolicy, config: Any = None, metrics: Any | None = None):
        super().__init__(page, account.key, policy)
        self.account = account
        self.artifacts = artifacts
        self._config = config
        self._metrics = metrics

        # Initialize GeminiWriter for Wellfound pitches
        settings = get_settings()
        self.gemini = GeminiWriter(api_key=getattr(settings, "gemini_api_key", ""))

    def _profile_must_skills(self, profile_name: str) -> list[str]:
        """Core stack for this profile from config.yaml. Empty = gate skipped."""
        cfg = self._config
        if cfg is None:
            return []
        wanted = (profile_name or "").strip().lower()
        for p in getattr(cfg, "profiles", []) or []:
            if (p.name or "").strip().lower() == wanted:
                skills = [s.strip() for s in (p.core_skills or []) if s and s.strip()]
                if skills:
                    return skills
                return [k.strip() for k in (p.title_keywords or []) if k and k.strip()]
        return []

    @property
    def platform_name(self) -> str:
        return "wellfound"

    async def _is_authenticated(self) -> bool:
        """
        Positive verification that candidate is actually authenticated on Wellfound.
        Checks all open pages in the context (in case login opened a new tab/popup).
        Avoids checking footer recruiter links which say 'Log in' even when logged in.
        """
        pages_to_check = list(reversed(self.page.context.pages))
        for p in pages_to_check:
            url_low = p.url.lower()
            if "wellfound.com" not in url_low:
                continue

            # If this page has active login form fields, it's not authenticated yet
            try:
                if await p.locator("input#user_email").first.is_visible():
                    continue
            except Exception:
                pass

            # Check positive logged-in candidate indicators
            for logged_in_sel in (
                "a[href*='/messages']",
                "button[aria-label*='user' i]",
                "button[aria-label*='User Menu' i]",
                "button[data-test*='UserMenu']",
                "button[data-test*='user-menu']",
                "div[data-test*='UserAvatar']",
                "a[href*='/profile/edit']",
                "a[href*='/candidate']",
                "a[href*='/jobs/applied']",
                "header img[alt*='avatar' i]",
                "nav img[alt*='avatar' i]",
                "button:has-text('Log out')",
                "button:has-text('Sign out')",
                "a[href*='/logout']",
            ):
                try:
                    if await p.locator(logged_in_sel).first.is_visible():
                        self.page = p
                        return True
                except Exception:
                    pass

        # Check cookies for verified user session
        try:
            cookies = await self.page.context.cookies()
            cookie_names = {
                c.get("name") for c in cookies
                if "wellfound" in c.get("domain", "") or "angellist" in c.get("domain", "")
            }
            auth_cookies = {"_al_u", "_al_s", "user_id", "remember_user_token", "ajs_user_id"}
            if auth_cookies.intersection(cookie_names):
                for p in pages_to_check:
                    if "wellfound.com" in p.url and "login" not in p.url.lower() and "signup" not in p.url.lower():
                        self.page = p
                        return True
        except Exception:
            pass

        return False

    async def ensure_logged_in(self) -> bool:
        import sys
        target_url = "https://wellfound.com/jobs"
        if "wellfound.com/jobs" not in self.page.url:
            try:
                await self.page.goto(target_url, wait_until="commit", timeout=30000)
                await human_pause(1500, 3000)
            except Exception:
                try:
                    await self.page.goto(target_url, timeout=30000)
                    await human_pause(1500, 3000)
                except Exception as exc:
                    log.warning("wellfound.auth.goto_warning", error=str(exc))
        else:
            await human_pause(500, 1000)

        # 1. Check if already logged in from saved DB session
        if await self._is_authenticated():
            log.info("wellfound.auth.session_reused")
            return True

        # 2. In headless CI mode, fail-fast so pipeline doesn't freeze
        is_headless = getattr(getattr(self._config, "browser", None), "headless", True) if self._config else True
        if is_headless:
            log.warning(
                "wellfound.auth.headless_session_missing",
                msg="No active Wellfound session in Postgres. Run 'python -m naukri_agent login --platform wellfound' locally to save session.",
            )
            return False

        # 3. In headed mode, guide user directly to login page
        login_url = "https://wellfound.com/login"
        try:
            await self.page.goto(login_url, wait_until="domcontentloaded", timeout=25000)
            await human_pause(1000, 2000)
        except Exception:
            pass

        timeout_s = 300
        print("\n" + "=" * 76)
        print("🔑 WELLFOUND LOGIN REQUIRED (Headed Mode)")
        print("=" * 76)
        print("Please log in to your Wellfound account in the opened Chrome window.")
        print("You can use 'Continue with Google' or your Email & Password.")
        print()
        print("👉 Press [ENTER] in this terminal as soon as you have finished logging in!")
        print(f"   (Auto-detection is also monitoring in the background for {timeout_s}s)")
        print("=" * 76 + "\n")

        log.warning(
            "wellfound.auth.waiting_for_user",
            timeout_s=timeout_s,
            msg="Please log in to Wellfound in Chrome. Press ENTER when done or wait for auto-detect.",
        )

        import time
        deadline = time.time() + timeout_s
        last_log_s = time.time()

        # Listen for ENTER key press non-blockingly if interactive terminal
        loop = asyncio.get_running_loop()
        enter_task = None
        if sys.stdin.isatty():
            try:
                enter_task = loop.run_in_executor(None, sys.stdin.readline)
            except Exception:
                enter_task = None

        while time.time() < deadline:
            # Check 1: User pressed ENTER to confirm login
            if enter_task and enter_task.done():
                log.info("wellfound.auth.user_confirmed_via_enter", msg="User confirmed login via ENTER key.")
                for p in reversed(self.page.context.pages):
                    if "wellfound.com" in p.url and "login" not in p.url.lower():
                        self.page = p
                        break
                if "wellfound.com/jobs" not in self.page.url:
                    try:
                        await self.page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                        await human_pause(1500, 2500)
                    except Exception:
                        pass
                return True

            # Check 2: Auto-detection verified login
            if await self._is_authenticated():
                log.info("wellfound.auth.manual_login_success", msg="Login confirmed! Session will be saved.")
                for p in reversed(self.page.context.pages):
                    if "wellfound.com" in p.url and "login" not in p.url.lower():
                        self.page = p
                        break
                if "wellfound.com/jobs" not in self.page.url:
                    try:
                        await self.page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                        await human_pause(1500, 2500)
                    except Exception:
                        pass
                return True

            now = time.time()
            if now - last_log_s >= 15:
                rem = int(deadline - now)
                log.info("wellfound.auth.waiting_for_login", remaining_s=rem)
                last_log_s = now

            await asyncio.sleep(1)

        log.error("wellfound.auth.timeout")
        return False


    async def fetch_jobs(self, profile: JobProfile, exclude_job_ids: set[str]) -> list[Job]:
        log.info("wellfound.fetch.start", profile=profile.name)
        jobs: list[Job] = []
        seen_job_ids: set[str] = set()
        link_elements: list = []
        target_url = "https://wellfound.com/jobs"
        if "wellfound.com/jobs" not in self.page.url:
            try:
                await self.page.goto(target_url, wait_until="commit", timeout=30000)
                await human_pause(2000, 4000)
            except Exception:
                try:
                    await self.page.goto(target_url, timeout=30000)
                    await human_pause(2000, 4000)
                except Exception as exc:
                    log.warning("wellfound.fetch.goto_warning", error=str(exc))
        else:
            await human_pause(1000, 2000)

        # Infinite scroll until the UNIQUE job-id set stabilises: guest probe
        # holds 50 unique ids with zero churn, but logged-in runs stopped at
        # 24 link els / 19 unique because the old guard compared raw element
        # counts with only 2 stagnant rounds. Track unique ids, allow up to
        # 40 rounds, require 5 stagnant rounds, and drive the scroll with
        # JS + End + wheel so virtualized cards keep rendering.
        stagnant = 0
        seen_ids: set[str] = set()
        last_unique = -1
        for _ in range(40):
            try:
                if self.page.is_closed():
                    log.warning("wellfound.fetch.page_closed_mid_scroll")
                    break
                try:
                    await self.page.evaluate(
                        """() => {
                            window.scrollTo(0, document.body.scrollHeight);
                            document.scrollingElement?.scrollTo(0, document.scrollingElement.scrollHeight);
                            document.querySelectorAll('*').forEach(el => {
                                if (el.scrollHeight > el.clientHeight && el.clientHeight > 300) {
                                    el.scrollTop = el.scrollHeight;
                                }
                            });
                        }"""
                    )
                    more_btn = self.page.locator("button:has-text('Load more'), button:has-text('Show more'), button:has-text('View more')").first
                    if await more_btn.count() > 0 and await more_btn.is_visible():
                        await more_btn.click()
                        await human_pause(1000, 1500)
                except Exception:
                    pass
                await self.page.keyboard.press("End")
                await human_pause(800, 1200)
                try:
                    await self.page.mouse.wheel(0, 1500)
                except Exception:
                    pass
                await human_pause(400, 700)
            except Exception as exc:
                # Feed churn (closed/navigated tab mid-scroll) is a skip, not
                # a failure: partial results stay usable, and FAILED would trip
                # the consecutive-failure breaker for a markup change that
                # never happened.
                log.warning("wellfound.fetch.scroll_interrupted", error=str(exc)[:150])
                break
            try:
                link_elements = await self.page.locator("a[href*='/jobs/']").all()
            except Exception as exc:
                log.warning("wellfound.fetch.links_unreadable", error=str(exc)[:150])
                break
            for link in link_elements:
                try:
                    href = await link.get_attribute("href") or ""
                except Exception:
                    continue
                m = re.search(r"/jobs/(\d+)-", href)
                if m:
                    seen_ids.add(m.group(1))
            if len(seen_ids) <= last_unique:
                stagnant += 1
                if stagnant >= 5:
                    break
            else:
                stagnant = 0
            last_unique = len(seen_ids)
        log.info("wellfound.fetch.scrolled", links=len(link_elements), unique_ids=len(seen_ids))

        # Feed audit: persist the full scrolled link inventory (id + href)
        # BEFORE dedupe/prefilter cuts it. Run 319 showed links=24 but
        # count=1 — without this file the 23 missing rows are unprovable.
        # Writes under the run's ArtifactStore dir (git-ignored) so debug
        # output never lands in the committed analysis/ CSVs.
        try:
            inv: dict[str, str] = {}
            for link in link_elements:
                try:
                    href = await link.get_attribute("href") or ""
                except Exception:
                    continue
                m = re.search(r"/jobs/(\d+)-", href)
                if m:
                    inv.setdefault(f"wellfound-{m.group(1)}", href)
            if inv:
                from ..config import PROJECT_ROOT as _ROOT  # noqa: PLC0415

                audit_dir = _ROOT / "artifacts" / "wellfound_debug"
                audit_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
                (audit_dir / f"links_{stamp}.txt").write_text(
                    "\n".join(f"{jid} {href}" for jid, href in sorted(inv.items())),
                    encoding="utf-8",
                )
                (audit_dir / "latest.txt").write_text(
                    "\n".join(f"{jid} {href}" for jid, href in sorted(inv.items())),
                    encoding="utf-8",
                )
            log.info(
                "wellfound.fetch.link_audit",
                unique_ids=len(inv),
            )
        except Exception as exc:
            log.warning("wellfound.fetch.link_audit_failed", error=str(exc)[:150])

        # Fetch-stage drop accounting: answers "129 results but 0 collected"
        # without re-running. Buckets: already-seen (dedupe), stale (>7d),
        # off-stack titles, unreadable cards.
        drop_seen = 0
        drop_stale = 0
        drop_blocked: dict[str, int] = {}
        drop_chrome = 0
        for index, link in enumerate(link_elements, start=1):
            try:
                url = await link.get_attribute("href") or ""
                match = re.search(r"/jobs/(\d+)-", url)
                if not match:
                    # Feed navigation links (/jobs/home, /jobs/applied) carry
                    # no job id — run 308 persisted one as a "Home" job row.
                    drop_chrome += 1
                    continue
                if url.startswith("/"):
                    url = f"https://wellfound.com{url}"

                job_id = f"wellfound-{match.group(1)}"
                # Exclude + intra-feed dedupe BEFORE any DOM reads: the
                # already-applied rows in run 308 (33 of them) each cost a
                # company-xpath lookup, lines scan and regex pass first.
                if job_id in exclude_job_ids or job_id in seen_job_ids:
                    drop_seen += 1
                    continue
                seen_job_ids.add(job_id)

                raw_text = await safe_text(link)
                lines = [line.strip() for line in raw_text.split("\n") if line.strip()]
                if not lines:
                    drop_chrome += 1
                    continue
                title = _clean_wellfound_title(lines[0])
                if not title or title.lower() in _CHROME_TITLES:
                    drop_chrome += 1
                    continue

                # Immediate title pre-filter against off-stack families only
                # (user-first policy: QA/senior/lead/fresher pass here and are
                # judged by the allowlist + experience gates instead).
                block_hit = None
                for lbl, rgx in _BLOCKED_TITLE_RES:
                    if rgx.search(title):
                        block_hit = lbl
                        break
                if block_hit:
                    drop_blocked[block_hit] = drop_blocked.get(block_hit, 0) + 1
                    log.debug("wellfound.fetch.prefilter_blocked", title=title, blocked=block_hit)
                    continue

                # Company name: extract from preceding company link in the feed.
                # Chrome strings ("Save Learn more", "Apply on Wellfound…")
                # are filtered so they never persist as the company.
                comp_loc = link.locator("xpath=preceding::a[contains(@href, '/company/')][1]")
                company = ""
                if await comp_loc.count() > 0:
                    company = _clean_wellfound_company(await comp_loc.inner_text())
                    if not company:
                        img = comp_loc.locator("img").first
                        if await img.count() > 0:
                            company = _clean_wellfound_company(
                                (await img.get_attribute("alt") or "").replace("company logo", "")
                            )

                # Location & meta from remaining lines: keep the segment that
                # names a place/remote mode, cut the salary/recruiter/posted tail.
                location = ""
                for line in lines[1:]:
                    cleaned = _clean_wellfound_location(line)
                    if cleaned:
                        location = cleaned
                        break

                posted_days = _parse_posted_days(raw_text)
                if posted_days is not None and posted_days > MAX_POSTED_DAYS:
                    drop_stale += 1
                    continue

                min_exp, max_exp = _parse_card_experience(f"{title} {raw_text}")
                min_sal, max_sal = None, None
                if "$" not in raw_text:
                    min_sal, max_sal = parse_salary_lpa(raw_text)

                jobs.append(
                    Job(
                        job_id=job_id,
                        title=title,
                        company=company or "Confidential",
                        url=url,
                        location=location,
                        min_experience=min_exp,
                        max_experience=max_exp,
                        min_salary_lpa=min_sal,
                        max_salary_lpa=max_sal,
                        posted_days_ago=posted_days,
                        recommendation_tab="default",
                        recommendation_position=index,
                        platform="wellfound",
                    )
                )
            except Exception as exc:
                log.debug("wellfound.fetch.parse_error", error=str(exc))
                continue

        log.info(
            "wellfound.fetch.done",
            count=len(jobs),
            drop_seen=drop_seen,
            drop_stale=drop_stale,
            drop_chrome=drop_chrome,
            drop_blocked=drop_blocked,
        )
        return jobs

    async def apply_to_job(
        self,
        job: Job,
        profile_name: str,
        pre_submit_check: Callable[[Job], FilterDecision] | None = None,
    ) -> ApplyOutcome:
        import time as _time

        self.require_mutation("application.apply_flow")
        t0 = _time.perf_counter()
        jt = None
        if self._metrics is not None:
            try:
                from ..core.runtime_metrics import JobTiming

                jt = JobTiming(job_id=job.job_id, title=job.title)
            except Exception:
                jt = None
        log.info("wellfound.apply.start", job_id=job.job_id)

        async def navigate():
            try:
                await self.page.goto(job.url, wait_until="commit", timeout=30000)
            except Exception:
                await self.page.goto(job.url, timeout=30000)
            await human_pause(1000, 2000)

        try:
            await retry_async(navigate, attempts=2, label=f"wellfound-open:{job.job_id}")
        except Exception as exc:
            return ApplyOutcome(ApplicationStatus.FAILED, detail=f"Failed to load: {exc!s}")

        # Check if already applied.
        # Target the application button/badge inside the job detail, NEVER the navbar link!
        # (The timestamp fallback below only handles layout drift where even
        # the detail-page marker is absent; buttons/links stay excluded.)
        already_applied = await first_visible(
            self.page,
            [
                "button[disabled]:has-text('Applied')",
                "div[class*='JobDetail'] button:has-text('Applied')",
                "div[class*='primaryContent'] button:has-text('Applied')",
                "main button:has-text('Applied')",
                "button:has-text('Applied')",
                "text=/You applied (on|.{0,20}ago)/i",
            ],
            timeout_ms=1500,
        )
        if already_applied:
            is_nav = await already_applied.evaluate(
                "el => !!el.closest('header, nav') || el.getAttribute('href') === '/jobs/applications' || el.tagName === 'A'"
            )
            if not is_nav:
                return ApplyOutcome(ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)

        # Enrich job description to generate a better pitch
        desc_el = await first_visible(self.page, ["div[data-test='JobDescription']", "div[class*='description']"])
        if desc_el:
            job.description = await safe_text(desc_el)
            links, emails = extract_description_metadata(job.description)
            job.form_links = links
            job.recruiter_emails = emails

        # Freshness re-check on the detail page (cards often omit dates).
        # Past the 1-week window the post is dead — skip, don't burn an apply.
        try:
            head_text = await self.page.evaluate(
                "() => document.body ? document.body.innerText.slice(0, 3000) : ''"
            )
        except Exception:
            head_text = ""
        detail_posted = _parse_posted_days(f"{head_text} {job.description}")
        if detail_posted is not None:
            job.posted_days_ago = detail_posted
            if detail_posted > MAX_POSTED_DAYS:
                return ApplyOutcome(
                    status=ApplicationStatus.SKIPPED,
                    reason=SkipReason.FILTER_FRESHNESS,
                    detail=f"posted {detail_posted}d ago (> {MAX_POSTED_DAYS}d window)",
                )

        # Suitability screen: spam, wrong stack/track, foreign on-site.
        suitable, reason = _wellfound_suitable(job.title, job.description, job.location)
        if not suitable:
            log.info("wellfound.apply.unsuitable", job_id=job.job_id, reason=reason)
            return ApplyOutcome(status=ApplicationStatus.SKIPPED, reason=SkipReason.LOW_MATCH_SCORE, detail=reason)

        # JD skill match: the profile's stack must appear in the title + JD.
        # This is the "only apply to matching jobs" gate.
        must_skills = self._profile_must_skills(profile_name)
        if must_skills:
            haystack = f"{job.title} {job.description}".lower()
            if not any(_stack_hit(skill, haystack) for skill in must_skills):
                return ApplyOutcome(
                    status=ApplicationStatus.SKIPPED,
                    reason=SkipReason.FILTER_DESCRIPTION,
                    detail=f"JD mentions none of the profile stack ({', '.join(must_skills[:6])})",
                )


        if pre_submit_check:
            decision = pre_submit_check(job)
            if not decision.passed:
                return ApplyOutcome(ApplicationStatus.SKIPPED, reason=decision.reason, detail=decision.detail)

        apply_btn = await first_visible(
            self.page, ["button:has-text('Apply')", "button:has-text('Apply now')", "a:has-text('Apply')"]
        )
        if not apply_btn:
            return ApplyOutcome(ApplicationStatus.FAILED, detail="Apply button not found")

        # A disabled Apply button means a prerequisite is unmet (profile
        # location too broad, screening answer required first). Clicking it
        # burns the 10s action timeout and the 300s job cap for nothing —
        # run 323 lost 3 jobs this way. Fail fast with a reviewable reason.
        try:
            if not await apply_btn.is_enabled():
                return ApplyOutcome(
                    ApplicationStatus.NEEDS_REVIEW,
                    reason=SkipReason.STUCK_FLOW,
                    detail="Apply button disabled (unmet prerequisite: location or screening answer)",
                    unanswered_questions=[{
                        "text": "Wellfound Apply button disabled — resolve location/screening prerequisite",
                        "kind": "unknown",
                        "options": [],
                    }],
                )
        except Exception:
            pass

        await apply_btn.click()
        await human_pause(1000, 2000)

        if self.page.url.startswith("http") and "wellfound.com" not in self.page.url.lower():
            return ApplyOutcome(
                ApplicationStatus.EXTERNAL,
                external_url=self.page.url,
                detail="External company-site application is not automated",
            )

        # Check if the modal asks to sign up / log in (session not authenticated)
        unauth_prompt = await first_visible(
            self.page,
            [
                "text=Complete the fields below or Log in",
                "text=Log in with your account to apply",
                "text=Set a Password*",
                "text=Sign up to apply",
            ],
            timeout_ms=1000,
        )
        if unauth_prompt:
            log.warning("wellfound.apply.unauthenticated_session", job_id=job.job_id)
            return ApplyOutcome(
                ApplicationStatus.FAILED,
                detail="Wellfound apply dialog prompted for login/registration — session is not authenticated",
            )

        # Check experience requirement displayed in modal (e.g. 'Experience: 5+ years')
        try:
            modal_loc = self.page.locator("[role='dialog']").first
            if await modal_loc.count() > 0:
                modal_text = await modal_loc.inner_text()
                exp_m = re.search(r"experience\s*[:\n]\s*(\d+(?:\.\d+)?)\s*\+?\s*years?", modal_text, re.IGNORECASE)
                if exp_m:
                    req_exp = float(exp_m.group(1))
                    if req_exp > 3.5:
                        log.info("wellfound.apply.modal_experience_mismatch", req_years=req_exp, candidate_max=3.5)
                        close_btn = await first_visible(self.page, ["button[aria-label='Close']", "button:has-text('✕')"], timeout_ms=1000)
                        if close_btn:
                            try:
                                await close_btn.click()
                            except Exception:
                                pass
                        return ApplyOutcome(
                            ApplicationStatus.SKIPPED,
                            reason=SkipReason.FILTER_EXPERIENCE,
                            detail=f"Requires {req_exp}+ YOE (candidate has 2.5 YOE)",
                        )
        except Exception:
            pass

        # Check for Location-gated rejection
        if await first_visible(self.page, ["text=not accepting applications from your", "text=not accepting applications from your location"]):
            log.info("wellfound.apply.location_gated", job_id=job.job_id)
            return ApplyOutcome(ApplicationStatus.SKIPPED, reason=SkipReason.BLOCKED_LOCATION, detail="Location-gated by company")

        # Handle Relocation Prompt if requested by Wellfound
        relocate_choice = await first_visible(
            self.page,
            [
                "label:has-text('I can relocate')",
                "button:has-text('I can relocate')",
                "div:has-text('I can relocate')",
            ],
            timeout_ms=1000,
        )
        if relocate_choice:
            try:
                await relocate_choice.click()
                await human_pause(400, 800)
            except Exception:
                pass

        # Handle Pitch Textarea ("What interests you about working for this company?")
        textarea = await first_visible(
            self.page,
            [
                "textarea[name='note']",
                "textarea[name='userNote']",
                "textarea[placeholder*='interests' i]",
                "textarea[placeholder*='note' i]",
                "[role='dialog'] textarea",
                "div[class*='modal'] textarea",
                "textarea",
            ],
            timeout_ms=4000,
        )
        if textarea:
            # A disabled pitch box is gated on a prior field (run 323: the
            # profile-location combobox must narrow first). Typing into it
            # burns the 10s action timeout per char loop — route to review
            # with the blocking question named so the loop can learn it.
            try:
                if not await textarea.is_enabled():
                    return ApplyOutcome(
                        ApplicationStatus.NEEDS_REVIEW,
                        reason=SkipReason.STUCK_FLOW,
                        detail="Pitch textarea disabled (profile location or prior answer required first)",
                        unanswered_questions=[{
                            "text": "Wellfound profile location too broad — narrow before pitch",
                            "kind": "unknown",
                            "options": [],
                        }],
                    )
            except Exception:
                pass
            log.info("wellfound.apply.generating_pitch")
            # Try Gemini cold email logic first
            pitch = ""
            try:
                pitch = self.gemini.generate_email_body(
                    role_name=job.title,
                    job_description=job.description or "",
                    company_name=job.company,
                )
            except Exception:
                pitch = ""

            # Run 313 proved Gemini can return a stub (49 chars, one sentence).
            # A one-liner pitch is worse than the template, so stubs are
            # discarded and the deterministic fallback carries the submit.
            pitch = (pitch or "").strip()
            if pitch and len(pitch) < 150:
                log.warning(
                    "wellfound.apply.pitch_stub_rejected",
                    chars=len(pitch),
                    model=self.gemini.model,
                )
                pitch = ""

            # Deterministic fallback pitch if Gemini is not configured or fails.
            # Identity resolves from config.yaml — never literals.
            if not pitch:
                from ..core.gemini_writer import applicant_snapshot
                who = applicant_snapshot()
                pitch = (
                    f"Hi Hiring Team,\n\n"
                    f"I am writing to express my strong interest in the {job.title} role at {job.company}. "
                    f"With {who.experience_label} of engineering experience developing resilient backend APIs and AI/Full-Stack services, I have a track record of "
                    f"shipping clean, production-ready features in fast-paced teams.\n\n"
                    f"I am an immediate joiner ({who.notice_label} notice period) based in {who.location}, open to remote and hybrid opportunities, "
                    f"and excited to contribute to {job.company}'s engineering goals.\n\n"
                    f"Best regards,\n{who.name}"
                )

            await human_type(textarea, pitch)
            await human_pause(500, 1000)
            try:
                delivered = await textarea.input_value()
            except Exception:
                delivered = ""
            log.info("wellfound.apply.pitch_typed", chars=len(delivered or ""))

        # Submit Application inside Dialog
        submit_btn = await first_visible(
            self.page,
            [
                "[role='dialog'] button:has-text('Apply')",
                "[role='dialog'] button:has-text('Send application')",
                "[role='dialog'] button:has-text('Submit application')",
                "[role='dialog'] button[type='submit']",
                "button:has-text('Apply')",
                "button:has-text('Send application')",
                "button:has-text('Submit application')",
                "button[type='submit']",
            ],
            timeout_ms=4000,
        )
        if submit_btn:
            try:
                await submit_btn.click(timeout=4_000)
            except Exception:
                try:
                    await submit_btn.click(force=True, timeout=3_000)
                except Exception:
                    await submit_btn.evaluate("node => node.click()")
            await human_pause(1500, 3000)

            # Check for success
            if await first_visible(
                self.page, [
                    "text=Application sent",
                    "text=You applied",
                    "button:has-text('Applied')",
                    "div[class*='success']",
                ],
                timeout_ms=5000,
            ):
                if jt is not None and self._metrics is not None:
                    jt.total_s = _time.perf_counter() - t0
                    try:
                        self._metrics.job_timings.append(jt)
                    except Exception:
                        pass
                return ApplyOutcome(
                    ApplicationStatus.APPLIED,
                    confirmation_type="dom_marker",
                    confirmation_evidence="Wellfound application success marker observed",
                )

        if jt is not None and self._metrics is not None:
            jt.total_s = _time.perf_counter() - t0
            try:
                self._metrics.job_timings.append(jt)
            except Exception:
                pass
        return ApplyOutcome(ApplicationStatus.FAILED, detail="No success confirmation received")

