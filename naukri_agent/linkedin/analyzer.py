"""
LinkedIn Post & Lead Analyzer.

Contains:
- Target search URLs for AI/Python and Full Stack/React job posts.
- Email extraction regex tailored for recruiter post text.
- Experience level matching (0-3 years / freshers).
- Role classification (AI vs Full Stack).
"""
import re
from typing import Any

TRACK_1_AI_URL = (
    "https://www.linkedin.com/search/results/content/?"
    "keywords=AI%20OR%20Python%20OR%20GenAI%20OR%20LLM%20OR%20FastAPI%20hiring%20email"
    "&origin=FACETED_SEARCH&sortBy=%5B%22date_posted%22%5D"
)

TRACK_2_FULLSTACK_URL = (
    "https://www.linkedin.com/search/results/content/?"
    "keywords=React%20OR%20%22Full%20Stack%22%20OR%20Node.js%20OR%20MERN%20hiring%20email"
    "&origin=FACETED_SEARCH&sortBy=%5B%22date_posted%22%5D"
)

EXCLUDED_EMAIL_DOMAINS = {
    "example.com",
    "domain.com",
    "linkedin.com",
}

EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)

EXPERIENCED_REJECT_REGEX = re.compile(
    r"\b(4\+|5\+|6\+|7\+|8\+|10\+|\b[4-9]\s*\+\s*years?|\b1[0-5]\s*\+\s*years?|senior|lead|principal"
    r"|(?:engineering|tech|technical|product|project|program|delivery|design)\s+manager|head\s+of)\b",
    re.IGNORECASE,
)

# Keyword lists for role classification. Matched with word boundaries so that
# e.g. "ai" does not match inside "email", and "react" not inside "reaction".
AI_KEYWORDS = [
    "ai", "python", "genai", "llm", "fastapi", "rag", "langchain", "machine learning", "pytorch",
]
FS_KEYWORDS = [
    "react", "full stack", "fullstack", "node.js", "nodejs", "mern", "next.js", "typescript", "frontend",
]


def extract_recruiter_emails(text: str) -> list[str]:
    """Extracts unique, valid recruiter email addresses from post text."""
    if not text:
        return []
    found = EMAIL_REGEX.findall(text)
    valid_emails = set()
    for email in found:
        email_clean = email.strip().lower()
        domain = email_clean.split("@")[-1]
        if domain not in EXCLUDED_EMAIL_DOMAINS and not email_clean.endswith(".png") and not email_clean.endswith(".jpg"):
            valid_emails.add(email_clean)
    return sorted(list(valid_emails))


def is_experience_match(text: str) -> bool:
    """Returns True if the post targets freshers or 0-3 years experience, rejecting senior roles."""
    if not text:
        return False
    if EXPERIENCED_REJECT_REGEX.search(text):
        return False
    return True


def _count_keyword_hits(text_lower: str, keywords: list[str]) -> int:
    """Counts keywords present in the text using word-boundary matching."""
    return sum(
        1
        for kw in keywords
        if re.search(rf"\b{re.escape(kw)}\b", text_lower)
    )


def classify_role(text: str) -> str | None:
    """Classifies the post into 'AI / Python Engineer' or 'Full Stack Engineer'."""
    if not text:
        return None
    text_lower = text.lower()

    ai_score = _count_keyword_hits(text_lower, AI_KEYWORDS)
    fs_score = _count_keyword_hits(text_lower, FS_KEYWORDS)

    if ai_score >= fs_score and ai_score > 0:
        return "AI / Python Engineer"
    elif fs_score > ai_score:
        return "Full Stack Engineer"
    elif ai_score > 0:
        return "AI / Python Engineer"
    return None
