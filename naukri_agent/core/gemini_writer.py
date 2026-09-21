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
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Last-resort default when Settings cannot load. Must equal
# `Settings.gemini_model`'s default; GEMINI_MODEL env overrides both.
PINNED_DEFAULT_MODEL = "gemini-3-flash-preview"


@dataclass(slots=True)
class ApplicantSnapshot:
    """Identity facts for email copy, resolved from AgentConfig."""

    name: str = "a Software Engineer"
    location: str = "India"
    experience_label: str = "2+ years"
    notice_label: str = "immediate"
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

    name = (config.applicant_name or "").strip() or "a Software Engineer"
    location = (config.applicant_location or "").strip() or "India"

    years: float | None = None
    try:
        years = config.experience.total_years
    except (AttributeError, ValueError):
        years = None
    if not years or years <= 0:
        for profile in config.active_profiles():
            if profile.experience_years and profile.experience_years > 0:
                years = profile.experience_years
                break
    experience_label = f"{years:g} years" if years and years > 0 else "2+ years"

    notice_label = "immediate"
    try:
        answers = config.answers or {}
        lowered = {str(k).strip().lower(): str(v).strip() for k, v in answers.items()}
        notice_raw = lowered.get("notice period", "")
        if notice_raw:
            notice_label = notice_raw if "day" in notice_raw.lower() else f"{notice_raw} notice"
    except (AttributeError, ValueError) as exc:
        log.debug("gemini.notice_unavailable", error=str(exc)[:120])

    return ApplicantSnapshot(
        name=name,
        location=location,
        experience_label=experience_label,
        notice_label=notice_label,
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
        if is_immediate_joiner(who.notice_label):
            availability_line = "I am an immediate joiner and can start right away."
        else:
            availability_line = f"My notice period is {who.notice_label}."
        prompt = f"""
You are {who.name}, a Software Engineer writing a direct job application email for the "{role_name}" role at "{company_name or 'your company'}".

Applicant Background (true facts, use them):
- Experience: {who.experience_label} building production software systems
- Location: {who.location}
- Notice Period: {who.notice_label} (immediate joiner)
- Links:{links_line if links_line else " none"}

Job Requirements to Target:
{job_description[:2500] if job_description else "Full-stack software engineering."}

CONTEXT THAT DECIDES THE TONE: this is the Indian job market (Naukri/Instahyre norms). The reader is almost always an HR recruiter doing 3-second checklist filtering — stack match, years, notice period, resume — not a tech lead to impress with stories. Nobody oversells themselves here; confidence is quiet and factual. Write for the HR scan first, with one solid proof line the hiring manager will also respect.

RULES FOR THE EMAIL:
1. Length: 80 to 110 words. Must read fully on a phone screen. No "Hope you are doing well" opener.
2. Structure:
   - Salutation: "{salutation}"
   - Hook (1 sentence): experienced engineer applying for the {role_name} role, naming 2-3 of THEIR exact stack terms from the description.
   - Proof (2 sentences): what you built with those exact tools in production — systems shipped, services maintained, uptime owned. Concrete verbs only.
   - Availability + CTA (1-2 sentences): {availability_line} Resume attached, request a short intro call this week.
   - Sign off exactly: "Best regards,\\n{who.name}\\n{who.location}{links_line}"
3. HUMAN PUNCTUATION (anti-bot rules — a recruiter must believe a person typed this on a phone):
   - NEVER use em dashes (—), en dashes (–), or dash-joined clauses (-). Use commas and periods. Hyphens stay ONLY inside single technical terms (e.g. full-stack).
   - NEVER use bullet points or numbered lists. Plain sentences only.
   - NEVER use colon labels ("Tech Stack:", "Notice Period:"). Write everything as normal sentences.
4. FORBIDDEN:
   - Buzzwords: passionate, cutting-edge, synergy, leverage, rockstar, ninja, guru, delve, testament, thrilled, robust, game-changer, world-class, esteemed, utmost.
   - Sob stories and hustle drama: NEVER mention late nights, 2am debugging, sweat, struggle, sacrifice, or nights. Talk reliability and shipping, never hours worked.
   - Begging or oversell: no "align with your goals", no "hit the ground running", no "thanking for your time and consideration" paragraph — one plain "Thanks," is enough inside the CTA.
5. NUMBERS DISCIPLINE: never invent metrics, percentages, user counts, company names, or tools. You may echo scale signals ONLY if written in their description. No proof exists without evidence — stay truthful over punchy, always.
6. Output ONLY the raw email body. No markdown, no subject lines, no placeholders.
"""

        payload = {
            "contents": [{"parts": [{"text": prompt.strip()}]}],
            # NOTE (Sep 2026): the pinned preview model is a thinking model —
            # hidden chain-of-thought consumes the SAME token budget as visible
            # output. 350 tokens starved every reply to ~11 visible tokens
            # (finishReason MAX_TOKENS, thoughtsTokenCount ~335). 2048 leaves
            # room for thought AND the full email. Verified live.
            "generationConfig": {"temperature": 0.5, "maxOutputTokens": 2048},
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
            # Thinking models reason before writing: 12s starved most calls
            # into timeouts on busy hours. 30s comfortably covers thought +
            # output at 2048 tokens.
            with urllib.request.urlopen(req, timeout=30) as response:
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
                                # Markdown fence bleed: a ```text / ```markdown
                                # wrapper would otherwise leave the bare word
                                # "text"/"markdown" as line 1 of the email.
                                while clean_lines and re.match(
                                    r"^(text|markdown|email|plain)$",
                                    clean_lines[0].strip().lower(),
                                ):
                                    clean_lines.pop(0)
                                while clean_lines and not clean_lines[-1].strip():
                                    clean_lines.pop()
                                cleaned_text = "\n".join(clean_lines).strip()
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
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            log.warning("gemini.api_error", model=self.model, error=str(exc)[:150])

        return None
