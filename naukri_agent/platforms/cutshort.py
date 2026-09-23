"""
Cutshort Platform Implementation (Direct-Action & Inbox Message Handler).
"""
from __future__ import annotations

import re
from collections.abc import Callable

from playwright.async_api import Page

from ..browser.artifacts import ArtifactStore
from ..browser.resilience import (
    first_visible,
    human_pause,
    human_type,
    safe_text,
    scroll_page,
)
from ..config import AgentConfig, JobProfile, NaukriAccount
from ..core.answers import AnswerEngine
from ..core.models import (
    ApplicationStatus,
    ApplyOutcome,
    FilterDecision,
    Job,
    ScreeningQuestion,
    SkipReason,
)
from ..core.run_policy import RunPolicy
from ..logging_setup import get_logger
from .base import BaseJobPlatform

log = get_logger(__name__)

CUTSHORT_AI_SKILLS: list[str | tuple[str, str]] = [
    "Python",
    "Generative AI",
    "Agentic AI",
    "Large Language Models (LLM)",
    ("RAG", "Retrieval Augmented Generation (RAG)"),
    ("Artificial Intelligence", "Artificial Intelligence (AI)"),
    "MLOps",
    ("tuning", "Large Language Models (LLM) tuning"),
    ("Bedrock", "AWS Bedrock"),
    ("FastAPI", "FastAPI"),
    "LangGraph",
    ("Prompt", "Prompt engineering"),
    ("Node", "NodeJS (Node.js)"),
    "TypeScript",
    "Javascript",
]

CUTSHORT_FULLSTACK_SKILLS: list[str | tuple[str, str]] = [
    ("React", "React.js"),
    ("Next", "NextJs (Next.js)"),
    "Javascript",
    ("Node", "NodeJS (Node.js)"),
    "TypeScript",
    "Python",
    "FastAPI",
]

# Unified automatically merges AI and Full Stack with zero duplication
CUTSHORT_UNIFIED_SKILLS: list[str | tuple[str, str]] = list(
    dict.fromkeys(CUTSHORT_AI_SKILLS + CUTSHORT_FULLSTACK_SKILLS)
)


async def _safe_click(loc) -> bool:
    """Click-or-JS-click without raising: questionnaire controls are best
    effort; callers decide what a missed click means."""
    try:
        await loc.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        await loc.click(force=True, timeout=1000)
        return True
    except Exception:
        pass
    try:
        await loc.evaluate("el => el.click()")
        return True
    except Exception as exc:
        log.debug("cutshort.click_failed", error=str(exc)[:80])
        return False


class CutshortChatbot:
    """Handles recruiter screening questions and quick-reply options in Cutshort messages."""

    def __init__(self, page: Page, answers: AnswerEngine, max_questions: int = 10):
        self.page = page
        self.answers = answers
        self.max_questions = max_questions

    def _match_chip(self, chip_text: str, resolved_val: str) -> bool:
        """Fuzzy matches a quick-reply chip against the resolved answer."""
        ct = chip_text.strip().lower()
        rv = resolved_val.strip().lower()
        if not ct or not rv:
            return False
        if ct == rv or rv in ct or ct in rv:
            return True
        if rv in ("yes", "true") and any(w in ct for w in ["yes", "agree", "comfortable", "available", "willing", "open", "ready", "works"]):
            return True
        if rv in ("no", "false") and any(w in ct for w in ["no", "never", "not"]):
            return True
        if "0" in rv or "immediate" in rv or "15" in rv:
            if any(w in ct for w in ["immediate", "served notice", "serving", "< 15", "0-15", "15 days", "0 days"]):
                return True
        return False

    def _extract_composite_answers(self, text: str) -> str | None:
        """
        Parses recruiter messages asking multiple screening questions at once
        and builds a polite, structured multi-part response.
        """
        norm_text = text.lower()
        needed_fields: list[tuple[str, str]] = []
        cfg = AgentConfig.load()
        phone = (cfg.applicant_phone or "").strip()
        email = (cfg.applicant_email or "").strip()

        # 1. Contact Number / Mobile / Phone / WhatsApp
        if phone and any(k in norm_text for k in ["contact number", "contact no", "mobile number", "mobile no", "phone number", "phone no", "whatsapp", "call you", "share your contact", "share your number", "share your phone"]):
            needed_fields.append(("Contact Number", phone))

        # 2. Email Address
        if email and any(k in norm_text for k in ["email address", "email id", "mail address", "mail id", "share your email", "share your mail"]):
            needed_fields.append(("Email", email))

        # 3. Notice period / LWD
        if any(k in norm_text for k in ["notice period", "notice", "how soon", "when can you join", "joining time", "lwd", "last working"]):
            resolved = self.answers.resolve(ScreeningQuestion(text="notice period", kind="unknown", options=[]))
            if resolved is not None:
                needed_fields.append(("Notice Period", resolved.value))

        # 4. Current CTC
        if any(k in norm_text for k in ["current ctc", "current salary", "cctc", "present ctc"]):
            resolved = self.answers.resolve(ScreeningQuestion(text="current ctc", kind="unknown", options=[]))
            if resolved is not None:
                needed_fields.append(("Current CTC", resolved.value))

        # 5. Expected CTC
        if any(k in norm_text for k in ["expected ctc", "expected salary", "ectc"]):
            resolved = self.answers.resolve(ScreeningQuestion(text="expected ctc", kind="unknown", options=[]))
            if resolved is not None:
                needed_fields.append(("Expected CTC", resolved.value))

        # 6. Total Experience
        if any(k in norm_text for k in ["total experience", "overall experience", "total exp", "years of experience"]):
            resolved = self.answers.resolve(ScreeningQuestion(text="total experience", kind="unknown", options=[]))
            if resolved is not None:
                needed_fields.append(("Total Experience", resolved.value))

        # 7. Relevant / Specific Skill Experience
        for skill in ["python", "fastapi", "ai", "llm", "rag", "react", "node", "typescript", "full stack"]:
            if f"experience in {skill}" in norm_text or f"{skill} experience" in norm_text:
                resolved = self.answers.resolve(ScreeningQuestion(text=f"experience in {skill}", kind="unknown", options=[]))
                if resolved:
                    needed_fields.append((f"Experience with {skill.title()}", f"{resolved.value} years"))

        # 8. Location / Relocation / Hybrid
        if any(k in norm_text for k in ["current location", "where are you based", "relocate", "relocation", "wfo", "work from office", "hybrid"]):
            resolved = self.answers.resolve(
                ScreeningQuestion(text=text[:250], kind="unknown", options=[])
            )
            if resolved is not None:
                needed_fields.append(("Location / Availability", resolved.value))

        if not needed_fields:
            single_res = self.answers.resolve(ScreeningQuestion(text=text[:250], kind="unknown", options=[]))
            if single_res:
                return single_res.value
            return None

        # Format structured reply
        reply_lines = ["Hi,\n\nPlease find the requested details below:"]
        for key, val in needed_fields:
            reply_lines.append(f"• {key}: {val}")
        reply_lines.append(f"\nPlease let me know if you need any additional information.\n\nBest regards,\n{(cfg.applicant_name or '').strip() or 'Applicant'}")
        return "\n".join(reply_lines)

    async def run(self) -> tuple[bool, int, str]:
        """Returns (success, questions_answered, error_message)."""
        answered = 0
        last_question = ""
        stagnant_rounds = 0

        for _ in range(self.max_questions):
            await human_pause(1200, 2000)

            # Phase 1: Cutshort Form-based Questionnaires (Textareas + Fieldset Radios)
            form_loc = self.page.locator("form:has(fieldset), form:has(textarea), form:has(button[type='submit'])").first
            form_present = await form_loc.count() > 0 and await form_loc.is_visible()

            # 1A. Textareas & Text Inputs inside form
            if form_present:
                textareas = await form_loc.locator("textarea:not([name='message']), input[type='text']:not([name='message'])").all()
                for ta in textareas:
                    if not await ta.is_visible():
                        continue
                    current_val = (await ta.input_value()).strip()
                    if current_val:
                        continue

                    # Extract question label
                    q_label = ""
                    try:
                        label_loc = ta.locator("xpath=ancestor::li[1]//label, xpath=preceding::label[1]").first
                        if await label_loc.count() > 0:
                            q_label = (await safe_text(label_loc)).strip()
                    except Exception:
                        pass
                    if not q_label:
                        try:
                            parent_li = ta.locator("xpath=ancestor::li[1]").first
                            if await parent_li.count() > 0:
                                q_label = (await safe_text(parent_li)).strip()
                        except Exception:
                            pass

                    q_label_low = q_label.lower()

                    if any(k in q_label_low for k in ["hardest", "challenging", "complex problem", "technical problem", "problems you have worked on"]):
                        # Fail closed: answer only from user config, never a
                        # hard-coded achievement narrative.
                        _hard = self.answers.resolve(ScreeningQuestion(text=q_label[:200], kind="text"))
                        ta_ans = str(_hard.value) if _hard and _hard.value else ""
                    elif any(k in q_label_low for k in ["technical skillset", "strong in", "strength", "skillsets do you consider"]):
                        # Fail closed: answer only from user config, never a
                        # hard-coded skill/tenure claim.
                        _stren = self.answers.resolve(ScreeningQuestion(text=q_label[:200], kind="text"))
                        ta_ans = str(_stren.value) if _stren and _stren.value else ""
                    elif any(k in q_label_low for k in ["phone", "mobile", "contact number", "contact no", "whatsapp", "call you"]):
                        ta_ans = (AgentConfig.load().applicant_phone or "").strip()
                    elif any(k in q_label_low for k in ["email", "mail id", "email address", "mail address"]):
                        ta_ans = (AgentConfig.load().applicant_email or "").strip()
                    elif any(k in q_label_low for k in ["ctc", "salary", "fixed", "variable", "in hand", "in-hand", "annual ctc", "compensation"]):
                        ctc_parts = []
                        for q_text, label in (("current ctc", "Current CTC"), ("expected ctc", "Expected CTC"), ("notice period", "Notice Period")):
                            part = self.answers.resolve(ScreeningQuestion(text=q_text, kind="text"))
                            if part and part.value:
                                ctc_parts.append(f"{label}: {part.value}")
                        ta_ans = ". ".join(ctc_parts)
                    elif any(k in q_label_low for k in ["docker", "kubernetes", "container"]):
                        # Fail closed: answer only from user config, never a
                        # hard-coded tenure claim.
                        _dock = self.answers.resolve(ScreeningQuestion(text=q_label[:200], kind="text"))
                        ta_ans = str(_dock.value) if _dock and _dock.value else ""
                    elif any(k in q_label_low for k in ["notice", "how soon", "when can you join", "joining date", "availability"]):
                        notice_res = self.answers.resolve(ScreeningQuestion(text="notice period", kind="text"))
                        ta_ans = notice_res.value if notice_res and notice_res.value else ""
                    elif any(k in q_label_low for k in ["location", "relocate", "relocation", "bangalore", "bengaluru", "mumbai", "hyderabad"]):
                        loc_res = self.answers.resolve(ScreeningQuestion(text=q_label[:200], kind="text"))
                        ta_ans = loc_res.value if loc_res and loc_res.value else ""
                    else:
                        resolved = self.answers.resolve(ScreeningQuestion(text=q_label[:200], kind="text"))
                        if resolved and resolved.value:
                            ta_ans = str(resolved.value)
                        else:
                            ta_ans = ""

                    if not (ta_ans or "").strip():
                        # Config holds no answer for this question: fail closed
                        # instead of submitting a guessed or literal response.
                        log.debug("cutshort.chatbot.no_config_answer", question=q_label[:60])
                        continue
                    try:
                        await ta.scroll_into_view_if_needed()
                        await human_pause(200, 400)
                        await ta.fill(ta_ans)
                        await human_pause(200, 400)
                        # Crucial: dispatch input & change so React synthetic state updates
                        await ta.dispatch_event("input")
                        await ta.dispatch_event("change")
                        log.info("cutshort.chatbot.answered_textarea", question=q_label[:60], chars=len(ta_ans))
                        answered += 1
                    except Exception as exc:
                        log.debug("cutshort.chatbot.textarea_fill_failed", error=str(exc))

            # 1B. Fieldset Radios inside form or page
            fieldsets = await self.page.locator("fieldset").all()
            if fieldsets:
                for fs in fieldsets:
                    if not await fs.is_visible():
                        continue
                    legend_el = fs.locator("legend").first
                    q_text = (await safe_text(legend_el)).strip() if await legend_el.count() > 0 else (await safe_text(fs)).strip()
                    if not q_text:
                        continue

                    options = await fs.locator("div[tabindex='0']").all()
                    if not options:
                        options = await fs.locator("div[role='radio'], label, button").all()
                    opt_texts = [(await safe_text(opt)).strip() for opt in options if (await safe_text(opt)).strip()]

                    # Display rows masquerading as questions (run 375: "Resume /
                    # Work experience / Current company / Current location" with
                    # a single echo option). Not choices — skip without failing,
                    # UNLESS an unchecked declaration checkbox needs ticking.
                    distinct_opts = {o.lower() for o in opt_texts}
                    if len(distinct_opts) <= 1:
                        boxes = await fs.locator("input[type='checkbox']").all()
                        ticked = False
                        for cb in boxes:
                            try:
                                if not await cb.is_visible() or await cb.is_checked():
                                    continue
                                cb_low = q_text.lower()
                                if any(k in cb_low for k in ("declar", "agree", "accept", "confirm", "certify", "consent", "acknowledge", "terms", "policy", "true and correct")):
                                    try:
                                        await cb.check(force=True)
                                    except Exception:
                                        await _safe_click(cb)
                                    await human_pause(200, 400)
                                    log.info("cutshort.chatbot.checked_declaration", question=q_text[:70])
                                    answered += 1
                                    ticked = True
                            except Exception:
                                continue
                        if not ticked:
                            log.debug("cutshort.chatbot.display_row_skipped", question=q_text[:70])
                        continue

                    # Specialization choice (run 375: Software development vs
                    # fullstack). Candidate is full-stack first: prefer the
                    # fullstack option explicitly instead of leaving it to the
                    # generic resolver, which has no specialization mapping.
                    if "specializ" in q_text.lower():
                        _pref = ["fullstack", "full stack", "software development", "backend", "frontend", "ai", "python"]
                        picked_spec = None
                        for want in _pref:
                            for opt in options:
                                if want in (await safe_text(opt)).strip().lower():
                                    picked_spec = opt
                                    break
                            if picked_spec is not None:
                                break
                        if picked_spec is not None:
                            await _safe_click(picked_spec)
                            log.info("cutshort.chatbot.answered_specialization", selected=(await safe_text(picked_spec)).strip()[:50])
                            await human_pause(200, 400)
                            answered += 1
                            continue

                    # Salary inputs disguised as degenerate radios (run 375:
                    # options ['INR','INR'] with current/expected text boxes).
                    # Fill from configured CTC answers — never fabricated.
                    if any(k in q_text.lower() for k in ["salary", "ctc", "lacs", "compensation", "drawn salary", "expected salary"]) and (
                        len(distinct_opts) <= 1 or all(o.lower() in ("inr", "rs", "₹", "lacs", "lpa") for o in opt_texts)
                    ):
                        sal_inputs = await fs.locator("input[type='text'], input[type='number'], input:not([type='radio']):not([type='checkbox']):not([type='hidden']):not([type='file']):not([type='submit'])").all()
                        sal_inputs = [i for i in sal_inputs if await i.is_visible()]
                        cur_res = self.answers.resolve(ScreeningQuestion(text="current ctc", kind="text", options=[]))
                        exp_res = self.answers.resolve(ScreeningQuestion(text="expected ctc", kind="text", options=[]))
                        cur_val = (cur_res.value if cur_res and cur_res.value else "").strip()
                        exp_val = (exp_res.value if exp_res and exp_res.value else "").strip()
                        filled = 0
                        for idx, inp in enumerate(sal_inputs[:2]):
                            want_val = cur_val if idx == 0 else exp_val
                            if not want_val:
                                continue
                            try:
                                cur = (await inp.input_value()).strip()
                                if cur:
                                    filled += 1
                                    continue
                                await inp.scroll_into_view_if_needed()
                                await inp.fill(want_val)
                                await human_pause(200, 400)
                                try:
                                    await inp.dispatch_event("input")
                                    await inp.dispatch_event("change")
                                except Exception:
                                    pass
                                log.info("cutshort.chatbot.answered_salary", question=q_text[:60], value=want_val)
                                filled += 1
                            except Exception as exc:
                                log.debug("cutshort.chatbot.salary_fill_failed", error=str(exc))
                        if filled:
                            answered += filled
                            continue

                    resolved = self.answers.resolve(ScreeningQuestion(text=q_text, kind="radio", options=opt_texts))
                    target_val = resolved.value if resolved else ""

                    for opt in options:
                        opt_text = (await safe_text(opt)).strip()
                        if target_val and (self._match_chip(opt_text, target_val) or opt_text.lower() == target_val.lower()):
                            try:
                                await opt.scroll_into_view_if_needed()
                                await opt.click(force=True, timeout=1000)
                            except Exception:
                                try:
                                    await opt.evaluate("el => el.click()")
                                except Exception:
                                    pass
                            log.info("cutshort.chatbot.answered_field", question=q_text[:70], selected=opt_text[:50])
                            await human_pause(200, 400)
                            answered += 1
                            break

            # 1C. Audio Questionnaire Challenge (e.g. "Tell us about a difficult project... Pick from 1 saved audios")
            audio_btn = await first_visible(
                self.page,
                [
                    "form [role='button']:has-text('Pick from')",
                    "[role='button']:has-text('Pick from')",
                    "button:has-text('Pick from')",
                    "div:has-text('Pick from'):has-text('saved audio')",
                    "[role='button']:has-text('saved audio')",
                    "button:has-text('saved audio')",
                ],
                timeout_ms=1500,
            )
            if audio_btn:
                try:
                    await audio_btn.scroll_into_view_if_needed()
                    await human_pause(400, 700)
                    try:
                        await audio_btn.click(force=True, timeout=2000)
                    except Exception:
                        await audio_btn.evaluate("el => el.click()")
                    log.info("cutshort.chatbot.clicked_saved_audio_picker")
                    await human_pause(1000, 1500)

                    # Check if an audio selection confirmation dialog/modal or list appears
                    confirm_audio_btn = await first_visible(
                        self.page,
                        [
                            "div[id='modal-root'] button:has-text('Select')",
                            "div[id='modal-root'] button:has-text('Choose')",
                            "div[id='modal-root'] button:has-text('Use')",
                            "div[id='modal-root'] button:has-text('Confirm')",
                            "button:has-text('Select audio')",
                            "button:has-text('Use this audio')",
                            "div[role='dialog'] button:has-text('Confirm')",
                            "div[role='dialog'] button:has-text('Select')",
                        ],
                        timeout_ms=2000,
                    )
                    if confirm_audio_btn:
                        try:
                            await confirm_audio_btn.click(force=True)
                        except Exception:
                            await confirm_audio_btn.evaluate("el => el.click()")
                        await human_pause(500, 1000)
                    answered += 1
                except Exception as exc:
                    log.warning("cutshort.chatbot.audio_picker_failed", error=str(exc))

            if form_present or fieldsets or audio_btn:
                submit_btn = await first_visible(
                    self.page,
                    [
                        "form button[type='submit']",
                        "form button:has-text('Submit')",
                        "button:has-text('Submit')",
                        "button:has-text('Send response')",
                        "button:has-text('Confirm')",
                        "button[type='submit']",
                    ],
                    timeout_ms=2500,
                )
                if submit_btn:
                    try:
                        await human_pause(800, 1200)
                        await submit_btn.scroll_into_view_if_needed()
                        try:
                            await submit_btn.click(force=True, timeout=1500)
                        except Exception:
                            await submit_btn.evaluate("el => el.click()")
                        try:
                            await self.page.evaluate("() => { const f = document.querySelector('form'); if(f) f.requestSubmit(); }")
                        except Exception:
                            pass
                        log.info("cutshort.chatbot.submitted_form", answered=answered)

                        # Await form submission completion & response recording
                        await human_pause(2500, 4000)
                        await first_visible(
                            self.page,
                            [
                                "text=Application sent",
                                "text=successfully applied",
                                "text=Applied successfully",
                                "text=Your response has been recorded",
                                "text=Thanks for your response",
                            ],
                            timeout_ms=3000,
                        )
                        return True, max(answered, 1), ""
                    except Exception as exc:
                        log.error("cutshort.chatbot.submit_failed", error=str(exc))
                else:
                    # If fieldsets or form is present, but no submit button exists,
                    # this form was already submitted or is read-only. Avoid looping in vain.
                    log.info("cutshort.chatbot.form_already_completed", answered=answered)
                    return True, answered, "Form already submitted / read-only"

            # Check if chat is already complete
            if await first_visible(
                self.page, [
                    "text=Application sent",
                    "text=successfully applied",
                    "text=Applied successfully",
                    "text=Your response has been recorded",
                ],
                timeout_ms=1000
            ):
                return True, answered, ""

            # Phase 2: Check for Quick-Reply Chips / Buttons on the screen
            chips = await self.page.locator(
                "button.chip, div[class*='quick-reply'], button[class*='option'], "
                "button[class*='choice'], button[class*='pill'], button[class*='btn-reply'], "
                "div[class*='Chip'], button[class*='Chip']"
            ).all()

            # Find incoming recruiter bubbles (excluding candidate's own pitch)
            incoming_bubbles = await self.page.locator(
                "div[class*='incoming'], div[class*='recruiter'], div[class*='bot-msg'], "
                "div[class*='chat-item']:not([class*='sent']):not([class*='outgoing']), "
                "div[class*='message-item']:not([class*='sent']):not([class*='outgoing']), "
                "div.message-content, div[class*='msg-text'], div[class*='message-text'], "
                "div[class*='chat-bubble'], div[class*='msg_body'], p[class*='message'], div[class*='chat_msg']"
            ).all()

            recruiter_text = ""
            for bubble in reversed(incoming_bubbles):
                b_text = (await safe_text(bubble)).strip()
                if not b_text:
                    continue
                # Ignore candidate's own pitch or name signatures
                if b_text.startswith("Hi ") and "I came across the" in b_text:
                    continue
                if (AgentConfig.load().applicant_name or "Applicant") in b_text and len(b_text) > 100:
                    continue
                recruiter_text = b_text
                break

            if not recruiter_text and not chips and not fieldsets:
                stagnant_rounds += 1
                if stagnant_rounds > 2:
                    break
                continue

            if recruiter_text == last_question and not chips and not fieldsets:
                stagnant_rounds += 1
                if stagnant_rounds > 2:
                    return False, answered, "Questionnaire stalled without a completion marker"
                continue

            last_question = recruiter_text
            stagnant_rounds = 0
            answered_successfully = False

            # Step 1: If interactive option chips / choice buttons are present
            if chips:
                chip_options: list[str] = []
                for chip in chips:
                    txt = (await safe_text(chip)).strip()
                    if txt:
                        chip_options.append(txt)

                prompt_text = recruiter_text or "screening question"
                resolved = self.answers.resolve(ScreeningQuestion(text=prompt_text, kind="radio", options=chip_options))
                target_val = resolved.value if resolved else ""

                for chip in chips:
                    chip_text = (await safe_text(chip)).strip()
                    if target_val and (self._match_chip(chip_text, target_val) or chip_text.lower() == target_val.lower()):
                        await chip.click()
                        await human_pause(800, 1500)
                        answered_successfully = True
                        answered += 1
                        break

                if answered_successfully:
                    submit_btn = await first_visible(
                        self.page,
                        [
                            "button:has-text('Submit')",
                            "button:has-text('Send')",
                            "button:has-text('Confirm')",
                            "button:has-text('Next')",
                            "button:has-text('Save')",
                            "button[type='submit']",
                        ],
                        timeout_ms=1000
                    )
                    if submit_btn:
                        try:
                            await submit_btn.click()
                            await human_pause(800, 1500)
                        except Exception:
                            pass

            # Step 2: Handle single in-chat text inputs for specific questions
            if not answered_successfully:
                chat_inputs = await self.page.locator("div[class*='chat'] input[type='text'], div[class*='message'] input[type='text'], div[class*='questionnaire'] input").all()
                for inp in chat_inputs:
                    if await inp.is_visible():
                        placeholder = (await inp.get_attribute("placeholder") or "").lower()
                        name_attr = (await inp.get_attribute("name") or "").lower()
                        combined = f"{placeholder} {name_attr} {recruiter_text}".lower()
                        ans_val = None
                        if any(k in combined for k in ["phone", "mobile", "contact", "whatsapp"]):
                            ans_val = (AgentConfig.load().applicant_phone or "").strip()
                        elif any(k in combined for k in ["email", "mail id"]):
                            ans_val = (AgentConfig.load().applicant_email or "").strip()
                        elif any(k in combined for k in ["notice", "how soon", "when can you join", "lwd"]):
                            notice_res = self.answers.resolve(ScreeningQuestion(text="notice period", kind="text", options=[]))
                            ans_val = notice_res.value if notice_res and notice_res.value else None
                        elif any(k in combined for k in ["expected ctc", "ectc"]):
                            exp_res = self.answers.resolve(ScreeningQuestion(text="expected ctc", kind="text", options=[]))
                            ans_val = exp_res.value if exp_res and exp_res.value else None
                        elif any(k in combined for k in ["current ctc", "cctc"]):
                            cur_res = self.answers.resolve(ScreeningQuestion(text="current ctc", kind="text", options=[]))
                            ans_val = cur_res.value if cur_res and cur_res.value else None
                        else:
                            resolved = self.answers.resolve(ScreeningQuestion(text=f"{placeholder} {recruiter_text}", kind="text", options=[]))
                            if resolved:
                                ans_val = resolved.value

                        if ans_val:
                            await human_type(inp, ans_val)
                            await human_pause(300, 600)
                            answered_successfully = True
                            answered += 1

                if answered_successfully:
                    submit_btn = await first_visible(
                        self.page,
                        ["button:has-text('Submit')", "button:has-text('Send')", "button:has-text('Confirm')"],
                        timeout_ms=1000
                    )
                    if submit_btn:
                        try:
                            await submit_btn.click()
                            await human_pause(800, 1500)
                        except Exception:
                            pass

            # Step 3: Handle direct chat textarea / input box for recruiter messages
            text_input = await first_visible(
                self.page,
                [
                    "textarea[name='message']",
                    "textarea[placeholder*='message' i]",
                    "textarea[placeholder*='reply' i]",
                    "textarea",
                    "input[type='text'][placeholder*='message' i]",
                    "div[contenteditable='true']",
                ],
                timeout_ms=1500
            )

            if not answered_successfully and (recruiter_text or text_input):
                composite_reply = self._extract_composite_answers(recruiter_text or "recruiter message")
                if not composite_reply and recruiter_text:
                    single_res = self.answers.resolve(ScreeningQuestion(text=recruiter_text, kind="text", options=[]))
                    if single_res:
                        composite_reply = single_res.value

                if composite_reply and text_input:
                    await human_type(text_input, composite_reply)
                    await human_pause(400, 800)
                    send_btn = await first_visible(
                        self.page,
                        [
                            "button:has-text('Send')",
                            "button[aria-label='Send']",
                            "button[class*='send' i]",
                            "button[type='submit']",
                        ],
                        timeout_ms=1500
                    )
                    if send_btn:
                        await send_btn.click()
                    else:
                        await text_input.press("Enter")
                    await human_pause(1000, 2000)
                    answered_successfully = True
                    answered += 1

            if not answered_successfully and not fieldsets:
                return False, answered, f"Could not answer recruiter message: {recruiter_text[:80]}"

        return True, answered, ""

def _parse_cutshort_salary(text: str) -> tuple[float | None, float | None]:
    if not text:
        return None, None
    m_yr = re.search(r"₹?\s*([\d\.]+)\s*[lL]?\s*-\s*₹?\s*([\d\.]+)\s*[lL](?:\s*/\s*yr)?", text, re.IGNORECASE)
    if m_yr:
        try:
            return float(m_yr.group(1)), float(m_yr.group(2))
        except ValueError:
            pass
    m_single = re.search(r"₹?\s*([\d\.]+)\s*[lL](?:\s*/\s*yr)?", text, re.IGNORECASE)
    if m_single:
        try:
            v = float(m_single.group(1))
            return v, v
        except ValueError:
            pass
    m_mo = re.search(r"₹?\s*([\d,]+)\s*[–-]\s*₹?\s*([\d,]+)\s*(?:per month|/month)", text, re.IGNORECASE)
    if m_mo:
        try:
            min_mo = float(m_mo.group(1).replace(",", ""))
            max_mo = float(m_mo.group(2).replace(",", ""))
            return round((min_mo * 12) / 100000.0, 2), round((max_mo * 12) / 100000.0, 2)
        except ValueError:
            pass
    return None, None


def _parse_cutshort_experience(text: str) -> tuple[float | None, float | None]:
    if not text:
        return None, None
    m = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*yrs?", text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except ValueError:
            pass
    m_single = re.search(r"(?:\+?\s*(\d+(?:\.\d+)?)\s*yrs?\+?|(\d+(?:\.\d+)?)\s*\+\s*yrs?)", text, re.IGNORECASE)
    if m_single:
        try:
            val_str = m_single.group(1) or m_single.group(2)
            if val_str:
                return float(val_str), None
        except ValueError:
            pass
    return None, None


_POSTED_AGO_RE = re.compile(
    r"(\d+)\s*(hours?|hrs?|h|days?|d|weeks?|w|months?|mos?|years?|y)\s*ago|\bjust now\b|\btoday\b|\byesterday\b",
    re.IGNORECASE,
)


def _parse_cutshort_posted(text: str) -> int | None:
    """Relative posted age on the card ('3d ago' -> 3). None when unstated.

    Populates `posted_days_ago` so the configured `max_posted_days` gate
    enforces freshness even if the feed's 1-week UI filter silently fails.
    """
    if not text:
        return None
    low = text.lower()
    if any(k in low for k in ("just now", "few minutes", "today", "hour")):
        return 0
    if "yesterday" in low:
        return 1
    m = _POSTED_AGO_RE.search(low)
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


class CutshortPlatform(BaseJobPlatform):
    def __init__(self, page: Page, account: NaukriAccount, artifacts: ArtifactStore, answers: AnswerEngine, policy: RunPolicy):
        super().__init__(page, account.key, policy)
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

            for _ in range(90):
                await human_pause(1000, 1000)
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
    async def _apply_ui_filters(self, profile: JobProfile) -> None:
        """
        Applies verified native UI filters on Cutshort's All Jobs toolbar:
        1. Hiring Activity: In the last 1 week (active/fresh postings)
        2. Role Type: Full time
        3. Minimum Salary: >= 5 LPA (slider set to 500,000 INR)
        4. Experience Range: 0.0 – 3.5 years (slider dual-handles [0.0, 3.5])
        5. Job Category: Expands accordions and selects verified specialisations:
           - Full Stack: Software Development (Frontend, Backend, Full Stack),
                         Testing / QA (Automation Testing, QA),
                         DevOps & IT (DevOps, Site Reliability)
           - AI / Python: Data Science & Analytics (Data Science, ML Engineering, Data Analytics),
                          Software Development (Backend)
        6. Location & Skills: Intentionally untouched so candidate pool is not overly constrained.
        """
        log.info("cutshort.filters.applying_ui", profile=profile.name)

        # 1. Hiring activity on job ("In the last 1 week")
        try:
            btn = await first_visible(self.page, ["div[data-intercom-target='hiringActivityOnJob-filter']"], timeout_ms=3000)
            if btn:
                await btn.click(force=True)
                await human_pause(500, 800)
                popover = self.page.locator(".react-tiny-popover-container")

                # Click "In the last"
                radio = popover.locator("text='In the last'").first
                if await radio.is_visible():
                    await radio.click(force=True)
                    await human_pause(200, 400)

                # Set number input to 1 week of active hiring to guarantee top-notch freshness
                num_input = popover.locator("input[type='number']").first
                if await num_input.is_visible():
                    await num_input.fill("1")
                    await human_pause(200, 400)


                # If unit dropdown shows Months or Days, switch to Weeks
                trigger = popover.locator(".component_select_trigger_wrapper").first
                if await trigger.is_visible():
                    cur_unit = (await trigger.inner_text()).strip()
                    if cur_unit != "Weeks":
                        await trigger.click(force=True)
                        await human_pause(300, 500)
                        week_opt = self.page.locator("text='Weeks'").first
                        if await week_opt.is_visible():
                            await week_opt.click(force=True)
                            await human_pause(200, 400)

                await self.page.keyboard.press("Escape")
                await human_pause(400, 600)
                log.info("cutshort.filters.hiring_activity_applied")
        except Exception as exc:
            log.warning("cutshort.filters.activity_error", error=str(exc))

        # 2. Type of role (Full time)
        try:
            btn = await first_visible(self.page, ["div[data-intercom-target='roletype-filter']"], timeout_ms=2500)
            if btn:
                await btn.click(force=True)
                await human_pause(500, 800)
                opt = await first_visible(
                    self.page,
                    [
                        "label:has-text('Full time')",
                        "button:has-text('Full time')",
                        "span:has-text('Full time')",
                        "div:has-text('Full time')",
                    ],
                    timeout_ms=1500,
                )
                if opt:
                    await opt.click(force=True)
                    await human_pause(400, 600)
                await self.page.keyboard.press("Escape")
                await human_pause(400, 600)
                log.info("cutshort.filters.role_type_applied")
        except Exception as exc:
            log.warning("cutshort.filters.roletype_error", error=str(exc))

        # 3. Minimum salary (>= 5 LPA)
        min_salary_lpa = profile.filters.min_salary_lpa if (profile.filters and profile.filters.min_salary_lpa) else 5.0
        if min_salary_lpa and min_salary_lpa >= 5.0:
            try:
                btn = await first_visible(self.page, ["div[data-intercom-target='minsal-filter']"], timeout_ms=2500)
                if btn:
                    await btn.click(force=True)
                    await human_pause(500, 800)
                    slider = await first_visible(self.page, ["div[role='slider']"], timeout_ms=1500)
                    if slider:
                        await slider.focus()
                        await self.page.keyboard.press("Home")
                        await human_pause(150, 300)
                        # Step up to desired LPA (each step is 1 LPA / 100,000 INR)
                        steps = int(round(min_salary_lpa))
                        for _ in range(steps):
                            await self.page.keyboard.press("ArrowRight")
                            await human_pause(80, 150)
                        await human_pause(400, 600)
                    await self.page.keyboard.press("Escape")
                    await human_pause(400, 600)
                    log.info("cutshort.filters.minsal_applied", min_salary_lpa=min_salary_lpa)
            except Exception as exc:
                log.warning("cutshort.filters.minsal_error", error=str(exc))

        # 4. Experience range (0.0 – 3.5 years)
        try:
            btn = await first_visible(self.page, ["div[data-intercom-target='expRange-filter']"], timeout_ms=2500)
            if btn:
                await btn.click(force=True)
                await human_pause(500, 800)
                sliders = await self.page.locator("div[role='slider']").all()
                if len(sliders) >= 2:
                    min_slider = sliders[0]
                    max_slider = sliders[1]

                    # Ensure min handle is at 0
                    await min_slider.focus()
                    await self.page.keyboard.press("Home")
                    await human_pause(150, 300)

                    # Reset max handle to 0, then step to 3.5 years (7 steps of 0.5y)
                    await max_slider.focus()
                    await self.page.keyboard.press("Home")
                    await human_pause(150, 300)
                    for _ in range(7):
                        await self.page.keyboard.press("ArrowRight")
                        await human_pause(70, 130)
                    await human_pause(400, 600)

                await self.page.keyboard.press("Escape")
                await human_pause(400, 600)
                log.info("cutshort.filters.exp_range_applied", min_exp=0.0, max_exp=3.5)
        except Exception as exc:
            log.warning("cutshort.filters.exp_range_error", error=str(exc))

        # 5. Native Skills Filter (skills-filter) with exact autocomplete options
        try:
            btn = await first_visible(self.page, ["div[data-intercom-target='skills-filter']"], timeout_ms=3000)
            if btn:
                await btn.click(force=True)
                await human_pause(600, 1000)
                popover = self.page.locator(".react-tiny-popover-container").first

                # Clear existing skills filter if any active from previous pass
                clear_btn = popover.locator("text='Clear this filter'").first
                if await clear_btn.is_visible():
                    await clear_btn.click(force=True)
                    await human_pause(400, 600)

                prof_low = profile.name.lower()
                is_unified = ("unified" in prof_low) or ("ai" in prof_low and "full stack" in prof_low)
                if is_unified:
                    skills_to_select = CUTSHORT_UNIFIED_SKILLS
                elif "ai" in prof_low or "python" in prof_low:
                    skills_to_select = CUTSHORT_AI_SKILLS
                else:
                    skills_to_select = CUTSHORT_FULLSTACK_SKILLS

                input_el = popover.locator("input[placeholder*='skill' i]").first
                for item in skills_to_select:
                    query, exact_name = item if isinstance(item, tuple) else (item, item)
                    try:
                        await input_el.click()
                        await input_el.fill("")
                        await human_type(input_el, query)
                        await human_pause(500, 800)
                        flyout = popover.locator(".flyout_el_wrapper").first
                        if await flyout.count() > 0:
                            options = await flyout.locator("div, label, span").all()
                            for opt in options:
                                if (await safe_text(opt)).strip() == exact_name:
                                    await opt.click(force=True)
                                    await human_pause(350, 550)
                                    log.debug("cutshort.filters.skill_selected", skill=exact_name)
                                    break
                    except Exception as skill_err:
                        log.debug("cutshort.filters.skill_selection_failed", skill=exact_name, error=str(skill_err))

                await human_pause(400, 600)
                # Close popover by clicking filter button again
                await btn.click(force=True)
                await human_pause(600, 1000)
                log.info("cutshort.filters.skills_applied", profile=profile.name, count=len(skills_to_select))
        except Exception as exc:
            log.warning("cutshort.filters.skills_error", error=str(exc))

        log.info("cutshort.filters.applied_ui_successfully")
        await human_pause(2000, 3000)

    async def _set_recommendation_switch(self, want_on: bool) -> None:
        """Set the All-jobs recommendation switch ON (curated feed) or OFF
        (full inventory). Best-effort: a missing switch leaves the view as-is."""
        switch_el = await first_visible(
            self.page,
            [
                "input[role='switch']",
                "label:has(input[role='switch'])",
                "div:has-text('Turn it OFF to view all jobs') input[role='switch']",
            ],
            timeout_ms=3000,
        )
        if not switch_el:
            return
        try:
            is_checked = await switch_el.is_checked()
        except Exception:
            is_checked = True
        if is_checked == want_on:
            return
        try:
            await switch_el.scroll_into_view_if_needed()
            await human_pause(300, 600)
            try:
                await switch_el.click(force=True)
            except Exception:
                await switch_el.evaluate("el => el.click()")
            await human_pause(2000, 3000)
        except Exception as exc:
            log.debug("cutshort.switch_toggle_failed", want_on=want_on, error=str(exc))

    async def _scroll_feed(self, job_link_sel: str, target_pool: int, tab_name: str):
        """Infinite-scroll the current feed view until the pool target or a
        stagnant end-of-feed. Returns the collected link anchors."""
        last_link_count = 0
        stagnant_scrolls = 0
        while True:
            anchors = await self.page.locator(job_link_sel).all()
            link_count = len(anchors)
            if link_count >= target_pool:
                log.info("cutshort.fetch.target_pool_reached", tab=tab_name, count=link_count, target=target_pool)
                break
            if link_count == last_link_count:
                stagnant_scrolls += 1
                if stagnant_scrolls >= 5:
                    log.info("cutshort.fetch.stagnant_end_of_feed", tab=tab_name, count=link_count)
                    break
                # Scroll up slightly and then back down to kick intersection observer
                try:
                    await self.page.evaluate("window.scrollBy(0, -600)")
                    await human_pause(400, 700)
                except Exception:
                    pass
            else:
                stagnant_scrolls = 0
                last_link_count = link_count

            if anchors:
                try:
                    await anchors[-1].scroll_into_view_if_needed(timeout=2000)
                    await anchors[-1].hover(timeout=1000)
                except Exception:
                    pass

            # Deep scroll to document bottom to trigger infinite lazy load
            try:
                await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass
            await scroll_page(self.page, steps=4, delay_s=0.5)
            await human_pause(1500, 2500)

        anchors = await self.page.locator(job_link_sel).all()
        log.info("cutshort.fetch.cards_found", tab=tab_name, count=len(anchors))
        return anchors

    async def _parse_feed_anchors(
        self,
        anchors,
        tab_name: str,
        exclude_job_ids: set[str],
        seen_urls: set[str],
        jobs: list[Job],
    ) -> None:
        """Parse job cards from scrolled anchors into Job objects (shared by
        the recommended pass and the all-jobs pass). Appends in place so the
        recommended pass keeps global positions 1..N."""
        for anchor in anchors:
            try:
                url = (await anchor.get_attribute("href")) or ""
                if "/job/" not in url:
                    continue
                if url.startswith("/"):
                    url = f"https://cutshort.io{url}"
                if url.rstrip("/") in seen_urls:
                    continue

                title = (await safe_text(anchor)).strip()
                if not title:
                    log.debug(
                        "cutshort.fetch.empty_title_skipped",
                        url=url[:120],
                    )
                    continue
                seen_urls.add(url.rstrip("/"))

                company = ""
                desc = ""
                card_text = ""
                card = anchor.locator(
                    "xpath=ancestor::div[descendant::a[contains(@href,'/company/')]][1]"
                ).first
                if await card.count() > 0:
                    company = (await safe_text(card.locator("a[href*='/company/']").first)).strip()
                    prose_el = card.locator("div.prose").first
                    if await prose_el.count() > 0:
                        desc = (await safe_text(prose_el)).strip()

                # Comprehensive outer card containing action buttons and metadata (location, exp, salary)
                full_card = anchor.locator(
                    "xpath=ancestor::div[(contains(.,'yrs') or contains(.,'yr') or contains(.,'Exp')) and (descendant::button[contains(.,'Apply') or contains(.,'View') or contains(.,'Applied')])][1]"
                ).first
                if await full_card.count() == 0:
                    full_card = anchor.locator(
                        "xpath=ancestor::div[descendant::button[contains(.,'Apply') or contains(.,'View') or contains(.,'Applied')]][last()]"
                    ).first

                if await full_card.count() > 0:
                    card_text = (await safe_text(full_card)).strip()
                elif await card.count() > 0:
                    card_text = (await safe_text(card)).strip()

                min_sal, max_sal = _parse_cutshort_salary(card_text)
                min_exp, max_exp = _parse_cutshort_experience(card_text)
                posted_days = _parse_cutshort_posted(f"{title} {card_text}")

                match = re.search(r"-([a-zA-Z0-9]+)$", url)
                job_id = f"cutshort-{match.group(1)}" if match else f"cutshort-{Job.stable_id(url, title, company)}"

                if job_id in exclude_job_ids:
                    continue

                jobs.append(
                    Job(
                        job_id=job_id,
                        title=title,
                        company=company,
                        url=url,
                        description=desc,
                        min_salary_lpa=min_sal,
                        max_salary_lpa=max_sal,
                        min_experience=min_exp,
                        max_experience=max_exp,
                        posted_days_ago=posted_days,
                        recommendation_tab=tab_name,
                        recommendation_position=len(jobs) + 1,
                        platform="cutshort",
                    )
                )
            except Exception as exc:
                log.debug("cutshort.fetch.parse_error", error=str(exc))
                continue

    async def fetch_jobs(self, profile: JobProfile, exclude_job_ids: set[str]) -> list[Job]:
        log.info("cutshort.fetch.start", profile=profile.name)
        await self.page.goto("https://cutshort.io/profile/all-jobs", wait_until="domcontentloaded")
        await human_pause(2000, 3000)

        job_link_sel = "a[href*='/job/']"
        jobs: list[Job] = []
        seen_urls: set[str] = set()

        # PASS 1 — recommended feed first (switch ON): Cutshort's own
        # "recently active" curation outranks the raw grid. Bounded small.
        await self._set_recommendation_switch(True)
        await human_pause(1000, 2000)
        reco_anchors = await self._scroll_feed(job_link_sel, 50, "recommended")
        await self._parse_feed_anchors(reco_anchors, "recommended", exclude_job_ids, seen_urls, jobs)
        log.info("cutshort.fetch.recommended_done", count=len(jobs))

        # PASS 2 — full inventory overflow (switch OFF) + native UI filters.
        await self._set_recommendation_switch(False)
        await self._apply_ui_filters(profile)
        target_pool = max(100, min(profile.platform_limits.get(self.platform_name, 40) * 3, 150))
        all_anchors = await self._scroll_feed(job_link_sel, target_pool, "all_jobs")
        await self._parse_feed_anchors(all_anchors, "all_jobs", exclude_job_ids, seen_urls, jobs)

        # Dump AFTER scrolling so the artifact reflects what was actually parsed
        try:
            dump_path = self.artifacts.dir / "cutshort-dashboard.html"
            dump_path.write_text(await self.page.content(), encoding="utf-8")
            log.info("cutshort.dashboard_html_saved", path=str(dump_path))
        except Exception as exc:
            log.debug("cutshort.dashboard_dump_failed", error=str(exc))

        log.info("cutshort.fetch.done", count=len(jobs))
        return jobs

    async def _cloudflare_wall(self, *, blocked: bool = False) -> bool:
        """True when a Cloudflare Turnstile challenge is intercepting the page.

        Provenance: runs 375/376 failure dumps show an empty modal-root plus
        a cf-challenge widget and zero textareas — in that state no pitch
        modal can ever appear, so every queued job would burn ~30s into a
        wall. `blocked=True` (nothing actionable was found) also counts when
        challenge markers are present, covering embedded/invisible challenges.
        """
        try:
            html = (await self.page.content()).lower()
        except Exception:
            return False
        if not any(
            marker in html
            for marker in (
                "cf-turnstile",
                "cf-chl",
                "challenges.cloudflare.com",
                "verifying you are human",
            )
        ):
            return False
        if not blocked:
            widget = await first_visible(
                self.page,
                [
                    "iframe[src*='challenges.cloudflare.com']",
                    "input[name='cf-turnstile-response']",
                ],
                timeout_ms=800,
            )
            return widget is not None
        return True

    async def apply_to_job(
        self,
        job: Job,
        profile_name: str,
        pre_submit_check: Callable[[Job], FilterDecision] | None = None,
    ) -> ApplyOutcome:
        self.require_mutation("application.apply_flow")
        log.info("cutshort.apply.start", job_id=job.job_id)

        raw_id = job.job_id.replace("cutshort-", "")

        # 1. Clean slate: dismiss any stale modal or backdrop from a previous job
        try:
            old_modal = await first_visible(self.page, ["#modal__content", "div[role='dialog']"], timeout_ms=300)
            if old_modal:
                await self.page.keyboard.press("Escape")
                await human_pause(300, 600)
        except Exception:
            pass

        # 1b. Cloudflare wall pre-flight: when the challenge widget is up, no
        # pitch modal can ever appear. Fail fast with a distinct reason so the
        # run pauses the platform instead of burning every queued job.
        if await self._cloudflare_wall():
            log.warning("cutshort.apply.cloudflare_wall", job_id=job.job_id)
            return ApplyOutcome(
                status=ApplicationStatus.FAILED,
                reason=SkipReason.CLOUDFLARE_CHALLENGE,
                detail="Cloudflare Turnstile challenge is intercepting Cutshort; pitch modal cannot appear",
            )

        modal = None

        # 2. Try locating the job card in the current feed
        anchor = await first_visible(self.page, [f"a[href*='{raw_id}']"], timeout_ms=2000)
        if not anchor and job.recommendation_position:
            try:
                all_links = self.page.locator("a[href*='/job/']")
                pos = job.recommendation_position - 1
                if pos < await all_links.count():
                    candidate_link = all_links.nth(pos)
                    if raw_id in ((await candidate_link.get_attribute("href")) or ""):
                        anchor = candidate_link
            except Exception:
                pass

        if anchor:
            card = anchor.locator(
                "xpath=ancestor::div[descendant::button[contains(normalize-space(.),'Apply')"
                " or contains(normalize-space(.),'Interested')"
                " or contains(normalize-space(.),'Applied')]"
                " or descendant::a[contains(normalize-space(.),'View conversation')]][1]"
            ).first
            if await card.count() > 0:
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
                    card,
                    [
                        "button:has-text('Apply now')",
                        "button:has-text('Apply to this job')",
                        "button[label*='Apply to this job']",
                        "button:has-text('Apply')",
                        "button[label*='Apply']",
                        "button:has-text('Interested')",
                        "button[label*='Interested']",
                    ],
                    timeout_ms=2500,
                )
                if not apply_btn and await first_visible(card, ["button:has-text('Applied')", "a:has-text('View conversation')", "button:has-text('View conversation')"]):
                    return ApplyOutcome(ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)

                if apply_btn:
                    try:
                        await apply_btn.scroll_into_view_if_needed(timeout=1500)
                    except Exception:
                        pass
                    try:
                        await apply_btn.click(force=True, timeout=3000)
                    except Exception:
                        await apply_btn.evaluate("el => el.click()")
                    await human_pause(1500, 2500)

                    modal = await first_visible(
                        self.page,
                        [
                            "#modal__content",
                            "div.modal__wrapper",
                            "div[role='dialog']",
                            "div#modal-root > div",
                            "div#modal-root div[class*='Modal']",
                            "div#modal-root div[class*='modal']",
                            "div.modal-content",
                            "div[class*='modal']",
                            "form:has(textarea)",
                            "div:has(> textarea[name='message'])",
                        ],
                        timeout_ms=3500,
                    )

        # 3. Fallback or Dedicated Flow: If modal didn't open from feed card, navigate to job.url
        if not modal:
            if not (job.url and job.url.startswith("http")):
                return ApplyOutcome(
                    status=ApplicationStatus.SKIPPED,
                    reason=SkipReason.STALE_JOB,
                    detail=f"No feed card or URL for {job.title} at {job.company}",
                )

            log.info("cutshort.apply.navigating_to_job_url", job_id=job.job_id, url=job.url)
            try:
                await self.page.goto(job.url, wait_until="domcontentloaded")
                await human_pause(1500, 2500)
            except Exception as exc:
                return ApplyOutcome(ApplicationStatus.FAILED, detail=f"Failed to load job page: {str(exc)[:150]}")

            if pre_submit_check:
                decision = pre_submit_check(job)
                if not decision.passed:
                    return ApplyOutcome(ApplicationStatus.SKIPPED, reason=decision.reason, detail=decision.detail)

            # Check for Already Applied on dedicated job page
            already_applied_marker = await first_visible(
                self.page,
                [
                    "button:has-text('Applied')",
                    "a:has-text('View conversation')",
                    "button:has-text('View conversation')",
                    "text=Already applied",
                    "text=You have already applied",
                    "div:has-text('Already applied')",
                ],
                timeout_ms=1500,
            )
            if already_applied_marker:
                log.info("cutshort.apply.already_applied_on_job_page", job_id=job.job_id)
                return ApplyOutcome(ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)

            # Check for Inactive / Closed Job on dedicated job page
            closed_marker = await first_visible(
                self.page,
                [
                    "text=no longer active",
                    "text=job has expired",
                    "text=applications are closed",
                    "text=paused hiring",
                    "text=Job is closed",
                    "text=This job is no longer available",
                ],
                timeout_ms=1200,
            )
            if closed_marker:
                log.info("cutshort.apply.job_closed_on_page", job_id=job.job_id)
                return ApplyOutcome(ApplicationStatus.SKIPPED, reason=SkipReason.STALE_JOB, detail="Job posting is no longer active / closed on Cutshort")

            # Step 3A: On dedicated job page (cutshort.io/job/...), click primary in-viewport CTA button
            # The main job's primary CTA is 'Apply to this job' (in header & floating bar).
            # We prioritize 'Apply to this job' so we never accidentally click 'Apply now' on 'Similar jobs' below!
            target_btn = await first_visible(
                self.page,
                [
                    "button:has-text('Apply to this job')",
                    "a:has-text('Apply to this job')",
                    "button:has-text('Apply now')",
                    "button:has-text('Apply')",
                ],
                timeout_ms=3000,
            )

            if target_btn:
                try:
                    await target_btn.scroll_into_view_if_needed(timeout=1500)
                except Exception:
                    pass
                try:
                    await target_btn.click(force=True, timeout=3000)
                except Exception:
                    await target_btn.evaluate("el => el.click()")
                await human_pause(1500, 2500)

            # Check if modal appeared directly from Step 3A CTA click
            modal = await first_visible(
                self.page,
                [
                    "#modal__content",
                    "div.modal__wrapper",
                    "div[role='dialog']",
                    "div#modal-root > div",
                    "div#modal-root div[class*='Modal']",
                    "div#modal-root div[class*='modal']",
                    "div.modal-content",
                    "div[class*='modal']",
                    "form:has(textarea)",
                    "div:has(> textarea[name='message'])",
                ],
                timeout_ms=3000,
            )

            # Step 3B: Only if modal did NOT appear and the page redirected to /profile/all-jobs?jobid=...
            # check the feed card and click its 'Apply now' button
            if not modal and "/profile/all-jobs" in self.page.url:
                feed_apply = await first_visible(
                    self.page,
                    [
                        "button:has-text('Apply now')",
                        "button:has-text('Apply to this job')",
                        "button[label*='Apply to this job']",
                    ],
                    timeout_ms=3000,
                )
                if not feed_apply:
                    feed_already = await first_visible(
                        self.page,
                        [
                            "button:has-text('Applied')",
                            "a:has-text('View conversation')",
                            "button:has-text('View conversation')",
                            "text=Already applied",
                        ],
                        timeout_ms=1500,
                    )
                    if feed_already:
                        log.info("cutshort.apply.already_applied_on_feed_redirect", job_id=job.job_id)
                        return ApplyOutcome(ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)

                if feed_apply:
                    try:
                        await feed_apply.scroll_into_view_if_needed(timeout=1500)
                    except Exception:
                        pass
                    try:
                        await feed_apply.click(force=True, timeout=3000)
                    except Exception:
                        await feed_apply.evaluate("el => el.click()")
                    await human_pause(1500, 2500)

                    modal = await first_visible(
                        self.page,
                        [
                            "#modal__content",
                            "div.modal__wrapper",
                            "div[role='dialog']",
                            "div#modal-root > div",
                            "div#modal-root div[class*='Modal']",
                            "div#modal-root div[class*='modal']",
                            "div.modal-content",
                            "div[class*='modal']",
                            "form:has(textarea)",
                            "div:has(> textarea[name='message'])",
                        ],
                        timeout_ms=4000,
                    )

        modal_text = (await safe_text(modal)).strip() if modal else ""

        # Dynamic Per-Job Resume Selection based on job engineering track
        try:
            from ..config import PROJECT_ROOT
            cfg = AgentConfig.load()

            text_for_resume = f"{job.title} {modal_text} {job.description}".lower()
            is_ai_job = any(k in text_for_resume for k in ["ai", "llm", "genai", "gpt", "machine learning", "ml", "rag", "agent", "prompt", "nlp", "chatbot", "fastapi"])

            # Map to target resume tailored for this specific job track.
            # Filenames resolve from config.yaml per profile — never hardcoded.
            profiles = [
                p for p in (getattr(cfg, "profiles", []) or [])
                if p.enabled and getattr(p, "resume_file", None)
            ]

            def _track_resume(want_ai: bool) -> str:
                for p in profiles:
                    is_ai = any(k in (p.name or "").lower() for k in ["ai", "python", "ml", "llm"])
                    if is_ai == want_ai:
                        return p.resume_file
                return profiles[0].resume_file if profiles else ""

            if is_ai_job:
                target_resume_rel = _track_resume(True)
                target_track = "AI / Python Engineer"
            else:
                target_resume_rel = _track_resume(False)
                target_track = "Full Stack Engineer"

            resume_path = (PROJECT_ROOT / target_resume_rel) if target_resume_rel else None
            if resume_path is not None and resume_path.exists():
                target_stem = resume_path.stem.lower()
                current_resume_text = (await safe_text(modal)).lower() if modal else ""
                if target_stem not in current_resume_text:
                    upload_btn = await first_visible(
                        modal or self.page,
                        [
                            "div:has-text('Upload another resume')",
                            "button:has-text('Upload another resume')",
                            "span:has-text('Upload another resume')",
                            "a:has-text('Upload another resume')",
                        ],
                        timeout_ms=1500,
                    )
                    if upload_btn:
                        try:
                            await upload_btn.click(timeout=1500)
                            await human_pause(500, 1000)
                        except Exception:
                            pass
                        file_input = await first_visible(self.page, ["input[type='file']"], timeout_ms=1500)
                        if file_input:
                            await file_input.set_input_files(str(resume_path))
                            await human_pause(1000, 2000)
                            log.info(
                                "cutshort.apply.dynamic_resume_selected",
                                job_id=job.job_id,
                                track=target_track,
                                resume=str(resume_path.name),
                            )
                else:
                    log.info(
                        "cutshort.apply.resume_already_matched",
                        job_id=job.job_id,
                        track=target_track,
                        resume=str(resume_path.name),
                    )
        except Exception as exc:
            log.debug("cutshort.apply.dynamic_resume_failed", error=str(exc))

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

        # Dynamic tech stack, specialty, and achievement tailored to the role
        text_for_skills = f"{actual_title} {modal_text} {job.description}".lower()
        if any(k in text_for_skills for k in ["ai", "llm", "genai", "gpt", "machine learning", "rag", "agent", "prompt"]):
            key_tech_stack = "Python, FastAPI, GenAI, and LLM orchestration (RAG/LangChain)"
            core_specialty = "autonomous AI agents, vector search pipelines, and scalable Python backends"
            specific_achievement = "end-to-end LLM applications with high accuracy and low latency"
        elif any(k in text_for_skills for k in ["qa", "testing", "test automation", "sdet"]):
            key_tech_stack = "Python, Playwright, Selenium, and API automation frameworks"
            core_specialty = "robust automated testing pipelines and CI/CD quality gates"
            specific_achievement = "comprehensive automated test suites ensuring high defect coverage"
        elif any(k in text_for_skills for k in ["devops", "docker", "kubernetes", "aws", "gcp", "azure"]):
            key_tech_stack = "Docker, Kubernetes, AWS, and CI/CD pipelines"
            core_specialty = "scalable cloud infrastructure and containerized deployments"
            specific_achievement = "automated zero-downtime deployment workflows"
        elif any(k in text_for_skills for k in ["frontend", "fullstack", "full stack", "react", "next", "node"]):
            key_tech_stack = "React, Node.js, TypeScript, and Python"
            core_specialty = "scalable full-stack web applications and robust APIs"
            specific_achievement = "high-performance, responsive full-stack features"
        elif any(k in text_for_skills for k in ["backend", "python", "fastapi", "django"]):
            key_tech_stack = "Python, FastAPI, Django, and PostgreSQL"
            core_specialty = "high-concurrency backend services and database architectures"
            specific_achievement = "scalable, low-latency microservices and REST APIs"
        else:
            key_tech_stack = "Python, Node.js, React, and modern web architectures"
            core_specialty = "scalable backend and full-stack software systems"
            specific_achievement = "reliable, production-grade applications"

        from ..core.gemini_writer import applicant_snapshot
        who = applicant_snapshot()
        applicant_name = who.name if who.name != "a Software Engineer" else (AgentConfig.load().applicant_name or "").strip()

        modal_scope = modal or self.page
        textarea = await first_visible(
            modal_scope,
            [
                "textarea[name='message']",
                "textarea[placeholder*='message' i]",
                "textarea",
                "input[type='text'][placeholder*='note' i]",
                "input[type='text'][placeholder*='message' i]",
            ],
            timeout_ms=3000,
        )
        if not modal and textarea:
            try:
                wrapper = textarea.locator("xpath=ancestor::div[@id='modal-root']/div[1] | ancestor::div[contains(@class,'modal') or contains(@class,'Modal')][1] | ancestor::form[1]").first
                if await wrapper.count() > 0:
                    modal = wrapper
            except Exception:
                pass
        if textarea:
            pitch = (
                f"Hi {recruiter_name},\n\n"
                f"I'm applying for the {actual_title} role at {actual_company}. "
                f"With {who.experience_label} of hands-on experience in {key_tech_stack}, I specialize in building {core_specialty}—recently delivering {specific_achievement}.\n\n"
                f"My background matches the tech stack you're looking for, and as an immediate joiner ({who.notice_label} notice), I can hit the ground running with minimal ramp-up time.\n\n"
                f"Looking forward to discussing how I can contribute to the team!\n\n"
                f"Best,\n"
                f"{applicant_name}"
            )
            try:
                await textarea.scroll_into_view_if_needed(timeout=1000)
                await textarea.click()
            except Exception:
                pass
            try:
                await textarea.fill(pitch)
            except Exception:
                # React re-render can detach the resolved handle between
                # resolve and fill (run 388: 10s timeout on the dialog chain).
                # One re-resolve, then give up — never submit an empty pitch.
                # (Early return skips the feed nav-back below; the next job's
                # clean slate + URL fallback covers it.)
                try:
                    fresh = await first_visible(
                        modal_scope,
                        ["textarea[name='message']", "textarea[placeholder*='message' i]", "textarea"],
                        timeout_ms=2000,
                    )
                    if fresh is None:
                        raise RuntimeError("pitch textarea gone")
                    textarea = fresh
                    await textarea.fill(pitch)
                except Exception as exc2:
                    try:
                        await self.page.keyboard.press("Escape")
                        await human_pause(300, 500)
                    except Exception:
                        pass
                    return ApplyOutcome(
                        ApplicationStatus.FAILED,
                        detail=f"cutshort pitch fill failed for {job.title} at {job.company}: {str(exc2)[:120]}",
                    )
            # Crucial: dispatch synthetic events so React updates controlled component state
            await textarea.dispatch_event("input")
            await textarea.dispatch_event("change")
            await human_pause(300, 600)

        send_btn = await first_visible(
            modal_scope,
            [
                "button:has-text('Send')",
                "button[type='submit']",
                "button:has-text('Submit')",
                "button:has-text('Apply')",
                "button:has-text('Send application')",
                "button:has-text('Submit application')",
                "button:has-text('Confirm & Send')",
            ],
            timeout_ms=2500,
        )

        submission_attempted = False
        if send_btn:
            self.require_mutation("application.final_submit")
            try:
                await send_btn.scroll_into_view_if_needed(timeout=1000)
            except Exception:
                pass
            try:
                await send_btn.click(timeout=3000)
            except Exception:
                await send_btn.evaluate("el => el.click()")
            await human_pause(1500, 2500)
            submission_attempted = True
        elif textarea:
            # Typed pitch but no Send control — Enter may submit chat-style modals.
            self.require_mutation("application.final_submit")
            await textarea.press("Enter")
            await human_pause(1500, 2500)
            submission_attempted = True

        confirmation = None
        if submission_attempted:
            # If an in-modal screening questionnaire/chatbot pops up immediately, answer it
            if await first_visible(self.page, ["fieldset", "form:has(fieldset)", "div[class*='quick-reply']", "button.chip"], timeout_ms=1500):
                log.info("cutshort.apply.in_modal_questionnaire_detected", job_id=job.job_id)
                chatbot = CutshortChatbot(self.page, self.answers)
                await chatbot.run()
                await human_pause(1000, 2000)

            confirmation = await first_visible(
                self.page,
                [
                    "text=Application sent",
                    "text=successfully applied",
                    "text=Employer will review",
                    "text=Applied successfully",
                    "text=Your response has been recorded",
                    "text=Thanks for your response",
                    "text=Message sent",
                    "div:has-text('Application sent')",
                    "div:has-text('Applied successfully')",
                    "button:has-text('Applied')",
                    "button:has-text('View conversation')",
                    "a:has-text('View conversation')",
                ],
                timeout_ms=4000,
            )

        # Check if modal closed or disappeared
        modal_closed = False
        if modal:
            try:
                modal_closed = not (await modal.is_visible(timeout=2000))
            except Exception:
                modal_closed = True
        elif textarea:
            try:
                modal_closed = not (await textarea.is_visible(timeout=2000))
            except Exception:
                modal_closed = True

        # Close modal if still open (keep the page clean for the next job)
        close_btn = await first_visible(
            self.page,
            [
                "div:has(img[src*='close.png'])",
                "img[src*='close.png']",
                "div[class*='close']",
                "button[aria-label='Close']",
                "button.close",
                "span.close",
            ],
            timeout_ms=1000,
        )
        if close_btn:
            try:
                await close_btn.click(timeout=1000)
                await human_pause(400, 800)
            except Exception:
                try:
                    await close_btn.evaluate("el => el.click()")
                except Exception:
                    pass
        else:
            try:
                await self.page.keyboard.press("Escape")
                await human_pause(300, 500)
            except Exception:
                pass

        # Clear any lingering backdrops
        try:
            await self.page.evaluate("() => { document.querySelectorAll('.modal-backdrop, [class*=\"backdrop\"]').forEach(e => e.remove()); }")
        except Exception:
            pass

        # Check if card button updated to Applied or View conversation
        card_applied = await first_visible(
            self.page,
            ["button:has-text('Applied')", "a:has-text('View conversation')", "button:has-text('View conversation')"],
            timeout_ms=1500,
        )

        outcome: ApplyOutcome
        if submission_attempted and (confirmation or modal_closed or card_applied):
            outcome = ApplyOutcome(
                ApplicationStatus.APPLIED,
                detail="Application confirmed",
                confirmation_type="dom_marker",
                confirmation_evidence=f"Cutshort application confirmed (evidence: confirmation={bool(confirmation)}, modal_closed={modal_closed}, card_applied={bool(card_applied)})",
            )
        elif not submission_attempted and card_applied:
            outcome = ApplyOutcome(ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)
        else:
            # DUMP DOM for debugging
            try:
                dump_path = self.artifacts.dir / f"cutshort-apply-failed-{raw_id}.html"
                dump_path.write_text(await self.page.content(), encoding="utf-8")
                log.info("cutshort.apply_failed_dump", path=str(dump_path))
            except Exception:
                pass
            if await self._cloudflare_wall(blocked=True):
                outcome = ApplyOutcome(
                    ApplicationStatus.FAILED,
                    reason=SkipReason.CLOUDFLARE_CHALLENGE,
                    detail=f"Cloudflare challenge wall (no actionable modal) for {job.title} at {job.company}",
                )
            else:
                stage = "send" if send_btn else ("textarea" if textarea else ("modal" if modal else "cta"))
                outcome = ApplyOutcome(
                    ApplicationStatus.FAILED,
                    detail=f"cutshort apply stalled at stage={stage} (modal={bool(modal)}, textarea={bool(textarea)}, send={bool(send_btn)}) for {job.title} at {job.company}",
                )

        # Always ensure browser returns cleanly to /profile/all-jobs feed for subsequent jobs
        if "/profile/all-jobs" not in self.page.url:
            try:
                await self.page.goto("https://cutshort.io/profile/all-jobs", wait_until="domcontentloaded")
                await human_pause(800, 1500)
            except Exception:
                pass

        return outcome

    # ---------------------------------------------------------
    # Phase 2 Message Handling (Questionnaire Sweep)
    # ---------------------------------------------------------
    _CHAT_CLOSE_SELECTORS = [
        "button[aria-label='Close']",
        "button.close",
        "span.close",
        "svg[class*='close']",
        "button:has-text('Close')",
    ]

    async def handle_messages(self) -> None:
        """
        Questionnaire sweep — runs once after ALL applications are sent.

        Verified from saved DOM (__NEXT_DATA__ + tab markup): pending employer
        questionnaires render at candidate-conversations?stage=awaiting under
        the "Pending" tab — <button role="tab" id="tab-awaiting">Pending</button>
        with rows inside <div role="tabpanel" id="tabpanel-awaiting">. The
        default/unread stages render "( '.' ) No conversations here." even when
        questionnaires are pending, which is why earlier sweeps found nothing.
        """
        self.require_mutation("messages.answer_flow")
        log.info("cutshort.messages.start")
        processed: set[str] = set()

        # Source A (primary): Pending / awaiting stage
        try:
            await self.page.goto(
                "https://cutshort.io/profile/candidate-conversations?stage=awaiting",
                wait_until="domcontentloaded",
            )
            await human_pause(2500, 4000)
            await self._open_awaiting_tab()
            await self._process_questionnaire_rows(processed)
        except Exception as exc:
            log.error("cutshort.messages.awaiting_error", error=str(exc)[:150])

        # Source B (fallback): unfiltered inbox for recruiter threads without
        # the [Questionnaire] tag. Shares `processed` to avoid double answers.
        try:
            await self.page.goto(
                "https://cutshort.io/profile/candidate-conversations", wait_until="domcontentloaded"
            )
            await human_pause(2500, 4000)
            await self._process_inbox_threads(processed)
        except Exception as exc:
            log.error("cutshort.messages.inbox_error", error=str(exc)[:150])

        try:
            dump_path = self.artifacts.dir / "cutshort-messages.html"
            dump_path.write_text(await self.page.content(), encoding="utf-8")
        except Exception:
            pass

        log.info("cutshort.messages.done", processed=len(processed))

    async def _open_awaiting_tab(self) -> None:
        """Focuses the Pending tab if it isn't already active."""
        tab = self.page.locator("#tab-awaiting")
        try:
            if await tab.count() > 0 and await tab.is_visible():
                if (await tab.get_attribute("aria-selected")) != "true":
                    await tab.click()
                    await human_pause(1500, 2500)
                    log.info("cutshort.messages.pending_tab_opened")
        except Exception as exc:
            log.debug("cutshort.messages.tab_click_failed", error=str(exc))

    async def _answer_current_chat(self, key: str, index: int) -> None:
        """Dumps the opened thread and runs the chatbot against it."""
        try:
            dump_path = self.artifacts.dir / f"cutshort-thread-{index}.html"
            dump_path.write_text(await self.page.content(), encoding="utf-8")
        except Exception:
            pass

        chatbot = CutshortChatbot(self.page, self.answers)
        success, answered, error_msg = await chatbot.run()
        if answered > 0:
            log.info("cutshort.messages.success", thread=key[:60], answered=answered)
        elif not success:
            log.debug("cutshort.messages.no_action_needed", thread=key[:60], reason=(error_msg or "")[:100])

    async def _close_chat_overlay(self) -> None:
        """Closes an opened chat panel/overlay, or escapes back to the list."""
        close_btn = await first_visible(self.page, self._CHAT_CLOSE_SELECTORS, timeout_ms=800)
        if close_btn:
            try:
                await close_btn.click()
                await human_pause(600, 1200)
                return
            except Exception:
                pass
        try:
            await self.page.keyboard.press("Escape")
        except Exception:
            pass
        await human_pause(400, 800)

    def _clean_thread_key(self, text: str) -> str:
        """Strips relative timestamps and status tags so key remains invariant across rounds."""
        clean = re.sub(
            r"\b(?:just now|yesterday|a few seconds ago|(?:an?|\d+)\s*(?:sec|second|min|minute|hour|hr|day|week|month)s?\s*ago)\b",
            "",
            text,
            flags=re.IGNORECASE,
        )
        clean = clean.replace("[Questionnaire]", "")
        return " ".join(clean.split())[:160]

    def _is_stale_thread(self, text: str, max_age_days: int = 3) -> bool:
        """
        Returns True if thread relative timestamp indicates it is older than max_age_days
        (e.g., '2 weeks ago', '1 month ago', or '4 days ago').
        Fresh threads ('seconds ago', 'minutes ago', 'hours ago', '<= 3 days ago') return False.
        """
        match = re.search(r"\b(\d+)\s+(second|minute|hour|day|week|month)s?\s+ago\b", text, flags=re.IGNORECASE)
        if not match:
            return False
        amount, unit = int(match.group(1)), match.group(2).lower()
        if unit in ("second", "minute", "hour"):
            return False
        if unit == "day":
            return amount > max_age_days
        if unit in ("week", "month"):
            return True
        return False

    async def _process_questionnaire_rows(self, processed: set[str]) -> None:
        """Opens every fresh [Questionnaire] row under the Pending (#tabpanel-awaiting) tab."""
        cfg = AgentConfig.load()
        max_rounds = max([p.platform_limits.get("cutshort", 150) for p in cfg.profiles if p.enabled] or [150])

        for _round in range(max_rounds):  # Dynamic cap matching Cutshort limit
            rows = self.page.locator("#tabpanel-awaiting div[role='button']")
            if await rows.count() == 0:
                rows = self.page.locator("div[role='button']").filter(has_text="[Questionnaire]")
            count = await rows.count()

            target = None
            key = ""
            for i in range(count):
                row = rows.nth(i)
                try:
                    if not await row.is_visible():
                        continue
                except Exception:
                    continue
                txt = (await safe_text(row)).strip()
                key = self._clean_thread_key(txt)
                if not key or key in processed:
                    continue

                # Freshness Guard: Skip stale questionnaires older than 3 days
                if self._is_stale_thread(txt, max_age_days=3):
                    log.info("cutshort.messages.skip_stale_questionnaire", item=key[:60], age=">3 days old")
                    processed.add(key)
                    continue

                target = row
                break

            if target is None:
                try:
                    await self.page.evaluate("window.scrollBy(0, 600)")
                    await human_pause(800, 1500)
                    rows = self.page.locator("#tabpanel-awaiting div[role='button']")
                    if await rows.count() == 0:
                        rows = self.page.locator("div[role='button']").filter(has_text="[Questionnaire]")
                    for i in range(await rows.count()):
                        row = rows.nth(i)
                        if not await row.is_visible():
                            continue
                        txt = (await safe_text(row)).strip()
                        key = self._clean_thread_key(txt)
                        if not key or key in processed:
                            continue

                        if self._is_stale_thread(txt, max_age_days=3):
                            log.info("cutshort.messages.skip_stale_questionnaire", item=key[:60], age=">3 days old")
                            processed.add(key)
                            continue

                        target = row
                        break
                except Exception:
                    pass

            if target is None:
                break

            processed.add(key)
            log.info("cutshort.messages.opening_questionnaire", item=key[:80])
            url_before = self.page.url
            try:
                await target.click()
                await human_pause(2000, 3500)
                await self._answer_current_chat(key, len(processed))
            except Exception as exc:
                log.error("cutshort.messages.row_error", item=key[:60], error=str(exc)[:120])
            finally:
                # Allow in-flight submission network requests to settle cleanly
                await human_pause(1500, 2500)
                if self.page.url != url_before:
                    try:
                        await self.page.goto(url_before, wait_until="domcontentloaded")
                        await human_pause(2000, 3500)
                    except Exception:
                        break
                else:
                    await self._close_chat_overlay()

    async def _process_inbox_threads(self, processed: set[str]) -> None:
        """Opens every fresh conversation thread in the (unfiltered) inbox list."""
        cfg = AgentConfig.load()
        max_rounds = max([p.platform_limits.get("cutshort", 150) for p in cfg.profiles if p.enabled] or [150])

        thread_selector = (
            "div[class*='onversation'], div[class*='hread'], li[class*='hread'], "
            "div[class*='hat-item'], a[class*='onversation']"
        )
        for _round in range(max_rounds):
            candidates = []
            for el in await self.page.locator(thread_selector).all():
                try:
                    if not await el.is_visible():
                        continue
                except Exception:
                    continue
                txt = " ".join((await safe_text(el)).split())
                if len(txt) < 10 or txt.lower() in ("messages", "inbox"):
                    continue
                key = self._clean_thread_key(txt)
                if not key or key in processed:
                    continue

                # Freshness Guard: Skip stale threads older than 3 days
                if self._is_stale_thread(txt, max_age_days=3):
                    log.debug("cutshort.messages.skip_stale_inbox_thread", thread=key[:60])
                    processed.add(key)
                    continue

                candidates.append((el, key))

            if not candidates:
                break

            target, key = candidates[0]
            processed.add(key)
            log.info("cutshort.messages.opening_thread", thread=key[:80])
            url_before = self.page.url
            try:
                await target.click()
                await human_pause(2000, 3500)
                await self._answer_current_chat(key, len(processed))
            except Exception as exc:
                log.error("cutshort.messages.thread_error", thread=key[:60], error=str(exc)[:120])
            finally:
                if self.page.url != url_before:
                    try:
                        await self.page.goto(url_before, wait_until="domcontentloaded")
                        await human_pause(2000, 3500)
                    except Exception:
                        break
                else:
                    await self._close_chat_overlay()
