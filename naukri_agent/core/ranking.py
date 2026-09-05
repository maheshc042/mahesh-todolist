"""
Deterministic Ranking Engine (Version 1).

Pipeline:
  Collected Jobs (list[Job])
          ↓
     Hard Filter (HardFilter.evaluate)
          ↓
    Eligible Jobs
          ↓
  Ranking & Scoring (RankingEngine.rank_jobs)
          ↓
   RankedJob List (list[RankedJob] sorted by total score desc, normalized 0-100)

Design principles:
- 100% Deterministic: No LLMs, ML models, non-deterministic random calls, or async IO.
- Human Candidate Mental Model: Imitates how an experienced software engineer selects
  the top 30-40 roles out of 150+ candidates to maximize interview callback probability.
- Fully Explainable: Natural human bullet explanations and detailed ScoreBreakdown.
- Extensible Architecture: Interface-driven ResumeMatcher for zero-churn V2 upgrades.
- Pure Python: No Playwright, no async, no DB connection.
- Tech Alias Normalization: Normalizes tech variations (e.g. 'Fast API' vs 'FastAPI', 'Node-JS' vs 'NodeJS').
- Multi-Key Deterministic Tie-Breaking: Sorts by Score DESC → Position ASC → Freshness ASC → Job ID ASC.
"""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..config import FilterRules
from .filters import _contains_any, _normalise_str
from .models import FilterDecision, Job, RecommendationTab, SkipReason


# ---------------------------------------------------------------------------
# Centralized Configuration & Weights
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class CandidateProfile:
    """Candidate profile with tiered skills matching an experienced engineer's resume."""

    title_keywords: list[str]
    core_skills: list[str]        # High priority (e.g. Python, FastAPI, LangChain, LLM, RAG, PostgreSQL)
    secondary_skills: list[str]   # Medium priority (e.g. Docker, Redis, Celery, AWS, GitHub Actions)
    bonus_skills: list[str] = field(default_factory=list)  # Small bonus (e.g. Git, Linux, Jira)
    target_experience_years: float = 2.5
    min_acceptable_salary_lpa: float | None = None


@dataclass(slots=True)
class RankingWeights:
    """
    Centralized configuration for signal weights and decay parameters.
    All signal max weights sum to exactly 100.0.
    """

    # Signal ceilings (Total = 100.0)
    resume_match_max: float = 50.0
    experience_max: float = 20.0
    freshness_max: float = 12.0
    tab_max: float = 6.0
    position_max: float = 4.0
    salary_max: float = 4.0
    rating_max: float = 4.0

    # Resume Matcher Sub-Weights (Sum = 50.0)
    role_title_weight: float = 20.0
    core_skill_weight: float = 18.0
    secondary_skill_weight: float = 8.0
    bonus_skill_weight: float = 4.0

    # Recruiter Experience Decay Parameters
    exp_under_decay_lambda: float = 0.7  # Penalty rate when candidate < job min_exp
    exp_over_decay_lambda: float = 0.3   # Gentle penalty rate when candidate > job max_exp

    # Smooth Decay Parameters
    freshness_decay_lambda: float = 0.35  # Smooth exponential freshness decay
    position_decay_alpha: float = 0.025   # Position decay: 1 / (1 + alpha * (pos - 1))


# ---------------------------------------------------------------------------
# Output Dataclasses
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ResumeMatchResult:
    """Granular output from a ResumeMatcher component."""

    score: float
    role_score: float
    core_skill_score: float
    secondary_skill_score: float
    bonus_skill_score: float
    matched_core: list[str]
    matched_secondary: list[str]
    matched_bonus: list[str]
    reasons: list[str]


@dataclass(slots=True)
class ScoreBreakdown:
    """Normalized 0-100 signal breakdown for a single Job evaluation."""

    resume_match: float = 0.0       # Max 50.0
    experience_match: float = 0.0   # Max 20.0
    freshness: float = 0.0          # Max 12.0
    tab_priority: float = 0.0       # Max 6.0
    position_rank: float = 0.0      # Max 4.0
    salary: float = 0.0             # Max 4.0
    company_rating: float = 0.0     # Max 4.0

    @property
    def total(self) -> float:
        return round(
            min(
                100.0,
                self.resume_match
                + self.experience_match
                + self.freshness
                + self.tab_priority
                + self.position_rank
                + self.salary
                + self.company_rating,
            ),
            2,
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "resume_match": round(self.resume_match, 2),
            "experience_match": round(self.experience_match, 2),
            "freshness": round(self.freshness, 2),
            "tab_priority": round(self.tab_priority, 2),
            "position_rank": round(self.position_rank, 2),
            "salary": round(self.salary, 2),
            "company_rating": round(self.company_rating, 2),
            "total_score": self.total,
        }


@dataclass(slots=True)
class RankedJob:
    """Candidate job scored, ranked (0-100), and accompanied by human-readable explanations."""

    job: Job
    score: float
    rank: int
    reasons: list[str]
    breakdown: ScoreBreakdown


# ---------------------------------------------------------------------------
# Helper Normalization & Matching Functions (P1-1, P1-2, P2-2)
# ---------------------------------------------------------------------------
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
    """Normalize technology variations, hyphens, and aliases (P1-2)."""
    if not text:
        return ""
    norm = text.lower()
    for pattern, replacement in TECH_ALIASES:
        norm = re.sub(pattern, replacement, norm)
    norm = norm.replace("-", " ")
    return " ".join(norm.split())


def _build_searchable_haystack(job: Job) -> str:
    """Construct one unified normalized searchable text for the Job (P1-1)."""
    raw_text = f"{job.title} {job.description} {' '.join(job.tags)} {job.company} {job.location}"
    return _normalize_tech_text(raw_text)


def _exact_word_match(word: str, text: str) -> bool:
    """Exact word boundary match after tech normalization (P1-2)."""
    norm_word = _normalize_tech_text(word)
    if not norm_word:
        return False
    pattern = r"\b" + re.escape(norm_word) + r"\b"
    return bool(re.search(pattern, text))


def _exponential_decay(diff: float, lambda_param: float) -> float:
    """Mathematical exponential decay helper (P2-2)."""
    if diff <= 0:
        return 1.0
    return math.exp(-lambda_param * diff)


# ---------------------------------------------------------------------------
# 1. Hard Filter
# ---------------------------------------------------------------------------
LEAD_ARCHITECT_EXCLUDES = (
    "lead", "principal", "staff", "architect",
    "manager", "head", "director", "vp", "president",
)
NON_DEV_TITLE_EXCLUDES = (
    "aptitude trainer", "trainer", "desktop support", "it support",
    "technical support", "helpdesk", "service desk", "customer support",
    "salesforce", "sap", "mainframe", "teradata",
    "data engineer", "data analyst", "etl", "snowflake", "bi developer",
    "powerbi", "tableau", "data warehousing", "databricks",
)
DEDICATED_AI_KEYWORDS = (
    "machine learning", "ml engineer", "ml developer", "data scientist",
    "deep learning", "nlp engineer", "computer vision", "ai engineer",
    "ai developer", "ai/ml", "ai ml", "artificial intelligence engineer",
)


class HardFilter:
    """
    Synchronous Hard Filter.
    Responsible ONLY for rejecting non-eligible jobs before ranking.
    Never scores or ranks.
    """

    def __init__(self, rules: FilterRules, candidate: CandidateProfile | None = None) -> None:
        self.rules = rules
        self.candidate = candidate

    def evaluate(self, job: Job) -> FilterDecision:
        rules = self.rules
        title = job.title.lower()
        company = job.company.lower()
        location = job.location.lower()

        # 1. Lead / Architect title blocklist (Preserves startup senior roles)
        if any(_exact_word_match(term, title) or term in title for term in LEAD_ARCHITECT_EXCLUDES):
            return FilterDecision(
                False, SkipReason.FILTER_TITLE, "title indicates lead/architect/management role"
            )

        # 2. Non-developer and unaligned data track blocklist
        if any(term in title for term in NON_DEV_TITLE_EXCLUDES):
            return FilterDecision(
                False, SkipReason.FILTER_TITLE, "title indicates non-developer/unaligned track"
            )

        # 4. Title blocklist (with tech normalization)
        if rules.title_must_exclude_any:
            hit = _contains_any(title, rules.title_must_exclude_any)
            if hit:
                return FilterDecision(
                    False, SkipReason.FILTER_TITLE, f"title contains blocked term '{hit}'"
                )

        # 5. Dual-Gate Role Matching (Title OR Core Skills):
        has_title_match = bool(rules.title_must_include_any and _contains_any(title, rules.title_must_include_any))
        has_skill_match = False
        if self.candidate:
            haystack = _build_searchable_haystack(job)
            matched_core = [
                s for s in self.candidate.core_skills
                if _exact_word_match(s, haystack) or _normalize_tech_text(s) in haystack
            ]
            matched_sec = [
                s for s in self.candidate.secondary_skills
                if _exact_word_match(s, haystack) or _normalize_tech_text(s) in haystack
            ]
            if len(matched_core) >= 2 or (len(matched_core) >= 1 and len(matched_sec) >= 1):
                has_skill_match = True

        if rules.title_must_include_any and not has_title_match and not has_skill_match:
            return FilterDecision(
                False, SkipReason.FILTER_TITLE, "title lacks required keywords and lacks matching core skills"
            )

        # Company & Location blocklists
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

        if rules.skip_walkin and job.is_walkin:
            return FilterDecision(False, SkipReason.WALKIN, "walk-in drive")

        # Description & Tags blocklist (checks title, description, and card tags/badges)
        if rules.description_must_exclude_any:
            full_text = f"{job.title} {job.description or ''} {' '.join(job.tags or [])}".lower()
            hit = _contains_any(full_text, rules.description_must_exclude_any)
            if hit:
                return FilterDecision(
                    False, SkipReason.FILTER_DESCRIPTION, f"job contains blocked term '{hit}'"
                )

        # Dedicated AI / ML Recruiter Reality Gate:
        # Candidate has 6 months hands-on AI/ML experience. A recruiter screening for a
        # 3+ year dedicated ML/AI Engineer role will reject the profile immediately.
        # Allow entry/junior AI roles (min_exp <= 2.0y).
        is_dedicated_ai = any(term in title for term in DEDICATED_AI_KEYWORDS)
        if is_dedicated_ai and job.min_experience is not None and job.min_experience > 2.0:
            return FilterDecision(
                False,
                SkipReason.FILTER_EXPERIENCE,
                f"dedicated AI/ML role requires {job.min_experience}y > candidate's 6m AI experience (recruiter will reject)",
            )

        # Numeric experience bounds
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
            if job.max_experience < exp.min_years:
                return FilterDecision(
                    False,
                    SkipReason.FILTER_EXPERIENCE,
                    f"caps at {job.max_experience}y < min {exp.min_years}y",
                )
            if job.max_experience > 6.0:
                return FilterDecision(
                    False,
                    SkipReason.FILTER_EXPERIENCE,
                    f"caps at {job.max_experience}y > ceiling 6.0y (senior requisition)",
                )
            if job.min_experience is not None and (job.max_experience - job.min_experience) >= 5.0 and job.min_experience >= 2.0:
                return FilterDecision(
                    False,
                    SkipReason.FILTER_EXPERIENCE,
                    f"spread {job.min_experience}-{job.max_experience}y >= 5y (broad senior requisition)",
                )

        # Freshness hard ceiling (5 days)
        if rules.max_posted_days is not None and job.posted_days_ago is not None:
            if job.posted_days_ago > rules.max_posted_days:
                return FilterDecision(
                    False,
                    SkipReason.FILTER_FRESHNESS,
                    f"posted {job.posted_days_ago}d ago > max {rules.max_posted_days}d",
                )

        return FilterDecision(True)


# ---------------------------------------------------------------------------
# 2. Resume Matcher Interface & Rule-Based Implementation
# ---------------------------------------------------------------------------
class BaseResumeMatcher(ABC):
    """Abstract interface for Resume Matching. Enables V2 semantic/embedding swap."""

    @abstractmethod
    def match(self, job: Job, weights: RankingWeights) -> ResumeMatchResult:
        ...


class RuleBasedResumeMatcher(BaseResumeMatcher):
    """
    Tiered, deterministic rule-based Resume Matcher.
    Differentiates exact technology word matches vs partial matches across
    Title, Core Skills, Secondary Skills, and Bonus Skills.
    """

    def __init__(self, candidate: CandidateProfile) -> None:
        self.candidate = candidate

    def match(self, job: Job, weights: RankingWeights) -> ResumeMatchResult:
        haystack = _build_searchable_haystack(job)
        norm_title = _normalize_tech_text(job.title)
        reasons: list[str] = []

        # 1. Role / Title Match (Max role_title_weight, e.g. 20.0)
        matched_titles: list[str] = []
        for kw in self.candidate.title_keywords:
            if _exact_word_match(kw, norm_title):
                matched_titles.append(kw)
            elif _normalize_tech_text(kw) in norm_title:
                matched_titles.append(kw)

        if len(matched_titles) >= 2:
            role_score = weights.role_title_weight
        elif len(matched_titles) == 1:
            role_score = round(weights.role_title_weight * 0.8, 2)
        else:
            role_score = 0.0

        if matched_titles:
            reasons.append(f"✓ Excellent role relevance for {', '.join(matched_titles)}")

        # 2. Core Skills Match (Max core_skill_weight, e.g. 18.0)
        matched_core: list[str] = []
        for skill in self.candidate.core_skills:
            if _exact_word_match(skill, haystack) or _normalize_tech_text(skill) in haystack:
                matched_core.append(skill)
        num_core = len(matched_core)
        if num_core >= 4:
            core_score = weights.core_skill_weight
        elif num_core == 3:
            core_score = round(weights.core_skill_weight * 0.85, 2)
        elif num_core == 2:
            core_score = round(weights.core_skill_weight * 0.70, 2)
        elif num_core == 1:
            core_score = round(weights.core_skill_weight * 0.45, 2)
        else:
            core_score = 0.0

        if matched_core:
            top_matched = ", ".join(matched_core[:4])
            reasons.append(f"✓ Strong core tech match ({top_matched})")

        # 3. Secondary Skills Match (Max secondary_skill_weight, e.g. 8.0)
        matched_secondary: list[str] = []
        for skill in self.candidate.secondary_skills:
            if _exact_word_match(skill, haystack) or _normalize_tech_text(skill) in haystack:
                matched_secondary.append(skill)
        num_sec = len(matched_secondary)
        if num_sec >= 3:
            secondary_score = weights.secondary_skill_weight
        elif num_sec == 2:
            secondary_score = round(weights.secondary_skill_weight * 0.75, 2)
        elif num_sec == 1:
            secondary_score = round(weights.secondary_skill_weight * 0.50, 2)
        else:
            secondary_score = 0.0

        if matched_secondary:
            reasons.append(f"✓ Matched secondary skills ({', '.join(matched_secondary[:3])})")

        # 4. Bonus Skills Match (Max bonus_skill_weight, e.g. 4.0)
        matched_bonus: list[str] = []
        for skill in self.candidate.bonus_skills:
            if _exact_word_match(skill, haystack) or _normalize_tech_text(skill) in haystack:
                matched_bonus.append(skill)
        num_bonus = len(matched_bonus)
        if num_bonus >= 2:
            bonus_score = weights.bonus_skill_weight
        elif num_bonus == 1:
            bonus_score = round(weights.bonus_skill_weight * 0.60, 2)
        else:
            bonus_score = 0.0

        total_resume_score = min(
            weights.resume_match_max,
            round(role_score + core_score + secondary_score + bonus_score, 2),
        )

        return ResumeMatchResult(
            score=total_resume_score,
            role_score=role_score,
            core_skill_score=core_score,
            secondary_skill_score=secondary_score,
            bonus_skill_score=bonus_score,
            matched_core=matched_core,
            matched_secondary=matched_secondary,
            matched_bonus=matched_bonus,
            reasons=reasons,
        )


# ---------------------------------------------------------------------------
# 3. Deterministic Ranking Engine
# ---------------------------------------------------------------------------
class RankingEngine:
    """
    Pure Python, deterministic Ranking Engine (Version 1).
    Evaluates candidate jobs against single-responsibility scoring functions,
    normalizes final scores to 0-100, and returns sorted list[RankedJob].
    """

    def __init__(
        self,
        candidate: CandidateProfile,
        rules: FilterRules | None = None,
        weights: RankingWeights | None = None,
        resume_matcher: BaseResumeMatcher | None = None,
    ) -> None:
        self.candidate = candidate
        self.hard_filter = HardFilter(rules, candidate=candidate) if rules else None
        self.weights = weights or RankingWeights()
        self.resume_matcher = resume_matcher or RuleBasedResumeMatcher(candidate)

    def rank_jobs(self, jobs: list[Job]) -> list[RankedJob]:
        """Filter and rank candidate jobs deterministically, returning list[RankedJob] (0-100)."""
        eligible_jobs: list[Job] = []

        # Phase 1: Hard Filter Rejection
        for job in jobs:
            if self.hard_filter is not None:
                decision = self.hard_filter.evaluate(job)
                if not decision.passed:
                    continue
            eligible_jobs.append(job)

        # Phase 2: Signal Scoring
        scored_candidates: list[tuple[Job, ScoreBreakdown, list[str]]] = []
        for job in eligible_jobs:
            breakdown, reasons = self._evaluate_job_signals(job)
            scored_candidates.append((job, breakdown, reasons))

        # Phase 3: Multi-Key Deterministic Tie-Breaking (P1-5)
        # Score DESC → Position ASC → Freshness ASC → Job ID ASC
        scored_candidates.sort(
            key=lambda item: (
                -item[1].total,
                item[0].recommendation_position if item[0].recommendation_position is not None else 999,
                item[0].posted_days_ago if item[0].posted_days_ago is not None else 999,
                item[0].job_id,
            )
        )

        # Phase 4: Assign Ranks
        ranked_results: list[RankedJob] = []
        for rank, (job, breakdown, reasons) in enumerate(scored_candidates, start=1):
            ranked_results.append(
                RankedJob(
                    job=job,
                    score=breakdown.total,
                    rank=rank,
                    reasons=reasons,
                    breakdown=breakdown,
                )
            )

        return ranked_results

    # -------------------------------------------------- single signal functions
    def _evaluate_job_signals(self, job: Job) -> tuple[ScoreBreakdown, list[str]]:
        reasons: list[str] = []

        # 1. Resume Match Signal
        resume_result = self.resume_matcher.match(job, self.weights)
        reasons.extend(resume_result.reasons)

        # 2. Experience Signal (Recruiter mental model with smooth decay)
        exp_score, exp_reason = self._score_experience(job)
        if exp_reason:
            reasons.append(exp_reason)

        # 3. Freshness Signal (Smooth exponential decay)
        fresh_score, fresh_reason = self._score_freshness(job)
        if fresh_reason:
            reasons.append(fresh_reason)

        # 4. Recommendation Tab Priority Signal
        tab_score, tab_reason = self._score_tab(job)
        if tab_reason:
            reasons.append(tab_reason)

        # 5. Recommendation Position Signal (Smooth inverse decay)
        pos_score, pos_reason = self._score_position(job)
        if pos_reason:
            reasons.append(pos_reason)

        # 6. Disclosed Salary Signal (Unpenalized neutral missing)
        sal_score, sal_reason = self._score_salary(job)
        if sal_reason:
            reasons.append(sal_reason)

        # 7. Company Rating Signal (Weak signal, startup neutral)
        rating_score, rating_reason = self._score_rating(job)
        if rating_reason:
            reasons.append(rating_reason)

        breakdown = ScoreBreakdown(
            resume_match=resume_result.score,
            experience_match=exp_score,
            freshness=fresh_score,
            tab_priority=tab_score,
            position_rank=pos_score,
            salary=sal_score,
            company_rating=rating_score,
        )

        return breakdown, reasons

    def _score_experience(self, job: Job) -> tuple[float, str]:
        w = self.weights
        target = float(self.candidate.target_experience_years)
        min_e = float(job.min_experience) if job.min_experience is not None else None
        max_e = float(job.max_experience) if job.max_experience is not None else None

        if min_e is None and max_e is None:
            return round(w.experience_max * 0.75, 2), "✓ Experience requirements open"

        min_bound = min_e if min_e is not None else 0.0
        max_bound = max_e if max_e is not None else 99.0

        if min_bound <= target <= max_bound:
            return w.experience_max, "✓ Experience range perfectly fits role"

        if target < min_bound:
            diff = min_bound - target
            decay = _exponential_decay(diff, w.exp_under_decay_lambda)
            score = round(w.experience_max * decay, 2)
            if diff <= 1.0:
                return score, "✓ Experience closely aligns with minimum requirement"
            return score, "✓ Below target experience requirement"

        diff = target - max_bound
        decay = _exponential_decay(diff, w.exp_over_decay_lambda)
        score = round(w.experience_max * decay, 2)
        if diff <= 1.5:
            return score, "✓ Slightly above target experience range"
        return score, "✓ Overqualified for role experience cap"

    def _score_freshness(self, job: Job) -> tuple[float, str]:
        w = self.weights
        days = job.posted_days_ago
        if days is None or days == 0:
            return w.freshness_max, "✓ Posted today"

        decay = _exponential_decay(float(days), w.freshness_decay_lambda)
        score = round(w.freshness_max * decay, 2)

        if days == 1:
            return score, "✓ Posted 1 day ago"
        return score, f"✓ Posted {days} days ago"

    def _score_tab(self, job: Job) -> tuple[float, str]:
        w = self.weights
        tab_enum = RecommendationTab.normalize(job.recommendation_tab)
        tab_multipliers = {
            RecommendationTab.PROFILE: 1.0,
            RecommendationTab.TOP_CANDIDATE: 0.85,
            RecommendationTab.APPLIES: 0.70,
            RecommendationTab.PREFERENCES: 0.55,
            RecommendationTab.YOU_MIGHT_LIKE: 0.40,
            RecommendationTab.DEFAULT: 0.30,
            RecommendationTab.OTHER: 0.20,
        }
        mult = tab_multipliers.get(tab_enum, 0.30)
        score = round(w.tab_max * mult, 2)

        if tab_enum == RecommendationTab.PROFILE:
            return score, "✓ High-priority profile recommendation"
        if tab_enum == RecommendationTab.TOP_CANDIDATE:
            return score, "✓ Top candidate feed match"
        return score, "✓ Recommended feed match"

    def _score_position(self, job: Job) -> tuple[float, str]:
        w = self.weights
        pos = job.recommendation_position

        if pos is None or pos <= 0:
            return round(w.position_max * 0.5, 2), ""

        decay = 1.0 / (1.0 + (w.position_decay_alpha * (pos - 1)))
        score = round(w.position_max * decay, 2)

        if pos <= 5:
            return score, f"✓ Top feed position (#{pos})"
        return score, ""

    def _score_salary(self, job: Job) -> tuple[float, str]:
        w = self.weights
        sal = float(job.min_salary_lpa) if job.min_salary_lpa is not None else None
        target_sal = float(self.candidate.min_acceptable_salary_lpa) if self.candidate.min_acceptable_salary_lpa is not None else None

        if sal is None:
            return round(w.salary_max * 0.75, 2), ""

        if target_sal is not None and sal >= target_sal:
            return w.salary_max, f"✓ Disclosed salary meets target ({sal} LPA)"

        return round(w.salary_max * 0.60, 2), f"✓ Disclosed salary ({sal} LPA)"

    def _score_rating(self, job: Job) -> tuple[float, str]:
        w = self.weights
        r = float(job.rating) if job.rating is not None else None

        if r is None:
            return round(w.rating_max * 0.75, 2), ""

        if r >= 4.0:
            return w.rating_max, f"✓ Highly rated company ({r}★)"
        if r >= 3.5:
            return round(w.rating_max * 0.75, 2), ""

        return round(w.rating_max * 0.50, 2), ""
