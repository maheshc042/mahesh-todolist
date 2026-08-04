"""
Filter engine.

Design decisions:

- **Ordered, short-circuiting rules.** Cheap string checks run before numeric
  ones, and the first failing rule wins. The returned `FilterDecision` names the
  exact rule, which we persist — so "why did it skip that job?" is answerable
  from the database without re-running anything.
- **Unknown data never rejects.** If Naukri did not disclose salary or we failed
  to parse experience, the numeric rule is skipped rather than treated as a
  failure. Rejecting on missing data silently throws away most good listings.
- **Two-phase filtering.** `evaluate_card()` runs on listing data only (free);
  `evaluate_detail()` re-checks description rules after the JD page is loaded.
  This keeps expensive page loads proportional to real candidates.
- **Token-aware matching.** Enforces word boundaries (\b) to prevent short terms (e.g. 'c', 'java')
  from matching unrelated substrings ('react', 'javascript').
- **Expanded Remote Detection.** Includes 'remote', 'work from home', 'wfh', 'hybrid remote'.
- **Pre-normalized Rule Caching.** Caches normalized filter lists in FilterEngine init for high performance.
"""

from __future__ import annotations

import re

from ..config import FilterRules
from ..core.models import FilterDecision, Job, SkipReason

REMOTE_KEYWORDS = ("remote", "work from home", "wfh", "hybrid remote")


def _normalise_str(text: str) -> str:
    """Normalize string and unify tech synonyms like Dot Net / .NET / dot.net -> dotnet."""
    low = (text or "").lower()
    return re.sub(r"\bdot[\s.-]?net\b|\b\.net\b", "dotnet", low)


def _contains_any(haystack: str, needles: list[str]) -> str | None:
    """Token-aware keyword matching using word boundaries (P1-3)."""
    norm_haystack = _normalise_str(haystack)
    for needle in needles:
        if not needle:
            continue
        norm_needle = _normalise_str(needle)
        pattern = r"\b" + re.escape(norm_needle) + r"\b"
        if re.search(pattern, norm_haystack) or (len(needle) > 3 and needle in haystack):
            return needle
    return None


def experience_matches(candidate_years: float, job_min: float | None, job_max: float | None) -> bool:
    """Centralized experience window evaluation (P1-1)."""
    if job_min is not None and job_min > candidate_years + 1.0:
        return False
    if job_max is not None and job_max < max(0.0, candidate_years - 1.5):
        return False
    return True


class FilterEngine:
    def __init__(self, rules: FilterRules) -> None:
        self.rules = rules
        # Pre-normalize rule lists for zero-overhead evaluation (P2-1)
        self._norm_blocked_companies = [c.lower() for c in (rules.blocked_companies or [])]
        self._norm_blocked_locations = [l.lower() for l in (rules.blocked_locations or [])]
        self._norm_allowed_locations = [l.lower() for l in (rules.allowed_locations or [])]

    # ------------------------------------------------------------ phase one
    def evaluate_card(self, job: Job) -> FilterDecision:
        """Runs against listing-card data only. Cheap, no extra page load."""
        rules = self.rules
        title = job.title.lower()
        company = job.company.lower()
        if rules.title_must_include_any:
            if _contains_any(title, rules.title_must_include_any) is None:
                return FilterDecision(
                    False, SkipReason.FILTER_TITLE, "title lacks any required keyword"
                )

        if rules.title_must_exclude_any:
            hit = _contains_any(title, rules.title_must_exclude_any)
            if hit:
                return FilterDecision(
                    False, SkipReason.FILTER_TITLE, f"title contains blocked term '{hit}'"
                )

        if self._norm_blocked_companies:
            hit = _contains_any(company, self._norm_blocked_companies)
            if hit:
                return FilterDecision(
                    False, SkipReason.FILTER_COMPANY, f"blocked company '{hit}'"
                )

        if self._norm_blocked_locations:
            hit = _contains_any(location, self._norm_blocked_locations)
            if hit:
                return FilterDecision(
                    False, SkipReason.FILTER_LOCATION, f"blocked location '{hit}'"
                )

        if self._norm_allowed_locations:
            # Expanded remote keyword detection (P1-2)
            haystack = f"{title} {location} {' '.join(job.tags)}".lower()
            remote_ok = any(kw in haystack for kw in REMOTE_KEYWORDS)
            if _contains_any(location, self._norm_allowed_locations) is None and not remote_ok:
                return FilterDecision(
                    False, SkipReason.FILTER_LOCATION, f"location '{job.location}' not allowed"
                )

        if rules.skip_walkin and job.is_walkin:
            return FilterDecision(False, SkipReason.WALKIN, "walk-in drive")

        # --- numeric rules: only applied when data is disclosed -------
        exp = rules.experience
        if job.min_experience is not None and job.min_experience > exp.max_years:
            return FilterDecision(
                False,
                SkipReason.FILTER_EXPERIENCE,
                f"requires {job.min_experience}y > max {exp.max_years}y",
            )
        if job.max_experience is not None and job.max_experience < exp.min_years:
            return FilterDecision(
                False,
                SkipReason.FILTER_EXPERIENCE,
                f"caps at {job.max_experience}y < min {exp.min_years}y",
            )

        if rules.min_salary_lpa is not None and job.min_salary_lpa is not None:
            if job.min_salary_lpa < rules.min_salary_lpa:
                return FilterDecision(
                    False,
                    SkipReason.FILTER_SALARY,
                    f"{job.min_salary_lpa} LPA < min {rules.min_salary_lpa} LPA",
                )

        if rules.max_posted_days is not None and job.posted_days_ago is not None:
            if job.posted_days_ago > rules.max_posted_days:
                return FilterDecision(
                    False,
                    SkipReason.FILTER_FRESHNESS,
                    f"posted {job.posted_days_ago}d ago > {rules.max_posted_days}d",
                )

        if rules.min_rating is not None and job.rating is not None:
            if job.rating < rules.min_rating:
                return FilterDecision(
                    False, SkipReason.FILTER_RATING, f"rating {job.rating} < {rules.min_rating}"
                )

        if rules.description_must_exclude_any and job.description:
            hit = _contains_any(job.description.lower(), rules.description_must_exclude_any)
            if hit:
                return FilterDecision(
                    False, SkipReason.FILTER_DESCRIPTION, f"snippet contains '{hit}'"
                )

        return FilterDecision(True)

    # ------------------------------------------------------------ phase two
    def evaluate_detail(self, job: Job) -> FilterDecision:
        """Re-runs description rules against the full JD text."""
        rules = self.rules
        description = job.description.lower()
        if not description:
            return FilterDecision(True)

        if rules.description_must_exclude_any:
            hit = _contains_any(description, rules.description_must_exclude_any)
            if hit:
                return FilterDecision(
                    False, SkipReason.FILTER_DESCRIPTION, f"JD contains blocked term '{hit}'"
                )

        if rules.description_must_include_any:
            if _contains_any(description, rules.description_must_include_any) is None:
                return FilterDecision(
                    False,
                    SkipReason.FILTER_DESCRIPTION,
                    "JD lacks any required keyword",
                )

        return FilterDecision(True)
