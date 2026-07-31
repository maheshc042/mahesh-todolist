"""
Text normalisation for Naukri's human-formatted fields.

Naukri renders experience as "3-6 Yrs", salary as "12-18 Lacs PA" / "₹ 8,00,000
- 12,00,000 PA" / "Not disclosed", and freshness as "Just now" / "3 days ago" /
"30+ days ago". Filters need numbers, so all parsing lives here — pure functions,
fully unit-testable, no Playwright dependency.

Design decision: parsing is intentionally forgiving and returns `None` on
failure. A `None` never rejects a job (see filters.py) because a parse miss is
our bug, not a reason to skip a good opportunity.
"""

from __future__ import annotations

import re

_NUM = r"(\d+(?:\.\d+)?)"


def parse_experience(text: str) -> tuple[float | None, float | None]:
    """'3-6 Yrs' -> (3, 6); '5+ years' -> (5, None); 'Fresher' -> (0, 0)."""
    if not text:
        return None, None
    cleaned = text.lower().replace("yrs", "").replace("years", "").replace("year", "")
    if "fresher" in text.lower():
        return 0.0, 0.0
    span = re.search(rf"{_NUM}\s*[-–to]+\s*{_NUM}", cleaned)
    if span:
        return float(span.group(1)), float(span.group(2))
    plus = re.search(rf"{_NUM}\s*\+", cleaned)
    if plus:
        return float(plus.group(1)), None
    single = re.search(_NUM, cleaned)
    if single:
        value = float(single.group(1))
        return value, value
    return None, None


def parse_salary_lpa(text: str) -> tuple[float | None, float | None]:
    """
    Normalise to LPA (lakhs per annum).
    '12-18 Lacs PA'      -> (12, 18)
    '₹ 8,00,000 - 12,00,000 PA' -> (8, 12)
    '50,000 - 1,00,000 P.A. monthly' -> (6, 12)
    'Not disclosed'      -> (None, None)
    """
    if not text:
        return None, None
    lowered = text.lower()
    if "not disclosed" in lowered or "unpaid" in lowered:
        return None, None

    is_monthly = "month" in lowered or "/ month" in lowered or "pm" in lowered
    # Strip currency symbols and Indian digit grouping.
    numeric = re.sub(r"[₹,]", "", lowered)
    values = [float(v) for v in re.findall(_NUM, numeric)]
    if not values:
        return None, None

    def to_lpa(value: float) -> float:
        if is_monthly:
            value = value * 12
        if value >= 100_000:  # absolute rupees
            return round(value / 100_000, 2)
        if value >= 1_000:  # e.g. "8,00,000" already divided, or thousands
            return round(value / 100_000, 2) if value > 10_000 else round(value / 1_000, 2)
        return round(value, 2)  # already in lakhs

    if "lac" in lowered or "lakh" in lowered or "lpa" in lowered:
        converted = [round(v * 12, 2) if is_monthly else round(v, 2) for v in values]
    else:
        converted = [to_lpa(v) for v in values]

    low = min(converted)
    high = max(converted)
    return low, (high if high != low else None)


def parse_posted_days(text: str) -> int | None:
    """'Just now'/'Today' -> 0, '3 days ago' -> 3, '30+ days ago' -> 30."""
    if not text:
        return None
    lowered = text.lower().strip()
    if any(token in lowered for token in ("just now", "few minutes", "today", "hour")):
        return 0
    if "yesterday" in lowered:
        return 1
    days = re.search(rf"{_NUM}\s*\+?\s*day", lowered)
    if days:
        return int(float(days.group(1)))
    weeks = re.search(rf"{_NUM}\s*\+?\s*week", lowered)
    if weeks:
        return int(float(weeks.group(1)) * 7)
    months = re.search(rf"{_NUM}\s*\+?\s*month", lowered)
    if months:
        return int(float(months.group(1)) * 30)
    return None


def parse_rating(text: str) -> float | None:
    if not text:
        return None
    match = re.search(_NUM, text)
    if not match:
        return None
    value = float(match.group(1))
    return value if 0 < value <= 5 else None


def normalise_whitespace(text: str) -> str:
    return " ".join((text or "").split())


def slugify_keyword(keyword: str) -> str:
    """'AI Engineer Python' -> 'ai-engineer-python' for Naukri's SEO URLs."""
    slug = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")
    return slug or "jobs"


import urllib.parse


def build_search_url(
    keyword: str,
    locations: list[str] | None = None,
    experience_years: float | None = None,
    page: int = 1,
    sort_by: str = "date",
    freshness_days: int | None = None,
    custom_url: str | None = None,
) -> str:
    """
    Naukri's search URL scheme. If custom_url is provided, use it directly.
    """
    if custom_url and custom_url.strip():
        base = custom_url.strip()
        if page > 1:
            if "?" in base:
                path, query = base.split("?", 1)
                return f"{path}-{page}?{query}&pageNo={page}"
            else:
                return f"{base}-{page}?pageNo={page}"
        return base
    first_keyword = keyword.split(",")[0].strip()
    keyword_slug = slugify_keyword(first_keyword)
    location_slug = "-".join(slugify_keyword(loc) for loc in (locations or []) if loc.strip())

    path = f"{keyword_slug}-jobs"
    if location_slug:
        path += f"-in-{location_slug}"
    if page > 1:
        path += f"-{page}"

    encoded_k = urllib.parse.quote(keyword)
    params: list[str] = [f"k={encoded_k}"]
    if locations:
        params.append("l=" + "%2C".join(urllib.parse.quote(loc) for loc in locations))
    if experience_years is not None:
        params.append(f"experience={int(experience_years)}")
    if freshness_days:
        params.append(f"jobAge={int(freshness_days)}")
    if sort_by == "date":
        params.append("sort=f")
    if page > 1:
        params.append(f"pageNo={page}")

    return f"https://www.naukri.com/{path}?{'&'.join(params)}"
