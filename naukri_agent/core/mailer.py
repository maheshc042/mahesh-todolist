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
            tech_stack = "Python, FastAPI, LLMs, GenAI, and RAG architectures"
            highlights = "building intelligent, scalable AI agents and integrating LLM features into production applications"
        else:
            tech_stack = "React.js, Node.js, TypeScript, and AWS"
            highlights = "architecting high-performance web applications, developing RESTful APIs, and delivering seamless user experiences"

        return f"""Hi there,

I came across your recent post regarding the open role for a {role_name} and I would love to be considered for the position.

I am an experienced Software Engineer with 2.6 years of hands-on experience specializing in {tech_stack}. In my recent work, I have focused on {highlights}, consistently delivering robust and scalable solutions.

I have attached my resume for your review. I would welcome the opportunity to discuss how my technical expertise aligns with your team's goals.

Thank you for your time and consideration.

Best regards,

Mahesh Chitakoti
Bengaluru, India
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
        Returns True if successful, False if it failed.
        """
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
            msg = EmailMessage()
            msg["Subject"] = f"Application: {role_name} - Mahesh Chitakoti"
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

            # 4. Dispatch via Secure SMTP
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port, context=context, timeout=15.0) as server:
                server.login(self.sender_email, self.app_password)
                server.send_message(msg)

            log.info("mailer.sent_success", to=target_email, role=role_name)
            return True

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
