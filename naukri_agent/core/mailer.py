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
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from ..config import AgentConfig
from ..logging_setup import get_logger
from .gemini_writer import GeminiWriter, applicant_snapshot, is_immediate_joiner

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
    ) -> str:
        """
        Generates email body using Google Gemini AI if GEMINI_API_KEY is available,
        or falls back to the deterministic high-converting template.
        """
        gemini_body = self.gemini_writer.generate_email_body(
            role_name=role_name,
            job_description=job_description,
            company_name=company_name,
        )
        if gemini_body:
            return gemini_body

        role_low = role_name.lower()
        if "ai" in role_low or "python" in role_low or "ml" in role_low or "llm" in role_low:
            stack = "Python, FastAPI, LLMs and RAG pipelines"
            proof = "I build and maintain production LLM features, chatbot backends, retrieval pipelines and GenAI services."
        else:
            stack = "React, Node.js and TypeScript"
            proof = "I build and maintain production web apps end to end, React frontends on Node.js APIs."

        who = applicant_snapshot()
        if who.name != "a Software Engineer":
            name = who.name
        else:
            # Config disk read only on the fallback-of-fallback path.
            name = (AgentConfig.load().applicant_name or "").strip()
        links = " ".join(p for p in (who.github, who.linkedin) if p)
        salutation = f"Hi {company_name} Team," if (company_name or "").strip() else "Hi there,"
        if is_immediate_joiner(who.notice_label):
            availability = "I am an immediate joiner and can start right away."
        else:
            availability = f"My notice period is {who.notice_label}."

        return f"""{salutation}

I am applying for the {role_name} role. My stack is {stack}, with {who.experience_label} building production systems around them. {proof}

I am based in {who.location}. {availability} Resume attached, please consider a short intro call this week. Thanks,
{name}
{who.location}
{links}
"""

    def send_application(
        self,
        target_email: str,
        role_name: str,
        resume_path: str | Path,
        job_description: str = "",
        company_name: str = "",
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

            # Human-style subject: no pipes, no tags, no YOE counters, no
            # dashes — and the name is ALWAYS present so recruiters can find
            # the thread later by searching it.
            try:
                immediate = is_immediate_joiner(applicant_snapshot().notice_label)
            except Exception:
                immediate = False
            clean_name = " ".join(str(name or "").splitlines()).strip()
            if immediate and clean_name:
                subject = f"{clean_role} application, {clean_name} (immediate joiner)"
            elif clean_name:
                subject = f"Applying for {clean_role}, {clean_name}"
            else:
                subject = f"{clean_role} application"
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = self.sender_email
            msg["To"] = clean_target

            # 2. Add the body text (Gemini AI or template)
            body = self._generate_body(
                role_name=role_name,
                job_description=job_description,
                company_name=company_name,
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

    async def send_application_async(
        self,
        target_email: str,
        role_name: str,
        resume_path: str | Path,
        job_description: str = "",
        company_name: str = "",
    ) -> bool:
        """Non-blocking wrapper for send_application to avoid blocking event loops."""
        return await asyncio.to_thread(
            self.send_application,
            target_email,
            role_name,
            resume_path,
            job_description,
            company_name,
        )
