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
"""

from __future__ import annotations

from ..config import FilterRules
from ..core.models import FilterDecision, Job, SkipReason


def _contains_any(haystack: str, needles: list[str]) -> str | None:
    for needle in needles:
        if needle and needle in haystack:
            return needle
    return None


class FilterEngine:
    def __init__(self, rules: FilterRules) -> None:
        self.rules = rules

    # ------------------------------------------------------------ phase one
    def evaluate_card(self, job: Job) -> FilterDecision:
        """Runs against listing-card data only. Cheap, no extra page load."""
        rules = self.rules
        title = job.title.lower()
        company = job.company.lower()
        location = job.location.lower()

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

        if rules.blocked_companies:
            hit = _contains_any(company, rules.blocked_companies)
            if hit:
                return FilterDecision(
                    False, SkipReason.FILTER_COMPANY, f"blocked company '{hit}'"
                )

        if rules.blocked_locations:
            hit = _contains_any(location, rules.blocked_locations)
            if hit:
                return FilterDecision(
                    False, SkipReason.FILTER_LOCATION, f"blocked location '{hit}'"
                )

        if rules.allowed_locations:
            # "remote" in the title/tags counts as an allowed location.
            remote_ok = "remote" in f"{title} {location} {' '.join(job.tags)}"
            if _contains_any(location, rules.allowed_locations) is None and not remote_ok:
                return FilterDecision(
                    False, SkipReason.FILTER_LOCATION, f"location '{job.location}' not allowed"
                )

        if rules.skip_walkin and job.is_walkin:
            return FilterDecision(False, SkipReason.WALKIN, "walk-in drive")

        # --- numeric rules: only applied when the data actually parsed -------
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
