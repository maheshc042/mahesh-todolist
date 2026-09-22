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
    # Dotnet-family frameworks share one hiring pool: a Blazor / Razor /
    # ASP.NET requisition rejects a 0-dotnet resume as fast as ".NET" itself
    # (run 377 applied to a Blazor senior role). One family rule here beats a
    # 1M-term title blocklist — both haystack and needles flow through this.
    low = re.sub(r"\bblazor\b|\brazor\b|\basp[\s.-]?net\b|\bmaui\b|\bxamarin\b", "dotnet", low)
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
    "ai developer", "ai/ml", "ai ml", "artificial intelligence engineer",
    "chatbot engineer", "llm engineer", "genai engineer", "prompt engineer",
    "ai specialist", "ai lead", "ai researcher",
)

DATA_ROLES_KEYWORDS = (
    "data engineer", "data engineering", "big data", "etl developer", "etl engineer",
    "data warehouse", "snowflake developer", "databricks developer",
)

DISJOINT_SPECIALIZATIONS = (
    "big data", "etl developer", "etl engineer",
    "data warehouse", "data warehousing", "snowflake developer", "snowflake engineer",
    "databricks developer", "databricks engineer", "bi developer", "business intelligence",
    "it support", "helpdesk", "service desk", "it engineer", "systems engineer",
    "security engineer", "cyber security", "cybersecurity", "infosec", "soc analyst",
    "react native", "android developer", "ios developer", "flutter developer",
    # Hardware / embedded / core-engineering prefix class: a generic include
    # term ("software engineer", "engineer") sitting inside one of these titles
    # does not make it a software role (e.g. "Embedded Software Engineer").
    "embedded", "embedded systems", "firmware", "vlsi", "hardware",
    "mechanical", "electrical", "civil",
    # Non-software QA professions: lab/food testing shares only the "QA"
    # letters (run 368 applied to a "QA Executive - Microbiologist" food-lab
    # role). Surgical tokens — healthcare-IT (HL7, informatics) stays eligible.
    "microbiologist", "microbiology",
)

# Hybrid AI titles: full-stack/software markers that promote an AI-titled
# posting to the general 3y software-tenure gate instead of the 2y pure-AI cap.
HYBRID_AI_TITLE_MARKERS = (
    "full stack", "fullstack", "mern", "mean", "frontend", "backend",
    "react", "node", "sde", "software engineer", "software developer",
    "web developer",
)

# Wanted families gated by level (gate 7b): QA / DevOps / support /
# data-developer roles are in-scope only below ~2y stated minimum.
JUNIOR_FAMILY_KEYWORDS = (
    "qa", "automation", "sdet", "quality analyst", "quality engineer",
    "test engineer", "manual testing", "automation testing",
    "devops", "devops engineer", "platform engineer", "cloud engineer",
    "site reliability", "sre",
    "database developer", "sql developer", "data engineer", "data analyst",
    "technical support", "support engineer", "application support",
    "desktop support", "customer success",
)

# Same keyword set the LinkedIn card scraper uses to impute 5.0y/10.0y when no
# stated range exists. An imputed number corroborated by the title itself is
# trustworthy; one triggered by stray card text is not.
_IMPUTED_SENIORITY_MARKERS = ("senior", "sr.", "sr ", "lead", "principal", "staff")


def _title_suggests_senior(title: str) -> bool:
    low = f" {(title or '').lower()} "
    return any(marker in low for marker in _IMPUTED_SENIORITY_MARKERS)


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
            tech_norm = _normalize_tech_text(job.title)
            if tech_norm and tech_norm not in title_variants:
                title_variants.append(tech_norm)
            spaced_hyphen = re.sub(r"\s*-\s*", " ", title).strip()
            if spaced_hyphen and spaced_hyphen not in title_variants:
                title_variants.append(spaced_hyphen)
            no_hyphen = re.sub(r"\s*-\s*", "", title).strip()
            if no_hyphen and no_hyphen not in title_variants:
                title_variants.append(no_hyphen)
            if "software development engineer" in title or "software development engineer" in tech_norm:
                sde_equiv = re.sub(r"\bsoftware development engineer\b", "software engineer", tech_norm)
                if sde_equiv not in title_variants:
                    title_variants.append(sde_equiv)
                title_variants.extend(["sde", "software development engineer", "software engineer"])

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
                "software development engineer", "software development engineer 1", "software development engineer 2",
            }
            is_broad_title = any(
                _contains_any(title, [bt]) for bt in broad_generic_titles
            )
            specialized_tech_in_title = _contains_any(
                title,
                [
                    "react", "node", "python", "mern", "frontend", "backend", "full stack",
                    "fullstack", "ai", "ml", "fastapi", "django", "llm", "genai", "qa",
                    "automation", "sdet", "devops", "cloud", "database", "sql",
                ],
            ) is not None
            if is_broad_title and not specialized_tech_in_title:
                # Do NOT reject if description/tags are not loaded (e.g. search cards)
                # or if the title is explicitly a target software role requested in title_must_include_any
                has_card_content = bool(job.tags or job.description)
                is_target_role = any(
                    _contains_any(title, [tr])
                    for tr in (
                        "software engineer", "software developer", "sde", "associate software engineer",
                        "junior software engineer", "full stack", "backend developer", "frontend developer",
                        "software development engineer"
                    )
                )
                if has_card_content and not is_target_role:
                    haystack = _build_searchable_haystack(job)
                    core_skills = getattr(self.candidate, "core_skills", [])
                    sec_skills = getattr(self.candidate, "secondary_skills", [])
                    has_core_match = any(
                        _exact_word_match(s, haystack)
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

        # 7. AI / ML & Data Engineering Recruiter Reality Gate
        # Candidate has 3y total software experience with 2y commercial AI.
        # Recruiters for DEDICATED AI or Data Engineering roles requiring >
        # 2.0 years will immediately reject for lack of tenure — the 2.0y cap
        # stays for pure AI/ML titles.
        #
        # HYBRID titles (full-stack/software markers alongside AI terms, e.g.
        # "Applied AI Engineer with Fullstack") hire a software engineer first:
        # they are judged on the 3y software tenure via the general gate below,
        # not the 2y AI tenure.
        #
        # Provenance guard: `experience_imputed` numbers are scraper guesses, not
        # recruiter statements. They gate only when the title itself corroborates
        # seniority; otherwise the job flows to ranking/suitability scoring, which
        # still down-weights it via experience decay. Unknown data never rejects.
        is_ai_role = any(term in title for term in DEDICATED_AI_KEYWORDS)
        is_data_role = any(term in title for term in DATA_ROLES_KEYWORDS)
        is_hybrid_ai = is_ai_role and any(
            term in title for term in HYBRID_AI_TITLE_MARKERS
        )
        exp_trusted = job.min_experience is not None and (
            not job.experience_imputed or _title_suggests_senior(job.title)
        )
        ai_cap = rules.dedicated_ai_max_years
        if (is_ai_role or is_data_role) and not is_hybrid_ai and exp_trusted and job.min_experience > ai_cap:
            role_type = "AI/ML" if is_ai_role else "Data Engineering"
            return FilterDecision(
                False,
                SkipReason.FILTER_EXPERIENCE,
                f"{role_type} role requires {job.min_experience}y > candidate's {ai_cap:g}y AI / domain experience (recruiter will reject)",
            )

        # 7b. Junior-family level gate (QA / DevOps / support / data-developer).
        # These families are wanted, but only below ~2y: with 3y total and
        # 2y AI, a 2y+ QA/DevOps requisition is a wasted slot. Same provenance
        # guard as gate 7: trusted numbers only, unknown flows to ranking.
        is_junior_family = _contains_any(title, JUNIOR_FAMILY_KEYWORDS) is not None
        junior_cap = rules.junior_family_max_years
        if is_junior_family and exp_trusted and job.min_experience > junior_cap:
            return FilterDecision(
                False,
                SkipReason.FILTER_EXPERIENCE,
                f"junior-family role requires {job.min_experience}y > {junior_cap:g}y cap (low-probability slot)",
            )

        # 8. Numeric experience bounds (provenance-guarded: see gate 7)
        exp = rules.experience
        if exp_trusted and job.min_experience is not None:
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
        if exp_trusted and job.max_experience is not None:
            # Recruiter dummy values (e.g. >= 20 yrs on job boards indicate "no upper cap")
            is_dummy_unbounded = job.max_experience >= 20.0
            if not is_dummy_unbounded:
                if job.max_experience < exp.min_years:
                    return FilterDecision(
                        False,
                        SkipReason.FILTER_EXPERIENCE,
                        f"caps at {job.max_experience}y < min {exp.min_years}y",
                    )
                # Allow wide startup requisition bands up to the configured
                # ceiling if min_experience <= 3.5y; bands starting above the
                # general ceiling keep a fixed 6.0y safety net.
                max_ceiling = rules.max_experience_band_ceiling if (job.min_experience is not None and job.min_experience <= 3.5) else 6.0
                if job.max_experience > max_ceiling:
                    return FilterDecision(
                        False,
                        SkipReason.FILTER_EXPERIENCE,
                        f"caps at {job.max_experience}y > ceiling {max_ceiling}y (senior requisition)",
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
