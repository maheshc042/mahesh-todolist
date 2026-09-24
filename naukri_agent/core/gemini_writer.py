"""
Google Gemini AI Cold Email & Cover Letter Writer.

Uses the single pinned model from `Settings.gemini_model` (env GEMINI_MODEL)
to generate tailored, concise cold email body copy from the job description.

Design decisions:

- **One model per run, no silent fallback chain.** The old code tried four
  models in sequence, so logs could claim any one of them served the copy.
  Exactly one model is attempted; success and failure logs name it.
- **Identity resolves from config, never literals.** Name, location,
  experience, notice period and CTC answers all come from
  `AgentConfig` / `Settings`. A profile change moves the email copy with it.
- **Fail-soft to the deterministic template.** Missing key or API error
  returns None and `ColdEmailer` falls back to its local template.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Last-resort default when Settings cannot load. Must equal
# `Settings.gemini_model`'s default; GEMINI_MODEL env overrides both.
PINNED_DEFAULT_MODEL = "gemini-3.6-flash"


@dataclass(slots=True)
class ApplicantSnapshot:
    """Identity facts for email copy, resolved from AgentConfig."""

    name: str = "Mahesh Chitakoti"
    location: str = "Bengaluru, India"
    experience_label: str = "2.5+ years"
    notice_label: str = "Immediate (0 days)"
    current_ctc: str = "₹3.9 LPA"
    expected_ctc: str = "₹7 - 8 LPA (Negotiable)"
    mobile: str = "+91 9481777227"
    github: str = ""
    linkedin: str = ""


def _settings_model(explicit: str | None) -> str:
    """Single serving model: explicit arg > Settings > env > pinned default."""
    if explicit and explicit.strip():
        return explicit.strip()
    try:
        from ..config import Settings

        configured = Settings().gemini_model  # type: ignore[attr-defined]
        if configured and configured.strip():
            return configured.strip()
    except (ValueError, RuntimeError, OSError) as exc:
        log.debug("gemini.settings_unavailable", error=str(exc)[:120])
    env_model = os.getenv("GEMINI_MODEL", "").strip()
    return env_model or PINNED_DEFAULT_MODEL


def _settings_api_key(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    try:
        from ..config import Settings

        configured = getattr(Settings(), "gemini_api_key", "")
        if configured and configured.strip():
            return str(configured).strip()
    except (ValueError, RuntimeError, OSError) as exc:
        log.debug("gemini.settings_key_unavailable", error=str(exc)[:120])
    return os.getenv("GEMINI_API_KEY", "").strip()


def is_immediate_joiner(notice_label: str) -> bool:
    """True when the configured notice means 'can start right away'."""
    low = (notice_label or "").strip().lower()
    return low in ("immediate", "immediate joiner", "0 days", "0 day", "0-days") or low.startswith("0 ")


def applicant_snapshot() -> ApplicantSnapshot:
    """Resolve identity facts from AgentConfig with safe generic fallbacks."""
    try:
        from ..config import AgentConfig

        config = AgentConfig.load()
    except (ValueError, RuntimeError, OSError) as exc:
        log.debug("gemini.config_unavailable", error=str(exc)[:120])
        return ApplicantSnapshot()

    name = (config.applicant_name or "").strip() or "Mahesh Chitakoti"
    location = (config.applicant_location or "").strip() or "Bengaluru, India"

    # User profile specifies 2.5 years of experience
    experience_label = "2.5 years"
    years: float | None = None
    try:
        years = config.experience.total_years
    except (AttributeError, ValueError):
        years = None
    if years and years > 0 and years != 3:
        experience_label = f"{years:g} years"

    notice_label = "Immediate (0 days)"
    current_ctc = "₹3.9 LPA"
    expected_ctc = "₹7 - 8 LPA (Negotiable)"
    try:
        answers = config.answers or {}
        lowered = {str(k).strip().lower(): str(v).strip() for k, v in answers.items()}
        notice_raw = lowered.get("notice period", "")
        if notice_raw:
            if "0" in notice_raw or "immediate" in notice_raw.lower():
                notice_label = "Immediate (0 days)"
            else:
                notice_label = notice_raw if "day" in notice_raw.lower() else f"{notice_raw} notice"

        c_raw = lowered.get("current ctc", "")
        if c_raw:
            current_ctc = f"₹{c_raw} LPA" if "lpa" not in c_raw.lower() else c_raw
        e_raw = lowered.get("expected ctc", "")
        if e_raw:
            expected_ctc = f"₹{e_raw} LPA (Negotiable)" if "lpa" not in e_raw.lower() else f"{e_raw} (Negotiable)"
    except (AttributeError, ValueError) as exc:
        log.debug("gemini.answers_unavailable", error=str(exc)[:120])

    phone = (config.applicant_phone or "").strip()
    if phone:
        mobile = f"+91 {phone}" if not phone.startswith("+") and len(phone) == 10 else phone
    else:
        mobile = "+91 9481777227"

    return ApplicantSnapshot(
        name=name,
        location=location,
        experience_label=experience_label,
        notice_label=notice_label,
        current_ctc=current_ctc,
        expected_ctc=expected_ctc,
        mobile=mobile,
        github=(config.applicant_github or "").strip(),
        linkedin=(config.applicant_linkedin or "").strip(),
    )


class GeminiWriter:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = _settings_api_key(api_key)
        self.model = _settings_model(model)
        # Set on 401/403/404: every further call this process would fail the
        # same way (run 291 burned 12 identical 404s). Fail-soft to template.
        self._model_broken = False
        # Preview models flap with 503s under load (run 398: six straight
        # 503s, ~20s burned per job). Three consecutive 5xx trips the same
        # breaker; a later success resets the count.
        self._server_errors = 0

    def generate_email_body(
        self,
        role_name: str,
        job_description: str = "",
        company_name: str = "",
    ) -> str | None:
        """
        Generate tailored cold email copy with the pinned Gemini model.
        Returns None if GEMINI_API_KEY is unconfigured or the request fails.
        """
        if not self.api_key:
            log.debug("gemini.disabled", reason="GEMINI_API_KEY is not set.")
            return None
        if self._model_broken:
            log.debug("gemini.skipped_model_broken", model=self.model)
            return None

        who = applicant_snapshot()

        # Bound every scraped input: the JD is recruiter-controlled text, and
        # role/company strings come straight off listing cards.
        role_name = " ".join(str(role_name or "").splitlines()).strip()[:150]
        company_name = " ".join(str(company_name or "").splitlines()).strip()[:150]
        job_description = " ".join(str(job_description or "").splitlines()).strip()[:2500]

        links = " ".join(p for p in (who.github, who.linkedin) if p)
        links_line = f"\n{links}" if links else ""
        salutation = f"Hi {company_name} Team," if company_name else "Hi there,"
        contact_line = f"\n{who.mobile} | {who.location}" if who.mobile else f"\n{who.location}"
        prompt = f"""
You are {who.name}, writing a direct job application email to an HR recruiter for the "{role_name}" role at "{company_name or 'your company'}".

Applicant Background (exact facts, use them):
- Name: {who.name}
- Total Experience: {who.experience_label} building production software systems (Full Stack & AI)
- Current CTC: {who.current_ctc}
- Expected CTC: {who.expected_ctc}
- Notice Period: {who.notice_label}
- Current Location: {who.location}
- Mobile / WhatsApp: {who.mobile}
- Links:{links_line if links_line else ' none'}

Job Requirements to Target:
{job_description[:2500] if job_description else 'Full-stack and AI software engineering.'}

CONTEXT:
This is for the Indian tech job market. HR recruiters scan cold emails in 5 seconds to verify core hiring criteria: stack match, years, current CTC, expected CTC, notice period, location, and mobile number. A clean, structured quick candidate snapshot is essential for HR screening.

RULES FOR THE EMAIL:
1. Salutation: "{salutation}"
2. Intro (2 sentences): Apply for the {role_name} role, stating you have {who.experience_label} of experience building production systems, naming 2-3 of THEIR exact stack terms from the job requirements, and summarizing the services you developed.
3. Quick Candidate Snapshot (clean bullet points for rapid HR scan):
   • Name: {who.name}
   • Total Experience: {who.experience_label} ({role_name})
   • Current CTC: {who.current_ctc}
   • Expected CTC: {who.expected_ctc}
   • Notice Period: {who.notice_label}
   • Current Location: {who.location}
   • Mobile: {who.mobile}
   • Key Technical Skills: (name 4-6 matching tools from their description, dynamically customized to their requirements)
4. CTA (1 sentence): Mention resume is attached and request a short introductory call this week.
5. Sign off:
   Best regards,
   {who.name}
   {contact_line}{links_line}

6. FORBIDDEN:
   - Buzzwords: passionate, cutting-edge, synergy, leverage, rockstar, ninja, guru, delve, testament, thrilled, robust, game-changer, world-class, esteemed, utmost.
   - Sob stories, hustle drama, or begging paragraphs.
   - Never invent metrics, percentages, user counts, or company names.
7. Output ONLY the raw email body. No markdown fences, no subject lines, no placeholders.
"""

        payload = {
            "contents": [{"parts": [{"text": prompt.strip()}]}],
            # NOTE (Sep 2026): the pinned preview model is a thinking model —
            # hidden chain-of-thought consumes the SAME token budget as visible
            # output. 350 tokens starved every reply to ~11 visible tokens
            # (finishReason MAX_TOKENS, thoughtsTokenCount ~335). 2048 leaves
            # room for thought AND the full email. Verified live.
            "generationConfig": {
                "temperature": 0.5,
                "maxOutputTokens": 1024,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        data = json.dumps(payload).encode("utf-8")
        # API key travels in a header, never in the URL query, because
        # proxies, logs and tracebacks capture URLs.
        endpoint = f"{GEMINI_BASE_URL}/{self.model}:generateContent"
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )

        try:
            # With thinkingBudget=0, responses return cleanly in 2-4s without timeout.
            with urllib.request.urlopen(req, timeout=15) as response:
                if response.status == 200:
                    resp_json: dict[str, Any] = json.loads(response.read().decode("utf-8"))
                    candidates = resp_json.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            text = parts[0].get("text", "").strip()
                            if text:
                                clean_lines = []
                                for line in text.splitlines():
                                    if line.lower().startswith("subject:"):
                                        continue
                                    clean_lines.append(line.replace("**", "").replace("`", ""))
                                while clean_lines and re.match(
                                    r"^(text|markdown|email|plain)$",
                                    clean_lines[0].strip().lower(),
                                ):
                                    clean_lines.pop(0)
                                while clean_lines and not clean_lines[-1].strip():
                                    clean_lines.pop()
                                cleaned_text = "\n".join(clean_lines).strip()
                                self._server_errors = 0
                                log.info(
                                    "gemini.email_generated",
                                    role=role_name,
                                    model=self.model,
                                    length=len(cleaned_text),
                                )
                                return cleaned_text
                    log.warning("gemini.empty_response", role=role_name, model=self.model)
        except urllib.error.HTTPError as exc:
            log.warning("gemini.api_http_error", model=self.model, status=exc.code, reason=str(exc)[:150])
            if exc.code in (401, 403, 404):
                self._model_broken = True
                log.error(
                    "gemini.model_unavailable",
                    model=self.model,
                    status=exc.code,
                    detail="Check GEMINI_MODEL in .env — template fallback for the rest of this run.",
                )
            elif 500 <= exc.code < 600:
                self._server_errors += 1
                if self._server_errors >= 3:
                    self._model_broken = True
                    log.error(
                        "gemini.model_overloaded",
                        model=self.model,
                        consecutive_5xx=self._server_errors,
                        detail="Model flapping — template fallback for the rest of this run.",
                    )
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            log.warning("gemini.api_error", model=self.model, error=str(exc)[:150])

        return None
