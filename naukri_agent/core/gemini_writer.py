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

        prompt = f"""
You are {who.name}, a Software Engineer with {who.experience_label} of hands-on experience based in {who.location}.
You are available to join at {who.notice_label} notice period.

Write a high-converting cold email to a recruiter for the "{role_name}" role at "{company_name or 'the company'}".

Job Description Context:
{job_description[:2500] if job_description else "No description provided."}

STRICT INSTRUCTIONS FOR THE EMAIL:
1. Ignore all HR boilerplate, benefits, and "Equal Opportunity" text in the description. Focus ONLY on the core technical requirements.
2. Structure:
   - Salutation & Hook: Start directly with "Hi there," and 1 sentence mentioning the role.
   - Value: 1-2 sentences strictly highlighting hands-on expertise matching their technical stack.
   - Immediate Joiner: 1 sentence emphasizing {who.notice_label} availability.
   - Call to Action: 1 short sentence mentioning the attached resume and welcoming a discussion.
   - Sign off: "Best regards,\\n{who.name}\\n{who.location}"
3. Keep the total email strictly under 120 words.
4. Tone: Confident, direct, professional, and human. DO NOT use overly formal words like "delve", "esteemed", "testament", or "utmost".
5. Output ONLY the raw email body. No markdown formatting (no **bold**, no *italics*), no subject lines, no placeholders like [Recruiter Name] or [Company Name].
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
            with urllib.request.urlopen(req, timeout=12) as response:
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
