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
        phone = cfg.applicant_phone or "9481777227"
        email = cfg.applicant_email or "maheshchitkoti@gmail.com"

        # 1. Contact Number / Mobile / Phone / WhatsApp
        if any(k in norm_text for k in ["contact number", "contact no", "mobile number", "mobile no", "phone number", "phone no", "whatsapp", "call you", "share your contact", "share your number", "share your phone"]):
            needed_fields.append(("Contact Number", phone))

        # 2. Email Address
        if any(k in norm_text for k in ["email address", "email id", "mail address", "mail id", "share your email", "share your mail"]):
            needed_fields.append(("Email", email))

        # 3. Notice period / LWD
        if any(k in norm_text for k in ["notice period", "notice", "how soon", "when can you join", "joining time", "lwd", "last working"]):
            resolved = self.answers.resolve(ScreeningQuestion(text="notice period", kind="unknown", options=[]))
            if resolved is None:
                needed_fields.append(("Notice Period", "0 days (Immediate Joiner)"))
            else:
                needed_fields.append(("Notice Period", resolved.value))

        # 4. Current CTC
        if any(k in norm_text for k in ["current ctc", "current salary", "cctc", "present ctc"]):
            resolved = self.answers.resolve(ScreeningQuestion(text="current ctc", kind="unknown", options=[]))
            if resolved is None:
                needed_fields.append(("Current CTC", "4 LPA"))
            else:
                needed_fields.append(("Current CTC", resolved.value))

        # 5. Expected CTC
        if any(k in norm_text for k in ["expected ctc", "expected salary", "ectc"]):
            resolved = self.answers.resolve(ScreeningQuestion(text="expected ctc", kind="unknown", options=[]))
            if resolved is None:
                needed_fields.append(("Expected CTC", "7 LPA"))
            else:
                needed_fields.append(("Expected CTC", resolved.value))

        # 6. Total Experience
        if any(k in norm_text for k in ["total experience", "overall experience", "total exp", "years of experience"]):
            resolved = self.answers.resolve(ScreeningQuestion(text="total experience", kind="unknown", options=[]))
            if resolved is None:
                needed_fields.append(("Total Experience", "2.5 years"))
            else:
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
            if resolved is None:
                needed_fields.append(("Location / Availability", "Bengaluru (Open to relocate)"))
            else:
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
        reply_lines.append(f"\nPlease let me know if you need any additional information.\n\nBest regards,\n{cfg.applicant_name or 'Mahesh Chitakoti'}")
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
                        ta_ans = (
                            "Architected and deployed an end-to-end agentic AI and LLM orchestration pipeline using FastAPI, LangChain, and PostgreSQL with pgvector. "
                            "The primary challenge was unpredictable API latency and output validation during multi-step tool calls. "
                            "I solved this by implementing asynchronous worker queues, semantic caching in Redis, and strict Pydantic JSON schema enforcement. "
                            "This reduced end-to-end latency by 45%, eliminated schema drift, and ensured sub-second response times in production."
                        )
                    elif any(k in q_label_low for k in ["technical skillset", "strong in", "strength", "skillsets do you consider"]):
                        ta_ans = (
                            "Strongest in Python, FastAPI, React/Node.js, PostgreSQL, and LLM application development (RAG, agent workflows, LangChain). "
                            "Professional examples: designed scalable REST microservices handling high concurrency, built full-stack reactive dashboards with React and TypeScript, "
                            "and optimized SQL query execution plans and Redis caching layers for production deployments."
                        )
                    elif any(k in q_label_low for k in ["phone", "mobile", "contact number", "contact no", "whatsapp", "call you"]):
                        ta_ans = AgentConfig.load().applicant_phone or "9481777227"
                    elif any(k in q_label_low for k in ["email", "mail id", "email address", "mail address"]):
                        ta_ans = AgentConfig.load().applicant_email or "maheshchitkoti@gmail.com"
                    elif any(k in q_label_low for k in ["ctc", "salary", "fixed", "variable", "in hand", "in-hand", "annual ctc", "compensation"]):
                        ta_ans = "Current CTC: 4 LPA (Fixed: 3.8 LPA, Variable: 0 LPA). Expected CTC: 7 LPA. Notice Period: 0 days (Immediate Joiner)."
                    elif any(k in q_label_low for k in ["docker", "kubernetes", "container"]):
                        ta_ans = "Yes, 2+ years of hands-on experience containerizing microservices with Docker, creating optimized multi-stage builds, and orchestrating container workloads with Kubernetes and AWS."
                    elif any(k in q_label_low for k in ["notice", "how soon", "when can you join", "joining date", "availability"]):
                        ta_ans = "Available immediately (0-day notice period, already served notice)."
                    elif any(k in q_label_low for k in ["location", "relocate", "relocation", "bangalore", "bengaluru", "mumbai", "hyderabad"]):
                        ta_ans = "Currently based in Bengaluru, open to both onsite/hybrid work in Bengaluru and ready to relocate to other major tech hubs."
                    else:
                        resolved = self.answers.resolve(ScreeningQuestion(text=q_label[:200], kind="text"))
                        if resolved and resolved.value:
                            ta_ans = str(resolved.value)
                        else:
                            ta_ans = "Experienced software engineer with 2.5+ years building scalable full-stack applications, microservices, and AI workflows using Python, FastAPI, and React."

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
                            ans_val = AgentConfig.load().applicant_phone or "9481777227"
                        elif any(k in combined for k in ["email", "mail id"]):
                            ans_val = AgentConfig.load().applicant_email or "maheshchitkoti@gmail.com"
                        elif any(k in combined for k in ["notice", "how soon", "when can you join", "lwd"]):
                            ans_val = "0 days (Immediate)"
                        elif any(k in combined for k in ["expected ctc", "ectc"]):
                            ans_val = "7 LPA"
                        elif any(k in combined for k in ["current ctc", "cctc"]):
                            ans_val = "4 LPA"
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
        1. Hiring Activity: Active in last 7 days / 1-2 weeks (fresh postings only)
        2. Role Type: Full time
        3. Job Category: Intentionally left UNTOUCHED on UI so all tech roles
           (Software, AI/ML, DevOps, QA, Cloud, Systems, Support) are pulled for agent evaluation.
        4. Location: Intentionally left UNTOUCHED (all locations & remote allowed;
           avoids restricting candidate pool or dropping multi-city postings).
        5. Minimum Salary: >= 5 LPA (slider set to 500,000 INR)
        6. Experience Range: Max experience capped at 3.5y (min exp untouched at 0.0)
        """
        log.info("cutshort.filters.applying_ui", profile=profile.name)

        # 1. Hiring activity on job (Active in last 7 days / last week)
        try:
            btn = await first_visible(self.page, ["div[data-intercom-target='hiringActivityOnJob-filter']"], timeout_ms=2000)
            if btn:
                await btn.click(force=True)
                await human_pause(400, 800)
                opt = await first_visible(
                    self.page,
                    [
                        "div:has-text('Active in last 7 days')",
                        "div:has-text('Active in last 1 week')",
                        "div:has-text('Active in last week')",
                        "div:has-text('Active in last 1-2 weeks')",
                        "div:has-text('Active recently')",
                        "label:has-text('In the last')",
                        "div:has-text('In the last')",
                    ],
                    timeout_ms=1500,
                )
                if opt:
                    await opt.click(force=True)
                    await human_pause(300, 500)

                # If there is a unit dropdown showing Months, switch to Weeks
                unit_btn = await first_visible(self.page, ["div:has-text('Months')", "button:has-text('Months')", "span:has-text('Months')"], timeout_ms=600)
                if unit_btn:
                    await unit_btn.click(force=True)
                    await human_pause(200, 400)
                    week_opt = await first_visible(self.page, ["div:has-text('Weeks')", "div:has-text('Days')"], timeout_ms=600)
                    if week_opt:
                        await week_opt.click(force=True)

                await self.page.keyboard.press("Escape")
                await human_pause(300, 600)
        except Exception as exc:
            log.debug("cutshort.filters.activity_error", error=str(exc))

        # 2. Type of role (Full time)
        try:
            btn = await first_visible(self.page, ["div[data-intercom-target='roletype-filter']"], timeout_ms=2000)
            if btn:
                await btn.click(force=True)
                await human_pause(400, 800)
                opt = await first_visible(
                    self.page,
                    [
                        "button:has-text('Full time')",
                        "div:has-text('Full time')",
                        "label:has-text('Full time')",
                    ],
                    timeout_ms=1500,
                )
                if opt:
                    await opt.click(force=True)
                    await human_pause(400, 600)
                await self.page.keyboard.press("Escape")
                await human_pause(300, 600)
        except Exception as exc:
            log.debug("cutshort.filters.roletype_error", error=str(exc))

        # 3. Job category (tags-filter)
        try:
            btn = await first_visible(self.page, ["div[data-intercom-target='tags-filter']"], timeout_ms=2000)
            if btn:
                await btn.click(force=True)
                await human_pause(500, 900)
                # Categories & sub-tags specified:
                # - software-development: frontend, backend, fullstack
                # - testing-QA: manual-testing, automation-testing, others (all three)
                # - devops-IT-infrastructure
                target_tags = [
                    "Software Development", "Software development", "Software", "Tech",
                    "Frontend", "Backend", "Fullstack", "Full stack",
                    "Devops", "DevOps", "Devops-IT-infrastructure", "Devops-lT-infrastructure", "Infrastructure",
                    "Testing-QA", "Testing", "QA", "Manual Testing", "Automation Testing", "Others",
                    "Data Science", "Data Analytics",
                ]
                for tag in target_tags:
                    tag_el = await first_visible(
                        self.page,
                        [
                            f"label:has-text('{tag}')",
                            f"div[role='checkbox']:has-text('{tag}')",
                            f"span:has-text('{tag}')",
                            f"div:has-text('{tag}')",
                        ],
                        timeout_ms=250,
                    )
                    if tag_el:
                        try:
                            cb = tag_el.locator("input[type='checkbox']").first
                            if await cb.count() > 0:
                                if not await cb.is_checked():
                                    await cb.click(force=True)
                            else:
                                await tag_el.click(force=True)
                            await human_pause(100, 200)
                        except Exception:
                            pass
                await self.page.keyboard.press("Escape")
                await human_pause(300, 600)
        except Exception as exc:
            log.debug("cutshort.filters.category_error", error=str(exc))

        # 4. Minimum salary (>= 5 LPA)
        min_salary_lpa = profile.filters.min_salary_lpa if (profile.filters and profile.filters.min_salary_lpa) else 5.0
        if min_salary_lpa and min_salary_lpa >= 5.0:
            try:
                btn = await first_visible(self.page, ["div[data-intercom-target='minsal-filter']"], timeout_ms=2000)
                if btn:
                    await btn.click(force=True)
                    await human_pause(400, 800)
                    slider = await first_visible(self.page, ["div[role='slider']"], timeout_ms=1500)
                    if slider:
                        await slider.focus()
                        await self.page.keyboard.press("Home")
                        await human_pause(100, 200)
                        steps = int(min_salary_lpa)
                        for _ in range(steps):
                            await self.page.keyboard.press("ArrowRight")
                            await human_pause(80, 150)
                    await self.page.keyboard.press("Escape")
                    await human_pause(300, 600)
            except Exception as exc:
                log.debug("cutshort.filters.minsal_error", error=str(exc))

        # 5. Experience range: Allow up to 8.0 years on Cutshort UI slider so broad startup bands (2-8y) are loaded
        try:
            btn = await first_visible(self.page, ["div[data-intercom-target='expRange-filter']"], timeout_ms=2000)
            if btn:
                await btn.click(force=True)
                await human_pause(400, 800)
                sliders = await self.page.locator("div[role='slider']").all()
                if len(sliders) >= 2:
                    max_slider = sliders[1]
                    await max_slider.focus()
                    await self.page.keyboard.press("Home")
                    await human_pause(100, 200)
                    steps = int(round(8.0 / 0.5))
                    for _ in range(steps):
                        await self.page.keyboard.press("ArrowRight")
                        await human_pause(50, 100)
                await self.page.keyboard.press("Escape")
                await human_pause(300, 600)
        except Exception as exc:
            log.debug("cutshort.filters.exp_range_error", error=str(exc))

        log.info("cutshort.filters.applied_ui_successfully")
        await human_pause(1500, 2500)

    async def fetch_jobs(self, profile: JobProfile, exclude_job_ids: set[str]) -> list[Job]:
        log.info("cutshort.fetch.start", profile=profile.name)
        await self.page.goto("https://cutshort.io/profile/all-jobs", wait_until="domcontentloaded")
        await human_pause(2000, 3000)

        # Ensure recommendation switch is OFF so all filtered jobs are loaded
        switch_el = await first_visible(
            self.page,
            [
                "input[role='switch']",
                "label:has(input[role='switch'])",
                "div:has-text('Turn it OFF to view all jobs') input[role='switch']",
            ],
            timeout_ms=3000,
        )
        if switch_el:
            try:
                is_checked = await switch_el.is_checked()
            except Exception:
                is_checked = True
            if is_checked:
                await switch_el.scroll_into_view_if_needed()
                await human_pause(300, 600)
                try:
                    await switch_el.click(force=True)
                except Exception:
                    await switch_el.evaluate("el => el.click()")
                await human_pause(2000, 3000)

        await self._apply_ui_filters(profile)

        job_link_sel = "a[href*='/job/']"

        # Infinite scroll to fetch available jobs in the feed
        last_link_count = 0
        target_pool = min(profile.platform_limits.get(self.platform_name, 40) * 2, 80)
        while True:
            anchors = await self.page.locator(job_link_sel).all()
            link_count = len(anchors)
            if link_count >= target_pool:
                break
            if link_count == last_link_count:
                stagnant_scrolls += 1
                if stagnant_scrolls >= 3:
                    break
            else:
                stagnant_scrolls = 0
                last_link_count = link_count
                
            if anchors:
                try:
                    await anchors[-1].scroll_into_view_if_needed(timeout=2000)
                    await anchors[-1].hover(timeout=1000)
                except Exception:
                    pass

            await scroll_page(self.page, steps=3, delay_s=0.5)
            await human_pause(800, 1500)

        # Dump AFTER scrolling so the artifact reflects what was actually parsed
        try:
            dump_path = self.artifacts.dir / "cutshort-dashboard.html"
            dump_path.write_text(await self.page.content(), encoding="utf-8")
            log.info("cutshort.dashboard_html_saved", path=str(dump_path))
        except Exception as exc:
            log.debug("cutshort.dashboard_dump_failed", error=str(exc))

        anchors = await self.page.locator(job_link_sel).all()
        jobs: list[Job] = []
        seen_urls: set[str] = set()

        log.info("cutshort.fetch.cards_found", count=len(anchors))

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
                        recommendation_tab="default" if profile.use_recommended else "all_jobs",
                        recommendation_position=len(jobs) + 1,
                        platform="cutshort",
                    )
                )
            except Exception as exc:
                log.debug("cutshort.fetch.parse_error", error=str(exc))
                continue

        # Stage 2: Expand to active platform feed if recommended pool yields fewer than target applies
        target_applies = profile.platform_limits.get(self.platform_name, 150)
        min_needed_jobs = min(target_applies, 50)
        if profile.use_recommended and len(jobs) < min_needed_jobs:
            log.info(
                "cutshort.fetch.stage2_expand_active_jobs",
                current_count=len(jobs),
                target_needed=min_needed_jobs,
            )
            try:
                # Look for recommendation switch: input[role='switch'] or label containing the switch
                switch_el = await first_visible(
                    self.page,
                    [
                        "input[role='switch']",
                        "label:has(input[role='switch'])",
                        "div:has-text('Turn it OFF to view all jobs') input[role='switch']",
                    ],
                    timeout_ms=3000,
                )
                if switch_el:
                    try:
                        is_checked = await switch_el.is_checked()
                    except Exception:
                        is_checked = True
                    if is_checked:
                        await switch_el.scroll_into_view_if_needed()
                        await human_pause(400, 800)
                        try:
                            await switch_el.click(force=True)
                        except Exception:
                            await switch_el.evaluate("el => el.click()")
                        log.info("cutshort.fetch.turned_off_recommendation_switch")
                        await human_pause(2000, 3500)

                        # Apply all native Cutshort UI filters on the toolbar!
                        await self._apply_ui_filters(profile)

                        # Scroll to load expanded feed dynamically based on target limit
                        stagnant_count = 0
                        last_total = 0
                        target_pool = min(target_applies * 2, 80)
                        for _ in range(25):
                            anchors_now = await self.page.locator(job_link_sel).all()
                            current_total = len(anchors_now)
                            if current_total >= target_pool:
                                log.info("cutshort.fetch.target_pool_reached", count=current_total, target=target_pool)
                                break
                            if current_total == last_total:
                                stagnant_count += 1
                                if stagnant_count >= 3:
                                    log.info("cutshort.fetch.feed_end_reached", count=current_total)
                                    break
                            else:
                                stagnant_count = 0
                                last_total = current_total

                            if anchors_now:
                                try:
                                    await anchors_now[-1].scroll_into_view_if_needed(timeout=1500)
                                    await anchors_now[-1].hover(timeout=500)
                                except Exception:
                                    pass
                            await scroll_page(self.page, steps=3, delay_s=0.4)
                            await human_pause(600, 1200)

                        # Collect jobs from expanded feed
                        expanded_anchors = await self.page.locator(job_link_sel).all()
                        log.info("cutshort.fetch.expanded_cards_found", count=len(expanded_anchors))
                        for anchor in expanded_anchors:
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

                                # Climb to outer card wrapper to capture salary and experience badges
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
                                        recommendation_tab="active_feed",
                                        recommendation_position=len(jobs) + 1,
                                        platform="cutshort",
                                    )
                                )
                            except Exception as exc:
                                log.debug("cutshort.fetch.expanded_parse_error", error=str(exc))
                                continue
            except Exception as exc:
                log.warning("cutshort.fetch.stage2_expand_failed", error=str(exc))

        log.info("cutshort.fetch.done", count=len(jobs))
        return jobs

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
                            "div.modal-content",
                            "div[class*='modal']",
                        ],
                        timeout_ms=3000,
                    )

        # 3. Fallback or Dedicated Flow: If modal didn't open from feed card, navigate to job.url
        if not modal:
            if not (job.url and job.url.startswith("http")):
                return ApplyOutcome(ApplicationStatus.FAILED, detail=f"No feed card or URL for {job.title} at {job.company}")

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

            # Step 3A: On dedicated job page (cutshort.io/job/...), click primary in-viewport CTA button
            target_btn = None
            cta_buttons = await self.page.locator("button:has-text('Apply to this job'), button:has-text('Apply now'), button:has-text('Apply')").all()
            for b in cta_buttons:
                if await b.is_visible():
                    box = await b.bounding_box()
                    if box and box.get("y", 0) > 0:
                        target_btn = b
                        break
            if not target_btn and cta_buttons:
                target_btn = cta_buttons[0]

            if target_btn:
                try:
                    await target_btn.scroll_into_view_if_needed(timeout=1500)
                except Exception:
                    pass
                try:
                    await target_btn.click(force=True, timeout=3000)
                except Exception:
                    await target_btn.evaluate("el => el.click()")
                await human_pause(2000, 3000)

            # Step 3B: Cutshort redirects to /profile/all-jobs?jobid=... with an active feed card.
            # Click the feed card's "Apply now" button to trigger the pitch modal!
            feed_apply = await first_visible(
                self.page,
                [
                    "button:has-text('Apply now')",
                    "button:has-text('Apply to this job')",
                    "button[label*='Apply to this job']",
                ],
                timeout_ms=5000,
            )
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

            # Step 3C: Wait for pitch modal to appear
            modal = await first_visible(
                self.page,
                [
                    "#modal__content",
                    "div.modal__wrapper",
                    "div[role='dialog']",
                    "div#modal-root > div",
                    "div.modal-content",
                    "div[class*='modal']",
                ],
                timeout_ms=5000,
            )

        modal_text = (await safe_text(modal)).strip() if modal else ""

        # Check and handle profile-specific resume swapping if configured
        try:
            from ..config import PROJECT_ROOT
            cfg = AgentConfig.load()
            prof = next((p for p in cfg.profiles if p.name.lower() == profile_name.lower()), None)
            if prof and prof.resume_file:
                resume_path = PROJECT_ROOT / prof.resume_file
                if resume_path.exists():
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
                                    "cutshort.apply.swapped_resume",
                                    profile=profile_name,
                                    resume=str(resume_path.name),
                                )
        except Exception as exc:
            log.debug("cutshort.apply.resume_swap_failed", error=str(exc))

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

        applicant_name = AgentConfig.load().applicant_name or "Mahesh"
        exp_years_str = str(prof.experience_years) if (prof and prof.experience_years) else "2.5"

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
        if textarea:
            pitch = (
                f"Hi {recruiter_name},\n\n"
                f"I'm applying for the {actual_title} role at {actual_company}. "
                f"With {exp_years_str}+ years of hands-on experience in {key_tech_stack}, I specialize in building {core_specialty}—recently delivering {specific_achievement}.\n\n"
                f"My background matches the tech stack you're looking for, and as an immediate joiner (0-day notice), I can hit the ground running with minimal ramp-up time.\n\n"
                f"Looking forward to discussing how I can contribute to the team!\n\n"
                f"Best,\n"
                f"{applicant_name}"
            )
            try:
                await textarea.scroll_into_view_if_needed(timeout=1000)
                await textarea.click()
            except Exception:
                pass
            await textarea.fill(pitch)
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

        # Check if card button updated to Applied or View conversation
        card_applied = await first_visible(
            self.page,
            ["button:has-text('Applied')", "a:has-text('View conversation')", "button:has-text('View conversation')"],
            timeout_ms=1500,
        )

        if submission_attempted and (confirmation or modal_closed or card_applied):
            return ApplyOutcome(
                ApplicationStatus.APPLIED,
                detail="Application confirmed",
                confirmation_type="dom_marker",
                confirmation_evidence=f"Cutshort application confirmed (evidence: confirmation={bool(confirmation)}, modal_closed={modal_closed}, card_applied={bool(card_applied)})",
            )

        # Only mark ALREADY_APPLIED if NO submission was attempted (i.e. was already applied before)
        if not submission_attempted and card_applied:
            return ApplyOutcome(ApplicationStatus.ALREADY_APPLIED, reason=SkipReason.ALREADY_APPLIED)
            
        # DUMP DOM for debugging
        try:
            dump_path = self.artifacts.dir / f"cutshort-apply-failed-{raw_id}.html"
            dump_path.write_text(await self.page.content(), encoding="utf-8")
            log.info("cutshort.apply_failed_dump", path=str(dump_path))
        except Exception:
            pass
            
        return ApplyOutcome(
            ApplicationStatus.FAILED,
            detail=f"Pitch modal/send button never appeared for {job.title} at {job.company}",
        )

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

    def _is_stale_thread(self, text: str, max_age_days: int = 5) -> bool:
        """
        Returns True if thread relative timestamp indicates it is older than max_age_days
        (e.g., '2 weeks ago', '1 month ago', or '6 days ago').
        Fresh threads ('seconds ago', 'minutes ago', 'hours ago', '<= 5 days ago') return False.
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

                # Freshness Guard: Skip stale questionnaires from weeks/months ago
                if self._is_stale_thread(txt, max_age_days=5):
                    log.info("cutshort.messages.skip_stale_questionnaire", item=key[:60], age=">5 days old")
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

                        if self._is_stale_thread(txt, max_age_days=5):
                            log.info("cutshort.messages.skip_stale_questionnaire", item=key[:60], age=">5 days old")
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

                # Freshness Guard: Skip stale threads from weeks/months ago
                if self._is_stale_thread(txt, max_age_days=5):
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
