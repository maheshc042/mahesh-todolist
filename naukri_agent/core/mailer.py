"""
Autonomous Email Dispatcher.

Design Decisions:
- Zero external dependencies: Uses Python's native smtplib, ssl, and email modules.
- Secure by default: Uses strict SSL context for Gmail SMTP on port 465.
- Dynamic Templating: Adapts email copy based on target role (AI/Python vs Full Stack/MERN).
- Non-blocking support: Provides send_application_async for integration with async pipelines.
- Fail-safe attachments: Verifies the PDF exists before attempting to send.
"""

from __future__ import annotations

import asyncio
import imaplib
import re
import smtplib
import ssl
import email as email_pkg
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from ..config import AgentConfig
from ..logging_setup import get_logger
from .gemini_writer import (
    GeminiWriter,
    applicant_snapshot,
    clean_first_name,
    is_immediate_joiner,
)

log = get_logger(__name__)


class ColdEmailer:
    def __init__(
        self,
        sender_email: str,
        app_password: str,
        gemini_api_key: str | None = None,
    ) -> None:
        self.sender_email = sender_email
        self.app_password = app_password
        self.smtp_server = "smtp.gmail.com"
        self.smtp_port = 465
        self.gemini_writer = GeminiWriter(api_key=gemini_api_key)

    def verify_credentials(self) -> bool:
        """One-shot SMTP login check before a campaign burns minutes per recipient."""
        if not self.sender_email or not self.app_password:
            log.warning("mailer.unconfigured")
            return False
        try:
            context = ssl.create_default_context()
            server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=30.0)
            with server:
                server.login(self.sender_email, self.app_password)
            return True
        except smtplib.SMTPAuthenticationError:
            log.error("mailer.preflight_auth_failed", detail="Fix the Gmail App Password in .env — campaign skipped.")
            return False
        except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as exc:
            log.warning("mailer.preflight_unreachable", error=str(exc)[:150])
            return False

    def _generate_body(
        self,
        role_name: str,
        job_description: str = "",
        company_name: str = "",
        angle: str = "application",
        recipient_name: str = "",
    ) -> str:
        """
        Generates email body using Google Gemini AI if GEMINI_API_KEY is available,
        or falls back to the deterministic high-converting template.
        angle="referral" reframes both paths as a referral ask (used by the
        LinkedIn cold campaign); the post-apply path keeps "application".
        """
        gemini_body = self.gemini_writer.generate_email_body(
            role_name=role_name,
            job_description=job_description,
            company_name=company_name,
            angle=angle,
            recipient_name=recipient_name,
        )
        # Stub guard (Oct 2026: an 18-char and a 49-char "email" went out):
        who = applicant_snapshot()
        if gemini_body and len(gemini_body.strip()) >= 200:
            from .gemini_writer import finalize_email_body
            finalized = finalize_email_body(gemini_body, who)
            log.info("mailer.body_source", source="gemini", chars=len(finalized.strip()))
            return finalized
        if gemini_body:
            log.warning("mailer.stub_discarded", chars=len(gemini_body.strip()))
        log.info("mailer.body_source", source="template")

        role_low = role_name.lower()
        ml_track = bool(
            re.search(
                r"\b(machine learning|\bml\b|mlops|deep learning|computer vision|pytorch|tensorflow|data science)\b",
                role_low,
            )
        )
        ai_track = bool(
            re.search(
                r"\b(ai|artificial intelligence|machine learning|\bml\b|llm|genai|"
                r"generative ai|nlp|python|rag|agent|chatbot|fastapi|mlops)\b",
                role_low,
            )
        )

        if who.name != "a Software Engineer":
            name = who.name
        else:
            # Config disk read only on the fallback-of-fallback path.
            name = (AgentConfig.load().applicant_name or "").strip()

        if ml_track:
            stack = "Python, FastAPI, PyTorch, and ML pipelines"
            proof = (
                f"Over the past {who.experience_label}, I have focused on building and deploying production AI systems, "
                "scalable Python backends, and cloud-based ML pipelines."
            )
            skills = "Python, FastAPI, PyTorch, LangChain, Vector Search, AWS, Docker"
            domain_label = "Machine Learning & AI Engineering"
        elif ai_track:
            stack = "Python, FastAPI, LLMs and RAG pipelines"
            proof = (
                f"Over the past {who.experience_label}, I have focused on production GenAI "
                "workflows—building low-latency FastAPI services, RAG retrieval pipelines, and scalable cloud microservices."
            )
            skills = "Python, FastAPI, LangChain, RAG, PyTorch, AWS, PostgreSQL"
            domain_label = "AI & Backend Development"
        else:
            stack = "React, Node.js and TypeScript"
            proof = (
                f"Over the past {who.experience_label}, I have worked across the full stack—building "
                "responsive React frontends, robust Node.js/TypeScript backend services, and scalable REST APIs backed by PostgreSQL."
            )
            skills = "React, Node.js, TypeScript, JavaScript, PostgreSQL, REST APIs"
            domain_label = "Full Stack / Software Development"

        first = clean_first_name(recipient_name)
        if first:
            salutation = f"Hi {first},"
        elif (company_name or "").strip():
            salutation = f"Hi {company_name} Team,"
        else:
            salutation = "Hi,"

        co_suffix = f" for {company_name}" if (company_name or "").strip() else ""
        referral = str(angle or "application").strip().lower() == "referral"
        if referral:
            opener = (
                f"I came across your LinkedIn post regarding the {role_name} opening{co_suffix} and would "
                f"love to be considered — would you be open to referring me to the hiring team? {proof}"
            )
            closer = "My resume is attached for a quick look — would appreciate a referral if my background fits."
        else:
            opener = (
                f"I am reaching out regarding the {role_name} opening you posted on LinkedIn{co_suffix}. {proof}"
            )
            closer = "My resume is attached for your review. If my background aligns with your requirements, I would welcome a brief 10-minute chat to discuss how I can contribute."

        sender = self.sender_email or "maheshrwd042@gmail.com"
        links_lines = []
        if who.github:
            links_lines.append(f"GitHub: {who.github}")
        if who.linkedin:
            links_lines.append(f"LinkedIn: {who.linkedin}")
        links_str = "\n".join(links_lines) if links_lines else (who.github or who.linkedin or "")

        return f"""{salutation}

{opener}

Candidate Snapshot:
• Total Experience: {who.experience_label} ({domain_label})
• Primary Stack: {skills}
• Current Location: {who.location}
• Notice Period: {who.notice_label}
• Current CTC: {who.current_ctc}
• Expected CTC: {who.expected_ctc}
• Contact: {who.mobile} | {sender}

{closer}

Best regards,
{name}
{links_str}
"""

    @staticmethod
    def build_subject(role: str, name: str = "",
                      immediate: bool = False, referral: bool = False,
                      stack_summary: str = "", years: str = "") -> str:
        """Pure subject builder (unit-tested): role first for mobile
        scanning, then name, then the immediate tag. When stack_summary is
        provided, formats high-converting recruiter subject."""
        clean_role = " ".join(str(role or "").splitlines()).strip()[:150]
        clean_name = " ".join(str(name or "").splitlines()).strip()
        immediate_tag = " (Immediate Joiner)" if immediate else ""
        if referral and clean_name:
            return f"Quick referral request: {clean_role}, {clean_name}{immediate_tag}"
        if referral:
            return f"Referral request for {clean_role}{immediate_tag}"
        if stack_summary and clean_name:
            y_tag = f"{years} | " if years else ""
            im_tag = " | Immediate Joiner" if immediate else ""
            return f"Application: {clean_role} – {clean_name} ({y_tag}{stack_summary}{im_tag})"
        if immediate and clean_name:
            return f"{clean_role} application, {clean_name} (immediate joiner)"
        if clean_name:
            return f"Applying for {clean_role}, {clean_name}"
        return f"{clean_role} application"

    def send_application(
        self,
        target_email: str,
        role_name: str,
        resume_path: str | Path,
        job_description: str = "",
        company_name: str = "",
        angle: str = "application",
        recipient_name: str = "",
    ) -> bool:
        """
        Constructs and dispatches the email with the PDF attachment synchronously.
        Includes automatic retry for transient SMTP connection drops.
        """
        import time

        target_email = target_email.strip()
        resume_file = Path(resume_path)

        if not resume_file.exists():
            log.error("mailer.resume_missing", path=str(resume_file))
            return False

        if not self.sender_email or not self.app_password:
            log.warning("mailer.unconfigured", to=target_email)
            return False

        try:
            # 1. Construct the email container. Role/company come from scraped
            # listings: strip CR/LF (header injection) and cap lengths.
            config = AgentConfig.load()
            name = (config.applicant_name or "").strip()

            clean_role = " ".join(str(role_name or "").splitlines()).strip()[:150]
            clean_company = " ".join(str(company_name or "").splitlines()).strip()[:150]
            clean_target = str(target_email or "").strip()
            if "@" not in clean_target or len(str(target_email or "").splitlines()) > 1:
                log.warning("mailer.invalid_recipient", to=clean_target[:60])
                return False

            # Human-style subject: role first (mobile shows ~35 chars), then
            # company, then name — all three searchable later — plus the
            # immediate-joiner tag, your single strongest hook. No pipes,
            # no tags, no YOE counters.
            try:
                immediate = is_immediate_joiner(applicant_snapshot().notice_label)
            except Exception:
                immediate = False
            clean_name = " ".join(str(name or "").splitlines()).strip()
            referral = str(angle or "application").strip().lower() == "referral"

            role_low = clean_role.lower()
            ml_track = re.search(
                r"\b(machine learning|\bml\b|mlops|deep learning|computer vision|pytorch|tensorflow|data science)\b",
                role_low,
            )
            ai_track = re.search(
                r"\b(ai|artificial intelligence|machine learning|\bml\b|llm|genai|"
                r"generative ai|nlp|python|rag|agent|chatbot|fastapi|mlops)\b",
                role_low,
            )
            if ml_track:
                stack_tag = "Python / ML / GenAI / AWS"
            elif ai_track:
                stack_tag = "Python / FastAPI / GenAI"
            else:
                stack_tag = "React / Node.js / TS"

            subject = self.build_subject(
                role=clean_role,
                name=clean_name,
                immediate=immediate,
                referral=referral,
                stack_summary=stack_tag,
                years="2.5 Yrs Exp",
            )
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = self.sender_email
            msg["To"] = clean_target

            from ..linkedin.analyzer import resolve_recipient_first_name
            effective_recipient = resolve_recipient_first_name(recipient_name, clean_target)

            # 2. Add the body text (Gemini AI or template)
            body = self._generate_body(
                role_name=role_name,
                job_description=job_description,
                company_name=company_name,
                angle=angle,
                recipient_name=effective_recipient,
            )
            msg.set_content(body)

            # 3. Read and attach the PDF
            with open(resume_file, "rb") as f:
                pdf_data = f.read()

            msg.add_attachment(
                pdf_data,
                maintype="application",
                subtype="pdf",
                filename=resume_file.name,
            )

            # 4. Dispatch via Secure SMTP with 30s timeout. Connect/login retry
            # (idempotent); the send itself runs ONCE — retrying after a
            # disconnect that follows server-side acceptance would deliver
            # duplicates. A failed send simply returns False and the next
            # campaign run re-attempts it (contacted_recruiters is only
            # written on success).
            context = ssl.create_default_context()
            server = None
            try:
                last_err = None
                for attempt in range(1, 4):
                    try:
                        server = smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=30.0)
                        server.login(self.sender_email, self.app_password)
                        break
                    except smtplib.SMTPAuthenticationError:
                        # Wrong app password: retrying cannot help, and three
                        # sleeps per recipient burned 11 minutes in run 291.
                        log.error("mailer.auth_failed_no_retry", to=clean_target)
                        try:
                            if server is not None:
                                server.close()
                        except Exception:
                            pass
                        return False
                    except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as exc:
                        last_err = exc
                        log.warning("mailer.smtp_connect_retry", attempt=attempt, error=str(exc), to=clean_target)
                        try:
                            if server is not None:
                                server.close()
                        except Exception:
                            pass
                        server = None
                        time.sleep(2.0 * attempt)
                if server is None:
                    if last_err:
                        raise last_err
                    return False
                with server:
                    server.send_message(msg)
                log.info("mailer.sent_success", to=clean_target, role=clean_role)
                return True
            finally:
                try:
                    if server is not None:
                        server.close()
                except Exception:
                    pass

        except smtplib.SMTPAuthenticationError:
            log.error(
                "mailer.auth_failed",
                detail="Invalid Gmail credentials or App Password not set up correctly.",
            )
            return False
        except Exception as exc:
            log.exception("mailer.unexpected_error", to=target_email, error=str(exc))
            return False

    def check_inbox_replies(
        self,
        known_emails: list[str],
        since_days: int = 14,
        max_per_sender: int = 5,
        timeout_s: float = 30.0,
    ) -> tuple[list[dict[str, str]], str | None]:
        """Find inbound replies from contacted recruiters (stdlib IMAP).

        Returns (replies, error): replies are newest-first dicts with
        from/subject/date/snippet keys. error is None on success, else a
        short reason (auth failure, unreachable, ...). Read-only: nothing
        is flagged \\Seen (BODY.PEEK), nothing moved or deleted.
        """
        targets = {str(e or "").strip().lower() for e in (known_emails or []) if str(e or "").strip()}
        if not targets:
            return [], None
        if not self.sender_email or not self.app_password:
            return [], "gmail credentials not configured"
        me = self.sender_email.strip().lower()
        since = (datetime.now(UTC) - timedelta(days=max(1, since_days))).strftime("%d-%b-%Y")
        replies: list[dict[str, str]] = []
        try:
            imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=timeout_s)
        except (OSError, TimeoutError) as exc:
            return [], f"imap unreachable: {exc}"[:150]
        try:
            try:
                imap.login(self.sender_email, self.app_password)
            except imaplib.IMAP4.error:
                return [], "imap auth rejected (check GMAIL_APP_PASSWORD)"
            typ, _ = imap.select("INBOX", readonly=True)
            if typ != "OK":
                return [], "could not open INBOX"
            for target in sorted(targets):
                try:
                    typ, ids = imap.search(None, "FROM", f'"{target}"', "SENTSINCE", since)
                except Exception:
                    continue
                if typ != "OK" or not ids or not ids[0]:
                    continue
                for raw_id in ids[0].split()[-max_per_sender:]:
                    try:
                        typ, data = imap.fetch(raw_id, "(BODY.PEEK[])")
                    except Exception:
                        continue
                    if typ != "OK" or not data:
                        continue
                    raw = b"".join(part[1] for part in data if isinstance(part, tuple) and len(part) > 1)
                    if not raw:
                        continue
                    try:
                        msg = email_pkg.message_from_bytes(raw)
                    except Exception:
                        continue
                    from_addrs = [a.strip().lower() for _, a in getaddresses(msg.get_all("From", [])) if a]
                    if not from_addrs or from_addrs[0] == me:
                        continue
                    if from_addrs[0] not in targets:
                        continue
                    try:
                        dt = parsedate_to_datetime(str(msg.get("Date", "")))
                        date_s = dt.astimezone(UTC).isoformat(timespec="seconds")
                    except Exception:
                        date_s = ""
                    try:
                        payload = msg.get_payload(decode=True) or b""
                        if isinstance(msg.get_payload(), list):
                            payload = b""
                            for part in msg.walk():
                                if part.get_content_type() == "text/plain" and not part.get_filename():
                                    try:
                                        payload = part.get_payload(decode=True) or b""
                                    except Exception:
                                        payload = b""
                                    break
                        snippet = payload.decode("utf-8", "ignore")
                        snippet = " ".join(snippet.split())[:300]
                    except Exception:
                        snippet = ""
                    replies.append({
                        "from": from_addrs[0],
                        "subject": str(msg.get("Subject", "") or "")[:200],
                        "date": date_s,
                        "snippet": snippet,
                    })
            replies.sort(key=lambda r: r.get("date", ""), reverse=True)
            return replies, None
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    async def send_application_async(
        self,
        target_email: str,
        role_name: str,
        resume_path: str | Path,
        job_description: str = "",
        company_name: str = "",
        angle: str = "application",
        recipient_name: str = "",
    ) -> bool:
        """Non-blocking wrapper for send_application to avoid blocking event loops."""
        return await asyncio.to_thread(
            self.send_application,
            target_email,
            role_name,
            resume_path,
            job_description,
            company_name,
            angle,
            recipient_name,
        )
