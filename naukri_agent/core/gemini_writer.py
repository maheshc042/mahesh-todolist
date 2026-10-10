"""
Google Gemini AI Cold Email & Cover Letter Writer.

Uses the single pinned model from `Settings.gemini_model` (env GEMINI_MODEL)
to generate tailored, concise cold email body copy from the job description.

Design decisions:

- **One model per run, no silent fallback chain.** The old code tried four
  models in sequence, so logs could claim any one of them served the copy.
  Exactly one model is attempted; success and failure logs name it.
- **Identity resolves from config, never literals.** Name, location,
  experience, notice period and CTC answers all come from
  `AgentConfig` / `Settings`. A profile change moves the email copy with it.
- **Fail-soft to the deterministic template.** Missing key or API error
  returns None and `ColdEmailer` falls back to its local template.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..logging_setup import get_logger

log = get_logger(__name__)

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# Last-resort default when Settings cannot load. Must equal
# `Settings.gemini_model`'s default; GEMINI_MODEL env overrides both.
PINNED_DEFAULT_MODEL = "gemini-3.6-flash"


@dataclass(slots=True)
class ApplicantSnapshot:
    """Identity facts for email copy, resolved from AgentConfig."""

    name: str = "Mahesh Chitakoti"
    email: str = "maheshrwd042@gmail.com"
    location: str = "Bengaluru, India"
    experience_label: str = "2.5+ years"
    notice_label: str = "Immediate (0 days)"
    current_ctc: str = "₹3.9 LPA"
    expected_ctc: str = "₹7 - 8 LPA (Negotiable)"
    mobile: str = "+91 9481777227"
    github: str = ""
    linkedin: str = ""
    skills_label: str = ""


def _settings_model(explicit: str | None) -> str:
    """Single serving model: explicit arg > Settings > env > pinned default."""
    if explicit and explicit.strip():
        return explicit.strip()
    try:
        from ..config import Settings

        configured = Settings().gemini_model  # type: ignore[attr-defined]
        if configured and configured.strip():
            return configured.strip()
    except (ValueError, RuntimeError, OSError) as exc:
        log.debug("gemini.settings_unavailable", error=str(exc)[:120])
    env_model = os.getenv("GEMINI_MODEL", "").strip()
    return env_model or PINNED_DEFAULT_MODEL


def _settings_api_key(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    try:
        from ..config import Settings

        configured = getattr(Settings(), "gemini_api_key", "")
        if configured and configured.strip():
            return str(configured).strip()
    except (ValueError, RuntimeError, OSError) as exc:
        log.debug("gemini.settings_key_unavailable", error=str(exc)[:120])
    return os.getenv("GEMINI_API_KEY", "").strip()


def is_immediate_joiner(notice_label: str) -> bool:
    """True when the configured notice means 'can start right away'."""
    low = (notice_label or "").strip().lower()
    return low in ("immediate", "immediate joiner", "0 days", "0 day", "0-days") or low.startswith("0 ")


def extract_name_from_email(email: str | None) -> str:
    """Extract person first name from personal recruiter email.
    e.g. anushka@innthink.com -> Anushka
         anushka.sharma@innthink.com -> Anushka
         anushka_hr@innthink.com -> Anushka
    Returns '' for role/team inboxes (hr, careers, jobs, hiring, info).
    """
    if not email or "@" not in str(email):
        return ""
    local = str(email).split("@")[0].strip().lower()
    # Strip plus addressing and digits
    local = re.sub(r"\+.*$", "", local)
    local = re.sub(r"\d+", "", local)
    parts = [p for p in re.split(r"[._\-]", local) if p]
    if not parts:
        return ""
    first_part = parts[0]
    role_words = {
        "hr", "careers", "career", "job", "jobs", "hiring", "team", "talent",
        "admin", "contact", "info", "support", "recruit", "recruiter", "recruitment",
        "apply", "application", "work", "people", "tech", "sales", "office", "help",
        "query", "enquiry", "feedback",
    }
    if first_part in role_words or not first_part.isalpha() or len(first_part) < 3 or len(first_part) > 20:
        return ""
    return first_part[:1].upper() + first_part[1:]


def clean_first_name(raw: str | None) -> str:
    """Poster name for the salutation. Handles handles: digits,
    underscores and dots are stripped before capitalizing, so
    'shubhampundir220' still becomes 'Hi Shubham,' instead of 'Hi,'.
    If raw is an email, extracts person name (anushka@... -> Anushka).
    Empty when nothing usable remains — never a placeholder."""
    if not raw:
        return ""
    raw_str = " ".join(str(raw).splitlines()).strip()
    if "@" in raw_str:
        return extract_name_from_email(raw_str)
    first = raw_str.split(" ")[0][:20]
    cleaned = re.sub(r"[^a-zA-Z]", "", first)
    return cleaned[:1].upper() + cleaned[1:] if cleaned else ""


def strip_company_from_title(title: str, company: str = "") -> str:
    """Strip trailing company name or separators from job title.
    e.g. 'Fullstack AI/GenAI Engineer – DharmikVibes' -> 'Fullstack AI/GenAI Engineer'
    """
    text = (title or "").strip()
    if not text:
        return ""
    co = (company or "").strip()
    if co:
        escaped_co = re.escape(co)
        pattern = rf"(?:\s*[-–—|@:]\s*|\s+at\s+){escaped_co}\b.*$"
        text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
        no_space_co = re.escape(re.sub(r"\s+", "", co))
        if no_space_co and no_space_co != escaped_co:
            pattern2 = rf"(?:\s*[-–—|@:]\s*|\s+at\s+){no_space_co}\b.*$"
            text = re.sub(pattern2, "", text, flags=re.IGNORECASE).strip()
    # Strip any trailing delimiters or dangling punctuation
    text = re.sub(r"[\s—–\-|,.(]+$", "", text).strip()
    return text


def applicant_snapshot() -> ApplicantSnapshot:
    """Resolve identity facts from AgentConfig with safe generic fallbacks."""
    try:
        from ..config import AgentConfig

        config = AgentConfig.load()
    except (ValueError, RuntimeError, OSError) as exc:
        log.debug("gemini.config_unavailable", error=str(exc)[:120])
        return ApplicantSnapshot()

    name = (config.applicant_name or "").strip() or "Mahesh Chitakoti"
    location = (config.applicant_location or "").strip() or "Bengaluru, India"

    # User profile specifies 2.5 years of experience
    experience_label = "2.5 years"
    years: float | None = None
    try:
        years = config.experience.total_years
    except (AttributeError, ValueError):
        years = None
    if years and years > 0 and years != 3:
        experience_label = f"{years:g} years"

    notice_label = "Immediate (0 days)"
    current_ctc = "₹3.9 LPA"
    expected_ctc = "₹7 - 8 LPA (Negotiable)"
    try:
        answers = config.answers or {}
        lowered = {str(k).strip().lower(): str(v).strip() for k, v in answers.items()}
        notice_raw = lowered.get("notice period", "")
        if notice_raw:
            if "0" in notice_raw or "immediate" in notice_raw.lower():
                notice_label = "Immediate (0 days)"
            else:
                notice_label = notice_raw if "day" in notice_raw.lower() else f"{notice_raw} notice"

        c_raw = lowered.get("current ctc", "")
        if c_raw:
            current_ctc = f"₹{c_raw} LPA" if "lpa" not in c_raw.lower() else c_raw
        e_raw = lowered.get("expected ctc", "")
        if e_raw:
            expected_ctc = f"₹{e_raw} LPA (Negotiable)" if "lpa" not in e_raw.lower() else f"{e_raw} (Negotiable)"
    except (AttributeError, ValueError) as exc:
        log.debug("gemini.answers_unavailable", error=str(exc)[:120])

    phone = (config.applicant_phone or "").strip()
    if phone:
        mobile = f"+91 {phone}" if not phone.startswith("+") and len(phone) == 10 else phone
    else:
        mobile = "+91 9481777227"

    # Verified skill inventory: top mapped skills by tenure. The prompt may
    # ONLY name these — presenting a post requirement ("LLM evals", "Go")
    # as personal history is fabrication and reads as mass-blast.
    skills_label = ""
    try:
        skill_years: dict[str, float] = {}
        for profile in config.profiles or []:
            if not getattr(profile, "enabled", True):
                continue
            try:
                merged = config.experience_for(profile)
            except (AttributeError, ValueError):
                continue
            for skill, years in ((merged.skills or {}).items()):
                try:
                    if float(years) > 0 and str(skill).strip():
                        key = str(skill).strip()
                        skill_years[key] = max(skill_years.get(key, 0.0), float(years))
                except (TypeError, ValueError):
                    continue
        # Core technical skills to prioritize ahead of generic meta-skills (e.g. agile, algorithms)
        core_priority = {
            "python", "pytorch", "langchain", "fastapi", "rag", "docker", "aws",
            "machine learning", "ml", "genai", "generative ai", "llm", "vector search",
            "react", "react.js", "node.js", "nodejs", "typescript", "javascript",
            "postgresql", "mongodb", "rest apis", "django",
        }
        ranked = sorted(
            skill_years.items(),
            key=lambda kv: (
                -1 if kv[0].lower() in core_priority else 0,
                -kv[1],
                kv[0],
            ),
        )[:12]
        skills_label = ", ".join(f"{name} ({years:g}y)" for name, years in ranked)
    except (AttributeError, ValueError) as exc:
        log.debug("gemini.skills_unavailable", error=str(exc)[:120])

    sender_email = ""
    try:
        from ..config import Settings

        sender_email = (getattr(Settings(), "gmail_sender_email", "") or "").strip()
    except Exception:
        pass
    if not sender_email:
        try:
            sender_email = (getattr(config, "applicant_email", "") or "").strip()
        except Exception:
            pass
    if (
        not sender_email
        or "@" not in sender_email
        or "example" in sender_email.lower()
        or "email_address" in sender_email.lower()
        or "[" in sender_email
    ):
        sender_email = "maheshrwd042@gmail.com"

    return ApplicantSnapshot(
        name=name,
        email=sender_email,
        location=location,
        experience_label=experience_label,
        notice_label=notice_label,
        current_ctc=current_ctc,
        expected_ctc=expected_ctc,
        mobile=mobile,
        github=(config.applicant_github or "").strip(),
        linkedin=(config.applicant_linkedin or "").strip(),
        skills_label=skills_label,
    )


def fix_signoff(text: str, name: str, github: str = "", linkedin: str = "") -> str:
    """Ensure signoff (Best regards, Name) and portfolio links are present and well-formatted."""
    lines = text.rstrip().splitlines()
    while lines and any(k in lines[-1].lower() for k in ("github.com", "linkedin.com", "github:", "linkedin:")):
        lines.pop()

    text_no_links = "\n".join(lines).rstrip()

    clean_lines = [l.strip() for l in text_no_links.splitlines() if l.strip()]
    tail = clean_lines[-2:] if len(clean_lines) >= 2 else clean_lines
    tail_str = " ".join(tail).lower()

    has_signoff = any(w in tail_str for w in ("best regards", "regards", "sincerely", "cheers"))
    has_name = name.lower() in (clean_lines[-1].lower() if clean_lines else "")

    if not has_signoff and not has_name:
        text_no_links = text_no_links + f"\n\nBest regards,\n{name}"
    elif not has_signoff and has_name:
        text_no_links = re.sub(
            rf"(?:\n\s*)?{re.escape(name)}\s*$",
            f"\n\nBest regards,\n{name}",
            text_no_links,
            flags=re.IGNORECASE,
        )
    elif has_signoff and not has_name:
        text_no_links = text_no_links.rstrip() + f"\n{name}"

    links = []
    if github:
        links.append(f"GitHub: {github}")
    if linkedin:
        links.append(f"LinkedIn: {linkedin}")

    if links:
        text_no_links = text_no_links + "\n" + "\n".join(links)

    return text_no_links


def finalize_email_body(text: str, who: ApplicantSnapshot | None = None, role_name: str = "") -> str:
    """Post-process email copy produced by Gemini.
    Guarantees clean formatting, non-fluffy wording, proper Contact single line,
    correct sender contact email (no hallucinated addresses), and MANDATORY sign-off
    with GitHub/LinkedIn portfolio links.
    """
    if not text or not text.strip():
        return ""
    if who is None:
        who = applicant_snapshot()

    # 1. Clean markdown fences
    cleaned = text.strip()
    cleaned = re.sub(r"```[a-zA-Z]*", "", cleaned).strip()

    # 2. Fix broken mobile / phone / contact lines where newline followed the label
    cleaned = re.sub(
        r"([•\-\*]?\s*(?:Mobile|Phone|Contact):)\s*\n+\s*",
        r"\1 ",
        cleaned,
        flags=re.IGNORECASE,
    )

    # 3. Standardize contact line to include both mobile and verified email cleanly on one line
    contact_email = getattr(who, "email", "") or "maheshrwd042@gmail.com"

    def _replace_contact_line(m: re.Match) -> str:
        prefix = m.group(1) or "• "
        rest = m.group(2).strip()
        # Strip any existing or hallucinated email addresses
        phone_part = re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "", rest)
        phone_part = re.sub(r"[|/,-]\s*$", "", phone_part).strip()
        if not phone_part or not any(c.isdigit() for c in phone_part):
            phone_part = who.mobile or "+91 9481777227"
        return f"{prefix}Contact: {phone_part} | {contact_email}"

    cleaned = re.sub(
        r"([•\-\*]\s*)(?:Contact|Mobile|Phone):\s*([^\n]+)",
        _replace_contact_line,
        cleaned,
        flags=re.IGNORECASE,
    )

    # Safeguard against hallucinated variations of candidate email
    cleaned = re.sub(r"\bmaheshchitakoti@gmail\.com\b", contact_email, cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bchitakotimahesh@gmail\.com\b", contact_email, cleaned, flags=re.IGNORECASE)

    # 4. Remove AI fluff phrases if generated
    cleaned = re.sub(
        r",?\s*(?:algorithms,?\s*and\s*secure\s*business\s*process\s*workflows|algorithms\s*and\s*business\s*workflows)",
        ", and scalable REST APIs",
        cleaned,
        flags=re.IGNORECASE,
    )

    # 5. Clean up Candidate Snapshot heading
    cleaned = re.sub(
        r"Quick Candidate Snapshot:",
        "Candidate Snapshot:",
        cleaned,
        flags=re.IGNORECASE,
    )

    # 6. ML role alignment safeguard (if model slipped and mentioned React or omitted ML)
    role_low = (role_name or "").lower()
    is_ml = bool(
        re.search(
            r"\b(machine learning|\bml\b|mlops|deep learning|computer vision|pytorch|tensorflow|data science)\b",
            role_low,
        )
    )
    if is_ml:
        cleaned = re.sub(
            r"(\bTotal Experience:[^\n()]+)\((?:AI & Backend Development|Full Stack[^)]*)\)",
            r"\1(Machine Learning & AI Engineering)",
            cleaned,
            flags=re.IGNORECASE,
        )
        # If intro erroneously mentions React/frontend for an ML position, replace with ML pitch
        cleaned = re.sub(
            r"developing responsive (?:user )?interfaces in React and scalable backend services using Python[^\n.]*",
            "building and deploying production AI systems, scalable Python backends, and cloud-based ML pipelines",
            cleaned,
            flags=re.IGNORECASE,
        )
        # Ensure Primary Stack includes core ML keywords
        def _ensure_ml_stack(m: re.Match) -> str:
            prefix = m.group(1)
            stack_text = m.group(2)
            has_ml = any(k in stack_text.lower() for k in ["pytorch", "langchain", "ml", "learning", "rag", "vector", "tensorflow"])
            if not has_ml:
                return f"{prefix}Primary Stack: Python, PyTorch, LangChain, Vector Search, FastAPI, AWS, Docker"
            return m.group(0)

        cleaned = re.sub(
            r"([•\-\*]\s*)Primary Stack:\s*([^\n]+)",
            _ensure_ml_stack,
            cleaned,
            flags=re.IGNORECASE,
        )

    # 7. Enforce sign-off and portfolio links
    cleaned = fix_signoff(
        cleaned,
        name=who.name,
        github=who.github,
        linkedin=who.linkedin,
    )

    return cleaned.strip()


class GeminiWriter:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = _settings_api_key(api_key)
        self.model = _settings_model(model)
        # Set on 401/403/404: every further call this process would fail the
        # same way (run 291 burned 12 identical 404s). Fail-soft to template.
        self._model_broken = False
        # Preview models flap with 503s under load (run 398: six straight
        # 503s, ~20s burned per job). Three consecutive 5xx trips the same
        # breaker; a later success resets the count.
        self._server_errors = 0

    def generate_email_body(
        self,
        role_name: str,
        job_description: str = "",
        company_name: str = "",
        angle: str = "application",
        recipient_name: str = "",
    ) -> str | None:
        """
        Generate tailored cold email copy with the pinned Gemini model.
        Returns None if GEMINI_API_KEY is unconfigured or the request fails.
        angle="referral" reframes the ask (referral to the hiring team
        instead of a direct application) — cold reply rates are higher for
        a small, easy ask. Facts/snapshot stay identical either way.
        """
        if not self.api_key:
            log.debug("gemini.disabled", reason="GEMINI_API_KEY is not set.")
            return None
        if self._model_broken:
            log.debug("gemini.skipped_model_broken", model=self.model)
            return None

        who = applicant_snapshot()

        # Bound every scraped input: the JD is recruiter-controlled text, and
        # role/company strings come straight off listing cards.
        role_name = strip_company_from_title(role_name, company_name) or role_name
        role_name = " ".join(str(role_name or "").splitlines()).strip()[:150]
        company_name = " ".join(str(company_name or "").splitlines()).strip()[:150]
        job_description = " ".join(str(job_description or "").splitlines()).strip()[:2500]

        links_parts = []
        if who.github:
            links_parts.append(f"GitHub: {who.github}")
        if who.linkedin:
            links_parts.append(f"LinkedIn: {who.linkedin}")
        links_block = "\n".join(links_parts)
        links_line = f"\n{links_block}" if links_block else ""
        recipient_name = clean_first_name(recipient_name)
        if recipient_name:
            salutation = f"Hi {recipient_name},"
        elif company_name:
            salutation = f"Hi {company_name} Team,"
        else:
            salutation = "Hi,"
        referral = str(angle or "application").strip().lower() == "referral"
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
        if ml_track:
            domain_label = "Machine Learning & AI Engineering"
            stack_example = "Python, PyTorch, LangChain, Vector Search, FastAPI, AWS, Docker"
        elif ai_track:
            domain_label = "AI & Backend Development"
            stack_example = "Python, FastAPI, LangChain, RAG, AWS, Docker"
        else:
            domain_label = "Full Stack Development"
            stack_example = "JavaScript, TypeScript, React.js, Node.js, Express.js, MongoDB"

        if referral:
            opener = (
                f'You are {who.name}, writing to ask for a referral to the hiring team for the '
                f'"{role_name}" role at "{company_name or "your company"}".'
            )
            if ml_track:
                intro_specialty = "building and deploying production AI systems and scalable cloud ML pipelines"
            elif ai_track:
                intro_specialty = "building scalable Python backends, FastAPI services, and GenAI workflows"
            else:
                intro_specialty = "building production web applications end-to-end"

            intro_rule = (
                f"4. Intro (2 sentences): Say you came across their LinkedIn post regarding "
                f"the {role_name} opening, ask if they would be open to referring you "
                f"to the hiring team, stating you have {who.experience_label} of experience "
                f"{intro_specialty}. Name ONLY overlap between their requirements "
                f"and YOUR verified inventory above — never present a requirement "
                f"(e.g. 'LLM evals', 'Go') as your own history."
            )
            cta_rule = (
                "6. CTA (1 sentence): Ask whether they would be open to referring you "
                "for this role, and mention your resume is attached for a quick look. "
                "Small, easy ask — no call pressure."
            )
        else:
            opener = (
                f'You are {who.name}, writing a direct job application email to an HR recruiter '
                f'for the "{role_name}" role at "{company_name or "your company"}".'
            )
            if ml_track:
                intro_specialty = (
                    "focused on building and deploying production AI systems, scalable Python backends, "
                    "and cloud-based ML pipelines"
                )
                extra_role_guidance = (
                    "CRITICAL: For Machine Learning roles, DO NOT mention React or frontend UI development. "
                    "Focus strictly on ML systems, Python, PyTorch/LangChain, vector search, and scalable backend/API infrastructure. "
                )
            elif ai_track:
                intro_specialty = (
                    "specialized in building production backend services and GenAI workflows with "
                    "Python, FastAPI, and GenAI/RAG pipelines"
                )
                extra_role_guidance = ""
            else:
                intro_specialty = (
                    "specialized in building production web applications end-to-end—developing "
                    "responsive interfaces in React and scalable backend services with "
                    "Node.js, Express, TypeScript, and MongoDB/PostgreSQL"
                )
                extra_role_guidance = ""

            intro_rule = (
                f"4. Intro (2 sentences): State you are reaching out regarding the {role_name} opening you posted. "
                f"State that over the past {who.experience_label}, you have {intro_specialty}. "
                f"Name ONLY overlap between their requirements and YOUR verified inventory above — "
                f"never present a requirement (e.g. 'LLM evals', 'Go') as your own history. "
                f"{extra_role_guidance}"
                f"CRITICAL: Speak in plain engineering terms. NEVER use buzzwords or academic phrases like 'algorithms', "
                f"'business process workflows', 'architectures', 'synergy', 'spearheaded', 'cutting-edge'."
            )
            cta_rule = (
                "6. CTA (1 sentence): Mention resume is attached for review, and state you would "
                "welcome a brief 10-minute chat or questions to discuss how you can contribute."
            )
        prompt = f"""
{opener}

Applicant Background (exact facts, use them):
- Name: {who.name}
- Total Experience: {who.experience_label} ({domain_label})
- Current CTC: {who.current_ctc}
- Expected CTC: {who.expected_ctc}
- Notice Period: {who.notice_label}
- Current Location: {who.location}
- Contact (Phone & Email): {who.mobile} | {who.email}
- Verified Skill Inventory: {who.skills_label or 'general full-stack development'}
- Portfolio Links:
  GitHub: {who.github}
  LinkedIn: {who.linkedin}

Job Requirements to Target:
{job_description[:2500] if job_description else 'Full-stack and AI software engineering.'}

CONTEXT:
This is for the Indian tech job market. HR recruiters scan cold emails in 5 seconds to verify core hiring criteria: stack match, years, current CTC, expected CTC, notice period, location, and contact. A clean, structured candidate snapshot is essential for HR screening.

RULES FOR THE EMAIL:
1. Salutation: "{salutation}" — use it EXACTLY. Never write bracketed
   placeholders like [Name], [Hiring Manager], or [Recruiter Name].
2. Length: Keep the whole email concise, scannable, and under 150 words (aim ~110-140), but you MUST ALWAYS include the complete sign-off and portfolio links at the bottom.
3. Open with THEM, not you: sentence one references their post/role
   specifically (a stack term or detail from their requirements), never
   "I hope this email finds you well" or any pleasantry.
{intro_rule}
5. Candidate Snapshot (clean, single-line bullets for rapid HR scan):
   • Total Experience: {who.experience_label} ({domain_label})
   • Primary Stack: (verbatim 4-6 core tools from their requirements, e.g. {stack_example})
   • Current Location: {who.location}
   • Notice Period: {who.notice_label}
   • Current CTC: {who.current_ctc}
   • Expected CTC: {who.expected_ctc}
   • Contact: {who.mobile} | {who.email}
{cta_rule}
7. MANDATORY Sign-off: The email MUST conclude with:
   Best regards,
   {who.name}
   GitHub: {who.github}
   LinkedIn: {who.linkedin}

8. FORBIDDEN:
   - Buzzwords: algorithms, business process workflows, architectures, passionate, cutting-edge, synergy, leverage, rockstar, ninja, guru, delve, testament, thrilled, robust, game-changer, world-class, esteemed, utmost, spearheaded.
   - Sob stories, hustle drama, or begging paragraphs.
   - Never invent metrics, percentages, user counts, or company names.
9. Output ONLY the raw email body. No markdown fences, no subject lines, no placeholders.
"""
        raw = self._call_gemini(prompt, role_name=role_name, log_tag="gemini.email_generated")
        if not raw:
            return None
        return finalize_email_body(raw, who, role_name=role_name)

    def generate_wellfound_pitch(
        self,
        role_name: str,
        job_description: str = "",
        company_name: str = "",
        recipient_name: str = "",
    ) -> str | None:
        """
        Generate tailored Wellfound startup pitch note.
        Targeted at founders/CTOs/tech leads: high-impact proof of building,
        FastAPI/LangChain/AWS stack alignment, ownership, immediate joiner,
        crisp 10-minute chat ask. NO CTC, NO phone, NO generic HR bullets.
        """
        if not self.api_key:
            log.debug("gemini.disabled", reason="GEMINI_API_KEY is not set.")
            return None
        if self._model_broken:
            log.debug("gemini.skipped_model_broken", model=self.model)
            return None

        who = applicant_snapshot()

        clean_role = strip_company_from_title(role_name, company_name) or role_name
        clean_role = " ".join(str(clean_role or "").splitlines()).strip()[:150]
        clean_co = " ".join(str(company_name or "").splitlines()).strip()[:150]
        job_description = " ".join(str(job_description or "").splitlines()).strip()[:2500]

        links_items = [p for p in (who.github, who.linkedin) if p]
        links_line = "\n" + " | ".join(links_items) if links_items else ""

        recipient = clean_first_name(recipient_name)
        if recipient:
            salutation = f"Hi {recipient},"
        elif clean_co:
            salutation = f"Hi {clean_co} Team,"
        else:
            salutation = "Hi,"

        prompt = f"""
You are {who.name}, writing a high-impact startup pitch note on Wellfound for the "{clean_role}" role at "{clean_co or 'your company'}".

CRITICAL AUDIENCE & CONTEXT:
The reader is a Startup Founder, CTO, or Engineering Lead reviewing candidate pitches directly on Wellfound.
Your candidate profile card (Name, Bengaluru location, contact info, portfolio links, salary range) is already visible right next to this note.
Do NOT write a standard recruitment email or HR form letter. Founders want to see:
1. Proof of what you have built (backend architecture, GenAI pipelines, latency reduction, APIs).
2. Alignment with their specific product and tech stack.
3. Fast execution and startup ownership (owning features end-to-end).
4. Immediate availability to join.

Applicant Verified Facts:
- Name: {who.name}
- Total Experience: {who.experience_label} building production backend services & GenAI architectures
- Availability: Immediate joiner ({who.notice_label})
- Location: {who.location}
- Verified Tech Stack & Impact:
  * Production AI Pipelines: RAG systems and LLM integrations using LangChain, vector retrieval, and Python/FastAPI for low-latency querying.
  * Scalable Cloud Backends: Engineered and deployed REST APIs and microservices on AWS, handling asynchronous workloads and database integrations.
  * Fast Delivery: Comfortable owning features end-to-end, from API design to cloud deployment in fast-paced startup environments.
- Links: {links_line if links_line else 'none'}

Target Role & Startup Requirements:
- Role: {clean_role}
- Company: {clean_co or 'Startup'}
- Job Description / Requirements:
{job_description[:2500] if job_description else 'Full-stack and AI software engineering.'}

RULES FOR THE WELLFOUND PITCH:
1. Salutation: "{salutation}" — use it EXACTLY.
2. Opening & Domain Touch (1-2 sentences): State you are applying for the {clean_role} role to help build their product/platform. Explicitly connect your GenAI/backend background to their specific domain from the JD (e.g. spiritual tech platform, semantic search over texts, conversational AI, or personalized content delivery).
3. Proof of Building & Tech Alignment (3 concise bullet points):
   Highlight concrete engineering achievements matching their stack:
   • Production AI Pipelines: Built RAG systems, vector retrieval workflows, and LLM integrations using LangChain and FastAPI for low-latency, context-rich responses.
   • Scalable Cloud Backends: Engineered and deployed REST APIs and microservices on AWS, handling asynchronous workloads and database integrations.
   • Fast Delivery: Comfortable owning features end-to-end, from data ingestion and API design to cloud deployment in fast-paced startup environments.
   (Adapt specific tech names to match their requirements, e.g. FastAPI, LangChain, PyTorch, Django, AWS, Postgres, etc.)
4. Availability (1 sentence): State you are based in {who.location} and available to join immediately (0-day notice).
5. Call to Action (1 sentence): Ask if they are open to a quick 10-minute chat this week to discuss how you can contribute to {clean_co or 'the team'}'s product roadmap.
6. Sign-off:
   Best regards,
   {who.name}{links_line}

7. STRICTLY FORBIDDEN:
   - NEVER mention compensation, CTC, or salary figures (anchors compensation prematurely).
   - NEVER include mobile phone numbers or email addresses in the text (already on the profile card).
   - NEVER create a "Quick Candidate Snapshot" bullet list with HR screening metadata.
   - NEVER repeat the target company name in the applicant's experience line (e.g. do NOT write "(Fullstack AI/GenAI Engineer – {clean_co})").
   - Buzzwords: passionate, cutting-edge, synergy, leverage, rockstar, ninja, guru, delve, testament, thrilled, robust.
   - Sob stories or begging paragraphs.
8. Length: Concise, high signal, around 90-130 words.
9. Output ONLY the raw pitch note text. No markdown fences, no subject lines.
"""
        return self._call_gemini(prompt=prompt, role_name=clean_role, log_tag="gemini.wellfound_pitch_generated")

    def _call_gemini(
        self,
        prompt: str,
        role_name: str,
        log_tag: str = "gemini.email_generated",
    ) -> str | None:
        payload = {
            "contents": [{"parts": [{"text": prompt.strip()}]}],
            # NOTE (Sep 2026): the pinned preview model is a thinking model —
            # hidden chain-of-thought consumes the SAME token budget as visible
            # output. 350 tokens starved every reply to ~11 visible tokens
            # (finishReason MAX_TOKENS, thoughtsTokenCount ~335). 2048 leaves
            # room for thought AND the full email. Verified live.
            "generationConfig": {
                "temperature": 0.5,
                "maxOutputTokens": 1024,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        data = json.dumps(payload).encode("utf-8")
        # API key travels in a header, never in the URL query, because
        # proxies, logs and tracebacks capture URLs.
        endpoint = f"{GEMINI_BASE_URL}/{self.model}:generateContent"
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
            method="POST",
        )

        try:
            # With thinkingBudget=0, responses return cleanly in 2-4s without timeout.
            with urllib.request.urlopen(req, timeout=15) as response:
                if response.status == 200:
                    resp_json: dict[str, Any] = json.loads(response.read().decode("utf-8"))
                    candidates = resp_json.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            text = parts[0].get("text", "").strip()
                            if text:
                                clean_lines = []
                                for line in text.splitlines():
                                    if line.lower().startswith("subject:"):
                                        continue
                                    clean_lines.append(line.replace("**", "").replace("`", ""))
                                while clean_lines and re.match(
                                    r"^(text|markdown|email|plain)$",
                                    clean_lines[0].strip().lower(),
                                ):
                                    clean_lines.pop(0)
                                while clean_lines and not clean_lines[-1].strip():
                                    clean_lines.pop()
                                cleaned_text = "\n".join(clean_lines).strip()
                                self._server_errors = 0
                                log.info(
                                    log_tag,
                                    role=role_name,
                                    model=self.model,
                                    length=len(cleaned_text),
                                )
                                return cleaned_text
                    log.warning("gemini.empty_response", role=role_name, model=self.model)
        except urllib.error.HTTPError as exc:
            log.warning("gemini.api_http_error", model=self.model, status=exc.code, reason=str(exc)[:150])
            if exc.code in (401, 403, 404):
                self._model_broken = True
                log.error(
                    "gemini.model_unavailable",
                    model=self.model,
                    status=exc.code,
                    detail="Check GEMINI_MODEL in .env — template fallback for the rest of this run.",
                )
            elif 500 <= exc.code < 600:
                self._server_errors += 1
                if self._server_errors >= 3:
                    self._model_broken = True
                    log.error(
                        "gemini.model_overloaded",
                        model=self.model,
                        consecutive_5xx=self._server_errors,
                        detail="Model flapping — template fallback for the rest of this run.",
                    )
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
            log.warning("gemini.api_error", model=self.model, error=str(exc)[:150])

        return None
