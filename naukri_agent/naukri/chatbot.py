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
import re

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
        """Inspect which widget the drawer is currently rendering, allowing time for React hydration."""
        for attempt in range(5):
            radios = await self._option_texts(S.CHATBOT_RADIO_OPTIONS)
            if radios:
                return "radio", radios

            checkboxes = await self._option_texts(S.CHATBOT_CHECKBOX_OPTIONS)
            if checkboxes:
                return "checkbox", checkboxes

            chips = await self._option_texts(S.CHATBOT_CHIPS)
            if chips:
                return "radio", chips

            dropdown = await first_visible(self.page, S.CHATBOT_DROPDOWN, timeout_ms=300)
            if dropdown is not None:
                option_texts = [
                    (await safe_text(option))
                    for option in await dropdown.locator("option").all()
                ]
                options = [text for text in option_texts if text]
                if options:
                    return "dropdown", options

            # Custom combobox / dropdown options
            combobox_options = await self._option_texts(S.CHATBOT_DROPDOWN_OPTIONS)
            if combobox_options:
                return "combobox", combobox_options

            combobox_trigger = await first_visible(self.page, S.CHATBOT_COMBOBOX_TRIGGER, timeout_ms=300)
            if combobox_trigger is not None:
                try:
                    await combobox_trigger.click(timeout=1_500)
                    await human_pause(200, 400)
                    options = await self._option_texts(S.CHATBOT_DROPDOWN_OPTIONS)
                    if options:
                        return "combobox", options
                except Exception:
                    pass
                return "combobox", []

            if await first_visible(self.page, S.CHATBOT_TEXT_INPUT, timeout_ms=300):
                return "text", []

            if attempt < 4:
                await asyncio.sleep(0.4)

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

    async def _dismiss_blocking_overlays(self) -> None:
        try:
            await self.page.evaluate("""() => {
                document.querySelectorAll('#splScrn, .circleG, .loader-wrapper, div[class*="splashScreen"], div[class*="backdrop"]:not([class*="drawer"]):not([class*="chatbot"])').forEach(el => el.remove());
            }""")
        except Exception:
            pass

    # ------------------------------------------------------------------ write
    async def _answer_text(self, value: str) -> bool:
        await self._dismiss_blocking_overlays()
        field = await first_visible(self.page, S.CHATBOT_TEXT_INPUT, timeout_ms=4_000)
        if field is None:
            return False
        try:
            self.policy.require_mutation("naukri.screening.answer")
            try:
                await field.click(force=True, timeout=2_500)
            except Exception:
                await field.focus()
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

    @staticmethod
    def _match_numeric_option_idx(val_clean: str, option_texts: list[str]) -> int | None:
        """Find index of best matching numeric option for experience or salary values."""
        num_match = re.search(r"(\d+(?:\.\d+)?)", val_clean)
        if not num_match:
            return None
        val = float(num_match.group(1))

        # Pass 1: Range bounds check (e.g. '2-3 years' or '2 to 4 years')
        for idx, opt in enumerate(option_texts):
            opt_low = opt.lower()
            numbers = [float(n) for n in re.findall(r"(\d+(?:\.\d+)?)", opt_low)]
            if len(numbers) >= 2:
                if min(numbers) <= val <= max(numbers):
                    return idx
            elif len(numbers) == 1:
                n = numbers[0]
                if ("<" in opt_low or "under" in opt_low or "less than" in opt_low) and val <= n:
                    return idx
                if (">" in opt_low or "+" in opt_low or "more than" in opt_low) and val >= n:
                    return idx

        # Pass 2: Nearest single number (e.g. '2 years' vs '3 years')
        closest_idx = None
        min_diff = float("inf")
        for idx, opt in enumerate(option_texts):
            opt_low = opt.lower()
            numbers = [float(n) for n in re.findall(r"(\d+(?:\.\d+)?)", opt_low)]
            if len(numbers) == 1:
                diff = abs(numbers[0] - val)
                if diff < min_diff and diff <= 1.0:
                    min_diff = diff
                    closest_idx = idx

        return closest_idx

    async def _answer_combobox(self, value: str) -> bool:
        """Handles modern React custom dropdowns and searchable select comboboxes."""
        await self._dismiss_blocking_overlays()
        val_clean = value.strip().lower()

        # Step 1: Ensure options list is expanded if not already visible
        current_options = await self._option_texts(S.CHATBOT_DROPDOWN_OPTIONS)
        if not current_options:
            trigger = await first_visible(self.page, S.CHATBOT_COMBOBOX_TRIGGER, timeout_ms=1_500)
            if trigger is not None:
                try:
                    await trigger.click(force=True, timeout=2_000)
                    await human_pause(200, 400)
                except Exception:
                    pass

        # Step 2: If a search input exists in the dropdown, filter by value
        search_box = await first_visible(self.page, S.CHATBOT_SEARCH_INPUT, timeout_ms=800)
        if search_box is not None:
            try:
                await search_box.click(force=True, timeout=1_500)
                await search_box.press_sequentially(value, delay=35)
                await human_pause(200, 400)
            except Exception:
                pass

        # Step 3: Find matching option locator (exact text match first)
        for selector in S.CHATBOT_DROPDOWN_OPTIONS:
            locators = await self.page.locator(selector).all()
            for locator in locators:
                try:
                    if not await locator.is_visible():
                        continue
                    text = (await safe_text(locator)).strip().lower()
                    if text and (text == val_clean or val_clean in text or text in val_clean):
                        self.policy.require_mutation("naukri.screening.answer")
                        await locator.click(force=True, timeout=2_500)
                        await human_pause(200, 500)
                        await self._submit()
                        return True
                except Exception:
                    continue

        # Step 4: Fallback to numeric range match for experience/salary dropdowns
        for selector in S.CHATBOT_DROPDOWN_OPTIONS:
            locators = await self.page.locator(selector).all()
            texts: list[str] = []
            vis_locators = []
            for locator in locators:
                try:
                    if await locator.is_visible():
                        t = (await safe_text(locator)).strip()
                        if t:
                            texts.append(t)
                            vis_locators.append(locator)
                except Exception:
                    pass
            num_idx = self._match_numeric_option_idx(val_clean, texts)
            if num_idx is not None and num_idx < len(vis_locators):
                try:
                    self.policy.require_mutation("naukri.screening.answer")
                    await vis_locators[num_idx].click(force=True, timeout=2_500)
                    await human_pause(200, 500)
                    await self._submit()
                    return True
                except Exception:
                    pass

        return False

    async def _answer_option(self, value: str, kind: str) -> bool:
        await self._dismiss_blocking_overlays()
        if kind == "combobox":
            return await self._answer_combobox(value)

        val_clean = value.strip().lower()
        selectors = (
            S.CHATBOT_CHECKBOX_OPTIONS if kind == "checkbox" else S.CHATBOT_RADIO_OPTIONS
        ) + S.CHATBOT_CHIPS
        for selector in selectors:
            locators = await self.page.locator(selector).all()
            for locator in locators:
                text = (await safe_text(locator)).strip().lower()
                if text and (text == val_clean or val_clean in text or text in val_clean):
                    try:
                        self.policy.require_mutation("naukri.screening.answer")
                        await locator.click(force=True, timeout=3_000)
                        await human_pause(200, 500)
                        await self._submit()
                        return True
                    except Exception:
                        continue

        # Numeric range fallback for radio / chip options (e.g. experience chips '2-3 years')
        texts = []
        vis_locators = []
        for selector in selectors:
            for locator in await self.page.locator(selector).all():
                try:
                    if await locator.is_visible():
                        t = (await safe_text(locator)).strip()
                        if t:
                            texts.append(t)
                            vis_locators.append(locator)
                except Exception:
                    pass
        num_idx = self._match_numeric_option_idx(val_clean, texts)
        if num_idx is not None and num_idx < len(vis_locators):
            try:
                self.policy.require_mutation("naukri.screening.answer")
                await vis_locators[num_idx].click(force=True, timeout=3_000)
                await human_pause(200, 500)
                await self._submit()
                return True
            except Exception:
                pass

        # dropdown fallback
        dropdown = await first_visible(self.page, S.CHATBOT_DROPDOWN, timeout_ms=800)
        if dropdown is not None:
            try:
                self.policy.require_mutation("naukri.screening.answer")
                await dropdown.select_option(label=value)
                await self._submit()
                return True
            except Exception:
                pass

        return await self._answer_combobox(value)

    async def _submit(self) -> bool:
        await self._dismiss_blocking_overlays()
        for selector in S.CHATBOT_SAVE + S.CHATBOT_SEND:
            try:
                locator = self.page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    try:
                        await locator.click(force=True, timeout=2_500)
                    except Exception:
                        await locator.evaluate("el => el.click()")
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

            ok = False
            if question.kind == "combobox":
                ok = await self._answer_combobox(resolved.value)
            elif question.kind in ("text", "unknown"):
                ok = await self._answer_text(resolved.value)
            else:
                ok = await self._answer_option(resolved.value, question.kind)

            if not ok:
                # Robust fallback cascade across all widget types
                if question.kind in ("text", "unknown"):
                    ok = (
                        await self._answer_option(resolved.value, "radio")
                        or await self._answer_combobox(resolved.value)
                    )
                elif question.kind == "combobox":
                    ok = (
                        await self._answer_option(resolved.value, "radio")
                        or await self._answer_text(resolved.value)
                    )
                else:
                    ok = (
                        await self._answer_combobox(resolved.value)
                        or await self._answer_text(resolved.value)
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
