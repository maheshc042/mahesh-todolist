"""
LinkedIn Automation Module.

Includes:
- Post & recruiter lead analyzer (`analyzer.py`)
- Post scraper with stealth context (`scraper.py`)
- Cold email campaign runner (`campaign.py`)
"""
from .analyzer import (
    TRACK_1_AI_URL,
    TRACK_2_FULLSTACK_URL,
    classify_role,
    extract_recruiter_emails,
    is_experience_match,
)
from .campaign import run_campaign
from .scraper import CookieExpiredError, LinkedInHunter

__all__ = [
    "LinkedInHunter",
    "CookieExpiredError",
    "run_campaign",
    "TRACK_1_AI_URL",
    "TRACK_2_FULLSTACK_URL",
    "classify_role",
    "extract_recruiter_emails",
    "is_experience_match",
]
