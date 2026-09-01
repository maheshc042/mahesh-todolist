"""
Instahyre Platform Implementation (Pagination & Modal-Swiper Engine).
"""
from __future__ import annotations

import re
from typing import Callable

from playwright.async_api import Page

from ..browser.artifacts import ArtifactStore
from ..browser.resilience import (
    first_visible,
    human_pause,
    human_type,
    safe_text,
)
from ..config import JobProfile, NaukriAccount, get_settings

from ..core.models import ApplicationStatus, ApplyOutcome, FilterDecision, Job, SkipReason
from ..core.run_policy import RunPolicy
from ..logging_setup import get_logger
from .base import BaseJobPlatform

log = get_logger(__name__)


class InstahyrePlatform(BaseJobPlatform):
    def __init__(self, page: Page, account: NaukriAccount, artifacts: ArtifactStore, policy: RunPolicy):
        super().__init__(page, account.key, policy)
        self.account = account
        self.artifacts = artifacts

    @property
    def platform_name(self) -> str:
        return "instahyre"

    async def ensure_logged_in(self) -> bool:
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

        # Only target Node.js and Python for Instahyre filter search
        target_skills = ["Node.js", "Python"]

        if skills_input:
            # Clear any pre-existing stale skill tags
            try:
                remove_buttons = await self.page.locator("div.selectize-input div.item a.remove, div.selectize-input a.remove").all()
                for rem in remove_buttons:
                    try:
                        await rem.click()
                        await human_pause(100, 300)
                    except Exception:
                        pass
            except Exception:
                pass

            for skill in target_skills:
                try:
                    await skills_input.click()
                    await human_pause(200, 400)
                    await skills_input.fill("")
                    await human_type(skills_input, skill)
                    await human_pause(400, 600)
                    await skills_input.press("Enter")
                    await human_pause(400, 800)
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
        exp_val = str(int(profile.experience.max_years)) if (profile.experience and profile.experience.max_years is not None) else "3"

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

    async def fetch_jobs(self, profile: JobProfile, exclude_job_ids: set[str]) -> list[Job]:
        log.info("instahyre.fetch.start", profile=profile.name)

        jobs: list[Job] = []
        seen_ids: set[str] = set()

        feed_url = "https://www.instahyre.com/candidate/opportunities/"
        log.info("instahyre.fetch.query", url=feed_url)

        try:
            await self.page.goto(feed_url, wait_until="domcontentloaded")
            await human_pause(2000, 3000)

            # Automatically dismiss blocking popup modals
            await self._dismiss_modals()

            # Dump feed HTML for exact inspection
            try:
                dump_path = self.artifacts.dir / "instahyre-feed.html"
                content = await self.page.content()
                dump_path.write_text(content, encoding="utf-8")
                log.info("instahyre.feed_html_saved", path=str(dump_path))
            except Exception as exc:
                log.debug("instahyre.feed_html_dump_failed", error=str(exc))

            card_selector = (
                "div.employer-row, div.opportunity-box, div.opportunity-card, "
                "div.job-card, div[class*='employer-row']"
            )



            # Feed already showing opportunities? (Instahyre remembers filter
            # state in the URL/session.) The old check looked for hrefs and
            # .position-title classes that don't exist in this AngularJS DOM —
            # cards carry no hrefs at all (they use ng-click), so it matched
            # nothing and forced a re-filter on every single run.
            existing_cards = await self.page.locator("div.employer-row").count()
            if existing_cards == 0:
                log.info("instahyre.fetch.no_initial_jobs_filtering")
                await self._apply_ui_filters(profile)


            page_num = 1
            while True:
                log.info("instahyre.fetch.scraping_page", page_num=page_num)

                try:
                    await self.page.wait_for_selector(card_selector, timeout=8000)
                except Exception:
                    log.debug("instahyre.fetch.card_selector_timeout", page_num=page_num)

                cards = await self.page.locator(card_selector).all()

                if not cards:
                    log.info("instahyre.fetch.no_cards_found_exiting", page_num=page_num)
                    break  # No cards found on this feed page

                log.info("instahyre.fetch.cards_found", count=len(cards))


                for index, card in enumerate(cards, start=len(jobs) + 1):
                    try:
                        # Extract company-title from the desktop employer-job-name div
                        # Structure: div.employer-job-name > div.company-name
                        raw_name = ""
                        name_loc = card.locator("div.employer-job-name div.company-name").first
                        try:
                            if await name_loc.count() > 0:
                                raw_name = (await name_loc.inner_text(timeout=2000)).strip()
                        except Exception:
                            pass

                        # Fallback: try the title attribute on employer-job-name
                        if not raw_name:
                            title_attr_loc = card.locator("div.employer-job-name").first
                            try:
                                raw_name = (await title_attr_loc.get_attribute("title") or "").strip()
                            except Exception:
                                pass

                        if not raw_name:
                            log.debug("instahyre.fetch.card_no_name", index=index)
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

                        # Enrich card metadata for HardFilter/RankingEngine —
                        # a blank location fails allowed_locations checks
                        # (filters.py) and empty tags/description gut the skill
                        # ranking signal (ranking.py scores on all three).
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

                        # Instahyre links have no href — they use ng-click="openApplyModal(opp)"
                        url = "https://www.instahyre.com/candidate/opportunities/"

                        # Generate stable ID from title+company
                        job_id = f"instahyre-{Job.stable_id(url, title, company)}"

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
                                recommendation_tab=f"page_{page_num}",
                                recommendation_position=index,
                            )
                        )

                    except Exception as exc:
                        log.debug("instahyre.fetch.parse_error", index=index, error=str(exc))
                        continue


                # Pagination (verified from saved DOM): div.pagination holds
                # « Previous / numbered lis / 'Next »'. Next is an <li> with
                # ng-click="nextPage()" that gains class 'hidden' on the last
                # page — there is no <a> or .next link.
                has_next = False
                if page_num < 2:  # Safety cap for testing
                    next_li = self.page.locator("div.pagination li:has-text('Next'):not(.hidden)").first
                    try:
                        has_next = await next_li.count() > 0 and await next_li.is_visible()
                    except Exception:
                        pass

                    if not has_next:
                        # Fallback: click the next page number directly
                        next_num = self.page.locator(f"div.pagination li:text-is('{page_num + 1}')").first
                        try:
                            has_next = await next_num.count() > 0 and await next_num.is_visible()
                        except Exception:
                            pass
                        next_li = next_num

                if has_next:
                    await next_li.click()
                    await human_pause(1500, 3000)
                    page_num += 1
                else:
                    log.info("instahyre.fetch.last_page_reached", pages=page_num)
                    break

            # Always return to Page 1 so apply phase starts from the top
            try:
                first_page = self.page.locator("div.pagination li:text-is('1')").first
                if await first_page.count() > 0 and await first_page.is_visible():
                    await first_page.click()
                    await human_pause(1000, 2000)
            except Exception:
                pass

        except Exception as exc:
            log.warning("instahyre.fetch.error", url=feed_url, error=str(exc))

        log.info("instahyre.fetch.done", count=len(jobs))
        return jobs

    async def _ensure_modal_closed(self) -> None:
        """Forces the Instahyre carousel modal to close so the Orchestrator stays in control."""
        modal = await first_visible(
            self.page,
            ["div.modal-content", "div#employer-profile-modal", "div[class*='modal']"],
            timeout_ms=1000,
        )
        if modal is not None:
            close_btn = await first_visible(
                self.page,
                ["button.close", "span.close", "button[aria-label='Close']", ".modal-header button"],
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

    async def apply_to_job(
        self,
        job: Job,
        profile_name: str,
        pre_submit_check: Callable[[Job], FilterDecision] | None = None,
    ) -> ApplyOutcome:
        self.require_mutation("application.apply_flow")
        log.info("instahyre.apply.start", job_id=job.job_id)

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
            # 1. Determine target pagination page
            target_page = 1
            if job.recommendation_tab and job.recommendation_tab.startswith("page_"):
                try:
                    target_page = int(job.recommendation_tab.split("_")[1])
                except Exception:
                    target_page = 1

            # Check currently active page in pagination
            active_page_loc = self.page.locator("div.pagination li.active").first
            current_page_num = 1
            if await active_page_loc.count() > 0:
                try:
                    current_page_num = int((await safe_text(active_page_loc)).strip())
                except Exception:
                    current_page_num = 1

            if current_page_num != target_page:
                log.info("instahyre.apply.switching_page", from_page=current_page_num, to_page=target_page)
                page_btn = self.page.locator(f"div.pagination li:text-is('{target_page}')").first
                if await page_btn.count() > 0 and await page_btn.is_visible():
                    await page_btn.click()
                    await human_pause(1200, 2000)

            # Open modal from card — find by company or title text
            safe_title = job.title.replace("'", "\\'")
            safe_company = job.company.replace("'", "\\'")

            card = None

            # Try finding card by company name or title text
            for text_match in [safe_company, safe_title]:
                if not text_match:
                    continue
                try:
                    loc = self.page.locator(f"div.employer-row:has-text('{text_match}')").first
                    if await loc.count() > 0 and await loc.is_visible():
                        card = loc
                        break
                except Exception:
                    continue

            # Fallback: if not found, search across all employer-rows on page
            if not card:
                all_rows = await self.page.locator("div.employer-row").all()
                for row in all_rows:
                    row_text = (await safe_text(row)).lower()
                    if (job.company and job.company.lower() in row_text) or (job.title and job.title.lower()[:30] in row_text):
                        card = row
                        break

            # If not found on target_page, check alternate page (since earlier applies shift cards across pages)
            if not card:
                alternate_page = 1 if target_page == 2 else 2
                alt_btn = self.page.locator(f"div.pagination li:text-is('{alternate_page}')").first
                if await alt_btn.count() > 0 and await alt_btn.is_visible():
                    log.info("instahyre.apply.checking_alternate_page", alt_page=alternate_page, job=f"{job.company} - {job.title}")
                    await alt_btn.click()
                    await human_pause(1200, 2000)
                    for text_match in [safe_company, safe_title]:
                        if not text_match:
                            continue
                        try:
                            loc = self.page.locator(f"div.employer-row:has-text('{text_match}')").first
                            if await loc.count() > 0 and await loc.is_visible():
                                card = loc
                                break
                        except Exception:
                            continue
                    if not card:
                        all_rows = await self.page.locator("div.employer-row").all()
                        for row in all_rows:
                            row_text = (await safe_text(row)).lower()
                            if (job.company and job.company.lower() in row_text) or (job.title and job.title.lower()[:30] in row_text):
                                card = row
                                break

            if not card:
                return ApplyOutcome(ApplicationStatus.FAILED, detail=f"Job card for {job.company} - {job.title} not found on feed")

            try:
                # Click the opportunity card / view button to trigger openApplyModal(opp)
                await card.scroll_into_view_if_needed()
                await human_pause(300, 600)

                clicked = False
                view_btn = card.locator("button#interested-btn, span#interested-btn, .button-interested").first
                if await view_btn.count() > 0 and await view_btn.is_visible():
                    await view_btn.click(force=True)
                    clicked = True
                else:
                    link = card.locator("a#employer-profile-opportunity, a.text-link").first
                    if await link.count() > 0 and await link.is_visible():
                        await link.click()
                        clicked = True

                if not clicked:
                    await card.evaluate("node => node.click()")
                await human_pause(1500, 2500)
            except Exception as exc:
                return ApplyOutcome(ApplicationStatus.FAILED, detail=f"Failed to click job card: {str(exc)}")

            modal = await first_visible(
                self.page,
                [
                    "div#employer-profile-modal",
                    "div.modal.fade.in",
                    "div.modal.in",
                    "div.modal.show",
                    "div.modal-dialog",
                    "div.modal-content",
                    "div.bar-actions",
                    "div[class*='opportunity-modal']",
                ],
                timeout_ms=7000,
            )
            if not modal:
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
            return ApplyOutcome(
                ApplicationStatus.EXTERNAL,
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
                return ApplyOutcome(ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)
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
            return ApplyOutcome(ApplicationStatus.FAILED, detail=f"Click apply failed: {str(exc)}")


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
                "div.apply button:has-text('Applied')",
                "div.apply[class*='disabled']",
                "div.apply[class*='applied']",
                "div#refer",
                "div#go-premium-modal",
                "div.application-modal",
            ],
            timeout_ms=5000,
        )

        # Post-apply cleanup: Instahyre advances the carousel / pops the bulk
        # "apply to similar jobs" modal after an application — either can leave
        # a backdrop that eats the next card's clicks.
        await self._ensure_modal_closed()
        await self._dismiss_modals()

        if confirmation:
            return ApplyOutcome(
                ApplicationStatus.APPLIED,
                confirmation_type="dom_marker",
                confirmation_evidence="Instahyre application success marker observed",
            )
        return ApplyOutcome(
            ApplicationStatus.FAILED,
            detail="Submission clicked but no Instahyre success confirmation was observed",
        )
