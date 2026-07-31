"""
Persistence API.

Design decision: a thin repository object instead of an ORM. Every statement is
explicit, parameterised (`$1`, `$2` — asyncpg native, so SQL injection is not
possible) and tuned for the exact access pattern the agent needs. This keeps
cold-start cost near zero and makes the queries auditable.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import asyncpg

from ..core.models import (
    ApplicationStatus,
    ApplyOutcome,
    Job,
    RunStats,
    RunStatus,
)
from ..logging_setup import get_logger
from .pool import get_pool

log = get_logger(__name__)


class Repository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def create(cls) -> "Repository":
        return cls(await get_pool())

    # ------------------------------------------------------------------ runs
    async def start_run(
        self, mode: str, profiles: list[str], account: str = "primary"
    ) -> int:
        row = await self.pool.fetchrow(
            """
            INSERT INTO runs (mode, status, profiles, account)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            mode,
            RunStatus.RUNNING.value,
            profiles,
            account,
        )
        return int(row["id"])

    async def finish_run(
        self,
        run_id: int,
        status: RunStatus,
        stats: RunStats,
        error: str | None = None,
    ) -> None:
        await self.pool.execute(
            """
            UPDATE runs
               SET status = $2,
                   finished_at = now(),
                   duration_s = GREATEST(0, EXTRACT(EPOCH FROM (now() - started_at))::int),
                   stats = $3::jsonb,
                   error = $4
             WHERE id = $1
            """,
            run_id,
            status.value,
            json.dumps(stats.as_dict()),
            error,
        )

    async def log_event(
        self,
        run_id: int | None,
        event: str,
        level: str = "info",
        job_id: str | None = None,
        profile: str | None = None,
        payload: dict[str, Any] | None = None,
        account: str | None = None,
    ) -> None:
        """
        Best-effort audit trail; never let telemetry break a run.

        `account` is a real column (added in migration 0005) rather than only a
        payload key, so "show me everything account B did last Tuesday" is an
        indexed query instead of a JSONB scan.
        """
        try:
            await self.pool.execute(
                """
                INSERT INTO run_events
                    (run_id, level, event, job_id, profile, payload, account)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
                """,
                run_id,
                level,
                event,
                job_id,
                profile,
                json.dumps(payload or {}),
                account,
            )
        except Exception as exc:  # pragma: no cover - telemetry must not raise
            log.warning("db.event_log_failed", error=str(exc), event=event)

    # ------------------------------------------------------------------ jobs
    async def upsert_job(self, job: Job) -> None:
        row = job.to_row()
        await self.pool.execute(
            """
            INSERT INTO jobs (
                job_id, title, company, url, location, experience_text, salary_text,
                posted_text, rating, tags, min_experience, max_experience,
                min_salary_lpa, posted_days_ago, is_walkin, source_keyword
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
            ON CONFLICT (job_id) DO UPDATE
               SET last_seen_at = now(),
                   salary_text = EXCLUDED.salary_text,
                   posted_text = EXCLUDED.posted_text,
                   posted_days_ago = EXCLUDED.posted_days_ago,
                   rating = COALESCE(EXCLUDED.rating, jobs.rating)
            """,
            row["job_id"],
            row["title"],
            row["company"],
            row["url"],
            row["location"],
            row["experience_text"],
            row["salary_text"],
            row["posted_text"],
            row["rating"],
            row["tags"],
            row["min_experience"],
            row["max_experience"],
            row["min_salary_lpa"],
            row["posted_days_ago"],
            row["is_walkin"],
            row["source_keyword"],
        )

    async def known_job_ids(
        self, profile: str, window_days: int, account: str = "primary"
    ) -> set[str]:
        """
        Jobs already decided for this profile/account inside the dedupe window.

        Two statuses are deliberately treated as NOT decided, so they come back:

        - `failed`: transient (timeout, markup hiccup) and worth one more try.
        - `needs_review` whose questions have all been answered by a human. This
          is what makes `naukri-agent resolve <id> "<answer>"` mean anything: the
          old query treated needs_review as final, so a resolved answer was
          promoted into the knowledge base and then never used, because the job
          it unblocked was permanently excluded from every future run. Jobs whose
          questions are still unresolved stay excluded, so we do not re-open the
          same dead end every morning.
        """
        rows = await self.pool.fetch(
            """
            SELECT a.job_id
              FROM applications a
             WHERE a.status <> 'failed'
               AND a.created_at > now() - ($1 || ' days')::interval
               AND NOT (
                     a.status = 'needs_review'
                 AND NOT EXISTS (
                          SELECT 1
                            FROM question_review q
                           WHERE q.profile = a.profile
                             AND q.job_id = a.job_id
                             AND q.resolved = FALSE
                      )
                  )
            """,
            str(window_days),
        )
        return {row["job_id"] for row in rows}

    async def applied_today(self, account: str = "primary") -> int:
        """
        Applications submitted TODAY by THIS account (Asia/Kolkata calendar day).

        Account scoping is essential: the daily cap is a per-account budget, and
        an unscoped count let one account exhaust the other's quota.
        """
        row = await self.pool.fetchrow(
            """
            SELECT count(*) AS n
              FROM applications
             WHERE status = 'applied'
               AND account = $1
               AND (created_at AT TIME ZONE 'Asia/Kolkata')::date
                   = (now() AT TIME ZONE 'Asia/Kolkata')::date
            """,
            account,
        )
        return int(row["n"] or 0)

    async def record_outcome(
        self,
        job: Job,
        profile: str,
        run_id: int | None,
        outcome: ApplyOutcome,
        account: str = "primary",
    ) -> None:
        await self.upsert_job(job)
        await self.pool.execute(
            """
            INSERT INTO applications (
                job_id, run_id, profile, status, reason, detail, attempts,
                questions_answered, screenshot_path, account
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (job_id, profile) DO UPDATE
               SET status = EXCLUDED.status,
                   reason = EXCLUDED.reason,
                   detail = EXCLUDED.detail,
                   run_id = EXCLUDED.run_id,
                   account = EXCLUDED.account,
                   attempts = applications.attempts + EXCLUDED.attempts,
                   questions_answered = EXCLUDED.questions_answered,
                   screenshot_path = COALESCE(EXCLUDED.screenshot_path, applications.screenshot_path),
                   created_at = now()
            """,
            job.job_id,
            run_id,
            profile,
            outcome.status.value,
            outcome.reason.value if outcome.reason else None,
            outcome.detail[:2000] if outcome.detail else None,
            outcome.attempts,
            outcome.questions_answered,
            outcome.screenshot_path,
            account,
        )

    async def recent_applications(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT a.status, a.reason, a.profile, a.account, a.created_at,
                   j.title, j.company, j.url
              FROM applications a
              JOIN jobs j USING (job_id)
             ORDER BY a.created_at DESC
             LIMIT $1
            """,
            limit,
        )
        return [dict(row) for row in rows]

    async def applied_today_by_account(self) -> list[dict[str, Any]]:
        """Today's per-account application count — the live view of both budgets."""
        rows = await self.pool.fetch(
            """
            SELECT account,
                   count(*) FILTER (WHERE status = 'applied')      AS applied,
                   count(*) FILTER (WHERE status = 'failed')       AS failed,
                   count(*) FILTER (WHERE status = 'needs_review') AS needs_review
              FROM applications
             WHERE (created_at AT TIME ZONE 'Asia/Kolkata')::date
                   = (now() AT TIME ZONE 'Asia/Kolkata')::date
             GROUP BY account
             ORDER BY account
            """
        )
        return [dict(row) for row in rows]

    # --------------------------------------------------------------- answers
    async def load_answer_kb(self, profile: str) -> list[tuple[str, str]]:
        """Profile-specific rows first, then globals, ordered by priority."""
        rows = await self.pool.fetch(
            """
            SELECT pattern, answer
              FROM answer_kb
             WHERE profile IS NULL OR profile = $1
             ORDER BY (profile IS NULL), priority, length(pattern) DESC
            """,
            profile,
        )
        return [(row["pattern"], row["answer"]) for row in rows]

    async def seed_answer_kb(self, answers: dict[str, str], profile: str | None = None) -> int:
        """
        Sync YAML answers into the DB. YAML is the source of truth for seeded
        entries; human-resolved review answers are inserted separately and win
        via a lower priority value.
        """
        if not answers:
            return 0
        records = [(profile, pattern.strip().lower(), answer) for pattern, answer in answers.items()]
        await self.pool.executemany(
            """
            INSERT INTO answer_kb (profile, pattern, answer, priority)
            VALUES ($1, $2, $3, 100)
            ON CONFLICT (profile, pattern) DO UPDATE
               SET answer = EXCLUDED.answer, updated_at = now()
            """,
            records,
        )
        return len(records)

    async def bump_answer_hits(self, hits: dict[tuple[str, str], int], profile: str) -> None:
        """
        Record which KB patterns actually answered a question.

        `answer_kb.hits` existed but nothing ever incremented it, so there was no
        way to tell a load-bearing pattern from a typo that has never matched.
        Called once per profile at the end of a run, not per answer, to keep the
        write count proportional to profiles rather than to questions.
        """
        if not hits:
            return
        records = [
            (pattern, None if source == "kb" else profile, count)
            for (pattern, source), count in hits.items()
            # Synthetic patterns from the experience map have no KB row.
            if not pattern.startswith("experience:")
        ]
        if not records:
            return
        try:
            await self.pool.executemany(
                """
                UPDATE answer_kb
                   SET hits = hits + $3
                 WHERE pattern = $1
                   AND (profile = $2 OR ($2 IS NULL AND profile IS NULL))
                """,
                records,
            )
        except Exception as exc:  # pragma: no cover - accounting must not raise
            log.warning("db.answer_hits_failed", error=str(exc)[:200])

    async def resolved_review_answers(self, profile: str) -> list[tuple[str, str]]:
        rows = await self.pool.fetch(
            """
            SELECT question, answer
              FROM question_review
             WHERE resolved = TRUE AND answer IS NOT NULL AND profile = $1
            """,
            profile,
        )
        return [(row["question"], row["answer"]) for row in rows]

    async def queue_question_for_review(
        self,
        profile: str,
        question: str,
        kind: str,
        options: list[str],
        job_id: str | None = None,
        screenshot_path: str | None = None,
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO question_review
                (job_id, profile, question, question_kind, options, screenshot_path)
            VALUES ($1,$2,$3,$4,$5::jsonb,$6)
            ON CONFLICT (profile, question) DO UPDATE
               SET times_seen = question_review.times_seen + 1,
                   updated_at = now(),
                   options = EXCLUDED.options,
                   screenshot_path = COALESCE(EXCLUDED.screenshot_path,
                                              question_review.screenshot_path)
            """,
            job_id,
            profile,
            question.strip()[:1000],
            kind,
            json.dumps(options),
            screenshot_path,
        )

        try:
            from ..config import PROJECT_ROOT
            json_file = PROJECT_ROOT / "unresolved_questions.json"
            data = []
            if json_file.exists():
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                except Exception:
                    data = []
            existing = next((item for item in data if item.get("question") == question.strip()), None)
            if existing:
                existing["times_seen"] = existing.get("times_seen", 1) + 1
            else:
                data.append({
                    "question": question.strip(),
                    "kind": kind,
                    "options": options,
                    "profile": profile,
                    "job_id": job_id,
                    "times_seen": 1,
                })
            json_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("unresolved_json_save_failed", error=str(exc))

    async def pending_reviews(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT id, profile, question, question_kind, options, times_seen, created_at
              FROM question_review
             WHERE resolved = FALSE
             ORDER BY times_seen DESC, created_at DESC
             LIMIT $1
            """,
            limit,
        )
        return [dict(row) for row in rows]

    async def resolve_review(self, review_id: int, answer: str) -> bool:
        row = await self.pool.fetchrow(
            """
            UPDATE question_review
               SET resolved = TRUE, answer = $2, updated_at = now()
             WHERE id = $1
            RETURNING profile, question
            """,
            review_id,
            answer,
        )
        if not row:
            return False
        # Promote the human answer into the KB with high priority (lower = first).
        await self.pool.execute(
            """
            INSERT INTO answer_kb (profile, pattern, answer, priority)
            VALUES ($1, $2, $3, 10)
            ON CONFLICT (profile, pattern) DO UPDATE
               SET answer = EXCLUDED.answer, priority = 10, updated_at = now()
            """,
            row["profile"],
            row["question"].strip().lower(),
            answer,
        )
        return True

    # ------------------------------------------------------- profile refresh
    async def last_profile_refresh(self, account: str) -> dict[str, Any] | None:
        """The most recent SUCCESSFUL refresh for this account, or None."""
        row = await self.pool.fetchrow(
            """
            SELECT strategy, created_at, last_updated_text,
                   EXTRACT(EPOCH FROM (now() - created_at)) / 3600.0 AS hours_ago
              FROM profile_updates
             WHERE account = $1 AND ok = TRUE
             ORDER BY created_at DESC
             LIMIT 1
            """,
            account,
        )
        return dict(row) if row else None

    async def profile_refresh_due(self, account: str, min_hours_between: int) -> bool:
        """
        Idempotency gate for the daily touch.

        Enforced in SQL rather than in memory because the refresh has three
        independent triggers (the apply run, the standalone cron, and a manual
        CLI call) plus container restarts. Repeatedly editing a profile in one
        day is exactly what Naukri's abuse heuristics look for, so "did we
        already do this?" has to survive process death.
        """
        last = await self.last_profile_refresh(account)
        if last is None:
            return True
        return float(last["hours_ago"] or 0) >= float(min_hours_between)

    async def record_profile_refresh(
        self,
        account: str,
        run_id: int | None,
        *,
        ok: bool,
        strategy: str = "",
        detail: str = "",
        headline_before: str = "",
        headline_after: str = "",
        last_updated_text: str = "",
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO profile_updates
                (account, run_id, strategy, ok, detail,
                 headline_before, headline_after, last_updated_text)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            """,
            account,
            run_id,
            strategy,
            ok,
            (detail or "")[:1000],
            (headline_before or "")[:500],
            (headline_after or "")[:500],
            (last_updated_text or "")[:200],
        )

    async def profile_freshness(self) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(
            """
            SELECT account, last_success_at, last_attempt_at, successes, failures
              FROM profile_freshness
             ORDER BY account
            """
        )
        return [dict(row) for row in rows]

    # --------------------------------------------------------------- session
    async def save_session(self, key: str, state: dict[str, Any], ttl_hours: int = 240) -> None:
        await self.pool.execute(
            """
            INSERT INTO browser_sessions (key, state, valid_until, updated_at)
            VALUES ($1, $2::jsonb, now() + ($3 || ' hours')::interval, now())
            ON CONFLICT (key) DO UPDATE
               SET state = EXCLUDED.state,
                   valid_until = EXCLUDED.valid_until,
                   updated_at = now()
            """,
            key,
            json.dumps(state),
            str(ttl_hours),
        )

    async def load_session(self, key: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """
            SELECT state FROM browser_sessions
             WHERE key = $1 AND (valid_until IS NULL OR valid_until > now())
            """,
            key,
        )
        if not row:
            return None
        state = row["state"]
        return json.loads(state) if isinstance(state, str) else dict(state)

    async def clear_session(self, key: str) -> None:
        await self.pool.execute("DELETE FROM browser_sessions WHERE key = $1", key)

    # ----------------------------------------------------------------- stats
    async def summary(self) -> dict[str, Any]:
        row = await self.pool.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM applications WHERE status = 'applied') AS total_applied,
              (SELECT count(*) FROM applications WHERE status = 'failed') AS total_failed,
              (SELECT count(*) FROM applications WHERE status = 'skipped') AS total_skipped,
              (SELECT count(*) FROM jobs) AS total_jobs,
              (SELECT count(*) FROM question_review WHERE resolved = FALSE) AS pending_reviews,
              (SELECT max(finished_at) FROM runs WHERE status <> 'running') AS last_run_at
            """
        )
        data = dict(row or {})
        data["fetched_at"] = datetime.now(timezone.utc).isoformat()
        return data
