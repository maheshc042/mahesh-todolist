"""
LinkedIn Cold Email Campaign Runner.

Design Decisions:
- QUALITY OVER VOLUME (house rule): every send must match the candidate's
  profile (skill overlap + role + geography + poster). A send to a
  non-matching post is worse than no send — it burns cap AND domain
  reputation. When in doubt, the gates reject.
- DB Deduplication: Checks `contacted_recruiters` in Postgres to prevent duplicate recruiter contact.
- Human Rate Limits: Daily cap of 20 emails and 30-90s randomized pacing
  between sends to protect Gmail Sender Score. 20/day via Gmail SMTP stays
  far under limits; spam risk at this volume comes from bounces/complaints,
  not count — so role-inbox skips, 60-day dedupe, and reply monitoring matter
  more than shaving the number.
- Dual-Track Routing: Dynamically selects AI vs Full Stack resume attachments based on role classification.
"""
from __future__ import annotations

import asyncio
import os
import random
import socket
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from ..config import AgentConfig, get_settings
from ..core.mailer import ColdEmailer
from ..core.run_policy import RunPolicy
from ..db.repository import Repository
from ..logging_setup import get_logger
from ..notify.notifier import build_notifier
from .scraper import CookieExpiredError, LinkedInHunter, RateLimitedError

log = get_logger(__name__)

# 16 searches: broad ground across both personas (billions of posts exist;
# coverage comes from keyword breadth, never from lowering the gates).
SEARCH_KEYWORDS = [
    "AI Engineer",
    "GenAI Engineer",
    "Backend Developer",
    "AI Developer",
    "RAG Engineer",
    "Forward Deployed Engineer",
    "Python Developer",
    "Full Stack Developer",
    "Node.js Developer",
]

import urllib.parse

# Focus search strictly on active hiring posts containing email/resume contact points within the past 24h.
# sortBy=relevance WITH the past-24h bound: the time filter already caps
# staleness, so relevance ranks the best matches before scroll depth runs
# out (date-first buries good posts below the read window).
# Content-type facet deliberately LEFT DEFAULT (all posts): setting it to
# "Job posts" would re-scrape the listings the apply flow already covers
# (same duplication the removed jobs-tab hunter caused). Personal recruiter
# posts with emails are this campaign's unique coverage; the email-gate in
# the scraper already filters out videos/images/docs without emails.
SEARCH_URLS = [
    f'https://www.linkedin.com/search/results/content/?keywords={urllib.parse.quote(f"{kw} (hiring OR email OR resume)")}&origin=GLOBAL_SEARCH_HEADER&sortBy=%5B%22relevance%22%5D&datePosted=%5B%22past-24h%22%5D'
    for kw in SEARCH_KEYWORDS
]

# NOTE (2026-10-01): a jobs-tab (/jobs/search/) hunter lived here and was
# REMOVED. Reason: the LinkedIn APPLY flow already processes those exact
# listings (Easy Apply submits; company-site ones go to the Sidekick
# outbox as resolved ATS links) — re-scraping them for emails duplicated
# coverage while doubling automation exposure on the account. The campaign
# hunts feed posts only: recruiter posts with emails, a source nothing else
# in this repo touches.


async def _internet_available(timeout_s: float = 3.0) -> bool:
    """Best-effort connectivity probe (DNS + TCP). Keeps an outage from
    masquerading as an auth failure (run 486)."""
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: socket.create_connection(("8.8.8.8", 53), timeout=timeout_s).close()
            ),
            timeout=timeout_s + 2.0,
        )
        return True
    except Exception:
        return False


def _configured_resume_fallback(want_ai: bool, resume_dir: Path) -> Path:
    """Resume file from config.yaml profiles. Callers verify existence before attaching."""
    try:
        config = AgentConfig.load()
    except Exception:
        return resume_dir
    profiles = [p for p in (config.profiles or []) if p.enabled and getattr(p, "resume_file", None)]
    for p in profiles:
        is_ai = any(k in (p.name or "").lower() for k in ["ai", "python", "ml", "llm"])
        if is_ai == want_ai:
            return resume_dir / Path(p.resume_file).name
    if profiles:
        return resume_dir / Path(profiles[0].resume_file).name
    return resume_dir


def _get_resume_path(role: str, resume_dir: Path) -> Path:
    """Find appropriate resume path for the classified role (glob first, config fallback)."""
    if "AI" in role or "Python" in role or "ML" in role:
        candidates = list(resume_dir.glob("*2026.pdf")) + list(resume_dir.glob("*AI*.pdf")) + list(resume_dir.glob("*Python*.pdf"))
        if candidates:
            return candidates[0]
        return _configured_resume_fallback(want_ai=True, resume_dir=resume_dir)
    else:
        candidates = list(resume_dir.glob("*2026_1_*.pdf")) + list(resume_dir.glob("*FullStack*.pdf")) + list(resume_dir.glob("*Full_Stack*.pdf"))
        if candidates:
            return candidates[0]
        return _configured_resume_fallback(want_ai=False, resume_dir=resume_dir)


async def run_campaign(
    daily_email_limit: int = 20,
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
    if not dry_run and not mailer.verify_credentials():
        # verify_credentials is False on BOTH bad password and dead network
        # (run 486: outage reported as "auth failed"). Distinguish: offline
        # is routine and quiet; only a true auth rejection pages the user.
        if not await _internet_available():
            log.warning("linkedin.gmail_unreachable_offline",
                        detail="No internet — skipping campaign quietly, will retry next run.")
            return 0
        # Bad app password: every send would fail — exit before hunting, LOUDLY.
        # (Sept 18-19 went silent here: 0 emails, 0 hunts, 0 alerts.)
        log.error(
            "linkedin.gmail_auth_failed",
            detail="Gmail App Password rejected — regenerate at Google Account > Security > App passwords and update GMAIL_APP_PASSWORD in .env.",
        )
        await notifier.send(
            "🚨 Cold outreach DOWN: Gmail auth failed",
            "Gmail App Password was rejected. No cold emails can send until GMAIL_APP_PASSWORD is refreshed in .env.",
            is_error=True,
        )
        return 0
    # In-run memory of attempted addresses: when the DB is unreachable,
    # has_emailed/record_contacted go blind and the same lead would be
    # re-emailed once per search (run 291 sent 4 attempts to one address).
    attempted_this_run: set[str] = set()
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

    from .analyzer import company_domain_match, lead_is_addressable, overlap_report, prioritize_leads, resolve_recipient_first_name

    # Match-to-profile skill set: union of every enabled profile's mapped
    # skills with real tenure. Posts naming none of these are rejected at
    # hunt time — quality (1% useful beats 100 sends) over volume.
    required_skills: set[str] = set()
    for _prof in config.profiles or []:
        if not getattr(_prof, "enabled", True):
            continue
        try:
            _exp = config.experience_for(_prof)
        except Exception:
            continue
        for _skill, _years in ((_exp.skills or {}).items()):
            try:
                if float(_years) > 0 and str(_skill).strip():
                    required_skills.add(str(_skill).strip().lower())
            except (TypeError, ValueError):
                continue
    log.info("linkedin.required_skills", count=len(required_skills))

    collected: list[dict] = []
    overlap: dict[str, list[str]] = {}
    # Hunt time ceiling (minutes): deeper per-keyword reads must not turn
    # the campaign unbounded (run-291 class overrun). The current keyword
    # always finishes; new ones stop starting past the ceiling.
    HUNT_BUDGET_MINUTES = 12.0
    try:
        sources: list[tuple[str, str, str]] = [
            ("posts", kw, u) for kw, u in zip(SEARCH_KEYWORDS, SEARCH_URLS)
        ]
        hunt_start = time.monotonic()
        for source, keyword, url in sources:
            if collected and (time.monotonic() - hunt_start) / 60.0 >= HUNT_BUDGET_MINUTES:
                log.info("linkedin.hunt_budget_reached", collected=len(collected))
                break
            log.info("linkedin.starting_search", source=source, url=url[:60])
            try:
                posts = await hunter.hunt_for_jobs(
                    url, required_skills=required_skills,
                    search_label=keyword, seen=overlap,
                )
            except CookieExpiredError as exc:
                # Dead LinkedIn session stops HUNTING — but already-collected
                # leads still send below (Gmail needs no LinkedIn session).
                # Log-only by operator policy (no Telegram spam per run).
                log.error("linkedin.cookie_expired", error=str(exc))
                break
            except RateLimitedError as exc:
                # Throttled session/IP: further searches only deepen the flag.
                # Stop hunting now; send what was already collected.
                log.error("linkedin.rate_limited_stopping_hunt", error=str(exc)[:150])
                try:
                    await notifier.send(
                        "⚠️ LinkedIn campaign rate-limited",
                        "LinkedIn is throttling this session/IP (1200 class). "
                        f"Hunting stopped early; sending the {len(collected)} "
                        "already-collected leads, then standing down. "
                        "Let the account rest before the next run.",
                        is_error=True,
                    )
                except Exception:
                    pass
                break
            except Exception as exc:
                # One dead search (driver crash, ban page, OOM) must never
                # kill the remaining sources — log and move on.
                log.error("linkedin.search_failed_continuing", source=source,
                          error=str(exc)[:200])
                await asyncio.sleep(10.0)
                continue

            for post_data in posts:
                text = post_data["text"]
                emails = post_data["emails"]
                post_url = post_data.get("post_url", "")

                role = post_data["role"]
                company = (post_data.get("company") or "").strip()
                first_name = (post_data.get("first_name") or "").strip()

                for target_email in emails:
                    clean_email = target_email.strip().lower()
                    # Role inboxes (info@, noreply@, support@…) are never read
                    # by a human — skip before dedupe so they don't burn slots.
                    # NOTE: hr@ is deliberately KEPT: recruiters hire from it.
                    # Narrowed exception (2026-10-09, 30-day trial): careers@ /
                    # jobs@ / job@ / apply@ / application@ are KEPT when the
                    # domain matches the hiring company (Indian startups hire
                    # straight out of these boxes). Measure: kept events carry
                    # the address; replies join on it. Kill threshold: zero
                    # replies from kept role inboxes at day 30 -> revert.
                    # noreply/support/info-class stays dropped unconditionally.
                    local_part = clean_email.split("@")[0] if "@" in clean_email else ""
                    if local_part in (
                        "info", "support", "sales", "contact", "help", "admin",
                        "query", "feedback", "careers", "job", "jobs", "apply",
                        "application", "noreply", "no-reply", "donotreply",
                        "do-not-reply", "enquiry", "enquiries",
                    ):
                        if local_part in ("careers", "job", "jobs", "apply", "application") \
                                and company_domain_match(clean_email, company):
                            log.info("linkedin.role_inbox_kept_company_match",
                                     email=clean_email, company=(company or "")[:60])
                        else:
                            log.info("linkedin.role_inbox_skipped", email=clean_email)
                            continue
                    if clean_email in attempted_this_run:
                        continue
                    attempted_this_run.add(clean_email)
                    fn = first_name
                    if not fn and clean_email:
                        from ..core.gemini_writer import extract_name_from_email
                        fn = extract_name_from_email(clean_email)
                    collected.append({
                        "email": clean_email,
                        "role": role,
                        "text": text,
                        "post_url": post_url,
                        "company": company,
                        "first_name": fn,
                        "post_age_hours": post_data.get("post_age_hours"),
                    })

            log.info("linkedin.pause_between_searches", seconds=10)
            if not dry_run:
                await asyncio.sleep(10.0)

        # Overlap telemetry: which keywords earn their LinkedIn attention
        # with unique posts vs re-reading other keywords' hits. Drives
        # pruning with data (see plan notes): >50% duplicated keywords go.
        log.info("linkedin.overlap_report", **overlap_report(overlap))

        # Phase 2 — send, company-domain first: under a daily cap the
        # scarcest resource is sends, and employer inboxes convert better
        # than free mailboxes. Stable within tiers (discovery order kept).
        # Anonymous free-mailbox leads (no name, no company) never send:
        # a bare "Hi," to a stranger reads as mass-blast.
        for lead in prioritize_leads(collected):
            if emails_sent_today >= effective_limit:
                log.info("linkedin.daily_limit_reached", limit=daily_email_limit)
                break
            if not lead_is_addressable(lead):
                log.info("linkedin.skipped_anonymous", email=lead.get("email"))
                continue
            clean_email = lead["email"]
            role = lead["role"]
            text = lead["text"]
            post_url = lead["post_url"]
            company = lead["company"]
            first_name = resolve_recipient_first_name(lead.get("first_name", ""), clean_email)
            resume_path = _get_resume_path(role, resume_dir)
            if await repo.has_emailed(clean_email, within_days=60):
                log.info("linkedin.already_emailed", email=clean_email)
                continue

            if dry_run:
                body_preview = mailer._generate_body(
                    role_name=role, job_description=text,
                    company_name=company, recipient_name=first_name,
                    angle="application",
                )
                print("=" * 70)
                print(f"📧 [DRY RUN MATCH #{emails_sent_today + 1}]")
                print(f"  To:         {clean_email}")
                print(f"  Role:       {role}")
                print(f"  Company:    {company or 'unknown'}")
                print(f"  Name:       {first_name or 'unknown'}")
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
                    company_name=company,
                    recipient_name=first_name,
                    angle="application",
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
                    # Human sending rhythm: randomized 30-90s between
                    # sends so Gmail never sees burst automation.
                    # (20/day cap keeps total volume safe regardless.)
                    pace = random.uniform(30.0, 90.0)
                    log.debug("linkedin.send_pacing", seconds=round(pace, 1))
                    await asyncio.sleep(pace)

    except Exception as exc:
        # Crash outside any single search (SMTP blowup, DB outage): alert
        # loudly with a SHORT message (never a traceback — tracebacks print
        # Settings locals, i.e. passwords and keys, to the console).
        log.exception("linkedin.campaign_crashed", error=str(exc)[:200])
        try:
            await notifier.send(
                "🚨 LinkedIn campaign crashed",
                f"Crashed after {emails_sent_today} sends: {str(exc)[:200]}. "
                f"DB pool may need a check; already-sent leads are recorded.",
                is_error=True,
            )
        except Exception:
            pass
        return emails_sent_today
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

