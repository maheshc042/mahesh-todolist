"""
Instahyre Platform Implementation (Pagination & Modal-Swiper Engine).
"""
from __future__ import annotations

import re
import time as _time
from collections.abc import Callable
from typing import Any

from playwright.async_api import Page

from ..browser.artifacts import ArtifactStore
from ..browser.resilience import (
    first_visible,
    human_pause,
    human_type,
    safe_text,
)
from ..config import AgentConfig, JobProfile, NaukriAccount, get_settings
from ..core.models import ApplicationStatus, ApplyOutcome, FilterDecision, Job, SkipReason
from ..core.run_policy import RunPolicy
from ..logging_setup import get_logger
from .base import BaseJobPlatform

log = get_logger(__name__)


class InstahyrePlatform(BaseJobPlatform):
    def __init__(
        self,
        page: Page,
        account: NaukriAccount,
        artifacts: ArtifactStore,
        policy: RunPolicy,
        config: AgentConfig | None = None,
        metrics: Any | None = None,
    ):
        super().__init__(page, account.key, policy)
        self.account = account
        self.artifacts = artifacts
        self.policy = policy
        self.config = config
        self._metrics = metrics
        self._current_view: str = "recommended"
        self._current_profile: JobProfile | None = None
        self.logged_out_markers = [
            "input[type='email']",
            "input[name='email']",
            "input[type='password']",
            "form[action*='login']",
        ]

    @property
    def platform_name(self) -> str:
        return "instahyre"

    async def ensure_logged_in(self) -> bool:
        self._current_view = "recommended"
        await self.page.goto(
            "https://www.instahyre.com/candidate/opportunities/?matching=true", wait_until="domcontentloaded"
        )
        await human_pause(1000, 2000)

        email_input = await first_visible(
            self.page, ["input[type='email']", "input[name='email']"], timeout_ms=3000
        )
        pass_input = await first_visible(
            self.page, ["input[type='password']", "input[name='password']"], timeout_ms=3000
        )

        authenticated = await first_visible(
            self.page,
            [
                "a[href*='/candidate/profile']",
                "a[href*='/candidate/opportunities']",
                "div.employer-row",
                "button:has-text('Logout')",
            ],
            timeout_ms=3000,
        )
        if authenticated:
            log.info("instahyre.auth.session_reused")
            return True
        if not email_input or not pass_input:
            # Check if we can wait for manual login in headed mode
            log.warning("instahyre.auth.waiting_for_manual_login", msg="Please log in to Instahyre in the open browser window if prompted...")
            try:
                authenticated = await first_visible(
                    self.page,
                    [
                        "a[href*='/candidate/profile']",
                        "a[href*='/candidate/opportunities']",
                        "div.employer-row",
                        "button:has-text('Logout')",
                        "#opportunities",
                    ],
                    timeout_ms=30000,
                )
                if authenticated:
                    log.info("instahyre.auth.manual_login_success")
                    return True
            except Exception:
                pass
            log.error("instahyre.auth.uncertain_state", url=self.page.url)
            return False

        log.info("instahyre.auth.login_start")

        settings = get_settings()
        email = (settings.instahyre_email or self.account.email).strip()
        password = (settings.instahyre_password or self.account.password).strip()

        await human_type(email_input, email)
        await human_type(pass_input, password)
        await human_pause(300, 600)

        submit = await first_visible(self.page, ["button:has-text('Login')", "button[type='submit']"])
        if submit:
            await submit.click()
            await human_pause(2000, 4000)

            authenticated = await first_visible(
                self.page,
                [
                    "a[href*='/candidate/profile']",
                    "a[href*='/candidate/opportunities']",
                    "div.employer-row",
                    "button:has-text('Logout')",
                ],
                timeout_ms=5000,
            )
            if authenticated:
                log.info("instahyre.auth.login_success")
                return True

        log.error("instahyre.auth.login_failed")
        return False


    async def _apply_ui_filters(self, profile: JobProfile) -> None:
        """Interactively opens and sets the filter UI on Instahyre opportunities page."""
        log.info("instahyre.filters.applying_ui")
        await self._dismiss_modals()


        # 1. Expand "Search other jobs" section ONLY if currently hidden
        filters_panel = self.page.locator("div.show-filters:not(.ng-hide)")
        if not await filters_panel.is_visible():
            search_heading = await first_visible(
                self.page,
                [
                    ".job-search-heading",
                    "h6:has-text('Search other jobs')",
                    "#job-search-section h6",
                ],
                timeout_ms=2000,
            )
            if search_heading:
                try:
                    await search_heading.click()
                    await human_pause(400, 800)
                except Exception:
                    pass


        # 2. Fill Skills Input (#skills-selectized) with Node.js (with dot) and Python
        skills_input = await first_visible(
            self.page,
            [
                "#skills-selectized",
                "input[placeholder*='skills' i]",
                "div.selectize-input input",
                "input[placeholder*='Java' i]",
            ],
            timeout_ms=2500,
        )

        # Target skills: Configured skill set (Python, Node.js, React.js, TypeScript, FastAPI, Next.js, Generative AI, LLMs, AWS)
        inst_cfg = getattr(self.config, "instahyre", None) if self.config else None
        target_skills = (
            inst_cfg.skills
            if inst_cfg and inst_cfg.skills
            else [
                "Python",
                "Node.js",
                "React.js",
                "TypeScript",
                "FastAPI",
                "Next.js",
                "Generative AI",
                "LLMs",
                "AWS",
            ]
        )

        if skills_input:
            # Clear any pre-existing stale skill tags
            try:
                remove_buttons = await self.page.locator("div.selectize-input div.item a.remove, div.selectize-input a.remove").all()
                for rem in remove_buttons:
                    try:
                        await rem.click()
                        await human_pause(100, 250)
                    except Exception:
                        pass
            except Exception:
                pass

            for skill in target_skills:
                try:
                    await skills_input.click()
                    await human_pause(150, 300)
                    await skills_input.fill("")
                    await human_type(skills_input, skill)
                    await human_pause(300, 500)

                    # Click matching selectize dropdown option if visible, else press Enter
                    opt_loc = self.page.locator(".selectize-dropdown .option.active, .selectize-dropdown .option").first
                    if await opt_loc.count() > 0 and await opt_loc.is_visible():
                        await opt_loc.click()
                    else:
                        await skills_input.press("Enter")
                    await human_pause(250, 450)
                except Exception as exc:
                    log.debug("instahyre.filters.skill_select_failed", skill=skill, error=str(exc))

        # 3. Fill Experience Input (#years)
        exp_input = await first_visible(
            self.page,
            [
                "input#years",
                "input[name='years']",
                "input[placeholder*='e.g. 4' i]",
            ],
            timeout_ms=2000,
        )
        if inst_cfg and inst_cfg.experience_years is not None:
            exp_val = str(int(inst_cfg.experience_years))
        elif profile.experience_years > 0:
            exp_val = str(int(profile.experience_years))
        elif profile.filters and profile.filters.experience and profile.filters.experience.max_years < 50:
            exp_val = str(int(profile.filters.experience.max_years))
        else:
            exp_val = "2"

        if exp_input:
            try:
                await exp_input.click()
                await human_pause(200, 400)
                await exp_input.fill("")
                await human_type(exp_input, exp_val)
                await human_pause(400, 800)
            except Exception as exc:
                log.debug("instahyre.filters.exp_select_failed", error=str(exc))

        # 4. Click "Show results" Button (#show-results)
        submit_filter = await first_visible(
            self.page,
            [
                "button#show-results",
                "button.show-results",
                "button:has-text('Show results')",
            ],
            timeout_ms=2500,
        )
        if submit_filter:
            try:
                await submit_filter.click()
                await human_pause(1500, 2500)
                try:
                    await self.page.wait_for_selector(
                        "div.employer-row div.company-name, div.employer-job-name",
                        timeout=12000,
                    )
                except Exception:
                    pass
                await human_pause(1000, 2000)
            except Exception:
                await self.page.keyboard.press("Enter")
                await human_pause(3000, 5000)



        # Dump search results HTML for exact inspection
        try:
            dump_path = self.artifacts.dir / "instahyre-search-results.html"
            content = await self.page.content()
            dump_path.write_text(content, encoding="utf-8")
            log.info("instahyre.search_results_html_saved", path=str(dump_path))
        except Exception as exc:
            log.debug("instahyre.search_results_dump_failed", error=str(exc))


    async def _dismiss_modals(self) -> None:
        """Dismiss popup modals (e.g. 'Are you looking for a job actively?')."""
        try:
            # 1. "Are you looking for a job actively?" Modal
            # (scoped to the modal — a bare button:has-text('Save') could hit
            # unrelated Save controls anywhere on the page)
            active_modal_btn = await first_visible(
                self.page,
                [
                    "div.candidate-active-check-modal button.btn-success",
                    "div.candidate-active-check-modal button:has-text('Save')",
                    "div.candidate-active-check-modal button:has-text('Cancel')",
                ],
                timeout_ms=1500,
            )
            if active_modal_btn:
                log.info("instahyre.modal.dismissing_active_check")
                await active_modal_btn.click()
                await human_pause(1000, 1500)

            # 2. Post-apply bulk-apply modal ("Want to apply to other similar
            #    jobs?") — its backdrop intercepts clicks on the next card if
            #    left open (verified class names in feed DOM).
            bulk_cancel = await first_visible(
                self.page,
                [
                    "div.candidate-apply-all-modal button.back-button-modal-close",
                    "div.candidate-apply-all-modal button:has-text('Cancel')",
                ],
                timeout_ms=800,
            )
            if bulk_cancel:
                log.info("instahyre.modal.dismissing_bulk_apply")
                await bulk_cancel.click()
                await human_pause(600, 1000)

            # 3. Premium / Share / WhatsApp Modals
            premium_close = await first_visible(
                self.page,
                [
                    "a:has-text('No thanks')",
                    "div.application-modal-close",
                    ".modal-close",
                ],
                timeout_ms=1000,
            )
            if premium_close:
                log.info("instahyre.modal.dismissing_premium_modal")
                await premium_close.click()
                await human_pause(800, 1200)

            # 4. Error / Toast Message Popups (#messages)
            error_close = await first_visible(
                self.page,
                [
                    "#messages button",
                    "#messages button.close",
                    "#messages div.alert button",
                    "div.toast-close-button",
                ],
                timeout_ms=800,
            )
            if error_close:
                log.info("instahyre.modal.dismissing_error_toast")
                await error_close.click()
                await human_pause(400, 800)

        except Exception as exc:
            log.debug("instahyre.dismiss_modals_error", error=str(exc))

    async def _scrape_page_cards(
        self,
        tab_name: str,
        page_num: int,
        exclude_job_ids: set[str],
        seen_ids: set[str],
        jobs: list[Job],
        max_jobs: int,
    ) -> int:
        card_selector = (
            "div.employer-row, div.opportunity-box, div.opportunity-card, "
            "div.job-card, div[class*='employer-row']"
        )
        try:
            await self.page.wait_for_selector(card_selector, timeout=8000)
        except Exception:
            pass

        cards = await self.page.locator(card_selector).all()
        if not cards:
            return 0

        added_this_page = 0
        for index, card in enumerate(cards, start=len(jobs) + 1):
            if len(jobs) >= max_jobs:
                break
            try:
                raw_name = ""
                name_loc = card.locator("div.employer-job-name div.company-name").first
                try:
                    if await name_loc.count() > 0:
                        raw_name = (await name_loc.inner_text(timeout=2000)).strip()
                except Exception:
                    pass

                if not raw_name:
                    title_attr_loc = card.locator("div.employer-job-name").first
                    try:
                        raw_name = (await title_attr_loc.get_attribute("title") or "").strip()
                    except Exception:
                        pass

                if not raw_name:
                    continue

                if " - " in raw_name:
                    parts = raw_name.split(" - ", 1)
                    company = parts[0].strip()
                    title = parts[1].strip()
                else:
                    company = raw_name
                    title = raw_name

                if not title:
                    continue

                tags: list[str] = []
                seen_tags: set[str] = set()
                for t in await card.locator("ul.tags li").all():
                    txt = (await safe_text(t)).strip()
                    if txt and not txt.startswith("+") and txt.lower() not in seen_tags:
                        seen_tags.add(txt.lower())
                        tags.append(txt)

                location = ""
                loc_node = card.locator("div.employer-locations span").first
                if await loc_node.count() > 0:
                    location = (await safe_text(loc_node)).strip()

                note_text = ""
                note_loc = card.locator("div.employer-notes").first
                if await note_loc.count() > 0:
                    note_text = (await safe_text(note_loc)).strip()

                url = "https://www.instahyre.com/candidate/opportunities/"
                # Real opportunity id from the card's AngularJS scope (same
                # `opp` object the Apply button consumes). Falls back to the
                # title+company hash only when Angular is not bootstrapped.
                opp_id = ""
                try:
                    opp_id = (await card.evaluate("""(el) => {
                        try {
                            if (!window.angular) return '';
                            let s = null;
                            try { s = window.angular.element(el).scope(); } catch (e) { return ''; }
                            for (let depth = 0; depth < 6 && s; depth++) {
                                const o = s.opp || s.selectedOpp || s.opportunity || null;
                                const v = o && (o.id || o.opportunity_id || o.job_id || o._id || o.opp_id || o.opportunityId);
                                if (v) return String(v);
                                s = s.$parent;
                            }
                            return '';
                        } catch (e) { return ''; }
                    }""") or "").strip()
                    if opp_id:
                        self._opp_ids_resolved = getattr(self, "_opp_ids_resolved", 0) + 1
                except Exception as exc:
                    log.debug("instahyre.fetch.opp_id_failed", error=str(exc))
                job_id = f"instahyre-{opp_id}" if opp_id else f"instahyre-{Job.stable_id(url, title, company)}"

                if job_id in exclude_job_ids or job_id in seen_ids:
                    continue

                seen_ids.add(job_id)
                jobs.append(
                    Job(
                        job_id=job_id,
                        title=title,
                        company=company,
                        location=location,
                        url=url,
                        description=note_text,
                        tags=tags,
                        recommendation_tab=f"{tab_name}_{page_num}",
                        recommendation_position=index,
                        platform="instahyre",
                    )
                )
                added_this_page += 1
            except Exception as exc:
                log.debug("instahyre.fetch.parse_error", error=str(exc))
                continue

        return added_this_page

    async def _paginate_and_collect(
        self,
        tab_name: str,
        max_pages: int,
        exclude_job_ids: set[str],
        seen_ids: set[str],
        jobs: list[Job],
        max_jobs: int,
    ) -> None:
        page_num = 1
        while page_num <= max_pages and len(jobs) < max_jobs:
            log.info("instahyre.fetch.scraping_page", tab=tab_name, page_num=page_num, total_jobs=len(jobs))
            added = await self._scrape_page_cards(tab_name, page_num, exclude_job_ids, seen_ids, jobs, max_jobs)
            if added == 0 and page_num > 1:
                break

            if len(jobs) >= max_jobs or page_num >= max_pages:
                break

            has_next = False
            next_li = self.page.locator("div.pagination li:has-text('Next'):not(.hidden)").first
            try:
                has_next = await next_li.count() > 0 and await next_li.is_visible()
            except Exception:
                pass

            if not has_next:
                next_num = self.page.locator(f"div.pagination li:text-is('{page_num + 1}')").first
                try:
                    has_next = await next_num.count() > 0 and await next_num.is_visible()
                    if has_next:
                        next_li = next_num
                except Exception:
                    pass

            if has_next:
                await next_li.click()
                await human_pause(1500, 2500)
                page_num += 1
            else:
                log.info("instahyre.fetch.last_page_reached", tab=tab_name, pages=page_num)
                break

        # Return to page 1
        try:
            pagination_div = self.page.locator("div.pagination").first
            if await pagination_div.count() > 0:
                await pagination_div.scroll_into_view_if_needed()
                first_page = pagination_div.locator("li").filter(has_text=re.compile(r"^\s*1\s*$")).first
                if await first_page.count() > 0:
                    await first_page.click(force=True)
                    await human_pause(1200, 2000)
                    await self.page.evaluate("window.scrollTo(0, 0)")
        except Exception as exc:
            log.debug("instahyre.fetch.return_to_p1_failed", error=str(exc))

    async def fetch_jobs(self, profile: JobProfile, exclude_job_ids: set[str]) -> list[Job]:
        log.info("instahyre.fetch.start", profile=profile.name)
        jobs: list[Job] = []
        seen_ids: set[str] = set()
        feed_url = "https://www.instahyre.com/candidate/opportunities/"
        target_applies = profile.platform_limits.get(self.platform_name, 150)
        # Scrape a generous pool (2x apply limit) directly from UI search filters
        max_scrape = max(250, int(target_applies * 2))
        self._current_profile = profile
        self._current_view = "search"

        try:
            await self.page.goto(feed_url, wait_until="domcontentloaded")
            await human_pause(2000, 3000)
            await self._dismiss_modals()

            # Directly apply targeted UI Search Filters (Node.js/Python + Exp)
            log.info(
                "instahyre.fetch.direct_search_filters_start",
                target_limit=max_scrape,
                profile=profile.name,
            )
            await self._apply_ui_filters(profile)
            self._current_view = "search"

            # Paginate through filtered search results directly
            await self._paginate_and_collect(
                tab_name="search_page",
                max_pages=10,
                exclude_job_ids=exclude_job_ids,
                seen_ids=seen_ids,
                jobs=jobs,
                max_jobs=max_scrape,
            )
            log.info("instahyre.fetch.direct_search_filters_done", total_gathered=len(jobs))

        except Exception as exc:
            log.warning("instahyre.fetch.error", url=feed_url, error=str(exc))

        log.info(
            "instahyre.fetch.done",
            count=len(jobs),
            current_view=self._current_view,
            real_opp_ids=getattr(self, "_opp_ids_resolved", 0),
        )
        return jobs

    async def _switch_to_page(self, target_page: int) -> bool:
        """Safely switch to target page in Instahyre search/reco feed using direct pagination element interaction."""
        try:
            pagination_div = self.page.locator("div.pagination").first
            if await pagination_div.count() == 0:
                return False

            # Check if current page is already target_page
            active_btn = pagination_div.locator("li.active").first
            if await active_btn.count() > 0:
                cur_text = (await safe_text(active_btn)).strip()
                if cur_text == str(target_page):
                    return True

            # Scroll pagination into view
            await pagination_div.scroll_into_view_if_needed()
            await human_pause(200, 400)

            # Locate the exact page number li
            page_btn = pagination_div.locator("li").filter(has_text=re.compile(rf"^\s*{target_page}\s*$")).first
            clicked = False
            if await page_btn.count() > 0:
                try:
                    await page_btn.click(timeout=3000)
                    clicked = True
                except Exception:
                    await page_btn.evaluate("el => el.click()")
                    clicked = True

            if not clicked:
                # If target page number is beyond current visible numbers, click Next
                next_btn = pagination_div.locator("li:has-text('Next'):not(.hidden)").first
                if await next_btn.count() > 0:
                    try:
                        await next_btn.click(timeout=3000)
                        clicked = True
                    except Exception:
                        await next_btn.evaluate("el => el.click()")
                        clicked = True

            if not clicked:
                return False

            # Wait for active page indicator to update
            active_target = pagination_div.locator("li.active").filter(has_text=re.compile(rf"^\s*{target_page}\s*$"))
            try:
                await active_target.wait_for(state="visible", timeout=5000)
            except Exception:
                await human_pause(1200, 2000)

            # Wait for cards to be rendered on the feed
            try:
                await self.page.locator("div.employer-row").first.wait_for(state="visible", timeout=4000)
            except Exception:
                pass

            await self.page.evaluate("window.scrollTo(0, 0)")
            await human_pause(400, 800)

            # Verify that the active page is indeed target_page
            now_active = pagination_div.locator("li.active").first
            if await now_active.count() > 0:
                now_text = (await safe_text(now_active)).strip()
                if now_text == str(target_page):
                    return True
        except Exception as exc:
            log.debug("instahyre.pagination.switch_failed", target_page=target_page, error=str(exc))
        return False

    async def _ensure_modal_closed(self) -> None:
        """Forces the Instahyre carousel modal to close cleanly and removes any backdrops without destroying DOM templates."""
        # 1. Native AngularJS Scope Close Trigger on employerProfileModalCtrl
        try:
            await self.page.evaluate("""() => {
                const modalCtrl = document.querySelector('[ng-controller="employerProfileModalCtrl"]');
                if (modalCtrl && window.angular) {
                    const scope = window.angular.element(modalCtrl).scope();
                    if (scope) {
                        if (typeof scope.closeApplyModal === 'function') {
                            scope.closeApplyModal();
                        }
                        if (scope.bulkParams) {
                            scope.bulkParams.showApplyModal = false;
                        }
                        scope.$evalAsync();
                    }
                }
            }""")
        except Exception:
            pass

        modal = await first_visible(
            self.page,
            ["div.modal-content", "div#employer-profile-modal", "div[class*='modal']"],
            timeout_ms=1000,
        )
        if modal is not None:
            close_btn = await first_visible(
                self.page,
                ["button.close", "span.close", "button[aria-label='Close']", ".modal-header button", ".application-modal-backdrop"],
                timeout_ms=1000,
            )
            if close_btn:
                try:
                    await close_btn.click()
                except Exception:
                    pass
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            await human_pause(300, 600)

        # Force remove ONLY promotional overlays and stray backdrops (NEVER remove .application-modal!)
        try:
            await self.page.evaluate("""() => {
                document.querySelectorAll('.modal-backdrop, #go-premium-modal, #refer, #follow-premium-modal').forEach(el => el.remove());
                document.body.classList.remove('modal-open');
            }""")
        except Exception:
            pass

    async def apply_to_job(
        self,
        job: Job,
        profile_name: str,
        pre_submit_check: Callable[[Job], FilterDecision] | None = None,
    ) -> ApplyOutcome:
        self.require_mutation("application.apply_flow")
        log.info("instahyre.apply.start", job_id=job.job_id)
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

        # 1. Check if Modal is ALREADY open (carousel swiper mode)
        modal = await first_visible(
            self.page, ["div.modal-content", "div#employer-profile-modal", "div[class*='modal']"], timeout_ms=2000
        )

        if modal:
            # Stale-modal guard: a leftover carousel from the PREVIOUS job must
            # not receive this job's Apply/decline clicks.
            modal_text = (await safe_text(modal)).lower()
            matches_job = (
                bool(job.title) and job.title.lower()[:40] in modal_text
            ) or (bool(job.company) and job.company.lower() in modal_text)
            if not matches_job:
                log.warning(
                    "instahyre.apply.stale_modal_closed",
                    job_id=job.job_id,
                    expected=f"{job.title[:40]} / {job.company}".lower(),
                )
                await self._ensure_modal_closed()
                modal = None

        if not modal:
            cur_view = getattr(self, "_current_view", "search")
            if cur_view != "search":
                log.info("instahyre.apply.ensuring_search_view", job_id=job.job_id)
                if self._current_profile:
                    await self._apply_ui_filters(self._current_profile)
                self._current_view = "search"

            tab = job.recommendation_tab or ""

            # 1. Determine target pagination page
            target_page = 1
            if tab and "_" in tab:
                try:
                    target_page = int(tab.split("_")[-1])
                except Exception:
                    target_page = 1

            pagination_div = self.page.locator("div.pagination").first
            if await pagination_div.count() > 0:
                active_page_loc = pagination_div.locator("li.active").first
                current_page_num = 1
                if await active_page_loc.count() > 0:
                    try:
                        current_page_num = int((await safe_text(active_page_loc)).strip())
                    except Exception:
                        current_page_num = 1

                if current_page_num != target_page:
                    log.info("instahyre.apply.switching_page", from_page=current_page_num, to_page=target_page)
                    switched = await self._switch_to_page(target_page)
                    if not switched:
                        log.warning("instahyre.apply.page_switch_unconfirmed", from_page=current_page_num, to_page=target_page)

            # Open modal from card — find by company or distinct title
            safe_title = job.title.replace("'", "\\'")
            safe_company = job.company.replace("'", "\\'")

            async def _find_card_on_current_view():
                job_comp_lower = (job.company or "").lower().strip()
                job_title_lower = (job.title or "").lower().strip()

                async def _is_applied_card(row_loc) -> bool:
                    try:
                        applied_marker = row_loc.locator("button:has-text('Applied'), span:has-text('Applied'), div:has-text('Applied')").first
                        return await applied_marker.count() > 0 and await applied_marker.is_visible()
                    except Exception:
                        return False

                # 1. First priority: Card containing BOTH company name and title
                if safe_company and safe_title:
                    try:
                        loc = self.page.locator(f"div.employer-row:has-text('{safe_company}'):has-text('{safe_title}')").first
                        if await loc.count() > 0 and await loc.is_visible():
                            return loc
                    except Exception:
                        pass

                all_rows = await self.page.locator("div.employer-row").all()

                # Pass 1: both company and title match in row text
                clean_comp = re.sub(r'\b(pvt|ltd|limited|private|technologies|inc|corp|labs)\b|[.\s]ai\b|[.\s]io\b', '', job_comp_lower).strip()
                if job_comp_lower and job_title_lower:
                    for row in all_rows:
                        row_text = (await safe_text(row)).lower()
                        comp_match = (job_comp_lower in row_text) or (len(clean_comp) >= 3 and clean_comp in row_text)
                        title_match = (job_title_lower in row_text or job_title_lower[:25] in row_text)
                        if comp_match and title_match:
                            return row

                # Pass 2: exact company match in row text WHERE the card is NOT already applied
                unapplied_comp_row = None
                if job_comp_lower:
                    for row in all_rows:
                        row_text = (await safe_text(row)).lower()
                        comp_match = (job_comp_lower in row_text) or (len(clean_comp) >= 3 and clean_comp in row_text)
                        if comp_match:
                            if not await _is_applied_card(row):
                                return row
                            if unapplied_comp_row is None:
                                unapplied_comp_row = row

                # Pass 3: distinctive title match (only if title is not overly generic)
                generic_titles = {
                    "software engineer", "software developer", "developer",
                    "backend developer", "frontend developer", "full stack developer",
                    "backend engineer", "frontend engineer", "full stack engineer",
                    "sde", "sde 1", "sde 2", "sde-1", "sde-2",
                }
                if job_title_lower and job_title_lower not in generic_titles and len(job_title_lower) > 5:
                    for row in all_rows:
                        row_text = (await safe_text(row)).lower()
                        if job_title_lower[:30] in row_text:
                            return row

                return unapplied_comp_row

            card = await _find_card_on_current_view()

            # Dynamic pagination scan: if not on target_page, search across pages 1 to 10
            if not card and await pagination_div.count() > 0:
                available_pages: list[int] = []
                page_items = await pagination_div.locator("li").all()
                for p_item in page_items:
                    txt = (await safe_text(p_item)).strip()
                    if txt.isdigit():
                        available_pages.append(int(txt))

                search_targets = [p for p in sorted(set(available_pages)) if p != target_page][:10]
                for search_p in search_targets:
                    log.info("instahyre.apply.searching_across_pages", page=search_p, job=f"{job.company} - {job.title}")
                    switched = await self._switch_to_page(search_p)
                    if switched:
                        card = await _find_card_on_current_view()
                        if card:
                            log.info("instahyre.apply.card_found_on_page", page=search_p, job=f"{job.company} - {job.title}")
                            break

            if not card:
                # Feed churn (filled/expired between collect and apply), not an
                # agent failure: skip without tripping the failure breaker.
                _record_timing()
                return ApplyOutcome(
                    status=ApplicationStatus.SKIPPED,
                    reason=SkipReason.STALE_JOB,
                    detail=f"Job card for {job.company} - {job.title} no longer on feed",
                )

            try:
                # Click the opportunity card / view button to trigger openApplyModal(opp)
                await card.scroll_into_view_if_needed()
                await human_pause(300, 600)

                clicked = False
                trigger = card.locator("a#employer-profile-opportunity, button#interested-btn, span#interested-btn, .button-interested, div.employer-job-name").first
                if await trigger.count() > 0 and await trigger.is_visible():
                    try:
                        await trigger.click()
                        clicked = True
                    except Exception:
                        await trigger.evaluate("node => node.click()")
                        clicked = True

                if not clicked:
                    try:
                        await card.click()
                    except Exception:
                        await card.evaluate("node => node.click()")
                await human_pause(1500, 2500)
            except Exception as exc:
                _record_timing()
                return ApplyOutcome(ApplicationStatus.FAILED, detail=f"Failed to click job card: {exc!s}")

            modal = await first_visible(
                self.page,
                [
                    "div#employer-profile-modal",
                    "div.application-modal-block",
                    "div.modal.fade.in",
                    "div.modal.in",
                    "div.modal.show",
                    "div.modal-dialog",
                    "div.modal-content",
                    "div.bar-actions",
                    "div[class*='opportunity-modal']",
                ],
                timeout_ms=5000,
            )
            if not modal:
                # Fallback 1: direct evaluate click on the link carrying ng-click="openApplyModal(opp)"
                try:
                    direct_link = card.locator("a#employer-profile-opportunity").first
                    if await direct_link.count() > 0:
                        await direct_link.evaluate("node => node.click()")
                        await human_pause(1500, 2500)
                        modal = await first_visible(
                            self.page,
                            [
                                "div#employer-profile-modal",
                                "div.application-modal-block",
                                "div.modal.fade.in",
                                "div.modal.in",
                                "div.modal.show",
                                "div.modal-content",
                                "div.bar-actions",
                            ],
                            timeout_ms=4000,
                        )
                except Exception:
                    pass

            if not modal:
                # Fallback 2: direct AngularJS evaluate invocation on employerProfileModalCtrl scope
                try:
                    opened_via_angular = await card.evaluate("""(el) => {
                        if (!window.angular) return false;
                        const cardScope = window.angular.element(el).scope();
                        const opp = cardScope ? (cardScope.opp || (cardScope.$parent && cardScope.$parent.opp)) : null;
                        const modalCtrl = document.querySelector('[ng-controller="employerProfileModalCtrl"]');
                        const modalScope = modalCtrl ? window.angular.element(modalCtrl).scope() : null;
                        if (modalScope && opp && typeof modalScope.openApplyModal === 'function') {
                            modalScope.openApplyModal(opp);
                            if (modalScope.bulkParams) {
                                modalScope.bulkParams.showApplyModal = true;
                            }
                            modalScope.$evalAsync();
                            return true;
                        }
                        return false;
                    }""")
                    if opened_via_angular:
                        await human_pause(1500, 2500)
                        modal = await first_visible(
                            self.page,
                            [
                                "div#employer-profile-modal",
                                "div.application-modal-block",
                                "div.modal.fade.in",
                                "div.modal.in",
                                "div.modal.show",
                                "div.modal-content",
                                "div.bar-actions",
                                "div[class*='opportunity-modal']",
                            ],
                            timeout_ms=5000,
                        )
                except Exception as exc:
                    log.debug("instahyre.apply.angular_open_failed", error=str(exc))

            if not modal:
                _record_timing()
                return ApplyOutcome(ApplicationStatus.FAILED, detail="Modal did not open after clicking card")

            # Wait for AngularJS candidateOpportunityCtrl to render the action bar
            bar_actions = await first_visible(
                self.page,
                [
                    "div.bar-actions",
                    "div.row.bar-actions",
                ],
                timeout_ms=5000,
            )
            if bar_actions:
                log.info("instahyre.apply.bar_actions_found", job_id=job.job_id)
            else:
                log.warning("instahyre.apply.bar_actions_missing", job_id=job.job_id)


        # Dump opened modal HTML for exact inspection
        try:
            dump_path = self.artifacts.dir / f"instahyre-modal-{job.job_id}.html"
            content = await self.page.content()
            dump_path.write_text(content, encoding="utf-8")
            log.info("instahyre.modal_html_saved", path=str(dump_path))
        except Exception as exc:
            log.debug("instahyre.modal_dump_failed", error=str(exc))

        # 1.5 Scrape Full Description from Modal for AI Analysis
        try:
            jd_loc = self.page.locator("div.opportunity-description, div.job-description, .description").first
            if await jd_loc.count() > 0:
                full_desc = (await safe_text(jd_loc)).strip()
                if full_desc:
                    job.description = full_desc
                    log.info("instahyre.apply.description_scraped", job_id=job.job_id)
        except Exception as exc:
            log.debug("instahyre.apply.description_scrape_failed", error=str(exc))


        # 2. Evaluate Pre-Submit Check inside Modal
        if pre_submit_check:
            decision = pre_submit_check(job)
            if not decision.passed:
                log.info("instahyre.apply.declining", job_id=job.job_id, reason=decision.reason)
                decline_btn = await first_visible(
                    self.page,
                    [
                        "button.decline",
                        "button.btn-default.decline",
                        "div.bar-actions button.decline",
                        "button#not-interested-btn",
                        "button.button-not-interested",
                        "button[ng-click*='pass' i]",
                        "button[ng-click*='decline' i]",
                        "button[ng-click*='not' i]",
                        "div.pass button",
                        "button:has-text('Not interested')",
                        "button:has-text('Not Interested')",
                        "a:has-text('Not Interested')",
                        "button:has-text('Skip')",
                        "button:has-text('Decline')",
                        "button.btn-danger",
                    ],
                    timeout_ms=3000,
                )
                if decline_btn:
                    try:
                        await decline_btn.click()
                        await human_pause(800, 1500)
                    except Exception:
                        pass
                _record_timing()
                return ApplyOutcome(ApplicationStatus.SKIPPED, reason=decision.reason, detail=decision.detail)

        # 3. Fast Apply Action inside Modal (No description scraping delay)
        # Check for External Apply ("Apply on company site") first
        external_btn = await first_visible(
            self.page,
            [
                "button:has-text('Apply on company site')",
                "div.apply-cancel-bar button",
                "#apply-external-modal button",
            ],
            timeout_ms=1000,
        )
        if external_btn:
            log.info("instahyre.apply.external_site_detected", job_id=job.job_id)
            _record_timing()
            return ApplyOutcome(
                ApplicationStatus.EXTERNAL,
                reason=SkipReason.EXTERNAL_APPLY,
                external_url=job.url,
                detail="Instahyre external apply requires company website application",
            )

        apply_btn = await first_visible(
            self.page,
            [
                "#candidate-suggested-employers div.apply.ng-scope > button",
                "#candidate-suggested-employers div.apply",
                "div.apply",
                "div.apply button",
                "button.btn-primary.new-btn",
                "div.bar-actions div.apply button",
                "button#btn-apply",
                "button[ng-click*='submitChoice' i]",
                "button[ng-click*='apply' i]",
                "button[ng-click*='interested' i]",
                ".apply-button",
                "button:has-text('Apply')",
                "button.btn-success:has-text('Apply')",
                "button.btn-primary:has-text('Apply')",
                "a:has-text('Apply')",
            ],
            timeout_ms=4000,
        )


        if not apply_btn:
            # Debug: Log all visible buttons on page
            all_buttons = await self.page.locator("button, a.btn, input[type='button'], input[type='submit']").all()
            button_texts = []
            for btn in all_buttons:
                txt = (await safe_text(btn)).strip()
                if txt:
                    button_texts.append(txt[:30])
            log.info("instahyre.debug_buttons", count=len(all_buttons), buttons=button_texts[:20])

            # Dump page HTML for exact inspection
            try:
                dump_path = self.artifacts.dir / f"instahyre-dump-{job.job_id}.html"
                content = await self.page.content()
                dump_path.write_text(content, encoding="utf-8")
                log.info("instahyre.html_dump_saved", path=str(dump_path))
            except Exception as exc:
                log.debug("instahyre.html_dump_failed", error=str(exc))

            if await first_visible(self.page, ["text=Applied", "button.btn-disabled:has-text('Applied')"]):
                _record_timing()
                return ApplyOutcome(ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)
            _record_timing()
            return ApplyOutcome(ApplicationStatus.FAILED, detail="Apply button not found in modal")



        # Ensure the modal action bar (Not Interested & Apply buttons) is scrolled into view
        bar = self.page.locator("div.bar-actions, div.row.bar-actions").first
        if await bar.count() > 0:
            try:
                await bar.scroll_into_view_if_needed()
                await human_pause(800, 1500)
            except Exception:
                pass

        try:
            # Click div.apply / button which carries ng-click="submitChoice(opp, true)"
            apply_target = self.page.locator("div.bar-actions div.apply, div.apply, button.btn-primary.new-btn:has-text('Apply'), button:has-text('Apply')").first
            if await apply_target.count() > 0:
                await apply_target.scroll_into_view_if_needed()
                await human_pause(300, 600)
                try:
                    await apply_target.click()
                except Exception:
                    await apply_target.evaluate("node => node.click()")
            else:
                await apply_btn.scroll_into_view_if_needed()
                await apply_btn.click(force=True)
            await human_pause(1000, 2000)
        except Exception as exc:
            _record_timing()
            return ApplyOutcome(ApplicationStatus.FAILED, detail=f"Click apply failed: {exc!s}")


        # Handle Confirmation Modal ("Are you sure you want to apply?")
        confirm_btn = await first_visible(
            self.page,
            [
                "button:has-text('Confirm')",
                "button:has-text('Yes')",
                "button.btn-primary:has-text('Apply')",
            ],
            timeout_ms=1500,
        )
        if confirm_btn:
            try:
                await confirm_btn.click()
                await human_pause(1000, 2000)
            except Exception:
                pass

        confirmation = await first_visible(
            self.page,
            [
                "text=Application sent",
                "text=Successfully applied",
                "text=You have applied",
                "button:has-text('Applied')",
                "button.btn-disabled:has-text('Applied')",
                "button[class*='applied']",
                "span[class*='applied']",
                "div[class*='applied']",
                "span.applied-badge",
                "div.apply button:has-text('Applied')",
                "div.apply[class*='disabled']",
                "div.apply[class*='applied']",
                "div#refer",
                "div#go-premium-modal",
                "div[class*='bulk-apply']",
                "button[ng-click*='applyBulk']",
                "div.alert:has-text('applied')",
                "div.alert-success",
                ".toaster",
                ".toast",
            ],
            timeout_ms=5000,
        )

        modal_closed_after_submit = False
        if not confirmation and modal:
            try:
                # If the modal closed / disappeared from view after clicking apply, it was submitted successfully
                is_modal_vis = await modal.is_visible()
                if not is_modal_vis:
                    modal_closed_after_submit = True
                else:
                    # If modal is open but displays a different job/company, carousel advanced upon submission
                    modal_title_el = modal.locator(".employer-job-name, .company-name, h3, h4").first
                    if await modal_title_el.count() > 0:
                        current_modal_text = (await safe_text(modal_title_el)).lower()
                        if job.title.lower()[:20] not in current_modal_text and (not job.company or job.company.lower() not in current_modal_text):
                            modal_closed_after_submit = True
            except Exception:
                modal_closed_after_submit = True

        if not confirmation and not modal_closed_after_submit and card:
            try:
                card_applied = card.locator("button:has-text('Applied'), span:has-text('Applied'), div:has-text('Applied'), [class*='applied']").first
                if await card_applied.count() > 0 and await card_applied.is_visible():
                    confirmation = card_applied
            except Exception:
                pass

        # Post-apply cleanup: Instahyre advances the carousel / pops the bulk
        # "apply to similar jobs" modal after an application — either can leave
        # a backdrop that eats the next card's clicks.
        await self._ensure_modal_closed()
        await self._dismiss_modals()

        if confirmation or modal_closed_after_submit:
            evidence = (
                "Instahyre application success marker observed"
                if confirmation
                else "Instahyre modal closed or advanced upon application submission"
            )
            _record_timing()
            return ApplyOutcome(
                ApplicationStatus.APPLIED,
                confirmation_type="dom_marker",
                confirmation_evidence=evidence,
            )
        _record_timing()
        return ApplyOutcome(
            ApplicationStatus.FAILED,
            detail="Submission clicked but no Instahyre success confirmation was observed",
        )
