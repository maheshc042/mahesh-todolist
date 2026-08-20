"""
Instahyre Platform Implementation (Pagination & Modal-Swiper Engine).
"""
from __future__ import annotations

import re
import time
from typing import Callable

from playwright.async_api import Page

from ..browser.artifacts import ArtifactStore
from ..browser.resilience import (
    click_if_present,
    first_visible,
    human_pause,
    human_type,
    retry_async,
    safe_text,
)
from ..config import JobProfile, NaukriAccount, get_settings

from ..core.models import ApplicationStatus, ApplyOutcome, FilterDecision, Job, SkipReason, extract_description_metadata
from ..logging_setup import get_logger
from .base import BaseJobPlatform

log = get_logger(__name__)


class InstahyrePlatform(BaseJobPlatform):
    def __init__(self, page: Page, account: NaukriAccount, artifacts: ArtifactStore):
        super().__init__(page, account.key)
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

        if not email_input or not pass_input:
            log.info("instahyre.auth.session_reused")
            return True

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
            
            # Verify login form is gone or job feed is present
            if not await first_visible(self.page, ["input[type='password']", "input[name='password']"], timeout_ms=3000):
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

        target_skills = ["Node.js"]

        if skills_input:
            for skill in target_skills:
                try:
                    await skills_input.click()
                    await human_pause(200, 400)
                    await skills_input.fill("")
                    await human_type(skills_input, skill)
                    await human_pause(500, 1000)

                    # Select from Selectize dropdown
                    opt = await first_visible(
                        self.page,
                        [
                            f"div.selectize-dropdown-content div.option:has-text('{skill}')",
                            f"div.option:has-text('{skill}')",
                            "div.selectize-dropdown-content div.option",
                        ],
                        timeout_ms=1500,
                    )
                    if opt:
                        await opt.click()
                    else:
                        await self.page.keyboard.press("ArrowDown")
                        await human_pause(200, 400)
                        await self.page.keyboard.press("Enter")
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
            active_modal_btn = await first_visible(
                self.page,
                [
                    "div.candidate-active-check-modal button.btn-success",
                    "div.candidate-active-check-modal button:has-text('Save')",
                    "div.candidate-active-check-modal button:has-text('Cancel')",
                    "button:has-text('Save')",
                ],
                timeout_ms=1500,
            )
            if active_modal_btn:
                log.info("instahyre.modal.dismissing_active_check")
                await active_modal_btn.click()
                await human_pause(1000, 1500)

            # 2. Premium / Share / WhatsApp Modals
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



            # Check if actual job links/titles exist on the initial page
            initial_jobs = await self.page.locator(
                "a[href*='/job-'], a[href*='/opportunity'], .position-title, .job-title"
            ).all()

            if not initial_jobs:
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
                                url=url,
                                recommendation_tab="default",
                                recommendation_position=index,
                            )
                        )

                    except Exception as exc:
                        log.debug("instahyre.fetch.parse_error", index=index, error=str(exc))
                        continue


                # Pagination loop: click Next until end of list
                next_btn = await first_visible(
                    self.page,
                    [
                        "ul.pagination li.next a",
                        "li.next a",
                        "a.page-link:has-text('Next')",
                        "li.next:not(.disabled) a",
                    ],
                    timeout_ms=1500,
                )
                if next_btn:
                    await next_btn.click()

                    await human_pause(1500, 3000)
                    page_num += 1
                else:
                    break

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
        log.info("instahyre.apply.start", job_id=job.job_id)

        # 1. Check if Modal is ALREADY open (carousel swiper mode)
        modal = await first_visible(
            self.page, ["div.modal-content", "div#employer-profile-modal", "div[class*='modal']"], timeout_ms=2000
        )

        if not modal:
            # Open modal from card — find by title text or position index
            # Escape single quotes in company/title for CSS :has-text()
            safe_title = job.title.replace("'", "\\'")
            safe_company = job.company.replace("'", "\\'")

            card = None

            # Try finding card by title text first
            for text_match in [safe_title, safe_company]:
                if not text_match:
                    continue
                try:
                    loc = self.page.locator(f"div.employer-row:has-text('{text_match}')").first
                    if await loc.count() > 0 and await loc.is_visible():
                        card = loc
                        break
                except Exception:
                    continue

            # Fallback: use recommendation_position to find nth card
            if not card and job.recommendation_position:
                try:
                    pos = job.recommendation_position - 1  # 0-indexed
                    all_cards = self.page.locator("div.employer-row")
                    count = await all_cards.count()
                    if pos < count:
                        card = all_cards.nth(pos)
                        if not await card.is_visible():
                            card = None
                except Exception:
                    pass

            if not card:
                return ApplyOutcome(ApplicationStatus.FAILED, detail="Job card not found on page to open modal")

            try:
                # Click the View » button on the card
                view_btn = card.locator("button#interested-btn, button.button-interested, button.btn-success").first
                if await view_btn.count() > 0:
                    await view_btn.scroll_into_view_if_needed()
                    await human_pause(200, 400)
                    await view_btn.click(force=True)
                else:
                    # Fallback: click the card link directly
                    link = card.locator("a#employer-profile-opportunity, a.text-link").first
                    if await link.count() > 0:
                        await link.click(force=True)
                    else:
                        await card.click()
                await human_pause(1500, 2500)
            except Exception as exc:
                return ApplyOutcome(ApplicationStatus.FAILED, detail=f"Failed to click job card: {str(exc)}")


            modal = await first_visible(
                self.page,
                [
                    "div.modal-content",
                    "div#employer-profile-modal",
                    "div[class*='modal']",
                    "div[class*='opportunity-modal']",
                ],
                timeout_ms=5000,
            )
            if not modal:
                return ApplyOutcome(ApplicationStatus.FAILED, detail="Modal did not open after clicking card")

            # Wait for AngularJS candidateOpportunityCtrl to render the action bar
            # The bar-actions div with Apply/Not Interested is dynamically injected
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



        try:
            await apply_btn.scroll_into_view_if_needed()
            await human_pause(200, 400)
            await apply_btn.click(force=True)
            await human_pause(800, 1500)
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

        return ApplyOutcome(ApplicationStatus.APPLIED)
