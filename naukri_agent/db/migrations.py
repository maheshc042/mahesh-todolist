"""
Schema management.

Design decision: forward-only, idempotent SQL applied at startup, tracked in
`naukri.schema_migrations`. A full migration framework (Alembic) is overkill for
a single-service agent, but blindly running `CREATE TABLE IF NOT EXISTS` loses
the ability to evolve columns. The version table gives us both: cheap and
ordered.

All objects live in the `naukri` schema so the agent never collides with
application or `neon_auth` tables in the same Neon database.
"""

from __future__ import annotations

import asyncpg

from ..config import get_settings
from ..logging_setup import get_logger
from .pool import get_pool

log = get_logger(__name__)

MIGRATIONS: list[tuple[str, str]] = [
    (
        "0001_core_tables",
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id           TEXT PRIMARY KEY,
            title            TEXT NOT NULL,
            company          TEXT NOT NULL DEFAULT '',
            url              TEXT NOT NULL,
            location         TEXT NOT NULL DEFAULT '',
            experience_text  TEXT NOT NULL DEFAULT '',
            salary_text      TEXT NOT NULL DEFAULT '',
            posted_text      TEXT NOT NULL DEFAULT '',
            rating           NUMERIC(3,1),
            tags             TEXT[] NOT NULL DEFAULT '{}',
            min_experience   NUMERIC(4,1),
            max_experience   NUMERIC(4,1),
            min_salary_lpa   NUMERIC(6,2),
            posted_days_ago  INTEGER,
            is_walkin        BOOLEAN NOT NULL DEFAULT FALSE,
            source_keyword   TEXT NOT NULL DEFAULT '',
            first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS runs (
            id              BIGSERIAL PRIMARY KEY,
            mode            TEXT NOT NULL DEFAULT 'manual',
            status          TEXT NOT NULL DEFAULT 'running',
            profiles        TEXT[] NOT NULL DEFAULT '{}',
            started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at     TIMESTAMPTZ,
            duration_s      INTEGER,
            stats           JSONB NOT NULL DEFAULT '{}'::jsonb,
            error           TEXT
        );

        CREATE TABLE IF NOT EXISTS applications (
            id               BIGSERIAL PRIMARY KEY,
            job_id           TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
            run_id           BIGINT REFERENCES runs(id) ON DELETE SET NULL,
            profile          TEXT NOT NULL,
            status           TEXT NOT NULL,
            reason           TEXT,
            detail           TEXT,
            attempts         INTEGER NOT NULL DEFAULT 1,
            questions_answered INTEGER NOT NULL DEFAULT 0,
            screenshot_path  TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (job_id, profile)
        );

        CREATE INDEX IF NOT EXISTS applications_status_idx ON applications(status);
        CREATE INDEX IF NOT EXISTS applications_created_idx ON applications(created_at DESC);
        CREATE INDEX IF NOT EXISTS applications_run_idx ON applications(run_id);

        CREATE TABLE IF NOT EXISTS run_events (
            id          BIGSERIAL PRIMARY KEY,
            run_id      BIGINT REFERENCES runs(id) ON DELETE CASCADE,
            level       TEXT NOT NULL DEFAULT 'info',
            event       TEXT NOT NULL,
            job_id      TEXT,
            profile     TEXT,
            payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX IF NOT EXISTS run_events_run_idx ON run_events(run_id, created_at);
        """,
    ),
    (
        "0002_answers_and_review",
        """
        -- Knowledge base for screening questions. `pattern` is matched
        -- case-insensitively as a substring, then as a regex if it compiles.
        CREATE TABLE IF NOT EXISTS answer_kb (
            id          BIGSERIAL PRIMARY KEY,
            profile     TEXT,                       -- NULL = applies to all profiles
            pattern     TEXT NOT NULL,
            answer      TEXT NOT NULL,
            priority    INTEGER NOT NULL DEFAULT 100,
            hits        INTEGER NOT NULL DEFAULT 0,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (profile, pattern)
        );

        -- Questions the agent could not answer confidently. The run skips the
        -- job and a human fills `answer` here; the next run picks it up.
        CREATE TABLE IF NOT EXISTS question_review (
            id            BIGSERIAL PRIMARY KEY,
            job_id        TEXT,
            profile       TEXT NOT NULL,
            question      TEXT NOT NULL,
            question_kind TEXT NOT NULL DEFAULT 'unknown',
            options       JSONB NOT NULL DEFAULT '[]'::jsonb,
            screenshot_path TEXT,
            times_seen    INTEGER NOT NULL DEFAULT 1,
            resolved      BOOLEAN NOT NULL DEFAULT FALSE,
            answer        TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (profile, question)
        );
        """,
    ),
    (
        "0003_session_state",
        """
        -- Playwright storage_state (cookies + localStorage) persisted in the DB
        -- so a stateless container keeps the Naukri login across restarts.
        CREATE TABLE IF NOT EXISTS browser_sessions (
            key         TEXT PRIMARY KEY,
            state       JSONB NOT NULL,
            valid_until TIMESTAMPTZ,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
    (
        "0004_daily_counter_view",
        """
        CREATE OR REPLACE VIEW daily_application_counts AS
        SELECT (created_at AT TIME ZONE 'Asia/Kolkata')::date AS day,
               profile,
               count(*) FILTER (WHERE status = 'applied') AS applied,
               count(*) FILTER (WHERE status = 'failed') AS failed,
               count(*) FILTER (WHERE status = 'skipped') AS skipped
        FROM applications
        GROUP BY 1, 2;
        """,
    ),
    (
        "0005_multi_account",
        """
        -- Two Naukri logins, each with its own resume, share this database.
        -- Without an account column the daily cap was global: account A's
        -- applications consumed account B's budget, and the two accounts could
        -- not be reported on separately.
        ALTER TABLE applications ADD COLUMN IF NOT EXISTS account TEXT NOT NULL DEFAULT 'primary';
        ALTER TABLE runs         ADD COLUMN IF NOT EXISTS account TEXT NOT NULL DEFAULT 'primary';
        ALTER TABLE run_events   ADD COLUMN IF NOT EXISTS account TEXT;

        CREATE INDEX IF NOT EXISTS applications_account_created_idx
            ON applications(account, created_at DESC);
        CREATE INDEX IF NOT EXISTS applications_account_status_idx
            ON applications(account, status);
        CREATE INDEX IF NOT EXISTS runs_account_idx ON runs(account, started_at DESC);

        -- The view gains a column, so CREATE OR REPLACE is not enough.
        DROP VIEW IF EXISTS daily_application_counts;
        CREATE VIEW daily_application_counts AS
        SELECT (created_at AT TIME ZONE 'Asia/Kolkata')::date AS day,
               account,
               profile,
               count(*) FILTER (WHERE status = 'applied') AS applied,
               count(*) FILTER (WHERE status = 'failed') AS failed,
               count(*) FILTER (WHERE status = 'skipped') AS skipped,
               count(*) FILTER (WHERE status = 'needs_review') AS needs_review
        FROM applications
        GROUP BY 1, 2, 3;
        """,
    ),
    (
        "0006_profile_updates",
        """
        -- Daily profile refresh log. Naukri ranks recruiter-search results by
        -- "profile last updated", so the agent touches the profile once a day.
        --
        -- This table is the idempotency guard: `min_hours_between` is enforced
        -- by querying the last successful row, so three runs in one day still
        -- produce exactly one profile edit and a container restart cannot
        -- double-touch (repeated edits in one day is what Naukri flags).
        CREATE TABLE IF NOT EXISTS profile_updates (
            id            BIGSERIAL PRIMARY KEY,
            account       TEXT NOT NULL,
            run_id        BIGINT REFERENCES runs(id) ON DELETE SET NULL,
            strategy      TEXT NOT NULL DEFAULT '',
            ok            BOOLEAN NOT NULL DEFAULT FALSE,
            detail        TEXT,
            headline_before TEXT,
            headline_after  TEXT,
            last_updated_text TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        -- The hot query is "last successful refresh for this account".
        CREATE INDEX IF NOT EXISTS profile_updates_account_idx
            ON profile_updates(account, ok, created_at DESC);

        -- run_events gained an `account` column in 0005 but nothing wrote to it,
        -- so per-account log filtering silently returned nothing.
        CREATE INDEX IF NOT EXISTS run_events_account_idx
            ON run_events(account, created_at DESC);

        CREATE OR REPLACE VIEW profile_freshness AS
        SELECT account,
               max(created_at) FILTER (WHERE ok) AS last_success_at,
               max(created_at)                   AS last_attempt_at,
               count(*) FILTER (WHERE ok)        AS successes,
               count(*) FILTER (WHERE NOT ok)    AS failures
        FROM profile_updates
        GROUP BY account;
        """,
    ),
    (
        "0007_answer_learning",
        """
        -- Self-learning answer system: track where KB entries came from, and
        -- whether review resolutions were auto-resolved by fuzzy matching.
        ALTER TABLE answer_kb ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'yaml';
        ALTER TABLE answer_kb ADD COLUMN IF NOT EXISTS auto_resolved_from INTEGER;

        ALTER TABLE question_review ADD COLUMN IF NOT EXISTS auto_resolved BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE question_review ADD COLUMN IF NOT EXISTS resolved_by TEXT;

        -- Index for finding similar unresolved questions during auto-resolve.
        CREATE INDEX IF NOT EXISTS question_review_unresolved_idx
            ON question_review(resolved, profile);
        """,
    ),
    (
        "0008_stats_and_match_score",
        """
        -- Weekly application counts view for the stats CLI.
        CREATE OR REPLACE VIEW weekly_application_counts AS
        SELECT date_trunc('week', created_at AT TIME ZONE 'Asia/Kolkata')::date AS week_start,
               account,
               profile,
               count(*) FILTER (WHERE status = 'applied') AS applied,
               count(*) FILTER (WHERE status = 'failed') AS failed,
               count(*) FILTER (WHERE status = 'skipped') AS skipped,
               count(*) FILTER (WHERE status = 'needs_review') AS needs_review
        FROM applications
        GROUP BY 1, 2, 3;

        -- Match score cache: avoids re-fetching scores for jobs we have seen.
        CREATE TABLE IF NOT EXISTS match_scores (
            job_id          TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
            keyskills_score INTEGER NOT NULL DEFAULT 0,
            experience_match BOOLEAN NOT NULL DEFAULT FALSE,
            overall_score   INTEGER NOT NULL DEFAULT 0,
            fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
    (
        "0009_cold_email_tracking",
        """
        -- Tracks recruiters we have cold-emailed to prevent duplicate spam.
        CREATE TABLE IF NOT EXISTS contacted_recruiters (
            email TEXT PRIMARY KEY,
            role_pitched TEXT NOT NULL,
            post_snippet TEXT,
            contacted_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """,
    ),
    (
        "0010_linkedin_post_url",
        """
        -- Adds post_url column to contacted_recruiters table.
        ALTER TABLE contacted_recruiters ADD COLUMN IF NOT EXISTS post_url TEXT;
        """,
    ),
]


async def run_migrations() -> list[str]:
    settings = get_settings()
    pool = await get_pool()
    applied: list[str] = []

    async with pool.acquire() as conn:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{settings.db_schema}"')
        await conn.execute(f'SET search_path TO "{settings.db_schema}", public')
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     TEXT PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )
        done = {
            row["version"] for row in await conn.fetch("SELECT version FROM schema_migrations")
        }
        for version, sql in MIGRATIONS:
            if version in done:
                continue
            try:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1)", version
                    )
            except asyncpg.PostgresError as exc:
                log.error("db.migration_failed", version=version, error=str(exc))
                raise
            applied.append(version)
            log.info("db.migration_applied", version=version)

    if not applied:
        log.info("db.schema_up_to_date", schema=settings.db_schema)
    return applied
