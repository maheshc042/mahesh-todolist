"""
Cutshort Platform Implementation (Direct-Action & Inbox Message Handler).
"""
from __future__ import annotations

import re
import asyncio
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
    scroll_page,
)
from ..config import JobProfile, NaukriAccount, get_settings
from ..core.answers import AnswerEngine

from ..core.models import (
    ApplicationStatus,
    ApplyOutcome,
    FilterDecision,
    Job,
    ScreeningQuestion,
    SkipReason,
)
from ..logging_setup import get_logger
from .base import BaseJobPlatform

log = get_logger(__name__)


class CutshortChatbot:
    """Handles Cutshort's specific chat-based apply flow inside Inbox messages."""

    def __init__(self, page: Page, answers: AnswerEngine, max_questions: int = 10):
        self.page = page
        self.answers = answers
        self.max_questions = max_questions

    async def run(self) -> tuple[bool, int, str]:
        """Returns (success, questions_answered, error_message)."""
        answered = 0
        last_question = ""
        stagnant_rounds = 0

        for _ in range(self.max_questions):
            await human_pause(1500, 2500)

            # Check if chat is complete
            if await first_visible(
                self.page, ["text=Application sent", "text=successfully applied", "text=Employer will review"]
            ):
                return True, answered, ""

            # Get the last recruiter chat bubble
            bubbles = await self.page.locator(
                "div.message-content, div[class*='msg-text'], div[class*='message-text'], "
                "div[class*='chat-bubble'], div[class*='msg_body'], p[class*='message'], div[class*='chat_msg']"
            ).all()
            if not bubbles:
                stagnant_rounds += 1
                if stagnant_rounds > 3:
                    break
                continue

            current_text = await safe_text(bubbles[-1])
            if current_text == last_question:
                stagnant_rounds += 1
                if stagnant_rounds > 3:
                    return False, answered, "Chatbot stalled on the same question."
                continue

            last_question = current_text
            stagnant_rounds = 0

            # Resolve Answer
            question_obj = ScreeningQuestion(text=current_text, kind="unknown", options=[])
            resolved = self.answers.resolve(question_obj)

            if not resolved:
                return False, answered, f"Unanswered question: {current_text[:100]}"

            answered_successfully = False

            # Step A: Look for Chips (Quick Replies)
            chips = await self.page.locator(
                "button.chip, div[class*='quick-reply'], button[class*='option']"
            ).all()
            for chip in chips:
                chip_text = await safe_text(chip)
                if chip_text.strip().lower() == resolved.value.strip().lower():
                    await chip.click()
                    answered_successfully = True
                    break

            # Step B: Fallback to Text Input
            if not answered_successfully:
                text_input = await first_visible(self.page, ["textarea", "input[type='text']"])
                if text_input:
                    await human_type(text_input, resolved.value)
                    await human_pause(300, 600)
                    send_btn = await first_visible(
                        self.page, ["button[aria-label='Send']", "svg.send-icon"]
                    )
                    if send_btn:
                        await send_btn.click()
                    else:
                        await text_input.press("Enter")
                    answered_successfully = True

            if not answered_successfully:
                return False, answered, "Could not find text input or matching chip."

            answered += 1

        return False, answered, "Exceeded maximum chat questions."


class CutshortPlatform(BaseJobPlatform):
    def __init__(self, page: Page, account: NaukriAccount, artifacts: ArtifactStore, answers: AnswerEngine):
        super().__init__(page, account.key)
        self.account = account
        self.artifacts = artifacts
        self.answers = answers

    @property
    def platform_name(self) -> str:
        return "cutshort"

    async def ensure_logged_in(self) -> bool:
        await self.page.goto("https://cutshort.io/profile/recommended-jobs", wait_until="domcontentloaded")
        await human_pause(1000, 2000)


        login_prompt = await first_visible(
            self.page,
            [
                "text=Candidate login",
                "a[href*='/login']",
                "button:has-text('Sign in with Google')",
                "button:has-text('Login')",
            ],
            timeout_ms=3000,
        )

        if login_prompt:
            log.info(
                "cutshort.auth.google_sso_required",
                msg="Cutshort uses Google SSO. Please log in using 'Sign in with Google' in the opened browser window.",
            )
            print("\n" + "=" * 70)
            print("🔑 CUTSHORT GOOGLE LOGIN REQUIRED:")
            print("Cutshort uses 'Sign in with Google'. Please click 'Sign in with Google' in the opened browser window.")
            print("The agent will wait 90 seconds, save your session, and automatically resume!")
            print("=" * 70 + "\n")

            # Wait up to 90s for user to complete Google OAuth in the opened browser
            for _ in range(90):
                await human_pause(1000, 1000)
                # Check if logged in (URL contains /all-job or /profile or login elements disappear)
                if not await first_visible(
                    self.page,
                    ["text=Candidate login", "a[href*='/login']", "button:has-text('Sign in with Google')"],
                    timeout_ms=500,
                ):
                    log.info("cutshort.auth.google_sso_completed")
                    return True

            log.error("cutshort.auth.login_timeout")
            return False

        log.info("cutshort.auth.session_reused")
        return True

    async def fetch_jobs(self, profile: JobProfile, exclude_job_ids: set[str]) -> list[Job]:
        log.info("cutshort.fetch.start", profile=profile.name)
        # /profile/recommended-jobs is the official jobs feed page for logged-in candidates
        await self.page.goto("https://cutshort.io/profile/recommended-jobs", wait_until="domcontentloaded")
        await human_pause(2000, 3000)


        # Dump feed HTML for exact inspection
        try:
            dump_path = self.artifacts.dir / "cutshort-dashboard.html"
            content = await self.page.content()
            dump_path.write_text(content, encoding="utf-8")
            log.info("cutshort.dashboard_html_saved", path=str(dump_path))
        except Exception as exc:
            log.debug("cutshort.dashboard_dump_failed", error=str(exc))

        card_selector = "div:has(a[href*='/job/']):has(button:has-text('Apply now'))"

        # Infinite scroll to fetch ALL available jobs in the feed
        last_card_count = 0
        stagnant_scrolls = 0
        while True:
            cards = await self.page.locator(card_selector).all()
            current_count = len(cards)
            if current_count == last_card_count:
                stagnant_scrolls += 1
                if stagnant_scrolls >= 3:
                    break
            else:
                stagnant_scrolls = 0
                last_card_count = current_count

            await scroll_page(self.page, steps=3, delay_s=0.5)
            await human_pause(800, 1500)

        cards = await self.page.locator(card_selector).all()
        jobs = []

        log.info("cutshort.fetch.cards_found", count=len(cards))

        for index, card in enumerate(cards, start=1):
            try:
                title_el = card.locator("a[href*='/job/']").first
                company_el = card.locator("a[href*='/company/']").first

                title = (await safe_text(title_el)).strip() if title_el else ""
                company = (await safe_text(company_el)).strip() if company_el else ""

                url = await title_el.get_attribute("href") if title_el else ""
                if url and url.startswith("/"):
                    url = f"https://cutshort.io{url}"

                if not title:
                    continue

                match = re.search(r"-([a-zA-Z0-9]+)$", url)
                job_id = f"cutshort-{match.group(1)}" if match else f"cutshort-{Job.stable_id(url, title, company)}"

                if job_id in exclude_job_ids:
                    continue

                jobs.append(
                    Job(
                        job_id=job_id,
                        title=title,
                        company=company,
                        url=url or "https://cutshort.io/profile/recommended-jobs",
                        recommendation_tab="default",
                        recommendation_position=index,
                    )
                )
            except Exception as exc:
                log.debug("cutshort.fetch.parse_error", index=index, error=str(exc))
                continue

        log.info("cutshort.fetch.done", count=len(jobs))
        return jobs


    async def apply_to_job(
        self,
        job: Job,
        profile_name: str,
        pre_submit_check: Callable[[Job], FilterDecision] | None = None,
    ) -> ApplyOutcome:
        log.info("cutshort.apply.start", job_id=job.job_id)

        raw_id = job.job_id.replace("cutshort-", "")
        card_selectors = [
            f"div:has(a[href*='{raw_id}'])",
            f"div:has(a[href*='/job/']):has-text('{job.company}')",
            f"div:has-text('{job.company}')",
        ]
        card = await first_visible(self.page, card_selectors, timeout_ms=3000)

        # Fallback: use recommendation_position if card not found by ID/company
        if not card and job.recommendation_position:
            try:
                pos = job.recommendation_position - 1
                all_cards = self.page.locator("div:has(a[href*='/job/']):has(button:has-text('Apply now'))")
                if pos < await all_cards.count():
                    card = all_cards.nth(pos)
            except Exception:
                pass

        if not card and job.url and job.url.startswith("http"):
            try:
                await self.page.goto(job.url, wait_until="domcontentloaded")
                await human_pause(1000, 2000)
                card = await first_visible(self.page, card_selectors + ["div:has(button:has-text('Apply now'))"], timeout_ms=3000)
            except Exception as exc:
                return ApplyOutcome(ApplicationStatus.FAILED, detail=f"Failed to load: {str(exc)}")


        if not card:
            return ApplyOutcome(ApplicationStatus.FAILED, detail=f"Job card for {job.title} at {job.company} not found on page")

        if pre_submit_check:
            decision = pre_submit_check(job)
            if not decision.passed:
                not_interested = await first_visible(card, ["button:has-text('Not interested')", "a:has-text('Not interested')"])
                if not_interested:
                    try:
                        await not_interested.click()
                        await human_pause(500, 1000)
                    except Exception:
                        pass
                return ApplyOutcome(ApplicationStatus.SKIPPED, reason=decision.reason, detail=decision.detail)

        apply_btn = await first_visible(
            card, ["button:has-text('Apply now')", "button:has-text('Apply')", "button:has-text('Interested')"], timeout_ms=3000
        )
        if not apply_btn:
            if await first_visible(card, ["text=Applied", "text=Application Sent"]):
                return ApplyOutcome(ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)
            return ApplyOutcome(ApplicationStatus.FAILED, detail="Apply button not found on job card")

        await apply_btn.scroll_into_view_if_needed()
        await human_pause(200, 400)
        await apply_btn.click(force=True)
        await human_pause(1500, 2500)

        # Handle pitch modal
        modal = await first_visible(self.page, ["div.modal-content", "div[class*='modal']"], timeout_ms=4000)
        modal_text = (await safe_text(modal)).strip() if modal else ""

        recruiter_name = "Hiring Team"
        rec_match = re.search(r"To\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", modal_text)
        if rec_match:
            full_name = rec_match.group(1).strip()
            if " " in full_name and not any(w in full_name.lower() for w in ["team", "manager", "hr", "recruiter"]):
                recruiter_name = full_name.split()[0]
            else:
                recruiter_name = full_name

        actual_title = job.title
        title_match = re.search(r"applying for\s+([^\n]+?)\s+at\s+", modal_text, re.IGNORECASE)
        if title_match:
            actual_title = title_match.group(1).strip()

        actual_company = job.company
        company_match = re.search(r"\s+at\s+([^\n]+?)(?:\s+To\s+|$)", modal_text, re.IGNORECASE)
        if company_match:
            actual_company = company_match.group(1).strip()

        # Dynamic skill matching based on job title & description context
        text_for_skills = f"{actual_title} {modal_text}".lower()
        if any(k in text_for_skills for k in ["ai", "llm", "genai", "gpt", "machine learning", "rag"]):
            skill_phrase = "building AI-driven applications, LLM workflows, and scalable Python backends"
        elif any(k in text_for_skills for k in ["qa", "testing", "test automation"]):
            skill_phrase = "in software QA, API testing, test automation, and quality engineering"
        elif any(k in text_for_skills for k in ["devops", "docker", "kubernetes", "aws", "gcp", "azure"]):
            skill_phrase = "in DevOps, CI/CD automation, cloud infrastructure, and containerization"
        elif any(k in text_for_skills for k in ["golang", "go"]):
            skill_phrase = "building scalable microservices and APIs with Go, Python, and Node.js"
        elif any(k in text_for_skills for k in [".net", "dotnet", "dot net", "c#"]):
            skill_phrase = "building backend services with .NET, C#, and REST APIs"
        elif any(k in text_for_skills for k in ["frontend", "fullstack", "full stack", "react", "next"]):
            skill_phrase = "developing full-stack applications with React, Node.js, and Python"
        elif any(k in text_for_skills for k in ["backend", "python", "fastapi", "django", "node"]):
            skill_phrase = "designing high-performance backends and APIs with Python, FastAPI, and Node.js"
        else:
            skill_phrase = "building scalable backends and software systems"

        textarea = await first_visible(self.page, ["textarea", "input[type='text'][placeholder*='note' i]"], timeout_ms=3000)
        if textarea:
            pitch = (
                f"Hi {recruiter_name},\n\n"
                f"I came across the {actual_title} role at {actual_company} and would love to connect.\n\n"
                f"With strong hands-on experience {skill_phrase}, "
                f"I'm confident I can bring immediate value to your team.\n\n"
                f"Looking forward to connecting!\n\n"
                f"Best,\nMahesh Chitakoti"
            )
            await textarea.fill(pitch)
            await human_pause(300, 600)



        send_btn = await first_visible(self.page, ["button:has-text('Send')", "button[type='submit']", "button:has-text('Submit')"])
        if send_btn:
            await send_btn.click()
            await human_pause(1000, 2000)

        # Close modal if still open
        close_btn = await first_visible(self.page, ["button.close", "span.close", "button[aria-label='Close']"])
        if close_btn:
            try:
                await close_btn.click()
            except Exception:
                pass

        return ApplyOutcome(ApplicationStatus.APPLIED)

    # ---------------------------------------------------------
    # Phase 2 Message Handling (Inbox Questionnaire & Chat Clearing)
    # ---------------------------------------------------------
    async def handle_messages(self) -> None:
        """Navigates to the Inbox and clears out pending questionnaire & recruiter threads."""
        log.info("cutshort.messages.start")
        await self.page.goto("https://cutshort.io/messages", wait_until="domcontentloaded")
        await human_pause(2000, 4000)

        # Save HTML snapshot of the inbox for inspection
        try:
            dump_path = self.artifacts.dir / "cutshort-messages.html"
            content = await self.page.content()
            dump_path.write_text(content, encoding="utf-8")
        except Exception:
            pass

        # Look for active conversation threads (unread or recent chats)
        thread_selector = (
            "div[class*='conversation'], li[class*='thread'], a[href*='/messages/'], "
            "div[class*='thread-item'], div[class*='chat-item'], div[class*='message-item']"
        )
        threads = await self.page.locator(thread_selector).all()

        if not threads:
            log.info("cutshort.messages.no_threads_found")
            return

        log.info("cutshort.messages.found", count=len(threads))

        for index, thread in enumerate(threads[:15]):  # Process up to 15 threads per run
            try:
                if not await thread.is_visible():
                    continue

                await thread.click()
                await human_pause(1500, 2500)

                log.info("cutshort.messages.processing_thread", index=index + 1)
                chatbot = CutshortChatbot(self.page, self.answers)
                success, answered, error_msg = await chatbot.run()

                if answered > 0:
                    log.info("cutshort.messages.success", answered=answered)
                elif not success:
                    log.debug("cutshort.messages.no_action_needed", reason=error_msg)

            except Exception as exc:
                log.error("cutshort.messages.error", error=str(exc)[:100])
                continue
