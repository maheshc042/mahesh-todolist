"""
Naukri screening-chatbot driver.

After clicking Apply, Naukri may open a right-hand drawer chatbot that asks
recruiter questions one at a time. It is a React widget with no stable API, so
the driver is a bounded state machine:

    read current question -> classify input type -> resolve answer -> submit
    -> wait for the question text to CHANGE -> repeat

Design decisions:

- **Change-detection instead of fixed sleeps.** We record the last question text
  and wait for it to differ. This is the only reliable completion signal because
  the drawer reuses the same DOM nodes for every question.
- **Hard iteration cap (`max_questions`).** Without it a mis-detected question
  loops forever and burns the run's time budget.
- **Strict-mode abort.** If a question cannot be answered we close the drawer and
  report `UNANSWERED_QUESTION`; the orchestrator queues it for human review. We
  never submit a guessed answer to a recruiter.
- **contenteditable typing.** Naukri's text input is a contenteditable div, not
  an <input>, so `fill()` silently does nothing — `press_sequentially` is
  required for React's onChange to fire.
"""

from __future__ import annotations

import asyncio

from playwright.async_api import Page

from ..browser.resilience import dismiss_overlays, first_visible, human_pause, safe_text
from ..core.answers import AnswerEngine
from ..core.models import ScreeningQuestion
from ..core.run_policy import RunPolicy
from ..logging_setup import get_logger
from . import selectors as S

log = get_logger(__name__)


class ChatbotResult:
    def __init__(self) -> None:
        self.answered = 0
        self.completed = False
        self.unanswered: list[ScreeningQuestion] = []
        self.error: str | None = None


class ChatbotHandler:
    def __init__(
        self,
        page: Page,
        answers: AnswerEngine,
        policy: RunPolicy,
        max_questions: int = 15,
    ) -> None:
        self.page = page
        self.answers = answers
        self.policy = policy
        self.max_questions = max_questions

    async def is_open(self, timeout_ms: int = 5_000) -> bool:
        return await first_visible(self.page, S.CHATBOT_DRAWER, timeout_ms) is not None

    async def close(self) -> None:
        for selector in S.CHATBOT_CLOSE:
            try:
                locator = self.page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    await locator.click(timeout=2_000)
                    return
            except Exception:
                continue
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass

    # ------------------------------------------------------------------- read
    async def _current_question(self) -> ScreeningQuestion | None:
        """The last bot bubble is the active question."""
        bubbles: list = []
        for selector in S.CHATBOT_QUESTION:
            bubbles = await self.page.locator(selector).all()
            if bubbles:
                break
        if not bubbles:
            return None

        text = ""
        for bubble in reversed(bubbles[-4:]):
            candidate = await safe_text(bubble)
            if candidate and len(candidate) > 3:
                text = candidate
                break
        if not text:
            return None

        kind, options = await self._classify_input()
        return ScreeningQuestion(text=text, kind=kind, options=options)

    async def _classify_input(self) -> tuple[str, list[str]]:
        """Inspect which widget the drawer is currently rendering."""
        radios = await self._option_texts(S.CHATBOT_RADIO_OPTIONS)
        if radios:
            return "radio", radios

        checkboxes = await self._option_texts(S.CHATBOT_CHECKBOX_OPTIONS)
        if checkboxes:
            return "checkbox", checkboxes

        chips = await self._option_texts(S.CHATBOT_CHIPS)
        if chips:
            return "radio", chips

        dropdown = await first_visible(self.page, S.CHATBOT_DROPDOWN, timeout_ms=800)
        if dropdown is not None:
            option_texts = [
                (await safe_text(option))
                for option in await dropdown.locator("option").all()
            ]
            return "dropdown", [text for text in option_texts if text]

        if await first_visible(self.page, S.CHATBOT_TEXT_INPUT, timeout_ms=1_200):
            return "text", []

        return "unknown", []

    async def _option_texts(self, selectors: list[str]) -> list[str]:
        for selector in selectors:
            locators = await self.page.locator(selector).all()
            if not locators:
                continue
            texts: list[str] = []
            for locator in locators:
                try:
                    if not await locator.is_visible():
                        continue
                except Exception:
                    continue
                text = await safe_text(locator)
                if text:
                    texts.append(text)
            if texts:
                return texts
        return []

    # ------------------------------------------------------------------ write
    async def _answer_text(self, value: str) -> bool:
        field = await first_visible(self.page, S.CHATBOT_TEXT_INPUT, timeout_ms=4_000)
        if field is None:
            return False
        try:
            self.policy.require_mutation("naukri.screening.answer")
            await field.click()
            # contenteditable: clear any prefill, then type so React re-renders.
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Delete")
            await field.press_sequentially(value, delay=45)
            await human_pause(200, 500)
            if not await self._submit():
                await field.press("Enter")
            return True
        except Exception as exc:
            log.warning("chatbot.text_answer_failed", error=str(exc)[:200])
            return False

    async def _answer_option(self, value: str, kind: str) -> bool:
        selectors = (
            S.CHATBOT_CHECKBOX_OPTIONS if kind == "checkbox" else S.CHATBOT_RADIO_OPTIONS
        ) + S.CHATBOT_CHIPS
        for selector in selectors:
            locators = await self.page.locator(selector).all()
            for locator in locators:
                text = (await safe_text(locator)).strip().lower()
                if text and text == value.strip().lower():
                    try:
                        self.policy.require_mutation("naukri.screening.answer")
                        await locator.click(timeout=3_000)
                        await human_pause(200, 500)
                        await self._submit()
                        return True
                    except Exception:
                        continue
        # dropdown fallback
        dropdown = await first_visible(self.page, S.CHATBOT_DROPDOWN, timeout_ms=800)
        if dropdown is not None:
            try:
                self.policy.require_mutation("naukri.screening.answer")
                await dropdown.select_option(label=value)
                await self._submit()
                return True
            except Exception:
                return False
        return False

    async def _submit(self) -> bool:
        for selector in S.CHATBOT_SAVE + S.CHATBOT_SEND:
            try:
                locator = self.page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    await locator.click(timeout=2_500)
                    return True
            except Exception:
                continue
        return False

    # ------------------------------------------------------------------- loop
    async def run(self, on_unanswered=None) -> ChatbotResult:
        result = ChatbotResult()
        last_question = ""
        stagnant_rounds = 0

        for index in range(self.max_questions):
            await asyncio.sleep(1.2)  # let the drawer render the next bubble

            if await first_visible(self.page, S.CHATBOT_COMPLETE, timeout_ms=1_200):
                result.completed = True
                log.info("chatbot.completed", answered=result.answered)
                break

            question = await self._current_question()
            if question is None:
                stagnant_rounds += 1
                if stagnant_rounds >= 3:
                    # No question and no completion banner: assume the drawer is
                    # done (Naukri closes it silently on the last answer).
                    result.completed = not await self.is_open(1_500)
                    break
                continue

            if question.text == last_question:
                stagnant_rounds += 1
                if stagnant_rounds >= 3:
                    result.error = "chatbot stalled on the same question"
                    log.warning("chatbot.stalled", question=question.text[:150])
                    break
                continue

            stagnant_rounds = 0
            last_question = question.text
            log.info(
                "chatbot.question",
                index=index,
                kind=question.kind,
                options=len(question.options),
                question=question.text[:180],
            )

            resolved = self.answers.resolve(question)
            if resolved is None:
                result.unanswered.append(question)
                if on_unanswered is not None:
                    await on_unanswered(question)
                log.warning("chatbot.unanswered_abort", question=question.text[:180])
                await self.close()
                return result

            ok = (
                await self._answer_text(resolved.value)
                if question.kind in ("text", "unknown")
                else await self._answer_option(resolved.value, question.kind)
            )
            if not ok:
                # Widget classification may have been wrong; try the other path.
                ok = (
                    await self._answer_option(resolved.value, "radio")
                    if question.kind in ("text", "unknown")
                    else await self._answer_text(resolved.value)
                )
            if not ok:
                result.error = f"could not submit answer for: {question.text[:120]}"
                log.error("chatbot.answer_submit_failed", question=question.text[:150])
                break

            result.answered += 1
            log.info(
                "chatbot.answered",
                value=resolved.value[:80],
                source=resolved.source,
                matched=resolved.matched_pattern[:60],
            )

        if not result.completed and result.error is None:
            # Drawer gone == Naukri accepted the application.
            result.completed = not await self.is_open(1_500)

        await dismiss_overlays(self.page)
        return result
