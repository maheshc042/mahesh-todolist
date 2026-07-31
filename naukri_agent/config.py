"""
Configuration layer.

Design decision: two-tier configuration.

1. `Settings` (env vars / .env)  -> secrets and deployment-specific values only.
   These must never live in a file that gets committed.
2. `AgentConfig` (config/config.yaml) -> behavioural rules, job profiles, filters.
   These change often and are reviewed by a human, so YAML + strict pydantic
   validation gives us fail-fast errors on a typo instead of a 2 a.m. bad run.

Everything is validated by pydantic v2 so an invalid config crashes at startup
rather than halfway through a browsing session.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Default account key used when the CLI/config does not name one.
PRIMARY_ACCOUNT = "primary"
SECONDARY_ACCOUNT = "secondary"


@dataclass(frozen=True, slots=True)
class NaukriAccount:
    """
    One Naukri login. Two accounts are supported because Naukri attaches
    whatever resume currently sits on the *profile* — there is no per-application
    resume picker. One account therefore equals one resume/identity, and running
    two isolated accounts is the only way to pursue two different job families
    with two tailored CVs (and it doubles the daily headroom).
    """

    key: str
    email: str
    password: str

    @property
    def session_key(self) -> str:
        # Per-account session row: sharing one key made account B's cookies
        # overwrite account A's on every run.
        return f"naukri:session:{self.key}"

    @property
    def masked_email(self) -> str:
        local, _, domain = self.email.partition("@")
        return f"{local[:3]}…@{domain}" if domain else "unset"


# --------------------------------------------------------------------------- #
# Secrets / environment
# --------------------------------------------------------------------------- #
class Settings(BaseSettings):
    """Secrets and deployment knobs. Loaded from environment or .env file."""

    model_config = SettingsConfigDict(
        # Absolute paths: a cron entry or systemd unit starting the agent from
        # another working directory used to silently load no credentials at all.
        env_file=(
            PROJECT_ROOT / ".env",
            PROJECT_ROOT / ".env.local",
            PROJECT_ROOT / ".env.development.local",
        ),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Naukri credentials -------------------------------------------------
    # Account 1 (e.g. the Full-Stack + AI resume).
    naukri_email: str = ""
    naukri_password: str = ""
    # Account 2 (e.g. the AI / Python Engineer resume). Optional.
    naukri_email_2: str = ""
    naukri_password_2: str = ""
    # Which account a bare `run` targets.
    default_account: str = PRIMARY_ACCOUNT

    # --- Database -----------------------------------------------------------
    # Neon/Supabase pooled connection string.
    database_url: str = ""
    # asyncpg cannot parse libpq-only query params, so we sanitise below.
    db_pool_min_size: int = 1
    db_pool_max_size: int = 5
    db_schema: str = "naukri"

    # --- Notifications ------------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Paths --------------------------------------------------------------
    config_path: Path = PROJECT_ROOT / "config" / "config.yaml"
    artifacts_dir: Path = PROJECT_ROOT / "artifacts"
    log_dir: Path = PROJECT_ROOT / "logs"
    resume_dir: Path = PROJECT_ROOT / "resumes"

    # --- Runtime overrides (useful for `docker run -e`) ---------------------
    headless: bool | None = None
    log_level: str = "INFO"
    log_json: bool = False
    dry_run: bool = False

    @property
    def asyncpg_dsn(self) -> str:
        """
        asyncpg rejects libpq parameters such as `sslmode`, `channel_binding`,
        `options=endpoint%3D...`. Neon's pooled URL contains them, so strip the
        query string and pass ssl separately (see db/pool.py).
        """
        url = self.database_url.strip()
        if not url:
            raise RuntimeError(
                "DATABASE_URL is not set. Connect the Neon integration or export it manually."
            )
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        return url.split("?", 1)[0]

    @property
    def requires_ssl(self) -> bool:
        url = self.database_url.lower()
        return "sslmode=disable" not in url and "localhost" not in url and "127.0.0.1" not in url

    # ----------------------------------------------------------- accounts
    def _account_map(self) -> dict[str, tuple[str, str, tuple[str, str]]]:
        """key -> (email, password, (email_env_name, password_env_name))"""
        return {
            PRIMARY_ACCOUNT: (
                self.naukri_email,
                self.naukri_password,
                ("NAUKRI_EMAIL", "NAUKRI_PASSWORD"),
            ),
            SECONDARY_ACCOUNT: (
                self.naukri_email_2,
                self.naukri_password_2,
                ("NAUKRI_EMAIL_2", "NAUKRI_PASSWORD_2"),
            ),
        }

    def available_accounts(self) -> list[str]:
        """Account keys that have both an email and a password configured."""
        return [
            key
            for key, (email, password, _env) in self._account_map().items()
            if email and password
        ]

    def account(self, key: str | None = None) -> NaukriAccount:
        wanted = (key or self.default_account or PRIMARY_ACCOUNT).strip().lower()
        accounts = self._account_map()
        if wanted not in accounts:
            raise RuntimeError(
                f"Unknown account '{wanted}'. Valid keys: {', '.join(accounts)}."
            )
        email, password, (email_env, password_env) = accounts[wanted]
        missing = [name for name, value in ((email_env, email), (password_env, password)) if not value]
        if missing:
            raise RuntimeError(
                f"Account '{wanted}' is not configured. Missing: {', '.join(missing)}."
            )
        return NaukriAccount(key=wanted, email=email, password=password)

    def validate_for_run(self, account_key: str | None = None) -> NaukriAccount:
        """
        Called before a live run so config errors surface immediately.
        Returns the resolved account so the caller cannot forget to use it.
        """
        if not self.database_url:
            raise RuntimeError("Missing required environment variable: DATABASE_URL")
        return self.account(account_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# --------------------------------------------------------------------------- #
# YAML config models
# --------------------------------------------------------------------------- #
class ExperienceRange(BaseModel):
    min_years: float = 0
    max_years: float = 50

    @model_validator(mode="after")
    def _check(self) -> "ExperienceRange":
        if self.min_years > self.max_years:
            raise ValueError("experience.min_years cannot exceed max_years")
        return self


class FilterRules(BaseModel):
    """
    Declarative filter rules. Every rule is optional; an unset rule is a no-op.
    Keeping them declarative (instead of code) means non-engineers can tune the
    agent, and it makes the decision auditable — we persist which rule rejected
    a job.
    """

    title_must_include_any: list[str] = Field(default_factory=list)
    title_must_exclude_any: list[str] = Field(default_factory=list)
    description_must_include_any: list[str] = Field(default_factory=list)
    description_must_exclude_any: list[str] = Field(default_factory=list)
    allowed_locations: list[str] = Field(default_factory=list)
    blocked_locations: list[str] = Field(default_factory=list)
    blocked_companies: list[str] = Field(default_factory=list)
    experience: ExperienceRange = Field(default_factory=ExperienceRange)
    min_salary_lpa: float | None = None
    max_posted_days: int | None = 30
    min_rating: float | None = None
    require_easy_apply: bool = True
    skip_walkin: bool = True

    @field_validator(
        "title_must_include_any",
        "title_must_exclude_any",
        "description_must_include_any",
        "description_must_exclude_any",
        "allowed_locations",
        "blocked_locations",
        "blocked_companies",
        mode="before",
    )
    @classmethod
    def _lower(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [str(item).strip().lower() for item in v if str(item).strip()]
        return v

    def relaxed(self) -> "FilterRules":
        """
        A copy suitable for Naukri's own recommended feed.

        Naukri already matched these postings against the profile, so the
        *inclusion* rules (a title/JD must contain one of my keywords) and the
        aggressive freshness cap only throw away good, pre-qualified jobs. The
        hard blocklists — wrong tech stack, blocked companies, walk-ins,
        experience sanity — are all kept, because those are genuine no-gos.
        """
        return self.model_copy(
            update={
                "title_must_include_any": [],
                "description_must_include_any": [],
                # The recommended feed carries older-but-relevant postings; the
                # dedupe window already stops us re-evaluating the same job.
                "max_posted_days": None,
                "min_rating": None,
            }
        )


class SearchSpec(BaseModel):
    """One search query. Naukri's URL scheme is keyword + location + experience."""

    keyword: str
    custom_url: str | None = None
    locations: list[str] = Field(default_factory=list)
    max_pages: int = 3
    # Naukri sort options: `r` (relevance) or `f` (freshness/date).
    sort_by: Literal["relevance", "date"] = "date"
    # Naukri freshness filter in days: 1, 3, 7, 15, 30 (None = any)
    freshness_days: int | None = 7

    @field_validator("keyword")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("search keyword cannot be blank")
        return v.strip()


class RecommendedConfig(BaseModel):
    """
    Naukri's own "Recommended jobs" feed (/mnjuser/recommendedjobs).

    This is the primary — and by default the only — source of jobs. Naukri has
    already matched these postings against the profile, headline, skills and
    preferences, so relevance is far higher than any keyword query we could
    build, and no filter tuning is required to keep it that way.
    """

    enabled: bool = True
    # Tab labels to harvest, in order. Matched case-insensitively against the
    # visible text of the feed's tab strip; missing tabs are skipped silently.
    tabs: list[str] = Field(
        default_factory=lambda: ["top candidate", "preferences", "profile", "you might like"]
    )
    # The feed lazy-loads on scroll. Stop after this many scroll rounds that
    # yield no new cards, or once max_jobs is collected.
    max_scroll_rounds: int = 14
    stall_rounds_before_stop: int = 3
    max_jobs: int = 250
    # Click "Show more jobs"/"View all" style controls when present.
    follow_show_more: bool = True


class JobProfile(BaseModel):
    """
    A profile bundles: which feed to read, how to filter, which resume to attach
    and how to answer screening questions.

    A profile is bound to ONE account (`account`), because the resume lives on
    the account. The Full-Stack profile runs on the account holding the
    Full-Stack CV; the AI/Python profile runs on the account holding that CV.
    """

    name: str
    enabled: bool = True
    priority: int = 100  # lower runs first
    experience_years: float = 3
    # Which Naukri login this profile belongs to.
    account: str = PRIMARY_ACCOUNT
    # Primary source: Naukri's recommended feed.
    use_recommended: bool = True
    # Optional keyword-search fallback. Empty by default: the recommended feed
    # is the intended source and keyword search reintroduces irrelevant jobs.
    searches: list[SearchSpec] = Field(default_factory=list)
    # Only consulted when `searches` is non-empty AND the recommended feed did
    # not fill `max_applications_per_run`.
    search_is_fallback_only: bool = True
    filters: FilterRules = Field(default_factory=FilterRules)
    # Optional explicit override; when unset the recommended feed uses
    # `filters.relaxed()`.
    recommended_filters: FilterRules | None = None
    max_applications_per_run: int = 20
    # Resume switching: label of the resume as uploaded in the Naukri profile,
    # and/or a local file used for the "upload new resume" flow.
    resume_label: str | None = None
    resume_file: str | None = None
    # Profile-scoped answers override the global knowledge base.
    answers: dict[str, str] = Field(default_factory=dict)

    @field_validator("account", mode="before")
    @classmethod
    def _normalise_account(cls, v: Any) -> Any:
        return str(v).strip().lower() if v else PRIMARY_ACCOUNT

    @model_validator(mode="after")
    def _has_a_source(self) -> "JobProfile":
        if not self.use_recommended and not self.searches:
            raise ValueError(
                f"profile '{self.name}' has no job source: enable use_recommended "
                "or define at least one search"
            )
        return self

    def filters_for(self, source: str) -> FilterRules:
        """`source` is 'recommended' or 'search'."""
        if source != "recommended":
            return self.filters
        return self.recommended_filters or self.filters.relaxed()


class BrowserConfig(BaseModel):
    headless: bool = True
    slow_mo_ms: int = 0
    viewport_width: int = 1440
    viewport_height: int = 900
    locale: str = "en-IN"
    timezone: str = "Asia/Kolkata"
    user_agent: str | None = None
    default_timeout_ms: int = 30_000
    navigation_timeout_ms: int = 60_000
    # Human-like pacing: random delay range applied between interactions.
    min_action_delay_ms: int = 350
    max_action_delay_ms: int = 1_400
    # Blocking heavy resources cuts page load time ~40% and reduces detection
    # surface, but images are needed for some captchas, so it is configurable.
    block_resources: list[str] = Field(default_factory=lambda: ["font", "media"])


class RunConfig(BaseModel):
    # PER ACCOUNT, per calendar day (Asia/Kolkata). Counted from the DB, so a
    # second run on the same day resumes against the same budget instead of
    # starting over. Naukri throttles well before this; ~25-30 stays human.
    daily_application_cap: int = 30
    # Do not re-apply / re-evaluate the same job within this window.
    dedupe_window_days: int = 90
    # Stop the whole run after this many consecutive apply failures.
    max_consecutive_failures: int = 5
    # Pause between two applications (seconds, randomised).
    min_delay_between_applies_s: int = 8
    max_delay_between_applies_s: int = 25
    # Playwright action retries.
    max_retries_per_job: int = 2
    # If a screening question has no confident answer: skip and queue it.
    strict_answers: bool = True
    screenshot_on_failure: bool = True
    screenshot_on_success: bool = False
    # Hard ceiling for one run in minutes (safety valve for the scheduler).
    run_timeout_minutes: int = 90


class ScheduleConfig(BaseModel):
    enabled: bool = True
    # Standard 5-field cron, evaluated in `timezone`.
    cron: str = "40 9 * * 1-5"
    # Per-account overrides. Two accounts MUST be staggered by hours: two logins
    # from one IP inside the same window is the pattern most likely to trigger
    # Naukri's "unusual activity" challenge.
    cron_by_account: dict[str, str] = Field(default_factory=dict)
    timezone: str = "Asia/Kolkata"
    # Jitter avoids hitting Naukri at the exact same second every day.
    jitter_seconds: int = 600
    run_on_start: bool = False

    def cron_for(self, account: str | None) -> str:
        if account:
            return self.cron_by_account.get(account.strip().lower(), self.cron)
        return self.cron


class NotificationConfig(BaseModel):
    telegram_enabled: bool = True
    notify_on_success: bool = True
    notify_on_failure: bool = True
    include_job_list: bool = True
    max_jobs_in_message: int = 15


class AgentConfig(BaseModel):
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    run: RunConfig = Field(default_factory=RunConfig)
    recommended: RecommendedConfig = Field(default_factory=RecommendedConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    profiles: list[JobProfile]
    # Global fallback knowledge base for screening questions.
    answers: dict[str, str] = Field(default_factory=dict)

    @field_validator("profiles")
    @classmethod
    def _has_profile(cls, v: list[JobProfile]) -> list[JobProfile]:
        if not v:
            raise ValueError("config must define at least one profile")
        names = [p.name for p in v]
        if len(names) != len(set(names)):
            raise ValueError("profile names must be unique")
        return v

    def active_profiles(
        self,
        only: list[str] | None = None,
        account: str | None = None,
    ) -> list[JobProfile]:
        """
        Enabled profiles, optionally narrowed by name and by account.

        Account filtering is what keeps the two logins isolated: a run targeting
        `secondary` never touches the Full-Stack profile bound to `primary`.
        """
        selected = [p for p in self.profiles if p.enabled]
        if account:
            wanted_account = account.strip().lower()
            selected = [p for p in selected if p.account == wanted_account]
        if only:
            wanted = {name.lower() for name in only}
            selected = [p for p in selected if p.name.lower() in wanted]
        return sorted(selected, key=lambda p: (p.priority, p.name))

    def accounts_in_use(self) -> list[str]:
        seen: list[str] = []
        for profile in self.profiles:
            if profile.enabled and profile.account not in seen:
                seen.append(profile.account)
        return seen


def _expand_env(node: Any) -> Any:
    """Allow ${VAR} interpolation inside the YAML file."""
    if isinstance(node, str):
        return os.path.expandvars(node)
    if isinstance(node, list):
        return [_expand_env(item) for item in node]
    if isinstance(node, dict):
        return {key: _expand_env(value) for key, value in node.items()}
    return node


def load_config(path: Path | str | None = None) -> AgentConfig:
    settings = get_settings()
    config_path = Path(path or settings.config_path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found at {config_path}. Copy config/config.example.yaml to "
            "config/config.yaml and edit it."
        )
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config = AgentConfig.model_validate(_expand_env(raw))

    # Env override wins over YAML so a container can force headed/headless mode.
    if settings.headless is not None:
        config.browser.headless = settings.headless
    return config
