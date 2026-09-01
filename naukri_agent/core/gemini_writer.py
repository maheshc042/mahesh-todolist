"""
Google Gemini AI Cold Email & Cover Letter Writer.

Uses Google Gemini's `gemini-2.5-flash` model to generate tailored,
concise, and high-converting cold email body copy based on the job description.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..config import AgentConfig
from ..logging_setup import get_logger

log = get_logger(__name__)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"



class GeminiWriter:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()

    def generate_email_body(
        self,
        role_name: str,
        job_description: str = "",
        company_name: str = "",
    ) -> str | None:
        """
        Generates a custom cold email copy tailored to the job description using Google Gemini.
        Returns None if GEMINI_API_KEY is unconfigured or request fails.
        """
        if not self.api_key:
            log.debug("gemini.disabled", reason="GEMINI_API_KEY is not set.")
            return None

        config = AgentConfig.load()
        name = config.applicant_name or "a Software Engineer"
        location = config.applicant_location or "India"

        prompt = f"""
You are {name}, a Software Engineer with 2.6+ years of hands-on experience based in {location}.
Your core stack includes Python, FastAPI, React, Node.js, and AI/LLM integrations.
You are available to join immediately (0-day notice period).

Write a high-converting cold email to a recruiter for the "{role_name}" role at "{company_name or 'the company'}".

Job Description Context:
{job_description[:2500] if job_description else "No description provided."}

STRICT INSTRUCTIONS FOR THE EMAIL:
1. Ignore all HR boilerplate, benefits, and "Equal Opportunity" text in the description. Focus ONLY on the core technical requirements.
2. Structure: 
   - Salutation & Hook: Start directly with "Hi there," and 1 sentence mentioning the role.
   - Value: 1-2 sentences strictly highlighting hands-on expertise matching their technical stack.
   - Immediate Joiner: 1 sentence emphasizing immediate availability (0-day notice period).
   - Call to Action: 1 short sentence mentioning the attached resume and welcoming a discussion.
   - Sign off: "Best regards,\n{name}\n{location}"
3. Keep the total email strictly under 120 words.
4. Tone: Confident, direct, professional, and human. DO NOT use overly formal words like "delve", "esteemed", "testament", or "utmost".
5. Output ONLY the raw email body. No markdown formatting (no **bold**, no *italics*), no subject lines, no placeholders like [Recruiter Name] or [Company Name].
"""

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt.strip()}
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.5,
                "maxOutputTokens": 350,
            },
        }

        data = json.dumps(payload).encode("utf-8")

        models_to_try = [
            GEMINI_MODEL,
            "gemini-2.0-flash",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
        ]
        # Deduplicate while preserving order
        seen_models = set()
        unique_models = []
        for m in models_to_try:
            if m and m not in seen_models:
                seen_models.add(m)
                unique_models.append(m)

        for model in unique_models:
            endpoint = f"{GEMINI_BASE_URL}/{model}:generateContent?key={urllib.parse.quote(self.api_key)}"
            req = urllib.request.Request(
                endpoint,
                data=data,
                headers={
                    "Content-Type": "application/json",
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
                                    # Strip accidental markdown or Subject lines
                                    clean_lines = []
                                    for line in text.splitlines():
                                        if line.lower().startswith("subject:"):
                                            continue
                                        clean_lines.append(line.replace("**", "").replace("`", ""))
                                    cleaned_text = "\n".join(clean_lines).strip()
                                    log.info("gemini.email_generated", role=role_name, model=model, length=len(cleaned_text))
                                    return cleaned_text
            except urllib.error.HTTPError as exc:
                log.debug("gemini.api_http_error", model=model, status=exc.code, reason=str(exc))
                continue
            except Exception as exc:
                log.debug("gemini.api_error", model=model, error=str(exc)[:150])
                continue

        return None

