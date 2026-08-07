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

from ..logging_setup import get_logger

log = get_logger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"
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

        prompt = f"""
You are Mahesh Chitakoti, a Software Engineer based in Bengaluru, India with 2.6 years of hands-on experience. 
Your core stack includes Python, FastAPI, React, Node.js, and AI/LLM integrations.

Write a high-converting cold email to a recruiter for the "{role_name}" role at "{company_name or 'the company'}".

Job Description Context:
{job_description[:2500] if job_description else "No description provided."}

STRICT INSTRUCTIONS FOR THE EMAIL:
1. Ignore all HR boilerplate, benefits, and "Equal Opportunity" text in the description. Focus ONLY on the core technical requirements.
2. Structure: 
   - Hook: 1 sentence mentioning the specific role and company.
   - Value: 1-2 sentences strictly highlighting ONE specific project or tech skill of mine that matches their exact core requirement.
   - Call to Action: 1 short sentence mentioning the attached resume and asking for a brief chat.
3. Keep the total email strictly under 120 words.
4. Tone: Confident, direct, conversational, and human. DO NOT use overly formal words like "delve", "esteemed", "testament", or "utmost".
5. Output ONLY the raw email body. No subject lines, no markdown, no placeholders like [Recruiter Name]. Start directly with "Hi there,".
"""

        endpoint = f"{GEMINI_BASE_URL}/{GEMINI_MODEL}:generateContent?key={urllib.parse.quote(self.api_key)}"

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
                                log.info("gemini.email_generated", role=role_name, length=len(text))
                                return text
        except urllib.error.HTTPError as exc:
            log.warning("gemini.api_http_error", status=exc.code, reason=str(exc))
        except Exception as exc:
            log.warning("gemini.api_error", error=str(exc)[:150])

        return None
