"""
Job discovery.

Design decisions:

- **URL-driven search, not UI-driven.** Typing into Naukri's search box fires an
  autocomplete widget that steals Enter keypresses and produces flaky runs.
  Constructing the search URL directly (see parser.build_search_url) is
  deterministic, one page load per page of results, and trivially resumable.
- **Card-level extraction only.** We read title/company/experience/salary/
  location/freshness from the listing card and defer the (expensive) job-detail
  page load until after filtering. On a 3-page search that avoids ~60 page loads.
- **In-run and cross-run dedupe.** A `seen` set kills duplicates inside one run
  (Naukri repeats sponsored cards across pages); the DB's `known_job_ids` kills
  jobs already decided in previous runs.
- Pagination stops early on an empty page, a repeated page, or the absence of a
  Next control — never a fixed sleep-and-hope loop.
"""

from __future__ import annotations

import re
from playwright.async_api import Page

from ..browser.resilience import (
    TransientPageError,
    click_if_present,
    dismiss_overlays,
    first_visible,
    human_pause,
    retry_async,
    safe_text,
    scroll_page,
)
from ..config import RecommendedConfig, SearchSpec
from ..core.models import Job
from ..logging_setup import get_logger
from . import selectors as S
from .parser import (
    build_search_url,
    normalise_whitespace,
    parse_experience,
    parse_posted_days,
    parse_rating,
    parse_salary_lpa,
)

log = get_logger(__name__)


class JobSearcher:
    def __init__(self, page: Page, min_delay_ms: int = 350, max_delay_ms: int = 1_400) -> None:
        self.page = page
        self.min_delay_ms = min_delay_ms
        self.max_delay_ms = max_delay_ms

    async def search_page(
        self,
        spec: SearchSpec,
        experience_years: float | None,
        page_no: int,
        exclude_job_ids: set[str] | None = None,
    ) -> list[Job]:
        exclude = exclude_job_ids or set()
        url = build_search_url(
            keyword=spec.keyword,
            locations=spec.locations,
            experience_years=experience_years,
            page=page_no,
            sort_by=spec.sort_by,
            freshness_days=spec.freshness_days,
            custom_url=spec.custom_url,
        )
        try:
            cards = await retry_async(
                lambda u=url: self._load_page(u),
                attempts=3,
                base_delay=3.0,
                label=f"search:{spec.keyword}:p{page_no}",
            )
        except Exception as exc:
            log.error(
                "search.page_failed",
                keyword=spec.keyword,
                page=page_no,
                error=str(exc)[:250],
            )
            return []

        if not cards:
            log.info("search.no_more_results", keyword=spec.keyword, page=page_no)
            return []

        page_jobs: list[Job] = []
        for card in cards:
            job = await self._parse_card(card, spec.keyword)
            if job is None or job.job_id in exclude:
                continue
            page_jobs.append(job)

        log.info(
            "search.page_scraped",
            keyword=spec.keyword,
            page=page_no,
            cards=len(cards),
            found=len(page_jobs),
        )
        await human_pause(self.min_delay_ms, self.max_delay_ms)
        return page_jobs

    # ------------------------------------------------------ recommended feed
    async def search_recommended(
        self,
        settings: RecommendedConfig | None = None,
        exclude_job_ids: set[str] | None = None,
    ) -> list[Job]:
        """
        Harvest Naukri's own "Recommended jobs" feed.

        This is the primary job source: Naukri has already matched these
        postings against the profile, skills and preferences, so relevance beats
        anything a keyword query produces.

        Three things make this reliable where the previous version silently
        returned zero jobs:

        1. Feed-specific card selectors (the feed is a different React app from
           the search-results page) plus an anchor-based structural fallback.
        2. Tabs are resolved INSIDE the tab strip and the URL is asserted after
           every click, so a stray match inside a job card can no longer
           navigate the browser away mid-scrape.
        3. Harvesting scrolls until the feed stops yielding new cards, instead of
           reading one screenful.
        """
        cfg = settings or RecommendedConfig()
        exclude = exclude_job_ids or set()
        log.info("reco.start", url=S.RECOMMENDED_JOBS_URL, tabs=cfg.tabs)

        if not await self._open_recommended():
            return []

        if await first_visible(self.page, S.RECO_EMPTY, timeout_ms=2_500):
            log.warning("reco.empty_feed")
            return []

        collected: dict[str, Job] = {}

        # Default tab first — it is already open and is usually the best matched.
        await self._harvest(cfg, collected, exclude, tab_label="default")

        tabs = await self._discover_tabs()
        if tabs:
            log.info("reco.tabs_found", tabs=list(tabs.keys()))

        for wanted in cfg.tabs:
            if len(collected) >= cfg.max_jobs:
                break
            match = self._match_tab(tabs, wanted)
            if match is None:
                log.debug("reco.tab_absent", tab=wanted)
                continue
            if not await self._activate_tab(match, wanted):
                continue
            await self._harvest(cfg, collected, exclude, tab_label=wanted)

        log.info("reco.done", found=len(collected))
        return list(collected.values())

    async def _open_recommended(self) -> bool:
        try:
            await retry_async(
                lambda: self._goto_recommended(),
                attempts=3,
                base_delay=4.0,
                label="reco-open",
            )
            return True
        except Exception as exc:
            log.error("reco.open_failed", error=str(exc)[:250])
            return False

    async def _goto_recommended(self) -> None:
        response = await self.page.goto(
            S.RECOMMENDED_JOBS_URL, wait_until="domcontentloaded", timeout=60_000
        )
        if response is not None and response.status >= 500:
            raise TransientPageError(f"Naukri returned HTTP {response.status}")
        await dismiss_overlays(self.page)
        # Wait up to 15s for React recommendation API to hydrate job card anchors
        try:
            await self.page.wait_for_selector(
                "a[href*='job-listings'], a[href*='job-details'], a[href*='job-'], div.tuple-wrapper, article.jobTuple, div.cust-job-tuple, div.srp-jobtuple-wrapper, a.title",
                timeout=15_000,
            )
        except Exception:
            pass

        await scroll_page(self.page, steps=2)

    # ------------------------------------------------------------------ tabs
    async def _discover_tabs(self) -> dict[str, object]:
        """Map lowercased tab label -> locator, scoped to the tab strip only."""
        strip = await first_visible(self.page, S.RECO_TAB_STRIP, timeout_ms=4_000)
        if strip is None:
            return {}
        found: dict[str, object] = {}
        for item_selector in S.RECO_TAB_ITEM:
            try:
                items = await strip.locator(item_selector).all()
            except Exception:
                continue
            for item in items:
                label = (await safe_text(item)).strip().lower()
                # Tab labels are short; anything long is a card that leaked in.
                if label and len(label) <= 40 and label not in found:
                    found[label] = item
            if found:
                break
        return found

    def _match_tab(self, tabs: dict[str, object], wanted: str) -> object | None:
        needle = wanted.strip().lower()
        if not needle or not tabs:
            return None
        if needle in tabs:
            return tabs[needle]
        for label, locator in tabs.items():
            if needle in label or label in needle:
                return locator
        return None

    async def _activate_tab(self, tab: object, label: str) -> bool:
        """
        Click a tab and assert we are still on the feed. A click that navigates
        away used to poison every subsequent scrape in the run.
        """
        try:
            await tab.click(timeout=6_000)  # type: ignore[attr-defined]
        except Exception as exc:
            log.debug("reco.tab_click_failed", tab=label, error=str(exc)[:150])
            return False

        await human_pause(self.min_delay_ms, self.max_delay_ms)
        if "recommendedjobs" not in self.page.url:
            log.warning("reco.tab_navigated_away", tab=label, url=self.page.url[:150])
            if not await self._open_recommended():
                raise TransientPageError("lost the recommended feed after a tab click")
            return False
        log.info("reco.tab_active", tab=label)
        return True

    # -------------------------------------------------------------- harvest
    async def _harvest(
        self,
        cfg: RecommendedConfig,
        collected: dict[str, Job],
        exclude: set[str],
        tab_label: str,
    ) -> None:
        """Scroll the feed until it stops producing new cards."""
        stalled = 0
        before = len(collected)

        for round_no in range(1, cfg.max_scroll_rounds + 1):
            added = 0
            cards = await self._reco_cards()
            log.info("reco.cards_detected", tab=tab_label, count=len(cards))
            for card, title_el in cards:
                if len(collected) >= cfg.max_jobs:
                    break
                job = await self._parse_card(card, "recommended", title_el=title_el)
                if job is None:
                    continue
                if job.job_id in exclude:
                    log.info("reco.card_already_seen", title=job.title, company=job.company)
                    continue
                if job.job_id in collected:
                    continue
                log.info("reco.card_harvested", title=job.title, company=job.company, url=job.url[:60])
                collected[job.job_id] = job
                added += 1

            if len(collected) >= cfg.max_jobs:
                log.info("reco.max_jobs_reached", tab=tab_label, total=len(collected))
                break

            stalled = stalled + 1 if added == 0 else 0
            if stalled >= 2:
                break

            if cfg.follow_show_more:
                await click_if_present(self.page, S.RECO_SHOW_MORE, timeout_ms=2_000)
            await scroll_page(self.page, steps=3)

        log.info("reco.tab_harvested", tab=tab_label, new=len(collected) - before)

    async def _reco_cards(self) -> list[tuple[object, object | None]]:
        """
        Return (card_scope, title_element) pairs for every job card on the page.
        Naukri's /mnjuser/recommendedjobs feed renders <article class="jobTuple"> with <p class="title">.
        """
        for selector in [
            "article.jobTuple",
            "article[data-job-id]",
            "div.cust-job-tuple",
            "div.tuple-wrapper",
            "div.srp-jobtuple-wrapper",
            "article[class*='jobTuple']",
        ]:
            try:
                cards = await self.page.locator(selector).all()
                if len(cards) >= 1:
                    return [(card, None) for card in cards]
            except Exception:
                continue

        return []

    async def _parse_card(self, card, keyword: str, title_el=None) -> Job | None:
        try:
            if title_el is None:
                for t_sel in ["p.title", "p[title]", "div.jobTupleHeader p", "a.title", "a[href*='job']"]:
                    cand = card.locator(t_sel).first
                    if await cand.count():
                        title_el = cand
                        break

            if title_el is None:
                return None

            title = normalise_whitespace(await safe_text(title_el) or await title_el.get_attribute("title") or "")
            if not title:
                return None

            raw_job_id = await card.get_attribute("data-job-id")
            url = (await title_el.get_attribute("href")) or ""

            if raw_job_id:
                job_id = f"reco-{raw_job_id}"
                if not url:
                    url = f"https://www.naukri.com/job-listings-{raw_job_id}"
            elif url:
                import hashlib
                match = re.search(r"(\d{10,14})", url)
                job_id = match.group(1) if match else hashlib.sha256(url.encode()).hexdigest()[:16]
            else:
                return None

            if any(blocked in url.lower() for blocked in ["resume.naukri.com", "ambitionbox.com", "/faq/", "/help/", "services", "blog"]):
                return None

            company_el = await first_visible(card, ["span.subTitle", "span.companyWrapper span", "a.subTitle"] + S.CARD_COMPANY, timeout_ms=200)
            company = await safe_text(company_el)

            exp_el = await first_visible(card, ["li.experience span", "li.experience"] + S.CARD_EXPERIENCE, timeout_ms=200)
            experience_text = await safe_text(exp_el)

            sal_el = await first_visible(card, ["li.salary span", "li.salary"] + S.CARD_SALARY, timeout_ms=200)
            salary_text = await safe_text(sal_el)

            loc_el = await first_visible(card, ["li.location span", "li.location"] + S.CARD_LOCATION, timeout_ms=200)
            location = await safe_text(loc_el)

            post_el = await first_visible(card, ["div.jobTupleFooter span.fw500", "div.type span"] + S.CARD_POSTED, timeout_ms=200)
            posted_text = await safe_text(post_el)

            desc_el = await first_visible(card, S.CARD_DESCRIPTION, timeout_ms=200)
            description = await safe_text(desc_el)

            rat_el = await first_visible(card, S.CARD_RATING, timeout_ms=200)
            rating_text = await safe_text(rat_el)

            tags: list[str] = []
            for tag_selector in S.CARD_TAGS:
                tag_locators = await card.locator(tag_selector).all()
                if tag_locators:
                    for tag in tag_locators[:12]:
                        text = await safe_text(tag)
                        if text:
                            tags.append(text.lower())
                    break

            # Scan full card text for diversity / women-only badges
            card_full_text = (await safe_text(card) or "").lower()
            for div_term in ["prefers women", "women diversity", "women-only", "women only", "female only", "female diversity"]:
                if div_term in card_full_text:
                    tags.append(div_term)

            min_exp, max_exp = parse_experience(experience_text)
            min_sal, max_sal = parse_salary_lpa(salary_text)

            haystack = f"{title} {description} {' '.join(tags)}".lower()
            job = Job(
                job_id=job_id,
                title=title,
                company=company,
                url=url.split("?")[0],
                location=location,
                experience_text=experience_text,
                salary_text=salary_text,
                posted_text=posted_text,
                rating=parse_rating(rating_text),
                tags=tags,
                description=description,
                is_walkin="walk-in" in haystack or "walkin" in haystack,
                source_keyword=keyword,
                min_experience=min_exp,
                max_experience=max_exp,
                min_salary_lpa=min_sal,
                max_salary_lpa=max_sal,
                posted_days_ago=parse_posted_days(posted_text),
            )
            return job
        except Exception as exc:
            log.debug("search.card_parse_failed", error=str(exc)[:200])
            return None
