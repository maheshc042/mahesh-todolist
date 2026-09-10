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
from typing import Any

from ..config import FilterRules
from ..core.models import FilterDecision, Job, SkipReason

REMOTE_KEYWORDS = ("remote", "work from home", "wfh", "hybrid remote")


def _normalise_str(text: str) -> str:
    """Normalize string and unify tech synonyms like Dot Net / .NET / dot.net -> dotnet, reactjs -> react, nodejs -> node."""
    low = (text or "").lower()
    low = re.sub(r"\bdot[\s.-]?net\b|(?<!\w)\.net\b", "dotnet", low)
    low = re.sub(r"\breact[\s.-]?js\b", "react", low)
    low = re.sub(r"\bnode[\s.-]?js\b", "node", low)
    low = re.sub(r"\bnext[\s.-]?js\b", "next", low)
    return low


def _contains_any(haystack: str, needles: list[str]) -> str | None:
    """Token-aware keyword matching using boundary-safe regex (P1-3)."""
    norm_haystack = _normalise_str(haystack)
    for needle in needles:
        if not needle:
            continue
        norm_needle = _normalise_str(needle)
        left_b = r"\b" if re.match(r"^\w", norm_needle) else r"(?<!\w)"
        right_b = r"\b" if re.search(r"\w$", norm_needle) else r"(?!\w)"
        pattern = left_b + re.escape(norm_needle) + right_b
        if re.search(pattern, norm_haystack, re.IGNORECASE):
            return needle
    return None


DEDICATED_AI_KEYWORDS = (
    "machine learning", "ml engineer", "ml developer", "data scientist",
    "deep learning", "nlp engineer", "computer vision", "ai engineer",
    "ai developer", "ai/ml", "ai ml", "artificial intelligence engineer" ,"chatbot engineer"
)

DISJOINT_SPECIALIZATIONS = (
    "big data", "etl developer", "etl engineer",
    "data warehouse", "data warehousing", "snowflake developer", "snowflake engineer",
    "databricks developer", "databricks engineer", "bi developer", "business intelligence",
    "desktop support", "it support", "helpdesk", "service desk", "it engineer", "systems engineer",
    "security engineer", "cyber security", "cybersecurity", "infosec", "soc analyst",
)


def experience_matches(
    candidate_years: float,
    job_min: float | None,
    job_max: float | None,
    is_dedicated_ai: bool = False,
) -> bool:
    """Centralized experience window evaluation (P1-1)."""
    if is_dedicated_ai and job_min is not None and job_min > 2.0:
        return False
    if job_min is not None:
        if job_min > 3.5:
            return False
        if job_min > candidate_years + 1.0:
            return False
    if job_max is not None:
        if job_max < max(0.0, candidate_years - 1.5):
            return False
        max_ceiling = 8.5 if (job_min is not None and job_min <= 3.5) else 6.0
        if job_max > max_ceiling:
            return False
    return not (job_min is not None and job_max is not None and (job_max - job_min) >= 5.0 and job_min > 3.0)


TECH_ALIASES = (
    (r"\bfast\s*api\b", "fastapi"),
    (r"\bnode\s*js\b", "nodejs"),
    (r"\breact\s*js\b", "react"),
    (r"\bnext\s*js\b", "nextjs"),
    (r"\bvue\s*js\b", "vue"),
    (r"\brest\s*api\b", "restapi"),
    (r"\bllm\s*ops\b", "llmops"),
    (r"\bgen\s*ai\b", "genai"),
)


def _normalize_tech_text(text: str) -> str:
    """Normalize technology variations, hyphens, and aliases."""
    if not text:
        return ""
    norm = text.lower()
    norm = re.sub(r"\bdot[\s.-]?net\b|(?<!\w)\.net\b", "dotnet", norm)
    for pattern, replacement in TECH_ALIASES:
        norm = re.sub(pattern, replacement, norm)
    norm = norm.replace("-", " ")
    return " ".join(norm.split())


def _build_searchable_haystack(job: Job) -> str:
    """Construct one unified normalized searchable text for the Job."""
    raw_text = f"{job.title} {job.description} {' '.join(job.tags or [])} {job.company} {job.location}"
    return _normalize_tech_text(raw_text)


def _exact_word_match(word: str, text: str) -> bool:
    """Exact word boundary match after tech normalization."""
    norm_word = _normalize_tech_text(word)
    if not norm_word:
        return False
    pattern = r"\b" + re.escape(norm_word) + r"\b"
    return bool(re.search(pattern, text))


class FilterEngine:
    def __init__(self, rules: FilterRules, candidate: Any | None = None) -> None:
        self.rules = rules
        self.candidate = candidate
        # Pre-normalize rule lists for zero-overhead evaluation
        self._norm_blocked_companies = [c.lower() for c in (rules.blocked_companies or [])]
        self._norm_blocked_locations = [l.lower() for l in (rules.blocked_locations or [])]
        self._norm_allowed_locations = [l.lower() for l in (rules.allowed_locations or [])]

    # ------------------------------------------------------------ phase one
    def evaluate_card(self, job: Job) -> FilterDecision:
        """Runs against listing-card data only. Cheap, no extra page load."""
        rules = self.rules
        title = job.title.lower()
        company = job.company.lower()
        location = (job.location or "").lower()

        # 0. Check Easy Apply requirement (Card Pre-filter)
        if rules.require_easy_apply and getattr(job, "is_external", False):
            return FilterDecision(
                False, SkipReason.EXTERNAL_APPLY, "apply-on-company-site only (require_easy_apply=True)"
            )

        # 1. Configured title blocklist (Strict: Zero exceptions for blocked stacks)
        if rules.title_must_exclude_any:
            hit = _contains_any(title, rules.title_must_exclude_any)
            if hit:
                return FilterDecision(
                    False, SkipReason.FILTER_TITLE, f"title contains blocked term '{hit}'"
                )

        # 1.1 Disjoint Specialization Reality Gate
        # Candidate is a Full-Stack / Software / AI Engineer (FastAPI, Python, React, TypeScript).
        # Candidate has 0 commercial experience in pure Data Engineering (ETL pipelines, Snowflake, Spark)
        # or Technical Support / IT helpdesk. Recruiters for these specialized roles immediately reject
        # candidates without prior domain experience, exhausting daily platform quotas.
        hit_disjoint = _contains_any(title, DISJOINT_SPECIALIZATIONS)
        if hit_disjoint:
            candidate_skills = getattr(self.candidate, "core_skills", []) or []
            if not any(hit_disjoint in s.lower() for s in candidate_skills):
                return FilterDecision(
                    False,
                    SkipReason.FILTER_TITLE,
                    f"title contains disjoint specialization '{hit_disjoint}' (candidate lacks requisite experience; recruiter will reject)",
                )

        # 2. Mandatory Title Role Matching
        if rules.title_must_include_any:
            title_variants = [title]
            if "/" in job.title:
                parts = [p.strip().lower() for p in re.split(r"\s*/\s*", job.title) if p.strip()]
                if len(parts) >= 2:
                    suffix_match = re.search(r"\b(engineer|developer|specialist|architect|consultant|analyst|lead)\b", parts[-1])
                    if suffix_match:
                        suffix = suffix_match.group(0)
                        for part in parts[:-1]:
                            title_variants.append(f"{part} {suffix}")
                    for part in parts:
                        title_variants.append(part)

            matched_title_term = any(_contains_any(v, rules.title_must_include_any) for v in title_variants)
            if not matched_title_term:
                return FilterDecision(
                    False,
                    SkipReason.FILTER_TITLE,
                    f"title '{job.title}' lacks required role keywords from title_must_include_any",
                )

        # 2.1 For broad engineering titles, verify matching core tech stack
        if self.candidate:
            broad_generic_titles = {
                "software engineer", "software developer", "sde", "sde 1", "sde 2",
                "sde-1", "sde-2", "sde i", "sde ii", "developer", "engineer", "programmer",
                "member of technical staff", "associate software engineer",
            }
            is_broad_title = any(
                _contains_any(title, [bt]) for bt in broad_generic_titles
            )
            specialized_tech_in_title = any(
                s in title for s in (
                    "react", "node", "python", "mern", "frontend", "backend", "full stack",
                    "fullstack", "ai", "ml", "fastapi", "django", "llm", "genai", "qa",
                    "automation", "sdet", "devops", "cloud", "database", "sql",
                )
            )
            if is_broad_title and not specialized_tech_in_title:
                # Do NOT reject if description/tags are not loaded (e.g. search cards)
                # or if the title is explicitly a target software role requested in title_must_include_any
                has_card_content = bool(job.tags or job.description)
                is_target_role = any(
                    _contains_any(title, [tr])
                    for tr in (
                        "software engineer", "software developer", "sde", "associate software engineer",
                        "junior software engineer", "full stack", "backend developer", "frontend developer"
                    )
                )
                if has_card_content and not is_target_role:
                    haystack = _build_searchable_haystack(job)
                    core_skills = getattr(self.candidate, "core_skills", [])
                    sec_skills = getattr(self.candidate, "secondary_skills", [])
                    has_core_match = any(
                        _exact_word_match(s, haystack) or _normalize_tech_text(s) in haystack
                        for s in list(core_skills) + list(sec_skills)
                    )
                    if not has_core_match:
                        return FilterDecision(
                            False,
                            SkipReason.FILTER_TITLE,
                            f"generic title '{job.title}' lacks matching candidate core skills in listing tags",
                        )

        # 3. Blocked companies
        if self._norm_blocked_companies:
            hit = _contains_any(company, self._norm_blocked_companies)
            if hit:
                return FilterDecision(
                    False, SkipReason.FILTER_COMPANY, f"blocked company '{hit}'"
                )

        # 4. Blocked locations
        if self._norm_blocked_locations:
            hit = _contains_any(location, self._norm_blocked_locations)
            if hit:
                return FilterDecision(
                    False, SkipReason.FILTER_LOCATION, f"blocked location '{hit}'"
                )

        # 5. Allowed locations & remote check
        if self._norm_allowed_locations:
            haystack = f"{title} {location} {' '.join(job.tags or [])}".lower()
            remote_ok = any(kw in haystack for kw in REMOTE_KEYWORDS)
            if _contains_any(location, self._norm_allowed_locations) is None and not remote_ok:
                return FilterDecision(
                    False, SkipReason.FILTER_LOCATION, f"location '{job.location}' not allowed"
                )

        # 6. Walk-in check (Bangalore/Bengaluru is allowed to apply and alert)
        if rules.skip_walkin and job.is_walkin:
            if "bengaluru" in location or "bangalore" in location:
                pass
            else:
                return FilterDecision(False, SkipReason.WALKIN, f"walk-in drive outside Bangalore ({job.location})")

        # 7. Dedicated AI / ML Recruiter Reality Gate
        is_dedicated_ai = any(term in title for term in DEDICATED_AI_KEYWORDS)
        is_hybrid_dev = any(dev_term in title for dev_term in ("full stack", "fullstack", "software", "backend", "web", "developer", "engineer", "sde"))
        is_pure_research = any(res_term in title for res_term in ("research", "scientist", "phd"))
        if is_dedicated_ai and not (is_hybrid_dev and not is_pure_research):
            if job.min_experience is not None and job.min_experience > 2.0:
                return FilterDecision(
                    False,
                    SkipReason.FILTER_EXPERIENCE,
                    f"dedicated AI/ML role requires {job.min_experience}y > candidate's 6m AI experience (recruiter will reject)",
                )

        # 7.1 Data Engineering Reality Gate: Allow junior/entry (<= 2.0y), reject senior (> 2.5y)
        if _contains_any(title, ["data engineer", "data engineering"]) and job.min_experience is not None and job.min_experience > 2.5:
            return FilterDecision(
                False,
                SkipReason.FILTER_EXPERIENCE,
                f"data engineering role requires {job.min_experience}y > candidate's experience (senior ETL/pipeline role)",
            )

        # 8. Numeric experience bounds
        exp = rules.experience
        if job.min_experience is not None:
            if job.min_experience > 3.5:
                return FilterDecision(
                    False,
                    SkipReason.FILTER_EXPERIENCE,
                    f"requires {job.min_experience}y > ceiling 3.5y",
                )
            if job.min_experience > exp.max_years:
                return FilterDecision(
                    False,
                    SkipReason.FILTER_EXPERIENCE,
                    f"requires {job.min_experience}y > max {exp.max_years}y",
                )
        if job.max_experience is not None:
            # Recruiter dummy values (e.g. >= 20 yrs on job boards indicate "no upper cap")
            is_dummy_unbounded = job.max_experience >= 20.0
            if not is_dummy_unbounded:
                if job.max_experience < exp.min_years:
                    return FilterDecision(
                        False,
                        SkipReason.FILTER_EXPERIENCE,
                        f"caps at {job.max_experience}y < min {exp.min_years}y",
                    )
                # Allow wide startup requisition bands up to 8.5y if min_experience <= 3.5y
                max_ceiling = 8.5 if (job.min_experience is not None and job.min_experience <= 3.5) else 6.0
                if job.max_experience > max_ceiling:
                    return FilterDecision(
                        False,
                        SkipReason.FILTER_EXPERIENCE,
                        f"caps at {job.max_experience}y > ceiling {max_ceiling}y (senior requisition)",
                    )
                # Only reject wide spread if min_experience is senior (> 3.0y)
                if job.min_experience is not None and (job.max_experience - job.min_experience) >= 5.0 and job.min_experience > 3.0:
                    return FilterDecision(
                        False,
                        SkipReason.FILTER_EXPERIENCE,
                        f"spread {job.min_experience}-{job.max_experience}y >= 5y (broad senior requisition)",
                    )

        # 9. Salary, freshness, rating
        if rules.min_salary_lpa is not None:
            effective_salary = job.max_salary_lpa if job.max_salary_lpa is not None else job.min_salary_lpa
            if effective_salary is not None and effective_salary < rules.min_salary_lpa:
                return FilterDecision(
                    False,
                    SkipReason.FILTER_SALARY,
                    f"max offer {effective_salary} LPA < min {rules.min_salary_lpa} LPA",
                )

        if rules.max_posted_days is not None and job.posted_days_ago is not None and job.posted_days_ago > rules.max_posted_days:
            return FilterDecision(
                False,
                SkipReason.FILTER_FRESHNESS,
                f"posted {job.posted_days_ago}d ago > {rules.max_posted_days}d",
            )

        if rules.min_rating is not None and job.rating is not None and job.rating < rules.min_rating:
            return FilterDecision(
                False, SkipReason.FILTER_RATING, f"rating {job.rating} < {rules.min_rating}"
            )

        # 10. Description snippet & tags blocklist (pre-navigation)
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

        if rules.description_must_include_any and _contains_any(description, rules.description_must_include_any) is None:
            return FilterDecision(
                False,
                SkipReason.FILTER_DESCRIPTION,
                "JD lacks any required keyword",
            )

        return FilterDecision(True)
