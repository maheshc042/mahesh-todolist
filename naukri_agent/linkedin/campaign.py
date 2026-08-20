"""
LinkedIn Cold Email Campaign Runner (Disabled / Commented Out for Future Use).

Design Decisions:
- DB Deduplication: Checks `contacted_recruiters` in Postgres to prevent spamming recruiters.
- Human Rate Limits: Daily cap of 15 emails and 8-second delay between emails to protect Gmail Sender Score.
- Dual-Track Routing: Dynamically selects AI vs Full Stack resume attachments based on role classification.
"""

# import asyncio
# import os
# from pathlib import Path
# from dotenv import load_dotenv

# load_dotenv()

# from ..config import AgentConfig, get_settings
# from ..core.mailer import ColdEmailer
# from ..db.repository import Repository
# from ..logging_setup import get_logger
# from ..notify.notifier import build_notifier
# from .analyzer import TRACK_1_AI_URL, TRACK_2_FULLSTACK_URL, classify_role, is_experience_match
# from .scraper import CookieExpiredError, LinkedInHunter

# log = get_logger(__name__)

# SEARCH_URLS = [
#     TRACK_1_AI_URL,
#     TRACK_2_FULLSTACK_URL,
# ]

# RESUME_AI_PATH = Path("resumes/Mahesh_Chitakoti_AI_Engineer.pdf")
# RESUME_FS_PATH = Path("resumes/Mahesh_Chitakoti_FullStack_Engineer.pdf")


# async def run_campaign(daily_email_limit: int = 15) -> int:
#     li_cookie = os.getenv("LINKEDIN_LI_AT", "").strip()
#     gmail_user = os.getenv("GMAIL_USER", "").strip()
#     gmail_pass = os.getenv("GMAIL_APP_PASSWORD", os.getenv("GMAIL_APP_PASS", "")).strip()

#     settings = get_settings()
#     config = AgentConfig.load()
#     notifier = build_notifier(
#         telegram_enabled=config.notifications.telegram_enabled,
#         bot_token=settings.telegram_bot_token,
#         chat_id=settings.telegram_chat_id,
#     )

#     if not all([li_cookie, gmail_user, gmail_pass]):
#         log.error(
#             "linkedin.missing_secrets",
#             detail="Ensure LINKEDIN_LI_AT, GMAIL_USER, and GMAIL_APP_PASSWORD are set.",
#         )
#         return 0

#     headless_env = os.getenv("HEADLESS", "true").lower() != "false"
#     gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
#     hunter = LinkedInHunter(li_at_cookie=li_cookie, headless=headless_env)
#     mailer = ColdEmailer(sender_email=gmail_user, app_password=gmail_pass, gemini_api_key=gemini_key)
#     repo = await Repository.create()

#     emails_sent_today = 0
#     sent_records: list[dict[str, str]] = []

#     try:
#         for url in SEARCH_URLS:
#             if emails_sent_today >= daily_email_limit:
#                 log.info("linkedin.daily_limit_reached", limit=daily_email_limit)
#                 break

#             log.info("linkedin.starting_search", url=url[:60])
#             try:
#                 posts = await hunter.hunt_for_jobs(url)
#             except CookieExpiredError as exc:
#                 log.error("linkedin.cookie_expired", error=str(exc))
#                 await notifier.send(
#                     "🚨 LinkedIn Cookie Expired!",
#                     "Please grab a new li_at cookie from your browser and update your environment or secrets.",
#                     is_error=True,
#                 )
#                 return emails_sent_today

#             for post_data in posts:
#                 if emails_sent_today >= daily_email_limit:
#                     break

#                 text = post_data["text"]
#                 emails = post_data["emails"]
#                 post_url = post_data.get("post_url", "")

#                 if not is_experience_match(text):
#                     continue

#                 role = classify_role(text)
#                 if not role:
#                     continue

#                 resume_path = RESUME_AI_PATH if "AI" in role else RESUME_FS_PATH

#                 for target_email in emails:
#                     if emails_sent_today >= daily_email_limit:
#                         break

#                     clean_email = target_email.strip().lower()
#                     if await repo.has_emailed(clean_email, within_days=60):
#                         log.info("linkedin.already_emailed", email=clean_email)
#                         continue

#                     success = await mailer.send_application_async(
#                         target_email=clean_email,
#                         role_name=role,
#                         resume_path=resume_path,
#                         job_description=text,
#                     )
#                     if success:
#                         await repo.record_contacted_recruiter(clean_email, role, text, post_url)
#                         emails_sent_today += 1
#                         sent_records.append(
#                             {
#                                 "email": clean_email,
#                                 "role": role,
#                                 "post_url": post_url,
#                             }
#                         )
#                         log.info("linkedin.email_sent", to=clean_email, role=role, sent_today=emails_sent_today)
#                         await asyncio.sleep(8.0)

#             log.info("linkedin.pause_between_searches", seconds=10)
#             await asyncio.sleep(10.0)

#     finally:
#         if sent_records:
#             lines = [f"Sent {len(sent_records)} cold application emails today:"]
#             for rec in sent_records:
#                 line = f"  - {rec['email']} ({rec['role']})"
#                 if rec.get("post_url"):
#                     line += f"\n    Post: {rec['post_url']}"
#                 lines.append(line)
#             summary_text = "\n".join(lines)
#             await notifier.send(
#                 f"🔗 LinkedIn Cold Email Campaign Finished! ({len(sent_records)} Sent)",
#                 summary_text,
#                 is_error=False,
#             )
#         elif emails_sent_today > 0:
#             await notifier.send(
#                 "🔗 LinkedIn Cold Email Campaign Finished!",
#                 f"Sent {emails_sent_today} cold application emails today.",
#                 is_error=False,
#             )

#     log.info("linkedin.campaign_finished", total_emails_sent=emails_sent_today)
#     return emails_sent_today


# if __name__ == "__main__":
#     asyncio.run(run_campaign())
