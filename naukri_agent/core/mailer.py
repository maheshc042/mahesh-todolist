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
from .gemini_writer import GeminiWriter

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
            tech_stack = "Python, FastAPI, GenAI, LLMs, and RAG pipelines"
            highlights = "building autonomous AI agents, scalable Python backends, and low-latency LLM workflows"
        else:
            tech_stack = "React, Node.js, TypeScript, and modern web architectures"
            highlights = "architecting high-performance full-stack web applications and robust REST/GraphQL APIs"

        config = AgentConfig.load()
        name = config.applicant_name or "Mahesh"
        location = config.applicant_location or "Bengaluru, India"

        return f"""Hi there,

I came across your recent hiring post for the {role_name} role and would love to be considered.

I have 2.6+ years of hands-on experience specializing in {tech_stack}. In my recent work, I have focused on {highlights}, consistently delivering robust and scalable solutions.

As an immediate joiner (0-day notice period), I can hit the ground running with minimal ramp-up time. I have attached my resume for your review and would welcome the opportunity to discuss how my technical expertise aligns with your team's goals.

Thank you for your time and consideration!

Best regards,
{name}
{location}
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
            # 1. Construct the email container
            config = AgentConfig.load()
            name = config.applicant_name or "Mahesh"
            
            msg = EmailMessage()
            msg["Subject"] = f"Application: {role_name} - {name}"
            msg["From"] = self.sender_email
            msg["To"] = target_email

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

            # 4. Dispatch via Secure SMTP with 30s timeout and 3-attempt retry
            context = ssl.create_default_context()
            last_err = None
            for attempt in range(1, 4):
                try:
                    with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=30.0) as server:
                        server.login(self.sender_email, self.app_password)
                        server.send_message(msg)
                    log.info("mailer.sent_success", to=target_email, role=role_name, attempt=attempt)
                    return True
                except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as exc:
                    last_err = exc
                    log.warning("mailer.smtp_transient_retry", attempt=attempt, error=str(exc), to=target_email)
                    time.sleep(2.0 * attempt)

            if last_err:
                raise last_err
            return False

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
