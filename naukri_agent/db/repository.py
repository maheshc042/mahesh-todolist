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
from difflib import SequenceMatcher
from typing import Any

import asyncpg

from ..core.models import (
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
        Jobs already decided inside the dedupe window.
        
        PRODUCT UPGRADE: Global Shared Memory. 
        This query no longer filters by `account` or `profile`. If Account A 
        applies to or skips a job, Account B instantly learns about it and 
        will never apply to the same job, preventing duplicate recruiter spam.
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
                           WHERE q.job_id = a.job_id
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
                   screenshot_path = COALESCE(EXCLUDED.screenshot_path, applications.screenshot_path)
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
        """
        Queue an unknown question for human review.

        Before inserting a new row, fuzzy-match against already-resolved
        questions for this profile. If similarity > 0.85, auto-resolve using
        the existing answer instead of creating a new pending review item —
        this is the self-learning loop.
        """
        q_norm = question.strip()[:1000]

        # --- fuzzy dedup: look for resolved questions with similar text -------
        resolved_rows = await self.pool.fetch(
            """
            SELECT id, question, answer
              FROM question_review
             WHERE resolved = TRUE
               AND answer IS NOT NULL
               AND profile = $1
            """,
            profile,
        )
        best_ratio = 0.0
        best_row: Any = None
        q_lower = q_norm.lower()
        for row in resolved_rows:
            ratio = SequenceMatcher(
                None, q_lower, (row["question"] or "").lower(), autojunk=False
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_row = row

        AUTO_RESOLVE_THRESHOLD = 0.85
        if best_row is not None and best_ratio >= AUTO_RESOLVE_THRESHOLD:
            # Auto-resolve: this question is close enough to one already answered.
            await self.pool.execute(
                """
                INSERT INTO question_review
                    (job_id, profile, question, question_kind, options,
                     screenshot_path, resolved, answer, auto_resolved,
                     resolved_by, times_seen)
                VALUES ($1,$2,$3,$4,$5::jsonb,$6,TRUE,$7,TRUE,'auto-fuzzy',1)
                ON CONFLICT (profile, question) DO UPDATE
                   SET times_seen = question_review.times_seen + 1,
                       resolved = TRUE,
                       answer = EXCLUDED.answer,
                       auto_resolved = TRUE,
                       resolved_by = 'auto-fuzzy',
                       updated_at = now()
                """,
                job_id, profile, q_norm, kind,
                json.dumps(options), screenshot_path, best_row["answer"],
            )
            # Also promote to KB so the answer engine uses it immediately.
            await self.pool.execute(
                """
                INSERT INTO answer_kb (profile, pattern, answer, priority, source)
                VALUES ($1, $2, $3, 20, 'auto-fuzzy')
                ON CONFLICT (profile, pattern) DO UPDATE
                   SET answer = EXCLUDED.answer,
                       priority = LEAST(answer_kb.priority, 20),
                       source = 'auto-fuzzy',
                       updated_at = now()
                """,
                profile, q_norm.lower(), best_row["answer"],
            )
            log.info(
                "answer_learn.auto_resolved",
                question=q_norm[:80],
                matched=best_row["question"][:80],
                ratio=round(best_ratio, 3),
                answer=best_row["answer"][:40],
            )
            return  # Do NOT create a pending review row

        # --- Normal path: insert as unresolved pending review ----------------
        row = await self.pool.fetchrow(
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
            RETURNING id
            """,
            job_id,
            profile,
            q_norm,
            kind,
            json.dumps(options),
            screenshot_path,
        )
        review_id = row["id"] if row else None

        # Send Interactive Telegram Alert with [#review_id]
        if review_id:
            try:
                from ..config import AgentConfig, get_settings
                from ..notify.notifier import build_notifier
                settings = get_settings()
                config = AgentConfig.load()
                notifier = build_notifier(
                    telegram_enabled=config.notifications.telegram_enabled,
                    bot_token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                )
                opts_str = " | ".join(str(o) for o in options) if options else "Text Input"
                alert_body = (
                    f"❓ <b>Unanswered Question [#`{review_id}`]</b>\n"
                    f"<b>Profile:</b> {profile}\n"
                    f"<b>Question:</b> \"{q_norm}\"\n"
                    f"<b>Type:</b> {kind}\n"
                    f"<b>Options:</b> {opts_str}\n\n"
                    f"👉 <i>Reply directly to this message with your answer to train your AI!</i>"
                )
                await notifier.send(f"❓ Question Review Required [#{review_id}]", alert_body, is_error=False)
            except Exception as exc:
                log.warning("telegram_review_alert_failed", error=str(exc)[:150])

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
                    "id": review_id,
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
               SET resolved = TRUE, answer = $2, resolved_by = 'human', updated_at = now()
             WHERE id = $1
            RETURNING profile, question
            """,
            review_id,
            answer,
        )
        if not row:
            return False
        profile = row["profile"]
        question = row["question"]

        # Promote the human answer into the KB with high priority (lower = first).
        await self.pool.execute(
            """
            INSERT INTO answer_kb (profile, pattern, answer, priority, source)
            VALUES ($1, $2, $3, 10, 'human')
            ON CONFLICT (profile, pattern) DO UPDATE
               SET answer = EXCLUDED.answer,
                   priority = 10,
                   source = 'human',
                   updated_at = now()
            """,
            profile,
            question.strip().lower(),
            answer,
        )

        # Auto-cascade: find other UNRESOLVED questions for the same profile
        # that are similar enough to auto-answer with the same value.
        unresolved = await self.pool.fetch(
            """
            SELECT id, question
              FROM question_review
             WHERE resolved = FALSE
               AND profile = $1
               AND id <> $2
            """,
            profile, review_id,
        )
        AUTO_CASCADE_THRESHOLD = 0.82
        cascaded = 0
        q_lower = question.lower()
        for pending in unresolved:
            ratio = SequenceMatcher(
                None, q_lower, (pending["question"] or "").lower(), autojunk=False
            ).ratio()
            if ratio >= AUTO_CASCADE_THRESHOLD:
                await self.pool.execute(
                    """
                    UPDATE question_review
                       SET resolved = TRUE,
                           answer = $1,
                           auto_resolved = TRUE,
                           resolved_by = 'auto-cascade',
                           updated_at = now()
                     WHERE id = $2
                    """,
                    answer, pending["id"],
                )
                # Also add to KB.
                await self.pool.execute(
                    """
                    INSERT INTO answer_kb (profile, pattern, answer, priority, source)
                    VALUES ($1, $2, $3, 15, 'auto-cascade')
                    ON CONFLICT (profile, pattern) DO UPDATE
                       SET answer = EXCLUDED.answer,
                           priority = LEAST(answer_kb.priority, 15),
                           source = 'auto-cascade',
                           updated_at = now()
                    """,
                    profile,
                    pending["question"].strip().lower(),
                    answer,
                )
                cascaded += 1

        if cascaded:
            log.info(
                "answer_learn.cascade_resolved",
                from_question=question[:80],
                cascaded=cascaded,
                answer=answer[:40],
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

    async def prune_stale_data(self, keep_days: int = 30) -> dict[str, int]:
        """Prune old run events and expired sessions to keep database lean."""
        deleted_events = await self.pool.execute(
            "DELETE FROM run_events WHERE created_at < now() - ($1 || ' days')::interval",
            str(keep_days),
        )
        deleted_sessions = await self.pool.execute(
            "DELETE FROM browser_sessions WHERE valid_until < now()"
        )
        log.info("db.pruned", events=deleted_events, sessions=deleted_sessions)
        return {"events": deleted_events, "sessions": deleted_sessions}

    # ----------------------------------------------------------- match scores
    async def cached_match_score(self, job_id: str) -> dict[str, Any] | None:
        """Return a cached match score if fetched within the last 24 hours."""
        row = await self.pool.fetchrow(
            """
            SELECT keyskills_score, experience_match, overall_score
              FROM match_scores
             WHERE job_id = $1
               AND fetched_at > now() - interval '24 hours'
            """,
            job_id,
        )
        return dict(row) if row else None

    async def cache_match_score(
        self,
        job_id: str,
        keyskills_score: int,
        experience_match: bool,
        overall_score: int,
    ) -> None:
        """Persist a match score result to the cache table."""
        try:
            await self.pool.execute(
                """
                INSERT INTO match_scores
                    (job_id, keyskills_score, experience_match, overall_score, fetched_at)
                VALUES ($1, $2, $3, $4, now())
                ON CONFLICT (job_id) DO UPDATE
                   SET keyskills_score = EXCLUDED.keyskills_score,
                       experience_match = EXCLUDED.experience_match,
                       overall_score = EXCLUDED.overall_score,
                       fetched_at = now()
                """,
                job_id, keyskills_score, experience_match, overall_score,
            )
        except Exception as exc:  # pragma: no cover - cache must not raise
            log.warning("db.match_score_cache_failed", error=str(exc)[:120])

    # ------------------------------------------------------------ analytics
    async def daily_stats(self, days: int = 7) -> list[dict[str, Any]]:
        """Per-day, per-account breakdown for the stats CLI."""
        rows = await self.pool.fetch(
            """
            SELECT (created_at AT TIME ZONE 'Asia/Kolkata')::date AS day,
                   account,
                   count(*) FILTER (WHERE status = 'applied')      AS applied,
                   count(*) FILTER (WHERE status = 'failed')       AS failed,
                   count(*) FILTER (WHERE status = 'skipped')      AS skipped,
                   count(*) FILTER (WHERE status = 'needs_review') AS needs_review
              FROM applications
             WHERE created_at > now() - ($1 || ' days')::interval
             GROUP BY 1, 2
             ORDER BY 1 DESC, 2
            """,
            str(days),
        )
        return [dict(row) for row in rows]

    async def success_rate(self, days: int = 7) -> dict[str, Any]:
        """Overall success metrics for the requested window."""
        row = await self.pool.fetchrow(
            """
            SELECT
                count(*) FILTER (WHERE status = 'applied')      AS applied,
                count(*) FILTER (WHERE status = 'failed')       AS failed,
                count(*) FILTER (WHERE status = 'needs_review') AS needs_review,
                count(*) FILTER (WHERE status = 'skipped')      AS skipped,
                count(*) FILTER (
                    WHERE status = 'skipped'
                    AND reason = 'low_match_score'
                )                                               AS skipped_by_score
              FROM applications
             WHERE created_at > now() - ($1 || ' days')::interval
            """,
            str(days),
        )
        data = dict(row or {})
        total = (data.get("applied") or 0) + (data.get("failed") or 0) + (data.get("needs_review") or 0)
        data["success_rate_pct"] = round(100 * (data.get("applied") or 0) / total, 1) if total else 0.0
        data["days"] = days
        return data

    async def top_unanswered(self, limit: int = 10) -> list[dict[str, Any]]:
        """Most-seen unresolved screening questions — your action list."""
        rows = await self.pool.fetch(
            """
            SELECT id, profile, question, question_kind, options, times_seen
              FROM question_review
             WHERE resolved = FALSE
             ORDER BY times_seen DESC, created_at DESC
             LIMIT $1
            """,
            limit,
        )
        return [dict(row) for row in rows]

    async def answer_coverage(self) -> dict[str, Any]:
        """What fraction of screening questions the KB covers, and dead-weight entries."""
        total_questions = await self.pool.fetchval(
            "SELECT count(*) FROM question_review"
        ) or 0
        resolved = await self.pool.fetchval(
            "SELECT count(*) FROM question_review WHERE resolved = TRUE"
        ) or 0
        pending = await self.pool.fetchval(
            "SELECT count(*) FROM question_review WHERE resolved = FALSE"
        ) or 0
        kb_entries = await self.pool.fetchval(
            "SELECT count(*) FROM answer_kb"
        ) or 0
        zero_hit_entries = await self.pool.fetchval(
            "SELECT count(*) FROM answer_kb WHERE hits = 0"
        ) or 0
        auto_resolved = await self.pool.fetchval(
            "SELECT count(*) FROM question_review WHERE auto_resolved = TRUE"
        ) or 0
        return {
            "total_questions": total_questions,
            "resolved": resolved,
            "pending": pending,
            "auto_resolved": auto_resolved,
            "coverage_pct": round(100 * resolved / total_questions, 1) if total_questions else 0.0,
            "kb_entries": kb_entries,
            "zero_hit_kb_entries": zero_hit_entries,
        }

    async def answer_hit_report(
        self, profile: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """
        KB entries sorted by hit count (desc). Entries with zero hits are dead
        weight — the operator can safely remove them from config.yaml.
        """
        rows = await self.pool.fetch(
            """
            SELECT pattern, answer, source, hits, priority, updated_at
              FROM answer_kb
             WHERE ($1::text IS NULL OR profile = $1 OR profile IS NULL)
             ORDER BY hits DESC, priority, length(pattern) DESC
             LIMIT $2
            """,
            profile, limit,
        )
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ cold emails
    async def has_emailed(self, email: str, within_days: int = 60) -> bool:
        """Check if we have pitched this recruiter within the last N days (default: 60 days)."""
        clean_email = (email or "").strip().lower()
        if not clean_email:
            return False
        val = await self.pool.fetchval(
            """
            SELECT 1 FROM contacted_recruiters
             WHERE email = $1
               AND contacted_at >= now() - ($2 || ' days')::interval
            """,
            clean_email,
            within_days,
        )
        return val is not None

    async def record_contacted_recruiter(
        self, email: str, role_pitched: str, snippet: str = "", post_url: str = ""
    ) -> None:
        """Log a successful cold email."""
        clean_email = (email or "").strip().lower()
        if not clean_email:
            return
        await self.pool.execute(
            """
            INSERT INTO contacted_recruiters (email, role_pitched, post_snippet, post_url)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (email) DO UPDATE
               SET role_pitched = EXCLUDED.role_pitched,
                   post_snippet = COALESCE(EXCLUDED.post_snippet, contacted_recruiters.post_snippet),
                   post_url = COALESCE(EXCLUDED.post_url, contacted_recruiters.post_url)
            """,
            clean_email,
            role_pitched,
            snippet[:500],
            post_url[:1000],
        )
