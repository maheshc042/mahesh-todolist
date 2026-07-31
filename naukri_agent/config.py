"""
Configuration: environment (secrets) + YAML (behaviour).

The split is deliberate and load-bearing:

- **`Settings` (environment)** holds everything that is a secret or deployment
  specific: the two Naukri logins, `DATABASE_URL`, the Telegram token, paths.
  These must never be committed, and on Vercel/Fly/ECS they arrive as env vars.
- **`AgentConfig` (config/config.yaml)** holds behaviour: filters, caps, pacing,
  screening answers, schedules. It is reviewable in git and mounted read-only
  into the container, because the agent must never rewrite its own rules.

Design decisions
----------------

- **Pydantic models with `extra="forbid"`.** A misspelled key (`max_posted_day`)
  in a silently-ignoring loader means a filter that never runs, and you only
  discover it weeks later from the applications table. Here it is a startup
  error with the key name in it.
- **Validation happens once, at load.** By the time the orchestrator touches
  `config.run.daily_application_cap` it is an int in range, so no call site does
  defensive parsing.
- **Per-account credentials are resolved lazily via `validate_for_run()`.** A run
  targeting `secondary` must fail loudly if only account 1 is configured, rather
  than silently applying with the wrong resume — which is the single most
  expensive mistake this agent can make.
- **`FilterRules.relaxed()`** exists because Naukri's recommended feed has
  already matched the job to the profile: inclusion keyword lists and the
  freshness cap only throw good jobs away there, while every blocklist and the
  experience sanity check must still apply.
- **`${VAR}` interpolation** in the YAML, so a value can reference the
  environment without turning the config into a template language.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .core.answers import ExperienceAnswers

# Repository root: <root>/naukri_agent/config.py -> <root>
PROJECT_ROOT = Path(__file__).resolve().parent.parent

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ACCOUNT_KEYS = ("primary", "secondary")


class ConfigError(RuntimeError):
    """Raised for any unusable configuration; the CLI turns it into exit code 2."""


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class NaukriAccount:
    """One Naukri login. One account == one resume == one job family."""

    key: str
    email: str
    password: str

    @property
    def masked_email(self) -> str:
        name, _, domain = self.email.partition("@")
        head = name[:2] if len(name) > 2 else name
        return f"{head}{'*' * max(0, len(name) - 2)}@{domain}" if domain else "unset"

    @property
    def session_key(self) -> str:
        """Storage-state row key. Per account: a shared key made the two logins
        overwrite each other's cookies on every run."""
        return f"naukri:session:{self.key}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- accounts ---------------------------------------------------------
    naukri_email: str = ""
    naukri_password: str = ""
    naukri_email_2: str = ""
    naukri_password_2: str = ""
    default_account: str = "primary"

    # --- database ---------------------------------------------------------
    database_url: str = ""
    db_schema: str = "naukri"
    db_pool_min_size: int = Field(default=1, ge=1, le=20)
    db_pool_max_size: int = Field(default=5, ge=1, le=50)
    # Escape hatch for self-hosted Postgres with a self-signed certificate.
    # Off by default: skipping verification on a managed provider would trade a
    # real MITM protection for nothing.
    db_ssl_insecure: bool = False

    # --- notifications ----------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- paths ------------------------------------------------------------
    config_path: Path = Path("config/config.yaml")
    artifacts_dir: Path = Path("artifacts")
    log_dir: Path = Path("logs")
    resume_dir: Path = Path("resumes")

    # --- runtime knobs ----------------------------------------------------
    # None means "whatever config.yaml says"; set HEADLESS to override it.
    headless: bool | None = None
    log_level: str = "INFO"
    log_json: bool = False
    dry_run: bool = False

    @field_validator("config_path", "artifacts_dir", "log_dir", "resume_dir")
    @classmethod
    def _absolutise(cls, value: Path) -> Path:
        """Relative paths are relative to the repo root, not to the CWD, so a
        cron entry that does not `cd` first still finds the config."""
        return value if value.is_absolute() else (PROJECT_ROOT / value)

    @field_validator("db_schema")
    @classmethod
    def _safe_schema(cls, value: str) -> str:
        # Interpolated into `SET search_path` / `CREATE SCHEMA`, which cannot be
        # parameterised, so it must be an identifier and nothing else.
        if not _IDENTIFIER.match(value):
            raise ValueError(f"DB_SCHEMA must be a plain SQL identifier, got {value!r}")
        return value

    @field_validator("default_account")
    @classmethod
    def _known_account(cls, value: str) -> str:
        key = value.strip().lower()
        if key not in ACCOUNT_KEYS:
            raise ValueError(f"DEFAULT_ACCOUNT must be one of {ACCOUNT_KEYS}, got {value!r}")
        return key

    @model_validator(mode="after")
    def _pool_sizes(self) -> "Settings":
        if self.db_pool_max_size < self.db_pool_min_size:
            raise ValueError("DB_POOL_MAX_SIZE must be >= DB_POOL_MIN_SIZE")
        return self

    # ------------------------------------------------------------- database
    @property
    def requires_ssl(self) -> bool:
        if not self.database_url:
            return False
        parts = urlsplit(self.database_url)
        params = dict(parse_qsl(parts.query))
        mode = params.get("sslmode", "").lower()
        if mode in ("disable", "allow"):
            return False
        if mode:
            return True
        # No explicit mode: local Postgres has no TLS, managed providers do.
        return (parts.hostname or "") not in ("localhost", "127.0.0.1", "::1", "postgres", "db")

    @property
    def asyncpg_dsn(self) -> str:
        """
        asyncpg rejects libpq-only query parameters that managed providers put in
        their connection strings (`sslmode`, `channel_binding`, `options=...`), so
        they are stripped here and TLS is passed as an `ssl` object instead.
        """
        if not self.database_url:
            raise ConfigError("DATABASE_URL is not set")
        parts = urlsplit(self.database_url)
        scheme = "postgresql" if parts.scheme in ("postgres", "postgresql") else parts.scheme
        libpq_only = {
            "sslmode",
            "channel_binding",
            "sslrootcert",
            "sslcert",
            "sslkey",
            "gssencmode",
            "options",
            "target_session_attrs",
            "connect_timeout",
            "application_name",
        }
        kept = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in libpq_only]
        return urlunsplit((scheme, parts.netloc, parts.path, urlencode(kept), ""))

    # ------------------------------------------------------------- accounts
    def account(self, key: str | None = None) -> NaukriAccount:
        """Credentials for one account key; may be blank (see validate_for_run)."""
        resolved = (key or self.default_account or "primary").strip().lower()
        if resolved not in ACCOUNT_KEYS:
            raise ConfigError(f"Unknown account '{resolved}'. Use one of {ACCOUNT_KEYS}.")
        if resolved == "primary":
            return NaukriAccount("primary", self.naukri_email.strip(), self.naukri_password)
        return NaukriAccount("secondary", self.naukri_email_2.strip(), self.naukri_password_2)

    def configured_accounts(self) -> list[NaukriAccount]:
        """Only the accounts that actually have both an email and a password."""
        return [
            account
            for account in (self.account(key) for key in ACCOUNT_KEYS)
            if account.email and account.password
        ]

    def validate_for_run(self, key: str | None = None) -> NaukriAccount:
        """
        Resolve + assert the credentials for THIS account. Called at the top of
        every run so a misconfigured second account fails immediately instead of
        applying with account 1's resume.
        """
        if not self.database_url:
            raise ConfigError("DATABASE_URL is required. Copy .env.example to .env and fill it in.")
        account = self.account(key)
        if not account.email or not account.password:
            env = "NAUKRI_EMAIL/NAUKRI_PASSWORD" if account.key == "primary" else "NAUKRI_EMAIL_2/NAUKRI_PASSWORD_2"
            raise ConfigError(f"Account '{account.key}' is not configured — set {env} in .env")
        return account

    def ensure_dirs(self) -> None:
        for directory in (self.artifacts_dir, self.log_dir, self.resume_dir):
            try:
                Path(directory).mkdir(parents=True, exist_ok=True)
            except OSError:
                # Read-only mounts are acceptable: artifacts/logs degrade, the
                # run itself does not depend on them.
                pass


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except Exception as exc:  # pydantic ValidationError -> readable message
        raise ConfigError(f"Invalid environment configuration: {exc}") from exc


# ---------------------------------------------------------------------------
# YAML behaviour config
# ---------------------------------------------------------------------------
class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _stringify_answers(value: Any) -> dict[str, str]:
    """
    Answer keys are matched case-insensitively, so they are normalised once here
    rather than at every lookup. Values are coerced to `str` because YAML turns
    `expected ctc: 18` into an int and every downstream consumer types text.
    """
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValueError("`answers` must be a mapping of question pattern -> answer")
    normalised: dict[str, str] = {}
    for key, answer in value.items():
        pattern = str(key).strip().lower()
        if not pattern:
            continue
        if isinstance(answer, bool):
            normalised[pattern] = "Yes" if answer else "No"
        else:
            normalised[pattern] = "" if answer is None else str(answer).strip()
    return normalised


class BrowserConfig(_Model):
    headless: bool = True
    slow_mo_ms: int = Field(default=0, ge=0, le=5_000)
    viewport_width: int = Field(default=1440, ge=800)
    viewport_height: int = Field(default=900, ge=600)
    locale: str = "en-IN"
    timezone: str = "Asia/Kolkata"
    default_timeout_ms: int = Field(default=30_000, ge=1_000)
    navigation_timeout_ms: int = Field(default=60_000, ge=1_000)
    min_action_delay_ms: int = Field(default=350, ge=0)
    max_action_delay_ms: int = Field(default=1_400, ge=0)
    block_resources: list[str] = Field(default_factory=list)
    user_agent: str = ""

    @model_validator(mode="after")
    def _ordered_delays(self) -> "BrowserConfig":
        if self.max_action_delay_ms < self.min_action_delay_ms:
            raise ValueError("browser.max_action_delay_ms must be >= min_action_delay_ms")
        return self


class RunConfig(_Model):
    daily_application_cap: int = Field(default=60, ge=1, le=500)
    dedupe_window_days: int = Field(default=90, ge=1, le=3_650)
    max_consecutive_failures: int = Field(default=5, ge=1, le=100)
    min_delay_between_applies_s: float = Field(default=8, ge=0)
    max_delay_between_applies_s: float = Field(default=25, ge=0)
    max_retries_per_job: int = Field(default=2, ge=1, le=5)
    strict_answers: bool = True
    screenshot_on_failure: bool = True
    screenshot_on_success: bool = False
    run_timeout_minutes: int = Field(default=90, ge=1, le=1_440)

    @model_validator(mode="after")
    def _ordered_delays(self) -> "RunConfig":
        if self.max_delay_between_applies_s < self.min_delay_between_applies_s:
            raise ValueError("run.max_delay_between_applies_s must be >= min_delay_between_applies_s")
        return self


class RecommendedConfig(_Model):
    """Naukri's own 'Recommended jobs' feed — the primary job source."""

    enabled: bool = True
    tabs: list[str] = Field(
        default_factory=lambda: ["top candidate", "preferences", "profile", "you might like"]
    )
    max_scroll_rounds: int = Field(default=14, ge=1, le=100)
    stall_rounds_before_stop: int = Field(default=3, ge=1, le=20)
    max_jobs: int = Field(default=250, ge=1, le=2_000)
    follow_show_more: bool = True


class ScheduleConfig(_Model):
    enabled: bool = True
    cron: str = "30 9 * * 1-5"
    # The two accounts MUST be hours apart: two logins from one IP inside the
    # same window is the pattern most likely to trigger Naukri's challenge.
    cron_by_account: dict[str, str] = Field(default_factory=dict)
    timezone: str = "Asia/Kolkata"
    jitter_seconds: int = Field(default=600, ge=0, le=7_200)
    run_on_start: bool = False

    @field_validator("cron_by_account")
    @classmethod
    def _known_accounts(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned = {str(k).strip().lower(): str(v) for k, v in (value or {}).items()}
        unknown = set(cleaned) - set(ACCOUNT_KEYS)
        if unknown:
            raise ValueError(f"schedule.cron_by_account has unknown accounts: {sorted(unknown)}")
        return cleaned

    def cron_for(self, account: str) -> str:
        return self.cron_by_account.get(account.strip().lower(), self.cron)


class NotificationsConfig(_Model):
    telegram_enabled: bool = False
    notify_on_success: bool = True
    notify_on_failure: bool = True
    include_job_list: bool = True
    max_jobs_in_message: int = Field(default=15, ge=1, le=100)


class ProfileRefreshConfig(_Model):
    """
    Daily 'touch the profile so recruiter search ranks it' pass.

    Recruiter-side search sorts by *profile last updated*, so this is often worth
    more inbound contact than the applications themselves — and it has no quota.
    `min_hours_between` is enforced against the `profile_updates` table, so three
    runs a day still produce exactly one edit.
    """

    enabled: bool = True
    strategies: list[str] = Field(default_factory=lambda: ["headline"])
    min_hours_between: int = Field(default=20, ge=1, le=168)
    verify: bool = True
    headline_variants: list[str] = Field(default_factory=list)

    @field_validator("strategies")
    @classmethod
    def _known_strategies(cls, value: list[str]) -> list[str]:
        allowed = {"headline", "resume", "skills"}
        cleaned = [str(item).strip().lower() for item in value if str(item).strip()]
        unknown = set(cleaned) - allowed
        if unknown:
            raise ValueError(f"profile_refresh.strategies has unknown entries: {sorted(unknown)}")
        return cleaned


class SearchSpec(_Model):
    keyword: str
    locations: list[str] = Field(default_factory=list)
    max_pages: int = Field(default=3, ge=1, le=50)
    sort_by: str = "date"
    freshness_days: int | None = Field(default=None, ge=1, le=365)
    # A hand-built Naukri URL wins over the generated one; Naukri's own filter
    # UI produces query strings we cannot reconstruct from keywords alone.
    custom_url: str | None = None

    @field_validator("locations")
    @classmethod
    def _lower(cls, value: list[str]) -> list[str]:
        return [str(item).strip().lower() for item in value if str(item).strip()]


class ExperienceRange(_Model):
    min_years: float = Field(default=0, ge=0, le=50)
    max_years: float = Field(default=99, ge=0, le=50)

    @model_validator(mode="after")
    def _ordered(self) -> "ExperienceRange":
        if self.max_years < self.min_years:
            raise ValueError("filters.experience.max_years must be >= min_years")
        return self


class FilterRules(_Model):
    title_must_include_any: list[str] = Field(default_factory=list)
    title_must_exclude_any: list[str] = Field(default_factory=list)
    description_must_include_any: list[str] = Field(default_factory=list)
    description_must_exclude_any: list[str] = Field(default_factory=list)
    allowed_locations: list[str] = Field(default_factory=list)
    blocked_locations: list[str] = Field(default_factory=list)
    blocked_companies: list[str] = Field(default_factory=list)
    experience: ExperienceRange = Field(default_factory=ExperienceRange)
    min_salary_lpa: float | None = Field(default=None, ge=0)
    max_posted_days: int | None = Field(default=None, ge=0)
    min_rating: float | None = Field(default=None, ge=0, le=5)
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
    )
    @classmethod
    def _lower(cls, value: list[str]) -> list[str]:
        # Every comparison in filters.py is done on a lowercased haystack.
        return [str(item).strip().lower() for item in value if str(item).strip()]

    def relaxed(self) -> "FilterRules":
        """
        Ruleset for Naukri's recommended feed.

        Naukri already matched these postings against the profile, headline and
        preferences, so inclusion keyword lists and the freshness cap only
        discard good jobs there. Every blocklist and the experience sanity check
        are kept — those encode "I do not want this", which still holds.
        """
        return self.model_copy(
            update={
                "title_must_include_any": [],
                "description_must_include_any": [],
                "max_posted_days": None,
                "min_rating": None,
            }
        )


class ExperienceConfig(_Model):
    """
    Declarative skill -> years map for the open-ended "how many years of X?"
    family of screening questions, which no finite answer table can cover.
    """

    total_years: float | None = Field(default=None, ge=0, le=50)
    default_years: float | None = Field(default=None, ge=0, le=50)
    multi_skill_strategy: str = "max"
    skills: dict[str, float] = Field(default_factory=dict)

    @field_validator("multi_skill_strategy")
    @classmethod
    def _known_strategy(cls, value: str) -> str:
        strategy = str(value).strip().lower()
        if strategy not in ("max", "min", "avg"):
            raise ValueError("experience.multi_skill_strategy must be max, min or avg")
        return strategy

    @field_validator("skills", mode="before")
    @classmethod
    def _normalise_skills(cls, value: Any) -> dict[str, float]:
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise ValueError("experience.skills must be a mapping of skill -> years")
        out: dict[str, float] = {}
        for skill, years in value.items():
            name = " ".join(str(skill).strip().lower().split())
            if not name:
                continue
            try:
                out[name] = float(years)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"experience.skills['{skill}'] must be a number") from exc
        return out

    def to_answers(self) -> ExperienceAnswers:
        return ExperienceAnswers(
            total_years=self.total_years,
            default_years=self.default_years,
            multi_skill_strategy=self.multi_skill_strategy,
            skills=dict(self.skills),
        )


class JobProfile(_Model):
    name: str
    enabled: bool = True
    priority: int = 100
    # One account == one resume == one job family (Naukri attaches whatever CV
    # sits on the profile; there is no per-application picker).
    account: str = "primary"
    experience_years: float = Field(default=0, ge=0, le=50)
    use_recommended: bool = True
    max_applications_per_run: int = Field(default=15, ge=1, le=200)
    resume_label: str | None = None
    resume_file: str | None = None
    searches: list[SearchSpec] = Field(default_factory=list)
    # When true the keyword searches only run if the recommended feed did not
    # fill `max_applications_per_run`.
    search_is_fallback_only: bool = True
    filters: FilterRules = Field(default_factory=FilterRules)
    answers: dict[str, str] = Field(default_factory=dict)
    experience: ExperienceConfig | None = None

    @field_validator("answers", mode="before")
    @classmethod
    def _answers(cls, value: Any) -> dict[str, str]:
        return _stringify_answers(value)

    @field_validator("account")
    @classmethod
    def _account(cls, value: str) -> str:
        key = str(value).strip().lower()
        if key not in ACCOUNT_KEYS:
            raise ValueError(f"profiles[].account must be one of {ACCOUNT_KEYS}, got {value!r}")
        return key

    def filters_for(self, source: str) -> FilterRules:
        """`recommended` gets the relaxed ruleset; keyword search gets the full one."""
        return self.filters.relaxed() if source == "recommended" else self.filters


class AgentConfig(_Model):
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    run: RunConfig = Field(default_factory=RunConfig)
    recommended: RecommendedConfig = Field(default_factory=RecommendedConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    profile_refresh: ProfileRefreshConfig = Field(default_factory=ProfileRefreshConfig)
    answers: dict[str, str] = Field(default_factory=dict)
    experience: ExperienceConfig = Field(default_factory=ExperienceConfig)
    profiles: list[JobProfile] = Field(default_factory=list)

    @field_validator("answers", mode="before")
    @classmethod
    def _answers(cls, value: Any) -> dict[str, str]:
        return _stringify_answers(value)

    @model_validator(mode="after")
    def _unique_names(self) -> "AgentConfig":
        names = [profile.name.strip().lower() for profile in self.profiles]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            # Profile name is the persistence key (applications.profile), so a
            # duplicate would silently merge two profiles' dedupe state.
            raise ValueError(f"duplicate profile names: {sorted(duplicates)}")
        return self

    # ------------------------------------------------------------- selection
    def active_profiles(
        self, only: list[str] | None = None, account: str | None = None
    ) -> list[JobProfile]:
        """Enabled profiles for one account, highest priority (lowest number) first."""
        wanted = {name.strip().lower() for name in (only or []) if name.strip()}
        key = (account or "").strip().lower()
        selected = [
            profile
            for profile in self.profiles
            if profile.enabled
            and (not wanted or profile.name.strip().lower() in wanted)
            and (not key or profile.account == key)
        ]
        return sorted(selected, key=lambda profile: (profile.priority, profile.name))

    def accounts_in_use(self) -> list[str]:
        """Accounts referenced by at least one enabled profile, in config order."""
        seen: list[str] = []
        for profile in self.profiles:
            if profile.enabled and profile.account not in seen:
                seen.append(profile.account)
        return seen

    def experience_for(self, profile: JobProfile) -> ExperienceAnswers:
        """Global skill map, overridden (and merged) by the profile's own."""
        base = self.experience.to_answers()
        override = profile.experience.to_answers() if profile.experience else None
        return base.merged_with(override)

    def resume_for(self, account: str) -> str | None:
        """First configured resume file for an account (used by the refresher)."""
        for profile in self.profiles:
            if profile.enabled and profile.account == account and profile.resume_file:
                return profile.resume_file
        return None

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: Path | str | None = None) -> "AgentConfig":
        settings = get_settings()
        target = Path(path) if path else settings.config_path
        if not target.is_absolute():
            target = PROJECT_ROOT / target
        if not target.exists():
            example = target.with_name("config.example.yaml")
            hint = f" Copy {example} to {target} and edit it." if example.exists() else ""
            raise ConfigError(f"Config file not found: {target}.{hint}")

        try:
            raw = yaml.safe_load(_interpolate(target.read_text(encoding="utf-8"))) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{target} is not valid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{target} must contain a YAML mapping at the top level")

        try:
            config = cls.model_validate(raw)
        except Exception as exc:
            raise ConfigError(f"Invalid configuration in {target}:\n{exc}") from exc

        # HEADLESS in the environment is an operational override (a human
        # debugging a run needs to see the browser), so it wins over the file.
        if settings.headless is not None:
            config.browser.headless = settings.headless
        if not config.profiles:
            raise ConfigError(f"{target} defines no profiles — nothing to run")
        return config


def _interpolate(text: str) -> str:
    """Expand `${VAR}` and `${VAR:-default}` from the environment."""

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        value = os.environ.get(name)
        if value is None:
            if default is None:
                raise ConfigError(f"config references ${{{name}}} but it is not set")
            return default
        return value

    return _ENV_REF.sub(replace, text)


def load_config(path: Path | str | None = None) -> AgentConfig:
    """Module-level convenience wrapper used by the CLI and the scheduler."""
    return AgentConfig.load(path)
