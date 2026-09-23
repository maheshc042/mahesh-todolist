"""
Domain models.

These are transport-agnostic dataclasses shared by the scraper, the filter
engine, the apply engine and the persistence layer. Keeping them separate from
both Playwright objects and SQL rows means each layer can be unit tested with
plain Python objects.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class ApplicationStatus(str, Enum):
    APPLIED = "applied"
    SKIPPED = "skipped"
    FAILED = "failed"
    EXTERNAL = "external"
    ALREADY_APPLIED = "already_applied"
    NEEDS_REVIEW = "needs_review"


class RecommendationTab(str, Enum):
    DEFAULT = "default"
    PROFILE = "profile"
    TOP_CANDIDATE = "top_candidate"
    APPLIES = "applies"
    PREFERENCES = "preferences"
    YOU_MIGHT_LIKE = "you_might_like"
    RECOMMENDED = "recommended"
    OTHER = "other"

    @classmethod
    def normalize(cls, label: str) -> RecommendationTab:
        low = (label or "").strip().lower()
        if "profile" in low:
            return cls.PROFILE
        if "top candidate" in low or "candidate" in low:
            return cls.TOP_CANDIDATE
        if "applies" in low or "apply" in low:
            return cls.APPLIES
        if "preference" in low:
            return cls.PREFERENCES
        if "might like" in low or "you might" in low:
            return cls.YOU_MIGHT_LIKE
        if "recommend" in low:
            return cls.RECOMMENDED
        if "default" in low or not low:
            return cls.DEFAULT
        return cls.OTHER


class SkipReason(str, Enum):
    FILTER_TITLE = "filter_title"
    FILTER_DESCRIPTION = "filter_description"
    FILTER_LOCATION = "filter_location"
    FILTER_COMPANY = "filter_company"
    FILTER_EXPERIENCE = "filter_experience"
    FILTER_SALARY = "filter_salary"
    FILTER_FRESHNESS = "filter_freshness"
    FILTER_RATING = "filter_rating"
    WALKIN = "walkin"
    BANGALORE_WALKIN_ALERT = "bangalore_walkin_alert"
    EXTERNAL_APPLY = "external_apply"
    ALREADY_APPLIED = "already_applied"
    SEEN_RECENTLY = "seen_recently"
    UNANSWERED_QUESTION = "unanswered_question"
    DAILY_CAP = "daily_cap"
    PROFILE_CAP = "profile_cap"
    DRY_RUN = "dry_run"
    LOW_MATCH_SCORE = "low_match_score"
    FILTER_REJECTED = "filter_rejected"
    COMPANY_BLACKLIST = "company_blacklist"
    BLOCKED_LOCATION = "blocked_location"
    STALE_JOB = "stale_job"
    STUCK_FLOW = "stuck_flow"
    CLOUDFLARE_CHALLENGE = "cloudflare_challenge"


class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


@dataclass(slots=True)
class Job:
    """A job card scraped from a Naukri search result page."""

    job_id: str
    title: str
    company: str
    url: str
    location: str = ""
    experience_text: str = ""
    salary_text: str = ""
    posted_text: str = ""
    rating: float | None = None
    reviews_count: int | None = None
    tags: list[str] = field(default_factory=list)
    description: str = ""
    is_walkin: bool = False
    is_external: bool = False
    source_keyword: str = ""
    recommendation_tab: str = "default"
    recommendation_position: int | None = None
    total_jobs_in_tab: int | None = None
    scraped_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    form_links: list[str] = field(default_factory=list)
    recruiter_emails: list[str] = field(default_factory=list)
    platform: str = "naukri"

    # --- Derived numeric fields, parsed lazily by the parser module ---------
    min_experience: float | None = None
    max_experience: float | None = None
    # True when min/max were imputed from seniority keywords (no stated range
    # found on the card). Imputed numbers inform ranking but must never
    # hard-reject on their own: unknown data never rejects.
    experience_imputed: bool = False
    min_salary_lpa: float | None = None
    max_salary_lpa: float | None = None
    posted_days_ago: int | None = None
    # LinkedIn applicant count parsed from the card ("57 applicants" -> 57,
    # "Over 100 applicants" -> 101, "Be an early applicant" -> 5).
    # None when the card shows no count: unknown never penalizes.
    applicant_count: int | None = None
    # Easy Apply signal (LinkedIn card footer). True only on positive "Easy
    # Apply" evidence; False on a bare "Apply" badge; None when unknown.
    # Unknown never penalizes — it only deprioritizes confirmed company-site.
    easy_apply: bool | None = None

    @staticmethod
    def stable_id(url: str, title: str, company: str) -> str:
        """
        Naukri exposes a jobId in the URL, but sponsored/duplicate cards
        occasionally omit it. Fall back to a deterministic hash so dedupe still
        works and we never insert the same posting twice.
        """
        match = re.search(r"-(\d{6,})(?:\?|$)", url) or re.search(r"jobId=(\d+)", url)
        if match:
            return match.group(1)
        digest = hashlib.sha1(f"{_slug(title)}|{_slug(company)}".encode()).hexdigest()
        return f"h-{digest[:20]}"

    def to_row(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "title": self.title,
            "company": self.company,
            "url": self.url,
            "location": self.location,
            "experience_text": self.experience_text,
            "salary_text": self.salary_text,
            "posted_text": self.posted_text,
            "rating": self.rating,
            "tags": self.tags,
            "min_experience": self.min_experience,
            "max_experience": self.max_experience,
            "min_salary_lpa": self.min_salary_lpa,
            "posted_days_ago": self.posted_days_ago,
            "is_walkin": self.is_walkin,
            "source_keyword": self.source_keyword,
            "recommendation_tab": self.recommendation_tab,
            "recommendation_position": self.recommendation_position,
            "total_jobs_in_tab": self.total_jobs_in_tab,
        }


@dataclass(slots=True)
class FilterDecision:
    passed: bool
    reason: SkipReason | None = None
    detail: str = ""


@dataclass(slots=True)
class ApplyOutcome:
    status: ApplicationStatus
    reason: SkipReason | None = None
    detail: str = ""
    screenshot_path: str | None = None
    questions_answered: int = 0
    unanswered_questions: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 1
    external_url: str | None = None
    confirmation_type: str | None = None
    confirmation_evidence: str | None = None



@dataclass(slots=True)
class ScreeningQuestion:
    """One question rendered by Naukri's post-apply chatbot."""

    text: str
    kind: str  # text | radio | checkbox | dropdown | date | unknown
    options: list[str] = field(default_factory=list)
    required: bool = True


@dataclass
class RunStats:
    scraped: int = 0
    considered: int = 0
    filtered_out: int = 0
    # Plan-level hard rejects (title/experience/score gates inside
    # ApplicationPlanner). These jobs never reach _process_job, so without
    # this counter Telegram under-reports filtering ~5x. Informational only.
    plan_rejected: int = 0
    applied: int = 0
    failed: int = 0
    external: int = 0
    already_applied: int = 0
    needs_review: int = 0
    per_profile: dict[str, dict[str, int]] = field(default_factory=dict)
    per_platform: dict[str, dict[str, int]] = field(default_factory=dict)
    applied_jobs: list[dict[str, str]] = field(default_factory=list)
    external_jobs: list[dict[str, str]] = field(default_factory=list)
    walkin_alerts: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def bump(self, profile: str, key: str, amount: int = 1, platform: str | None = None) -> None:
        setattr(self, key, getattr(self, key, 0) + amount)
        bucket = self.per_profile.setdefault(profile, {})
        bucket[key] = bucket.get(key, 0) + amount
        if platform:
            plat_key = platform.lower()
            p_bucket = self.per_platform.setdefault(plat_key, {})
            p_bucket[key] = p_bucket.get(key, 0) + amount

    def bump_platform(self, platform: str, key: str, amount: int = 1) -> None:
        plat_key = platform.lower()
        p_bucket = self.per_platform.setdefault(plat_key, {})
        p_bucket[key] = p_bucket.get(key, 0) + amount

    def as_dict(self) -> dict[str, Any]:
        return {
            "scraped": self.scraped,
            "considered": self.considered,
            "filtered_out": self.filtered_out,
            "plan_rejected": self.plan_rejected,
            "applied": self.applied,
            "failed": self.failed,
            "external": self.external,
            "already_applied": self.already_applied,
            "needs_review": self.needs_review,
            "per_profile": self.per_profile,
            "per_platform": self.per_platform,
            "applied_jobs": self.applied_jobs,
            "external_jobs": self.external_jobs,
            "walkin_alerts": self.walkin_alerts,
            "errors": self.errors[:20],
        }


def extract_description_metadata(description_or_job: str | Any) -> tuple[list[str], list[str]]:
    """Extracts form links (Google Forms, Typeform, etc.) and valid recruiter emails from job description text."""
    if not description_or_job:
        return [], []
    if hasattr(description_or_job, "description"):
        description = description_or_job.description or ""
    elif isinstance(description_or_job, str):
        description = description_or_job
    else:
        description = str(description_or_job)

    if not description:
        return [], []

    form_links = re.findall(
        r"(https?://(?:forms\.gle|docs\.google\.com/forms|forms\.office\.com|typeform\.com)[^\s\"'>]+)",
        description,
    )
    clean_links = list(dict.fromkeys(form_links))

    raw_emails = re.findall(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)", description)
    ignored_prefixes = ("info@", "support@", "sales@", "contact@", "help@", "admin@", "query@", "feedback@")
    valid_emails = [
        e.lower().strip(".")
        for e in raw_emails
        if not e.lower().startswith(ignored_prefixes)
        and not e.lower().endswith(
            ("naukri.com", "naukrigulf.com", "example.com", "yopmail.com", "instahyre.com", "cutshort.io", "wellfound.com")
        )
    ]
    clean_emails = list(dict.fromkeys(valid_emails))
    return clean_links, clean_emails

