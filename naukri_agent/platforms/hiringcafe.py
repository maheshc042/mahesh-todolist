"""
HiringCafe platform (read-only source for the Sidekick handoff).

Design decisions:
- No login exists on HiringCafe: `ensure_logged_in()` only establishes
  Cloudflare clearance + cookies and returns True. There is no session to
  persist and nothing to pause on.
- Search terms come from the profile's `title_keywords` (personalized per
  account: Full-Stack terms on secondary, AI/Python terms on primary), not a
  hardcoded list — new roles flow from config.yaml with zero code changes.
- Fetch is SSR navigation, NOT the `/api/search-jobs` XHR: run 465 proved
  Cloudflare 403s the API path in the automated browser while page loads
  pass. Search state rides in the URL (`/classic?searchState={...}&page=N`),
  job cards (`/job/...` links) come from the DOM, and the company ATS link
  is resolved from each detail page — every step a normal page load.
- Oct 2026 UI refresh: search moved from `/?searchState=` (now 403 even for
  clean clients) to `/classic?searchState=` (200 + filtered SSR). Homepage
  is now a landing page with no cards, so clearance probes `/classic`.
- `apply_to_job()` NEVER applies: every HiringCafe item is a direct company
  ATS link, so it returns EXTERNAL and the orchestrator routes it to the
  Sidekick outbox. Universal hook, no HiringCafe-specific dispatch code.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from urllib.parse import quote, urljoin

from playwright.async_api import Page

from ..browser.resilience import human_pause
from ..config import JobProfile, NaukriAccount
from ..core.models import (
    ApplicationStatus,
    ApplyOutcome,
    Job,
    SkipReason,
)
from ..core.run_policy import RunPolicy
from ..logging_setup import get_logger
from .base import BaseJobPlatform, PlatformWalledError

log = get_logger(__name__)


class HiringCafePlatform(BaseJobPlatform):
    # No login exists: a failed clearance is a transient wall, never a dead
    # credential — skip the run, don't pause future runs.
    pause_on_login_failure: bool = False

    def __init__(
        self,
        page: Page,
        account: NaukriAccount,
        artifacts: Any,
        policy: RunPolicy,
        config: Any = None,
        metrics: Any | None = None,
    ):
        super().__init__(page, account.key, policy)
        self.account = account
        self.artifacts = artifacts
        self._config = config
        self._metrics = metrics
        # Consecutive challenged navigations in this fetch (reset per run):
        # aborts fast when the wall is structural instead of per-page.
        self._walls = 0
        # Last Turnstile click: re-clicking while a validation is in flight
        # resets the widget — one click per window, then hands off polling.
        self._last_cf_click = 0.0

    @property
    def platform_name(self) -> str:
        return "hiringcafe"

    def _hc_config(self) -> Any:
        return getattr(self._config, "hiringcafe", None) if self._config else None

    @staticmethod
    def _search_terms(profile: JobProfile) -> list[str]:
        terms = [t.strip() for t in (getattr(profile, "title_keywords", []) or []) if t and t.strip()]
        return terms[:8] or ["AI Engineer", "Python Developer", "Full Stack Engineer"]

    async def _challenge_present(self) -> tuple[bool, str]:
        """True when a Cloudflare challenge is visibly intercepting the page,
        regardless of what the tab title claims."""
        try:
            title = ((await self.page.title()) or "").lower()
        except Exception:
            title = ""
        for marker in ("just a moment", "verifying you are human", "attention required"):
            if marker in title:
                return True, f"title: {title[:60]}"
        try:
            widget = self.page.locator(
                "iframe[src*='challenges.cloudflare.com'], "
                "div.cf-turnstile, #cf-stage, div#cf-challenge-running"
            ).first
            if await widget.count() > 0 and await widget.is_visible():
                return True, "turnstile-widget-visible"
        except Exception:
            pass
        return False, ""

    async def _has_clearance(self) -> tuple[bool, str]:
        """(True, title) when on HiringCafe past any Cloudflare challenge.

        NOTE: hiring.cafe 301-redirects to hiringcafe.com — both are the app.
        Clearance = app domain AND no challenge indicators. Title-substring
        matching alone caused both failure modes seen live: a still-loading
        challenge tab titled 'Loading https://hiringcafe.com/' passed (run
        453: eight straight API 403s), and a solved page under an unexpected
        title would fail (user sent back to step 1 after solving, run 457).
        """
        try:
            url = (self.page.url or "").lower()
            if "hiring.cafe" not in url and "hiringcafe.com" not in url:
                return False, f"off-domain: {(self.page.url or '')[:80]}"
            challenged, why = await self._challenge_present()
            if challenged:
                return False, f"challenged: {why}"
            try:
                title = (await self.page.title()) or ""
            except Exception:
                title = ""
            return True, (title[:80] or "app-page")
        except Exception as exc:
            return False, f"check-failed: {exc}"[:100]

    async def _click_turnstile_checkbox(self) -> bool:
        """Click a Turnstile checkbox, in its iframe or inline in the page.

        Waits briefly for the iframe to attach first (it renders seconds
        after the challenge page). Cooldown-guarded: a click starts a
        server-side validation that takes seconds — clicking again (or
        reloading) mid-flight resets it, which is exactly the
        solve-reload-challenge loop. Bounded helper — never raises, never
        waits long. Returns True when a click was actually performed.
        """
        if time.monotonic() - self._last_cf_click < 10.0:
            return False
        try:
            try:
                await self.page.frame_locator(
                    "iframe[src*='challenges.cloudflare.com']"
                ).first.wait_for(state="attached", timeout=3000)
            except Exception:
                pass
            for frame in self.page.frames:
                url = (frame.url or "").lower()
                if "challenges.cloudflare.com" not in url and "turnstile" not in url:
                    continue
                box = frame.locator(
                    "input[type='checkbox'], #cf-stage label, .ctp-checkbox-label, label.ctp-checkbox-label"
                ).first
                try:
                    if await box.is_visible():
                        await box.click(timeout=2000)
                        self._last_cf_click = time.monotonic()
                        return True
                except Exception:
                    continue
            # Inline (non-iframe) widget, as seen on hiringcafe.com: a bare
            # checkbox beside "Verify you are human" text. Guarded to
            # challenge pages only — a bare checkbox click elsewhere could
            # toggle an unrelated opt-in (job alerts, filters).
            challenged, _ = await self._challenge_present()
            if not challenged:
                return False
            inline = self.page.locator(
                "div.cf-turnstile input[type='checkbox'], "
                "label:has-text('Verify you are human') input[type='checkbox'], "
                "input[type='checkbox']"
            ).first
            try:
                if await inline.count() > 0 and await inline.is_visible():
                    await inline.click(timeout=2000)
                    self._last_cf_click = time.monotonic()
                    return True
            except Exception:
                pass
        except Exception:
            pass
        return False

    @staticmethod
    def _search_path(base: str) -> str:
        """Listing path carrying the query (pure). Oct 2026 UI: `/classic`."""
        return f"{base.rstrip('/')}/classic"

    @staticmethod
    def _build_search_url(base: str, term: str, max_days: int, max_exp: int, page: int = 0) -> str:
        """Search URL carrying the whole query in `searchState` (pure).

        Mirrors the site's own share-link shape on `/classic`: the listing is
        server-rendered, so a plain navigation returns jobs with no API call.
        """
        state = {
            "searchQuery": term,
            "dateFetchedPastNDays": max_days,
            "roleYoeRange": [0, max_exp],
            "departments": [
                "Engineering", "Software Development", "Information Technology",
                "Data and Analytics", "Quality Assurance",
            ],
            "workplaceTypes": ["Remote", "Hybrid", "Onsite"],
            "sortBy": "default",
        }
        url = f"{HiringCafePlatform._search_path(base)}?searchState={quote(json.dumps(state), safe='')}"
        if page:
            url += f"&page={page}"
        return url

    @staticmethod
    def _select_apply_url(candidates: list[dict[str, str]]) -> str | None:
        """Pick the company ATS link out of a detail page's external anchors (pure).

        Skips screening forms (Sidekick 422-dead-letters them) and the site's
        own/social links. Prefers apply-flavoured anchors, else the first
        surviving external link. Returns None when nothing qualifies — the
        caller skips the job instead of enqueueing junk.
        """
        import re

        dead_substrings = (
            "hiring.cafe", "hiringcafe.com", "forms.gle", "docs.google.com/forms",
            "typeform.com", "facebook.com", "linkedin.com", "reddit.com",
            "twitter.com", "x.com", "googleusercontent.com",
        )
        apply_re = re.compile(r"apply|job posting|apply now|careers|view job", re.IGNORECASE)
        fallback: str | None = None
        for cand in candidates:
            href = (cand.get("href") or "").strip()
            if not href.lower().startswith("http"):
                continue
            lowered = href.lower()
            if any(dead in lowered for dead in dead_substrings):
                continue
            if fallback is None:
                fallback = href
            if apply_re.search(cand.get("text") or ""):
                return href
        return fallback

    @staticmethod
    def _card_prefilter_pass(card_text: str, include_terms: list[str]) -> bool:
        """Cheap title-family pre-check on raw card text (pure, fail-open).

        Detail visits are the challenge trigger (each is a fresh navigation),
        so obvious rejects (Oracle SOA, mechanical …) never cost one. Only
        skips when NO include term appears anywhere in the card — the full
        FilterEngine still judges everything that passes.
        """
        if not include_terms:
            return True
        blob = (card_text or "").lower()
        return any(t and t.lower() in blob for t in include_terms)

    async def _await_manual_solve(self, where: str, max_polls: int = 30) -> bool:
        """Headed-only: the human is watching — let them solve once instead
        of burning the term. Run 470 proved the auto-settle fails on
        `?searchState=` navigations while a solve would unblock the whole
        fetch (page loads pass; only unattended navigations wall)."""
        if not self._is_headed():
            return False
        print(
            f"\n[HiringCafe] Cloudflare challenge at {where}. "
            f"Please solve it in the browser — waiting {max_polls * 3}s…"
        )
        for _ in range(max_polls):
            await human_pause(2500, 3000)
            ok, _ = await self._has_clearance()
            if ok:
                cards_ok, _ = await self._probe_cards()
                if cards_ok:
                    log.info("hiringcafe.manual_solve_ok", where=where)
                    return True
        return False

    async def _settle_challenge(self, where: str) -> bool:
        """One bounded recovery shot on a challenged page.

        Click once, then WAIT without touching anything: Turnstile needs
        seconds for its server exchange, and both re-clicking and reloading
        mid-flight reset the widget — the old click-reload-click sequence is
        what manufactured the solve-reload-challenge loop. A single reload
        happens only if the page is still challenged after the full wait.
        True when the page looks clean afterwards.
        """
        await self._click_turnstile_checkbox()
        for _ in range(8):
            await human_pause(2000, 2500)
            ok, _ = await self._has_clearance()
            if ok:
                cards_ok, _ = await self._probe_cards()
                if cards_ok:
                    log.info("hiringcafe.challenge_settled", where=where)
                    return True
        try:
            await self.page.reload(wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            pass
        await human_pause(3000, 4000)
        ok, _ = await self._has_clearance()
        if ok:
            cards_ok, _ = await self._probe_cards()
            ok = ok and cards_ok
        log.info("hiringcafe.challenge_settled" if ok else "hiringcafe.challenge_persists", where=where)
        return ok

    @staticmethod
    def _posted_days_from_text(text: str) -> int | None:
        """Relative card timestamp ('2h', '3d') -> days ago (pure)."""
        import re

        match = re.search(r"(\d+)\s*h", text or "", re.IGNORECASE)
        if match:
            return 0
        match = re.search(r"(\d+)\s*d", text or "", re.IGNORECASE)
        if match:
            try:
                return max(0, int(match.group(1)))
            except (TypeError, ValueError):
                return None
        return None

    async def _probe_cards(self) -> tuple[bool, str]:
        """Ground-truth clearance: job cards in the DOM.

        Replaces the old XHR probe — run 465 proved the page loads fine while
        `/api/search-jobs` 403s, so cards (not API status) are the signal.
        """
        try:
            count = await self.page.locator("a[href*='/job/']").count()
        except Exception as exc:
            return False, f"probe-failed: {exc}"[:100]
        if count > 0:
            return True, f"cards-{count}"
        return False, "cards-0"

    async def ensure_logged_in(self) -> bool:
        """No login on HiringCafe: just clear Cloudflare and confirm reachability.

        The manager's stealth script already handles the webdriver property
        (skipped entirely on real Chrome, where it is natively false and any
        JS override is itself a tampering signature) — no per-page override
        here.
        """
        base = (getattr(self._hc_config(), "base_url", None) or "https://hiring.cafe").rstrip("/")
        # Oct 2026 UI: the homepage is a landing page with no job cards, so
        # clearance probes the SSR listing page instead.
        landing = self._search_path(base)
        # Loop detection: clearance achieved and then lost inside one call
        # means the solve is not sticking (IP reputation or session flagged).
        # Re-running goto in that state just burns minutes re-entering the
        # same wall — abort fast with a diagnosis instead (run 457: solved
        # repeatedly, back at step 1 every time).
        ever_cleared = False
        for attempt in (1, 2):
            try:
                await self.page.goto(landing, wait_until="domcontentloaded", timeout=30_000)
                # Cloudflare challenge can auto-clear after a few seconds:
                # poll clearance, clicking a checkbox if one is presented.
                # BOTH gates must pass: page looks clean AND job cards render
                # (SSR page, no API involved — the XHR path stays walled).
                domain_cleared = False
                for _ in range(12):
                    await human_pause(2000, 2500)
                    await self._click_turnstile_checkbox()
                    ok, detail = await self._has_clearance()
                    if ok:
                        cards_ok, cards_detail = await self._probe_cards()
                        if cards_ok:
                            ever_cleared = True
                            log.info("hiringcafe.clearance_ok", attempt=attempt, title=detail, probe=cards_detail)
                            return True
                        log.info("hiringcafe.no_cards_yet", attempt=attempt, detail=cards_detail)
                    if not ok and detail.startswith("challenged:") and not domain_cleared:
                        # Presenting a poisoned cf token re-challenges forever:
                        # drop this domain's cookies once and reload so the
                        # challenge re-issues against a clean jar. Scoped to
                        # hiringcafe domains only — Naukri/LinkedIn sessions
                        # in the same context survive untouched.
                        domain_cleared = True
                        try:
                            await self.page.context.clear_cookies(domain="hiringcafe.com")
                            await self.page.context.clear_cookies(domain="hiring.cafe")
                        except Exception:
                            pass
                        try:
                            await self.page.goto(landing, wait_until="domcontentloaded", timeout=30_000)
                        except Exception:
                            pass
                        log.info("hiringcafe.domain_cookies_dropped")
                # Interactive Turnstile never auto-clears for flagged automation.
                # Headed = human is present: let them solve once; the
                # cf_clearance cookie persists in the Postgres session and
                # future headless runs reuse it.
                ok, detail = await self._has_clearance()
                if not ok and self._is_headed():
                    log.warning("hiringcafe.manual_solve_needed")
                    print(
                        "\n[HiringCafe] Cloudflare is showing a challenge in the "
                        "browser window. Please solve it (checkbox) — waiting 120s…"
                    )
                    for _ in range(40):
                        await human_pause(2500, 3000)
                        ok, detail = await self._has_clearance()
                        if ok:
                            cards_ok, cards_detail = await self._probe_cards()
                            if cards_ok:
                                ever_cleared = True
                                log.info("hiringcafe.clearance_ok", attempt=attempt, title=detail, probe=cards_detail)
                                return True
                            log.info("hiringcafe.no_cards_yet", attempt=attempt, detail=cards_detail)
                log.warning("hiringcafe.clearance_challenged", attempt=attempt, detail=detail)
                if ever_cleared:
                    log.error(
                        "hiringcafe.clearance_loop",
                        detail="Clearance was achieved then lost: the solve is not "
                               "sticking (IP reputation or flagged session). Not "
                               "retrying into the same wall.",
                    )
                    break
            except Exception as exc:
                log.warning("hiringcafe.clearance_failed", attempt=attempt, error=str(exc)[:200])
            await human_pause(2000, 3000)
        log.error(
            "hiringcafe.clearance_blocked",
            hint="Run once headed and solve the Cloudflare challenge; "
                 "the session persists for headless runs.",
        )
        try:
            shot_path = self.artifacts.dir / "hiringcafe-challenge.png"
            await self.page.screenshot(path=str(shot_path), timeout=15000)
            log.info("hiringcafe.challenge_screenshot", path=str(shot_path))
        except Exception as exc:
            log.debug("hiringcafe.challenge_screenshot_failed", error=str(exc)[:120])
        return False

    def _is_headed(self) -> bool:
        try:
            return not bool(getattr(getattr(self._config, "browser", None), "headless", True))
        except Exception:
            return False

    _SEARCH_CARDS_JS = """() => {
        const out = []; const seen = new Set();
        for (const a of document.querySelectorAll("a[href*='/job/']")) {
            const href = a.getAttribute('href');
            if (!href || seen.has(href)) continue;
            seen.add(href);
            const card = a.closest('li, article') || a.parentElement;
            out.push({href, text: ((card ? card.innerText : a.innerText) || '').slice(0, 4000)});
        }
        return out;
    }"""

    _DETAIL_JS = """() => {
        const h1 = document.querySelector('h1');
        const org = document.querySelector("a[href^='/org/']");
        const cands = [];
        for (const a of document.querySelectorAll('a[href]')) {
            const href = a.getAttribute('href') || '';
            if (/^https?:\\/\\//i.test(href))
                cands.push({href, text: (a.innerText || '').trim().slice(0, 120)});
            if (cands.length >= 80) break;
        }
        return {
            title: ((h1 ? h1.innerText : '') || '').trim().slice(0, 200) || (document.title || '').slice(0, 200),
            company: ((org ? org.innerText : '') || '').trim().slice(0, 200),
            cands,
            body: ((document.body ? document.body.innerText : '') || '').slice(0, 12000),
        };
    }"""

    async def _collect_detail_urls(self, base: str, term: str, max_days: int, max_exp: int,
                                   max_pages: int, seen: set[str]) -> list[dict[str, str]]:
        """Paginated search navigations for one term. Returns [{url, text}].

        A challenged page gets ONE recovery shot, not an instant abort: the
        captcha re-appears between navigations, and giving up on the first
        one is exactly the run-467 stall (cards visible, zero collected).
        """
        found: list[dict[str, str]] = []
        for page_n in range(max_pages):
            url = self._build_search_url(base, term, max_days, max_exp, page_n)
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:
                log.warning("hiringcafe.search_goto_failed", term=term, page=page_n, error=str(exc)[:150])
                break
            challenged, _ = await self._challenge_present()
            if challenged and not await self._settle_challenge(f"search:{term}/{page_n}"):
                if not await self._await_manual_solve(f"search:{term}/{page_n}"):
                    log.warning("hiringcafe.search_challenged", term=term, page=page_n)
                    self._walls += 1
                    if self._walls >= 3:
                        raise PlatformWalledError(
                            "cloudflare wall on hiringcafe (challenge persists across "
                            f"{self._walls} search pages) — stopping platform"
                        )
                    break
                self._walls = 0
            try:
                await self.page.wait_for_selector("a[href*='/job/']", timeout=15_000)
            except Exception:
                log.info("hiringcafe.search_no_cards", term=term, page=page_n)
                break
            try:
                cards = await self.page.evaluate(self._SEARCH_CARDS_JS)
            except Exception as exc:
                log.warning("hiringcafe.cards_extract_failed", term=term, error=str(exc)[:150])
                break
            fresh = 0
            for card in cards or []:
                href = (card.get("href") or "")
                abs_url = urljoin(base + "/", href)
                if abs_url in seen:
                    continue
                seen.add(abs_url)
                found.append({"url": abs_url, "text": card.get("text") or ""})
                fresh += 1
            log.info("hiringcafe.search_page", term=term, page=page_n, fresh=fresh)
            if fresh == 0:
                break  # last page reached
            await human_pause(2000, 3500)
        return found

    async def _resolve_detail(self, detail_url: str, card_text: str, term: str,
                              max_days: int) -> Job | None:
        """One detail navigation -> Job with the company ATS link, or None."""
        try:
            await self.page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            log.debug("hiringcafe.detail_goto_failed", url=detail_url[:80], error=str(exc)[:120])
            return None
        challenged, _ = await self._challenge_present()
        if challenged:
            if not await self._settle_challenge(f"detail:{detail_url[:60]}"):
                if await self._await_manual_solve(f"detail:{detail_url[:60]}", max_polls=20):
                    self._walls = 0
                else:
                    log.warning("hiringcafe.detail_challenged", url=detail_url[:80])
                    self._walls += 1
                    if self._walls >= 5:
                        raise PlatformWalledError(
                            "cloudflare wall on hiringcafe (challenge persists across "
                            f"{self._walls} detail pages) — stopping platform"
                        )
                    return None
            else:
                self._walls = 0
        try:
            data = await self.page.evaluate(self._DETAIL_JS)
        except Exception as exc:
            log.debug("hiringcafe.detail_extract_failed", url=detail_url[:80], error=str(exc)[:120])
            return None
        apply_url = self._select_apply_url((data or {}).get("cands") or [])
        if not apply_url:
            log.info("hiringcafe.detail_no_apply_link", url=detail_url[:80])
            return None
        title = (data or {}).get("title") or term
        company = (data or {}).get("company") or "Company"
        job_id = f"hc-{hashlib.sha1(apply_url.encode()).hexdigest()[:20]}"
        body = (data or {}).get("body") or ""
        description = (card_text + "\n\n" + body)[:12_000] if card_text else body
        posted = self._posted_days_from_text(card_text)
        return Job(
            job_id=job_id,
            title=title,
            company=company,
            url=apply_url,
            location="",
            description=description,
            is_external=True,
            source_keyword=term,
            platform="hiringcafe",
            posted_days_ago=posted if posted is not None else max_days,
        )

    async def fetch_jobs(self, profile: JobProfile, exclude_job_ids: set[str]) -> list[Job]:
        hc = self._hc_config()
        max_days = int(getattr(hc, "max_days", 3))
        max_exp = int(getattr(hc, "max_experience", 3))
        target = int(getattr(hc, "target_jobs_per_run", 25))
        max_pages = int(getattr(hc, "max_pages", 2) or 2)
        base = (getattr(hc, "base_url", None) or "https://hiring.cafe").rstrip("/")

        seen_detail: set[str] = set()
        jobs: list[Job] = []
        seen_jobs: set[str] = set()
        no_apply_links = 0
        prefiltered = 0
        self._walls = 0
        include_terms = list((profile.filters.title_must_include_any or []) if profile.filters else [])
        for term in self._search_terms(profile):
            if len(jobs) >= target:
                break
            log.info("hiringcafe.search", term=term, max_days=max_days, max_exp=max_exp)
            cards = await self._collect_detail_urls(base, term, max_days, max_exp, max_pages, seen_detail)
            log.info("hiringcafe.batch", term=term, count=len(cards))
            for card in cards:
                if len(jobs) >= target:
                    break
                # Detail visits trigger the re-challenge loop: skip obvious
                # rejects here so only plausible cards cost a navigation.
                if not self._card_prefilter_pass(card["text"], include_terms):
                    prefiltered += 1
                    continue
                job = await self._resolve_detail(card["url"], card["text"], term, max_days)
                await human_pause(2000, 3500)
                if job is None:
                    no_apply_links += 1
                    continue
                if job.job_id in exclude_job_ids or job.job_id in seen_jobs:
                    continue
                seen_jobs.add(job.job_id)
                jobs.append(job)

        if not jobs:
            # Total wipeout: dump the page so the next diagnosis reads DOM,
            # not tea leaves (challenge vs empty-state vs markup drift).
            try:
                dump_path = self.artifacts.dir / "hiringcafe-search-empty.html"
                dump_path.write_text(await self.page.content(), encoding="utf-8")
                log.info("hiringcafe.empty_dump_saved", path=str(dump_path),
                         no_apply_links=no_apply_links, prefiltered=prefiltered,
                         walls=self._walls)
            except Exception as exc:
                log.debug("hiringcafe.empty_dump_failed", error=str(exc)[:120])
            if self._walls >= 3:
                # Cards never converted: the wall is structural, not a thin
                # term. Pause the platform instead of burning the next terms.
                raise PlatformWalledError(
                    "cloudflare wall on hiringcafe (zero jobs, "
                    f"{self._walls} challenged navigations) — stopping platform"
                )
        else:
            log.info("hiringcafe.fetch.ready", count=len(jobs),
                     no_apply_links=no_apply_links, prefiltered=prefiltered)
        return jobs

    async def apply_to_job(
        self,
        job: Job,
        profile_name: str,
        pre_submit_check: Any | None = None,
    ) -> ApplyOutcome:
        """HiringCafe items are always direct company ATS links: never apply here.

        Returns EXTERNAL so the orchestrator routes the job to the Sidekick
        outbox via the universal external-job hook.
        """
        _ = (profile_name, pre_submit_check)
        return ApplyOutcome(
            status=ApplicationStatus.EXTERNAL,
            reason=SkipReason.EXTERNAL_APPLY,
            detail="HiringCafe direct company ATS link; forwarded to Sidekick",
            external_url=job.url,
        )
