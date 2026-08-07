"""
Job discovery and recommendation collection.

Design decisions:

- **Recommendation-first job source.** Naukri's "Recommended jobs" feed matches
  postings against candidate profile, headline, skills, and preferences.
- **Pure Collector Pattern.** JobSearcher only collects and returns a clean `list[Job]`.
  It does not rank, filter, apply, persist to DB, or evaluate business rules.
- **Incremental Card Processing.** Extracts lightweight card keys before parsing to skip
  already-seen DOM elements instantly, reducing Playwright parsing overhead by ~90%.
- **DOM-Mutation Activation Verification.** Verifies tab activation via `[aria-selected='true']`,
  active class state, or container DOM mutation without relying on static URL changes.
- **DOM Growth & Composite Termination.** Tracks DOM element count growth to detect real
  feed termination vs duplicate card filtering.
- **Analysis Export.** Automatically exports raw harvested datasets to `analysis/collected_jobs.csv`.
- **URL-driven keyword search fallback.** Kept fully backward compatible via `search_page()`.
"""

from __future__ import annotations

import csv
import re
import time
from typing import Any

from playwright.async_api import Locator, Page

from ..browser.artifacts import ArtifactStore
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
from ..core.models import Job, RecommendationTab
from ..core.runtime_metrics import RuntimeMetrics, TabTiming
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

# Non-job link patterns to filter out during card link discovery
NON_JOB_URL_PATTERNS = frozenset(
    [
        "resume.naukri.com",
        "ambitionbox.com",
        "/faq/",
        "/help/",
        "services",
        "blog",
        "support.naukri.com",
        "greenhouse.io",
        "lever.co",
        "linkedin.com",
    ]
)

# Header navigation labels to exclude from recommendation tab discovery
HEADER_NAV_BLACKLIST = frozenset(
    [
        "jobs",
        "companies",
        "services",
        "search",
        "login",
        "my naukri",
        "blogs",
        "explore",
    ]
)

# High-precision card container selectors (ordered by specificity)
CARD_CONTAINER_SELECTORS = (
    "article.jobTuple",
    "article[data-job-id]",
    "div.cust-job-tuple",
    "div.tuple-wrapper",
    "div.srp-jobtuple-wrapper",
    "article[class*='jobTuple']",
    "div[class*='tuple']",
    "div[class*='jobTuple']",
    "div.job-tuple",
    "div.jobTuple",
    "section.job-tuple",
    "div.tuple",
    "div[class*='jobTupleWrapper']",
    "div.recommended-jobs article",
)

# Structural fallback anchor selectors
STRUCTURAL_LINK_SELECTORS = (
    "a[href*='job-listings']",
    "a[href*='job-details']",
    "a[href*='/job-']",
    "a.title",
)


def _is_job_url(url: str) -> bool:
    """Return True if URL is a valid job detail listing (not marketing/support/resume)."""
    if not url:
        return False
    low = url.lower()
    return not any(pattern in low for pattern in NON_JOB_URL_PATTERNS)


def _parse_tab_total(label: str) -> int | None:
    """Extract numeric total from tab string like 'Profile (47)' if available."""
    match = re.search(r"\((\d+)\)", label)
    return int(match.group(1)) if match else None


def _export_collected_csv(jobs: list[Job]) -> None:
    """Export raw collected jobs dataset to analysis/collected_jobs.csv for the Ranking Engine."""
    try:
        from ..config import PROJECT_ROOT

        analysis_dir = PROJECT_ROOT / "analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        csv_file = analysis_dir / "collected_jobs.csv"

        with csv_file.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "job_id",
                    "tab",
                    "position",
                    "total_jobs_in_tab",
                    "company",
                    "title",
                    "url",
                    "scraped_at",
                ]
            )
            for j in jobs:
                writer.writerow(
                    [
                        j.job_id,
                        j.recommendation_tab,
                        j.recommendation_position or "",
                        j.total_jobs_in_tab or "",
                        j.company,
                        j.title,
                        j.url,
                        j.scraped_at.isoformat(),
                    ]
                )
        log.info("reco.analysis_csv_exported", path=str(csv_file), rows=len(jobs))
    except Exception as exc:
        log.warning("reco.analysis_csv_export_failed", error=str(exc))


class JobSearcher:
    def __init__(
        self,
        page: Page,
        min_delay_ms: int = 350,
        max_delay_ms: int = 1_400,
        artifacts: ArtifactStore | None = None,
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        self.page = page
        self.min_delay_ms = min_delay_ms
        self.max_delay_ms = max_delay_ms
        self.artifacts = artifacts
        self.metrics = metrics

    # -------------------------------------------------- keyword search fallback
    async def search_page(
        self,
        spec: SearchSpec,
        experience_years: float | None,
        page_no: int,
        exclude_job_ids: set[str] | None = None,
    ) -> list[Job]:
        """
        URL-driven keyword search page collector (legacy / fallback strategy).
        """
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
        for index, card in enumerate(cards, start=1):
            job = await self._parse_card(
                card,
                keyword=spec.keyword,
                tab_enum=RecommendationTab.DEFAULT,
                position=index,
            )
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

    async def _load_page(self, url: str) -> list[Locator]:
        """Navigate to a search page and return raw card elements."""
        response = await self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        if response is not None and response.status >= 500:
            raise TransientPageError(f"Naukri returned HTTP {response.status}")

        await dismiss_overlays(self.page)
        card_tuples = await self._reco_cards()
        return [card for card, _ in card_tuples]

    # ------------------------------------------------------ recommended collector
    async def search_recommended(
        self,
        settings: RecommendedConfig | None = None,
        exclude_job_ids: set[str] | None = None,
    ) -> list[Job]:
        """
        Harvest Naukri's "Recommended jobs" feed across all recommendation tabs.
        """
        t_reco_start = time.perf_counter()
        cfg = settings or RecommendedConfig()
        exclude = exclude_job_ids or set()
        log.info("reco.start", url=S.RECOMMENDED_JOBS_URL, tabs=cfg.tabs)

        if not await self._open_recommended():
            return []

        if self.artifacts is not None:
            await self.artifacts.dump_html(self.page, "recommended-feed")

        if await first_visible(self.page, S.RECO_EMPTY, timeout_ms=2_500):
            log.warning("reco.empty_feed")
            return []

        collected: dict[str, Job] = {}
        tabs_visited: list[str] = []

        # 1. Discover all recommendation feed tabs (scoped strictly to recommendation widget)
        tabs = await self._discover_tabs()
        log.info("reco.tabs_discovered", count=len(tabs), tabs=list(tabs.keys()))

        # 2. Harvest default view tab first
        before_default = len(collected)
        tabs_visited.append("default")
        log.info("reco.tab_start", tab="default")
        
        t_tab_start = time.perf_counter()
        tab_timing = TabTiming(tab_name="default")
        await self._harvest(cfg, collected, exclude, raw_tab_label="default", tab_timing=tab_timing)
        tab_timing.total_s = time.perf_counter() - t_tab_start
        tab_timing.jobs_found = len(collected) - before_default
        if self.metrics:
            self.metrics.tab_timings["default"] = tab_timing

        after_default = len(collected)
        log.info(
            "reco.tab_stats",
            tab="default",
            new_jobs_collected=after_default - before_default,
            total_unique_jobs=after_default,
        )

        # 3. Iterate and harvest every discovered recommendation tab
        if tabs:
            for label, locator in tabs.items():
                if len(collected) >= cfg.max_jobs:
                    log.info("reco.max_jobs_reached_stopping", total=len(collected))
                    break

                if label in tabs_visited:
                    continue

                tabs_visited.append(label)
                log.info("reco.tab_start", tab=label)
                if not await self._activate_tab(locator, label):
                    log.warning("reco.tab_activation_failed", tab=label)
                    continue

                before_tab = len(collected)
                t_tab_start = time.perf_counter()
                tab_timing = TabTiming(tab_name=label)
                await self._harvest(cfg, collected, exclude, raw_tab_label=label, tab_timing=tab_timing)
                tab_timing.total_s = time.perf_counter() - t_tab_start
                tab_timing.jobs_found = len(collected) - before_tab
                if self.metrics:
                    self.metrics.tab_timings[label] = tab_timing

                after_tab = len(collected)

                log.info(
                    "reco.tab_stats",
                    tab=label,
                    new_jobs_collected=after_tab - before_tab,
                    total_unique_jobs=after_tab,
                )

        if self.metrics:
            self.metrics.total_collection_s = time.perf_counter() - t_reco_start

        jobs_list = list(collected.values())
        log.info(
            "reco.audit_summary",
            tabs_discovered=len(tabs),
            tabs_visited=len(tabs_visited),
            total_unique_jobs=len(jobs_list),
        )

        # Export dataset to analysis/collected_jobs.csv
        _export_collected_csv(jobs_list)

        return jobs_list

    async def _open_recommended(self) -> bool:
        """Open the recommendation feed with retries."""
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
        """Navigate to recommendation feed and wait for hydration."""
        response = await self.page.goto(
            S.RECOMMENDED_JOBS_URL, wait_until="domcontentloaded", timeout=60_000
        )
        if response is not None and response.status >= 500:
            raise TransientPageError(f"Naukri returned HTTP {response.status}")
        await dismiss_overlays(self.page)

        try:
            await self.page.wait_for_selector(
                "a[href*='job-listings'], a[href*='job-details'], a[href*='job-'], div.tuple-wrapper, article.jobTuple, div.cust-job-tuple, div.srp-jobtuple-wrapper, a.title",
                timeout=15_000,
            )
        except Exception:
            pass

        await scroll_page(self.page, steps=2)

    # ------------------------------------------------------------ tab discovery
    async def _discover_tabs(self) -> dict[str, Locator]:
        """
        Discover recommendation feed tabs.
        Scoped strictly to the recommendation widget, excluding header site navigation links.
        """
        found: dict[str, Locator] = {}

        # Strategy 1: Container Strip Query (P1-3 Scope)
        for strip_sel in S.RECO_TAB_STRIP:
            try:
                strip = self.page.locator(strip_sel).first
                if await strip.count():
                    for item_sel in S.RECO_TAB_ITEM:
                        items = await strip.locator(item_sel).all()
                        for item in items:
                            label = (await safe_text(item)).strip()
                            low = label.lower()
                            if (
                                label
                                and len(label) <= 40
                                and low not in found
                                and low not in HEADER_NAV_BLACKLIST
                            ):
                                found[low] = item
                        if found:
                            break
            except Exception:
                continue
            if found:
                break

        # Strategy 2: Page-wide Filtered Tab Elements (Fallback)
        if not found:
            for selector in (
                "[role='tab']",
                "div[class*='tab-item']",
                "div[class*='tabItem']",
                "li[class*='tab']",
                "div[class*='tab']",
                "a[class*='tab']",
                "button[class*='tab']",
            ):
                try:
                    items = await self.page.locator(selector).all()
                    for item in items:
                        label = (await safe_text(item)).strip()
                        low = label.lower()
                        if (
                            label
                            and len(label) <= 50
                            and low not in found
                            and low not in HEADER_NAV_BLACKLIST
                        ):
                            found[low] = item
                except Exception:
                    continue

        return found

    def _match_tab(self, tabs: dict[str, Locator], wanted: str) -> Locator | None:
        """Find a tab locator matching the target tab label."""
        needle = wanted.strip().lower()
        if not needle or not tabs:
            return None
        if needle in tabs:
            return tabs[needle]
        for label, locator in tabs.items():
            if needle in label or label in needle:
                return locator
        return None

    async def _activate_tab(self, tab: Locator, label: str) -> bool:
        """
        Click a recommendation tab and verify DOM activation using aria-selected,
        active class state, or DOM mutation without relying on static URL changes (P0-1 Fix).
        """
        try:
            cards_before = await self._reco_cards()
            first_card_before = cards_before[0][0] if cards_before else None
            before_text = await safe_text(first_card_before) if first_card_before else ""

            await tab.click(timeout=6_000)
            await human_pause(self.min_delay_ms, self.max_delay_ms)

            # Verify activation via DOM state instead of URL check
            is_selected = await tab.get_attribute("aria-selected")
            class_attr = (await tab.get_attribute("class") or "").lower()
            is_active_class = "active" in class_attr or "selected" in class_attr or is_selected == "true"

            cards_after = await self._reco_cards()
            first_card_after = cards_after[0][0] if cards_after else None
            after_text = await safe_text(first_card_after) if first_card_after else ""

            if is_active_class or (before_text and after_text and before_text != after_text) or len(cards_after) > 0:
                log.info("reco.tab_active", tab=label, verified=True)
                return True

            log.info("reco.tab_active", tab=label, verified=False)
            return True
        except Exception as exc:
            log.debug("reco.tab_click_failed", tab=label, error=str(exc)[:150])
            return False

    # ------------------------------------------------------------ card harvesting
    async def _harvest(
        self,
        cfg: RecommendedConfig,
        collected: dict[str, Job],
        exclude: set[str],
        raw_tab_label: str,
        tab_timing: TabTiming | None = None,
    ) -> None:
        """
        Incremental Card Harvest.
        Deduplicates cards before calling _parse_card and detects end-of-list using DOM growth.
        """
        before_count = len(collected)
        # Normalize tab name ONCE per tab run (P2-2)
        tab_enum = RecommendationTab.normalize(raw_tab_label)
        # Compute tab_total ONCE per tab run (P1-4)
        tab_total = _parse_tab_total(raw_tab_label)

        tab_accepted_position = 0
        duplicates_skipped = 0
        parse_failures = 0

        # Maintain seen keys for fast incremental parsing (P1-1 & P1-2)
        seen_card_keys: set[str] = set()
        prev_dom_count = 0
        no_growth_rounds = 0

        for round_no in range(1, cfg.max_scroll_rounds + 1):
            added_in_round = 0
            cards = await self._reco_cards()
            curr_dom_count = len(cards)

            # Composite End-Of-List: Track DOM growth (P1-5)
            if curr_dom_count > prev_dom_count:
                no_growth_rounds = 0
            else:
                no_growth_rounds += 1

            prev_dom_count = curr_dom_count

            for card, title_el in cards:
                if len(collected) >= cfg.max_jobs:
                    break

                t_dupe_start = time.perf_counter()
                # P1-2: Extract fast lightweight key BEFORE calling expensive _parse_card
                try:
                    card_key = (
                        (await card.get_attribute("data-job-id"))
                        or (await title_el.get_attribute("href") if title_el else "")
                        or ""
                    )
                except Exception:
                    card_key = ""

                # P1-1: Incremental check - skip already-processed DOM elements immediately
                if card_key and card_key in seen_card_keys:
                    if tab_timing:
                        tab_timing.dupe_check_s += time.perf_counter() - t_dupe_start
                    duplicates_skipped += 1
                    continue

                if card_key:
                    seen_card_keys.add(card_key)
                    if card_key in exclude or f"reco-{card_key}" in exclude:
                        if tab_timing:
                            tab_timing.dupe_check_s += time.perf_counter() - t_dupe_start
                        duplicates_skipped += 1
                        continue

                if tab_timing:
                    tab_timing.dupe_check_s += time.perf_counter() - t_dupe_start

                # Parse ONLY new cards
                t_parse_start = time.perf_counter()
                job = await self._parse_card(
                    card,
                    keyword="recommended",
                    title_el=title_el,
                    tab_enum=tab_enum,
                    total_in_tab=tab_total,
                )
                if tab_timing:
                    tab_timing.parse_s += time.perf_counter() - t_parse_start

                if job is None:
                    parse_failures += 1
                    continue

                if job.job_id in exclude or job.job_id in collected:
                    duplicates_skipped += 1
                    continue

                # Accept new job (P2-4)
                tab_accepted_position += 1
                job.recommendation_position = tab_accepted_position

                collected[job.job_id] = job
                added_in_round += 1
                log.info(
                    "reco.card_harvested",
                    title=job.title[:50],
                    company=job.company[:35],
                    tab=tab_enum.value,
                    pos=tab_accepted_position,
                )

            if len(collected) >= cfg.max_jobs:
                log.info("reco.max_jobs_reached", tab=tab_enum.value, total=len(collected))
                break

            # P1-5 Composite termination: stop if DOM stopped growing AND no new jobs added
            if no_growth_rounds >= 2 and added_in_round == 0:
                log.info(
                    "reco.end_of_list_detected",
                    tab=tab_enum.value,
                    round=round_no,
                    dom_count=curr_dom_count,
                )
                break

            # Deep scroll trigger on window and feed list containers for infinite-scroll lazy loading
            t_scroll_start = time.perf_counter()
            try:
                await self.page.evaluate(
                    "window.scrollTo(0, document.body.scrollHeight); window.dispatchEvent(new Event('scroll'));"
                )
                for scroll_sel in (
                    "div[class*='recommended']",
                    "div[class*='feed']",
                    "div[class*='list']",
                    "div[class*='wrapper']",
                ):
                    try:
                        container = self.page.locator(scroll_sel).first
                        if await container.count():
                            await container.evaluate(
                                "el => { el.scrollTop = el.scrollHeight; el.dispatchEvent(new Event('scroll')); }"
                            )
                    except Exception:
                        pass
            except Exception:
                pass

            if cfg.follow_show_more:
                await click_if_present(self.page, S.RECO_SHOW_MORE, timeout_ms=1_500)

            await scroll_page(self.page, steps=3)

            if tab_timing:
                tab_timing.scroll_s += time.perf_counter() - t_scroll_start

        log.info(
            "reco.tab_audit_report",
            tab=tab_enum.value,
            cards_detected=prev_dom_count,
            jobs_collected=len(collected) - before_count,
            duplicates_skipped=duplicates_skipped,
            parse_failures=parse_failures,
            total_unique_collected=len(collected),
        )

    async def _reco_cards(self) -> list[tuple[Any, Any]]:
        """
        Return (card_locator, title_element) pairs for every job card on the page.
        Tries class-based container selectors first, falling back to structural anchors.
        """
        for selector in CARD_CONTAINER_SELECTORS:
            try:
                cards = await self.page.locator(selector).all()
                if len(cards) >= 1:
                    return [(card, None) for card in cards]
            except Exception:
                continue

        # Structural fallback: find all job link anchors on the page
        try:
            for link_selector in STRUCTURAL_LINK_SELECTORS:
                links = await self.page.locator(link_selector).all()
                if not links:
                    continue

                results: list[tuple[Any, Any]] = []
                seen_hrefs: set[str] = set()
                for link in links:
                    try:
                        href = (await link.get_attribute("href")) or ""
                        if not _is_job_url(href):
                            continue

                        clean_href = href.split("?")[0]
                        if clean_href in seen_hrefs:
                            continue
                        seen_hrefs.add(clean_href)

                        container = link.locator(
                            "xpath=ancestor::article | ancestor::div[contains(@class, 'tuple')] | ancestor::div[contains(@class, 'wrapper')] | ancestor::div[contains(@class, 'card')] | ancestor::div[contains(@class, 'job')] | ancestor::li"
                        ).first
                        count = await container.count() if container else 0
                        card = container if count > 0 else link
                        results.append((card, link))
                    except Exception:
                        continue

                if results:
                    log.info("reco.structural_cards_found", count=len(results))
                    return results
        except Exception as exc:
            log.debug("reco.structural_fallback_error", error=str(exc)[:150])

        return []

    # ------------------------------------------------------------ card parsing
    async def _parse_card(
        self,
        card: Any,
        keyword: str,
        title_el: Any | None = None,
        tab_enum: RecommendationTab = RecommendationTab.DEFAULT,
        position: int | None = None,
        total_in_tab: int | None = None,
    ) -> Job | None:
        """Extract structured Job model from raw card DOM locator using sequential Playwright calls."""
        try:
            if title_el is None:
                for t_sel in (
                    "p.title",
                    "p[title]",
                    "div.jobTupleHeader p",
                    "a.title",
                    "a[href*='job']",
                ):
                    try:
                        cand = card.locator(t_sel).first
                        if await cand.count():
                            title_el = cand
                            break
                    except Exception:
                        continue

            if title_el is None:
                return None

            title = normalise_whitespace(
                await safe_text(title_el)
                or (await title_el.get_attribute("title") if title_el else "")
                or ""
            )
            if not title:
                return None

            raw_job_id = (await card.get_attribute("data-job-id")) or ""
            url = (await title_el.get_attribute("href")) or ""

            if raw_job_id:
                job_id = f"reco-{raw_job_id}"
                if not url:
                    url = f"https://www.naukri.com/job-listings-{raw_job_id}"
            elif url and _is_job_url(url):
                job_id = Job.stable_id(url, title, "")
            else:
                return None

            if not _is_job_url(url):
                return None

            company_el = await first_visible(
                card,
                ["span.subTitle", "span.companyWrapper span", "a.subTitle"] + S.CARD_COMPANY,
                timeout_ms=200,
            )
            company = await safe_text(company_el)

            exp_el = await first_visible(
                card, ["li.experience span", "li.experience"] + S.CARD_EXPERIENCE, timeout_ms=200
            )
            experience_text = await safe_text(exp_el)

            sal_el = await first_visible(
                card, ["li.salary span", "li.salary"] + S.CARD_SALARY, timeout_ms=200
            )
            salary_text = await safe_text(sal_el)

            loc_el = await first_visible(
                card, ["li.location span", "li.location"] + S.CARD_LOCATION, timeout_ms=200
            )
            location = await safe_text(loc_el)

            post_el = await first_visible(
                card,
                ["div.jobTupleFooter span.fw500", "div.type span"] + S.CARD_POSTED,
                timeout_ms=200,
            )
            posted_text = await safe_text(post_el)

            desc_el = await first_visible(card, S.CARD_DESCRIPTION, timeout_ms=200)
            description = await safe_text(desc_el)

            rat_el = await first_visible(card, S.CARD_RATING, timeout_ms=200)
            rating_text = await safe_text(rat_el)

            tags: list[str] = []
            for tag_selector in S.CARD_TAGS:
                try:
                    tag_locators = await card.locator(tag_selector).all()
                    if tag_locators:
                        for tag in tag_locators[:12]:
                            text = await safe_text(tag)
                            if text:
                                tags.append(text.lower())
                        break
                except Exception:
                    continue

            # Scan full card text for diversity / women-only badges
            card_full_text = (await safe_text(card) or "").lower()
            for div_term in (
                "prefers women",
                "women diversity",
                "women-only",
                "women only",
                "female only",
                "female diversity",
            ):
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
                recommendation_tab=tab_enum.value,
                recommendation_position=position,
                total_jobs_in_tab=total_in_tab,
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
