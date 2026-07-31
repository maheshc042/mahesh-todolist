"""
Command line interface — the only entrypoint (`python -m naukri_agent ...`).

Design decisions
----------------

- **One-shot commands exit with meaningful codes** so cron, Kubernetes and CI can
  alert on them: `0` success, `1` the run failed, `2` misconfiguration. A
  scheduler that cannot tell "nothing to apply to" from "wrong password" is
  useless.
- **Every command sets up logging and directories first**, then loads config, then
  touches the network. Configuration errors therefore surface before a browser is
  launched.
- **`login` is a separate, interactive command.** A first login (or one after an
  OTP challenge) needs a visible browser and a human, so it forces
  `headless=False` and then persists the storage state to Postgres — after which
  the headless container reuses that session and never sees the login form.
- **`run --all-accounts` is sequential, never parallel.** See scheduler.py: two
  simultaneous logins from one IP is what gets an account flagged.
- **Read-only inspection commands** (`accounts`, `status`, `reviews`) exist so the
  operator never needs a psql prompt to answer "did it work this morning?".
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .browser.artifacts import ArtifactStore
from .browser.manager import BrowserManager
from .config import ACCOUNT_KEYS, AgentConfig, ConfigError, Settings, get_settings, load_config
from .core.models import RunStatus
from .core.orchestrator import Orchestrator
from .db.migrations import run_migrations
from .db.pool import close_pool, get_pool
from .db.repository import Repository
from .logging_setup import get_logger, setup_logging
from .naukri.auth import NaukriAuth
from .naukri.profile import ProfileRefresher

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Naukri.com auto-apply agent (Playwright + PostgreSQL).",
)
console = Console()
log = get_logger(__name__)

EXIT_OK = 0
EXIT_RUN_FAILED = 1
EXIT_CONFIG = 2


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _bootstrap(config_path: Path | None = None) -> tuple[Settings, AgentConfig]:
    """Settings + logging + directories + validated YAML, in that order."""
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json, settings.log_dir)
    settings.ensure_dirs()
    return settings, load_config(config_path)


def _settings_only() -> Settings:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json, settings.log_dir)
    return settings


def _run(coro) -> None:
    """Run an async command, mapping failures onto process exit codes."""
    try:
        asyncio.run(coro)
    except ConfigError as exc:
        console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise typer.Exit(EXIT_CONFIG) from exc
    except KeyboardInterrupt:  # pragma: no cover
        console.print("[yellow]interrupted[/yellow]")
        raise typer.Exit(130) from None


def _resolve_accounts(account: str | None, all_accounts: bool, settings: Settings) -> list[str]:
    if all_accounts:
        keys = [item.key for item in settings.configured_accounts()]
        if not keys:
            raise ConfigError("No Naukri account is configured — fill NAUKRI_EMAIL/PASSWORD in .env")
        return keys
    return [(account or settings.default_account).strip().lower()]


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
@app.command()
def migrate(
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to config.yaml"),
) -> None:
    """Create or upgrade the database schema (idempotent, safe to re-run)."""

    async def _main() -> None:
        _settings_only()
        try:
            applied = await run_migrations()
        finally:
            await close_pool()
        if applied:
            console.print(f"[green]applied migrations:[/green] {', '.join(applied)}")
        else:
            console.print("[green]schema already up to date[/green]")

    _ = config_path  # accepted for symmetry with the other commands
    _run(_main())


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
@app.command()
def run(
    account: Optional[str] = typer.Option(
        None, "--account", "-a", help=f"Which login to use: {' | '.join(ACCOUNT_KEYS)}"
    ),
    all_accounts: bool = typer.Option(
        False, "--all-accounts", help="Run every configured account, one after another"
    ),
    profile: Optional[list[str]] = typer.Option(
        None, "--profile", "-p", help="Only these profile names (repeatable)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Do everything except submitting an application"
    ),
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to config.yaml"),
) -> None:
    """Apply to jobs once and exit."""

    async def _main() -> None:
        settings, config = _bootstrap(config_path)
        targets = _resolve_accounts(account, all_accounts, settings)
        failures = 0
        try:
            for key in targets:
                # Fail fast per account, but keep going: account 2 being
                # unconfigured must not cancel account 1's run.
                try:
                    settings.validate_for_run(key)
                except ConfigError as exc:
                    console.print(f"[yellow]skipping {key}:[/yellow] {exc}")
                    failures += 1
                    continue

                orchestrator = Orchestrator(
                    config,
                    settings,
                    mode="manual",
                    only_profiles=list(profile) if profile else None,
                    dry_run=dry_run,
                    account=key,
                )
                stats = await orchestrator.run()
                _print_stats(key, stats)
                if stats.errors and stats.applied == 0:
                    failures += 1
        finally:
            await close_pool()

        if failures and failures == len(targets):
            raise typer.Exit(EXIT_RUN_FAILED)

    _run(_main())


def _print_stats(account: str, stats) -> None:
    table = Table(title=f"run summary — {account}", show_header=True, header_style="bold")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for key in (
        "scraped",
        "considered",
        "filtered_out",
        "applied",
        "already_applied",
        "external",
        "needs_review",
        "failed",
    ):
        table.add_row(key.replace("_", " "), str(getattr(stats, key, 0)))
    console.print(table)
    for message in stats.errors[:5]:
        console.print(f"  [red]![/red] {message}")


# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------
@app.command()
def schedule(
    profile: Optional[list[str]] = typer.Option(None, "--profile", "-p", help="Only these profiles"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Never submit an application"),
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to config.yaml"),
) -> None:
    """Stay running and apply on the configured cron (one job per account)."""

    async def _main() -> None:
        settings, config = _bootstrap(config_path)
        if not config.schedule.enabled:
            raise ConfigError("schedule.enabled is false in config.yaml")
        # Import here so `migrate`/`status` do not pay for APScheduler.
        from .scheduler import run_scheduler

        # The schema must exist before the first tick, and the scheduler is the
        # container's default command — so migrating here makes a fresh deploy
        # work without a manual step.
        await run_migrations()
        try:
            await run_scheduler(
                config,
                settings,
                only_profiles=list(profile) if profile else None,
                dry_run=dry_run,
            )
        finally:
            await close_pool()

    _run(_main())


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------
@app.command()
def login(
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Which login to use"),
    headed: bool = typer.Option(True, "--headed/--headless", help="Show the browser window"),
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to config.yaml"),
) -> None:
    """
    Log in once interactively and store the session in Postgres.

    Run this on a machine with a display after a fresh deploy or an OTP
    challenge; every later headless run reuses the saved session.
    """

    async def _main() -> None:
        settings, config = _bootstrap(config_path)
        target = settings.validate_for_run(account)
        await run_migrations()
        repo = await Repository.create()
        browser_config = config.browser.model_copy(update={"headless": not headed})
        artifacts = ArtifactStore(settings.artifacts_dir, "login")
        try:
            async with BrowserManager(
                browser_config, repo, session_key=target.session_key
            ) as browser:
                auth = NaukriAuth(browser, target.email, target.password, artifacts)
                page = await auth.ensure_logged_in()
                await browser.persist_session()
                console.print(
                    f"[green]logged in as {target.masked_email}[/green] "
                    f"(session key {target.session_key})"
                )
                await page.close()
        finally:
            await close_pool()

    _run(_main())


# ---------------------------------------------------------------------------
# profile refresh
# ---------------------------------------------------------------------------
@app.command(name="refresh-profile")
def refresh_profile(
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Which login to use"),
    force: bool = typer.Option(False, "--force", help="Ignore the min_hours_between guard"),
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to config.yaml"),
) -> None:
    """
    Touch the Naukri profile so recruiter search ranks it as updated today.

    The apply run already does this; use the command to verify it works or to
    refresh on a day you are not applying.
    """

    async def _main() -> None:
        settings, config = _bootstrap(config_path)
        target = settings.validate_for_run(account)
        await run_migrations()
        repo = await Repository.create()
        refresh_config = config.profile_refresh
        try:
            if not force and not await repo.profile_refresh_due(
                target.key, refresh_config.min_hours_between
            ):
                last = await repo.last_profile_refresh(target.key)
                hours = round(float(last["hours_ago"] or 0), 1) if last else 0
                console.print(
                    f"[yellow]skipped:[/yellow] already refreshed {hours}h ago "
                    f"(min_hours_between={refresh_config.min_hours_between}). Use --force."
                )
                return

            artifacts = ArtifactStore(settings.artifacts_dir, "refresh")
            async with BrowserManager(
                config.browser, repo, session_key=target.session_key
            ) as browser:
                auth = NaukriAuth(browser, target.email, target.password, artifacts)
                page = await auth.ensure_logged_in()
                refresher = ProfileRefresher(
                    page,
                    target.key,
                    strategies=refresh_config.strategies,
                    headline_variants=refresh_config.headline_variants,
                    resume_dir=settings.resume_dir,
                    resume_file=config.resume_for(target.key),
                    verify=refresh_config.verify,
                )
                result = await refresher.refresh()
                await repo.record_profile_refresh(
                    target.key,
                    None,
                    ok=result.ok,
                    strategy=result.strategy,
                    detail=result.detail,
                    headline_before=result.before,
                    headline_after=result.after,
                    last_updated_text=result.last_updated,
                )
            if result.ok:
                console.print(f"[green]profile refreshed[/green] via {result.strategy}")
            else:
                console.print(f"[red]refresh failed:[/red] {result.detail}")
                raise typer.Exit(EXIT_RUN_FAILED)
        finally:
            await close_pool()

    _run(_main())


# ---------------------------------------------------------------------------
# inspection
# ---------------------------------------------------------------------------
@app.command()
def accounts(
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to config.yaml"),
) -> None:
    """Show which logins are wired up, their profiles and today's usage."""

    async def _main() -> None:
        settings, config = _bootstrap(config_path)
        table = Table(title="accounts", header_style="bold")
        for column in ("account", "email", "credentials", "profiles", "cron", "applied today"):
            table.add_column(column)

        counts: dict[str, int] = {}
        if settings.database_url:
            try:
                repo = await Repository.create()
                counts = {
                    str(row["account"]): int(row["applied"] or 0)
                    for row in await repo.applied_today_by_account()
                }
            except Exception as exc:  # DB down must not hide the config view
                console.print(f"[yellow]could not read usage:[/yellow] {str(exc)[:120]}")

        for key in ACCOUNT_KEYS:
            account_cfg = settings.account(key)
            profiles = [p.name for p in config.active_profiles(account=key)]
            wired = bool(account_cfg.email and account_cfg.password)
            table.add_row(
                key,
                account_cfg.masked_email if wired else "-",
                "[green]set[/green]" if wired else "[red]missing[/red]",
                ", ".join(profiles) or "-",
                config.schedule.cron_for(key),
                str(counts.get(key, 0)),
            )
        console.print(table)
        await close_pool()

    _run(_main())


@app.command()
def status(
    limit: int = typer.Option(20, "--limit", "-n", min=1, max=200),
) -> None:
    """Recent applications, today's per-account totals and profile freshness."""

    async def _main() -> None:
        _settings_only()
        repo = await Repository.create()
        try:
            today = await repo.applied_today_by_account()
            table = Table(title="today", header_style="bold")
            for column in ("account", "applied", "failed", "needs review"):
                table.add_column(column, justify="right")
            for row in today:
                table.add_row(
                    str(row["account"]),
                    str(row["applied"]),
                    str(row["failed"]),
                    str(row["needs_review"]),
                )
            console.print(table if today else "[yellow]no applications today[/yellow]")

            freshness = await repo.profile_freshness()
            if freshness:
                fresh_table = Table(title="profile freshness", header_style="bold")
                for column in ("account", "last success", "last attempt", "ok", "failed"):
                    fresh_table.add_column(column)
                for row in freshness:
                    fresh_table.add_row(
                        str(row["account"]),
                        str(row["last_success_at"] or "-"),
                        str(row["last_attempt_at"] or "-"),
                        str(row["successes"]),
                        str(row["failures"]),
                    )
                console.print(fresh_table)

            recent = await repo.recent_applications(limit)
            recent_table = Table(title=f"last {len(recent)} decisions", header_style="bold")
            for column in ("when", "account", "status", "reason", "title", "company"):
                recent_table.add_column(column, overflow="fold")
            for row in recent:
                recent_table.add_row(
                    row["created_at"].strftime("%d %b %H:%M"),
                    str(row["account"]),
                    str(row["status"]),
                    str(row["reason"] or "-"),
                    str(row["title"])[:48],
                    str(row["company"])[:28],
                )
            console.print(recent_table)
        finally:
            await close_pool()

    _run(_main())


@app.command()
def reviews(
    limit: int = typer.Option(30, "--limit", "-n", min=1, max=200),
) -> None:
    """Screening questions the agent could not answer, most frequent first."""

    async def _main() -> None:
        _settings_only()
        repo = await Repository.create()
        try:
            pending = await repo.pending_reviews(limit)
            if not pending:
                console.print("[green]no questions waiting for review[/green]")
                return
            table = Table(title="unanswered screening questions", header_style="bold")
            for column in ("id", "profile", "seen", "kind", "question", "options"):
                table.add_column(column, overflow="fold")
            for row in pending:
                options = row["options"]
                table.add_row(
                    str(row["id"]),
                    str(row["profile"])[:24],
                    str(row["times_seen"]),
                    str(row["question_kind"]),
                    str(row["question"])[:90],
                    str(options)[:60],
                )
            console.print(table)
            console.print(
                'Answer one with: [bold]python -m naukri_agent resolve <id> "<answer>"[/bold]'
            )
        finally:
            await close_pool()

    _run(_main())


@app.command()
def resolve(
    review_id: int = typer.Argument(..., help="id from the `reviews` table"),
    answer: str = typer.Argument(..., help="the answer to submit for this question"),
) -> None:
    """
    Answer a queued screening question.

    The answer is promoted into the knowledge base at high priority, and the job
    it blocked becomes eligible again on the next run.
    """

    async def _main() -> None:
        _settings_only()
        repo = await Repository.create()
        try:
            if await repo.resolve_review(review_id, answer):
                console.print(f"[green]resolved review {review_id}[/green]")
            else:
                console.print(f"[red]no pending review with id {review_id}[/red]")
                raise typer.Exit(EXIT_RUN_FAILED)
        finally:
            await close_pool()

    _run(_main())


@app.command()
def doctor(
    config_path: Optional[Path] = typer.Option(None, "--config", help="Path to config.yaml"),
) -> None:
    """Validate environment, config and database connectivity without touching Naukri."""

    async def _main() -> None:
        settings, config = _bootstrap(config_path)
        table = Table(title="preflight", header_style="bold")
        table.add_column("check")
        table.add_column("result", overflow="fold")

        table.add_row("config file", str(settings.config_path))
        table.add_row(
            "profiles",
            ", ".join(f"{p.name} [{p.account}]" for p in config.active_profiles()) or "none enabled",
        )
        wired = [item.key for item in settings.configured_accounts()]
        table.add_row("accounts configured", ", ".join(wired) or "[red]none[/red]")
        table.add_row("resume dir", f"{settings.resume_dir} (exists={settings.resume_dir.exists()})")
        table.add_row("artifacts dir", str(settings.artifacts_dir))
        table.add_row("dry run", str(settings.dry_run))
        table.add_row("headless", str(config.browser.headless))

        if settings.database_url:
            try:
                pool = await get_pool()
                version = await pool.fetchval("SELECT version()")
                table.add_row("database", f"[green]reachable[/green] — {str(version)[:40]}")
                applied = await run_migrations()
                table.add_row("schema", f"up to date (applied now: {applied or 'none'})")
            except Exception as exc:
                table.add_row("database", f"[red]{str(exc)[:160]}[/red]")
        else:
            table.add_row("database", "[red]DATABASE_URL not set[/red]")

        console.print(table)

        missing = [key for key in config.accounts_in_use() if key not in wired]
        if missing:
            console.print(
                f"[yellow]warning:[/yellow] profiles reference unconfigured accounts: {missing}"
            )
        await close_pool()

    _run(_main())


@app.command()
def version() -> None:
    """Print the agent version."""
    from . import __version__

    console.print(f"naukri-agent {__version__}")


def main() -> None:  # console_scripts / python -m entrypoint
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
