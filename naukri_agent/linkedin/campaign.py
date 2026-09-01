"""
LinkedIn Cold Email Campaign Runner.

Design Decisions:
- DB Deduplication: Checks `contacted_recruiters` in Postgres to prevent duplicate recruiter contact.
- Human Rate Limits: Daily cap of 15 emails and 8-second delay between emails to protect Gmail Sender Score.
- Dual-Track Routing: Dynamically selects AI vs Full Stack resume attachments based on role classification.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from ..config import AgentConfig, Settings, get_settings, PROJECT_ROOT
from ..core.mailer import ColdEmailer
from ..core.run_policy import RunPolicy
from ..db.repository import Repository
from ..logging_setup import get_logger
from ..notify.notifier import build_notifier
from .analyzer import classify_role, is_experience_match
from .scraper import CookieExpiredError, LinkedInHunter

log = get_logger(__name__)

SEARCH_KEYWORDS = [
    "AI Engineer",
    "GenAI Engineer",
    "LLM Engineer",
    "AI Developer",
    "Python Developer",
    "Full Stack Developer",
    "MERN Developer",
    "Backend Developer",
    "Software Engineer",
    "Cloud Engineer"
]

import urllib.parse
SEARCH_URLS = [
    f"https://www.linkedin.com/search/results/content/?keywords={urllib.parse.quote(kw)}&origin=GLOBAL_SEARCH_HEADER&sortBy=%5B%22relevance%22%5D&datePosted=%5B%22past-24h%22%5D"
    for kw in SEARCH_KEYWORDS
]


def _get_resume_path(role: str, resume_dir: Path) -> Path:
    """Find appropriate resume path for the classified role."""
    if "AI" in role or "Python" in role or "ML" in role:
        candidates = list(resume_dir.glob("*2026.pdf")) + list(resume_dir.glob("*AI*.pdf")) + list(resume_dir.glob("*Python*.pdf"))
        if candidates:
            return candidates[0]
        return resume_dir / "CV_Mahesh_Chitakoti_2026.pdf"
    else:
        candidates = list(resume_dir.glob("*2026_1_*.pdf")) + list(resume_dir.glob("*FullStack*.pdf")) + list(resume_dir.glob("*Full_Stack*.pdf"))
        if candidates:
            return candidates[0]
        return resume_dir / "CV_Mahesh_Chitakoti_2026_1_.pdf"


async def run_campaign(
    daily_email_limit: int = 15,
    headed: bool = False,
    dry_run: bool = False,
) -> int:
    settings = get_settings()
    config = AgentConfig.load()
    policy = RunPolicy(
        dry_run=dry_run,
        side_effects_enabled=settings.side_effects_enabled,
    )
    if not dry_run and not settings.matched_outreach_enabled:
        log.warning("linkedin.outreach_disabled")
        return 0

    li_cookie = (getattr(settings, "linkedin_li_at", "") or os.getenv("LINKEDIN_LI_AT", "")).strip()
    gmail_user = os.getenv("GMAIL_USER", "").strip()
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD", os.getenv("GMAIL_APP_PASS", "")).strip()

    notifier = build_notifier(
        telegram_enabled=config.notifications.telegram_enabled,
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )

    if not all([gmail_user, gmail_pass]) and not dry_run:
        log.error(
            "linkedin.missing_secrets",
            detail="Ensure GMAIL_USER and GMAIL_APP_PASSWORD are set in .env.",
        )
        print("\n[!] GMAIL_USER and GMAIL_APP_PASSWORD must be configured in .env for cold email campaign.\n")
        return 0

    if dry_run and not all([gmail_user, gmail_pass]):
        log.info("linkedin.dry_run_no_secrets", detail="Running in simulation dry-run mode without live Gmail credentials.")
        gmail_user = gmail_user or "test.applicant@example.com"
        gmail_pass = gmail_pass or "dummy-app-pass"

    headless = not headed if headed else (config.browser.headless if settings.headless is None else settings.headless)
    gemini_key = getattr(settings, "gemini_api_key", "") or os.getenv("GEMINI_API_KEY", "")
    hunter = LinkedInHunter(li_at_cookie=li_cookie, headless=headless)
    mailer = ColdEmailer(sender_email=gmail_user, app_password=gmail_pass, gemini_api_key=gemini_key)
    repo = await Repository.create()

    resume_dir = settings.resume_dir
    emails_sent_today = 0
    sent_records: list[dict[str, str]] = []

    # The daily cap is per calendar day, not per run: subtract emails already
    # sent today (recorded in contacted_recruiters) from the configured limit.
    already_sent_today = await repo.count_contacted_today()
    effective_limit = max(0, daily_email_limit - already_sent_today)
    if effective_limit == 0 and not dry_run:
        log.info(
            "linkedin.daily_limit_already_reached",
            sent_today=already_sent_today,
            limit=daily_email_limit,
        )
        print(
            f"\n[!] Daily email limit already reached "
            f"({already_sent_today}/{daily_email_limit} sent today). Skipping campaign.\n"
        )
        return 0
    elif effective_limit == 0 and dry_run:
        effective_limit = daily_email_limit
        log.info(
            "linkedin.dry_run_preview_limit",
            limit=effective_limit,
            detail="Daily limit reached in DB, but previewing matches for dry run.",
        )

    if already_sent_today and not dry_run:
        log.info(
            "linkedin.daily_limit_adjusted",
            sent_today=already_sent_today,
            remaining_today=effective_limit,
        )

    if dry_run:
        print(f"\n[🔍 DRY RUN MODE] Hunting recruiter posts (limit: {effective_limit} emails preview). No emails will be sent.\n")

    try:
        for url in SEARCH_URLS:
            if emails_sent_today >= effective_limit:
                log.info("linkedin.daily_limit_reached", limit=daily_email_limit)
                break

            log.info("linkedin.starting_search", url=url[:60])
            try:
                posts = await hunter.hunt_for_jobs(url)
            except CookieExpiredError as exc:
                log.error("linkedin.cookie_expired", error=str(exc))
                await notifier.send(
                    "🚨 LinkedIn Cookie Expired!",
                    "Please update your LINKEDIN_LI_AT cookie or run login-linkedin in headed mode.",
                    is_error=True,
                )
                return emails_sent_today

            for post_data in posts:
                if emails_sent_today >= effective_limit:
                    break

                text = post_data["text"]
                emails = post_data["emails"]
                post_url = post_data.get("post_url", "")

                role = post_data["role"]

                resume_path = _get_resume_path(role, resume_dir)

                for target_email in emails:
                    if emails_sent_today >= effective_limit:
                        break

                    clean_email = target_email.strip().lower()
                    if await repo.has_emailed(clean_email, within_days=60):
                        log.info("linkedin.already_emailed", email=clean_email)
                        continue

                    if dry_run:
                        body_preview = mailer._generate_body(role_name=role, job_description=text)
                        print("=" * 70)
                        print(f"📧 [DRY RUN MATCH #{emails_sent_today + 1}]")
                        print(f"  To:         {clean_email}")
                        print(f"  Role:       {role}")
                        print(f"  Resume:     {resume_path.name}")
                        print(f"  Post URL:   {post_url or 'N/A'}")
                        print("  Pitch Snippet:")
                        for line in body_preview.strip().splitlines()[:5]:
                            print(f"    {line}")
                        print("=" * 70)
                        emails_sent_today += 1
                        sent_records.append(
                            {
                                "email": clean_email,
                                "role": role,
                                "post_url": post_url,
                            }
                        )
                        log.info("linkedin.dry_run_match", to=clean_email, role=role, total=emails_sent_today)
                        await asyncio.sleep(0.1)
                    else:
                        policy.require_mutation("linkedin.outreach.send_email")
                        success = await mailer.send_application_async(
                            target_email=clean_email,
                            role_name=role,
                            resume_path=resume_path,
                            job_description=text,
                        )
                        if success:
                            await repo.record_contacted_recruiter(clean_email, role, text, post_url)
                            emails_sent_today += 1
                            sent_records.append(
                                {
                                    "email": clean_email,
                                    "role": role,
                                    "post_url": post_url,
                                }
                            )
                            log.info("linkedin.email_sent", to=clean_email, role=role, sent_today=emails_sent_today)
                            await asyncio.sleep(8.0)

            log.info("linkedin.pause_between_searches", seconds=10)
            if not dry_run:
                await asyncio.sleep(10.0)

    finally:
        if sent_records:
            prefix = "[DRY RUN PREVIEW] " if dry_run else ""
            lines = [
                f"{prefix}Found {len(sent_records)} qualified recruiter email leads today "
                f"({len(sent_records)}/{daily_email_limit} cap):"
            ]
            for rec in sent_records:
                line = f"  - {rec['email']} ({rec['role']})"
                if rec.get("post_url"):
                    line += f"\n    Post: {rec['post_url']}"
                lines.append(line)
            summary_text = "\n".join(lines)
            if not dry_run:
                await notifier.send(
                    f"🔗 LinkedIn Cold Email Campaign Finished! ({len(sent_records)} Sent)",
                    summary_text,
                    is_error=False,
                )
            else:
                print(f"\n{summary_text}\n")

    log.info("linkedin.campaign_finished", total_emails=emails_sent_today, dry_run=dry_run)
    return emails_sent_today


if __name__ == "__main__":
    asyncio.run(run_campaign())

