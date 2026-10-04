"""
LinkedIn Post & Lead Analyzer.

Contains:
- Target search URLs for AI/Python and Full Stack/React job posts.
- Email extraction regex tailored for recruiter post text.
- Experience level matching (0-3 years / freshers).
- Role classification (AI vs Full Stack).
"""
import hashlib
import re

TRACK_1_AI_URL = (
    "https://www.linkedin.com/search/results/content/?"
    "keywords=AI%20OR%20Python%20OR%20GenAI%20OR%20LLM%20OR%20FastAPI%20hiring%20email"
    "&origin=FACETED_SEARCH&sortBy=%5B%22date_posted%22%5D"
)

TRACK_2_FULLSTACK_URL = (
    "https://www.linkedin.com/search/results/content/?"
    "keywords=React%20OR%20%22Full%20Stack%22%20OR%20Node.js%20OR%20MERN%20hiring%20email"
    "&origin=FACETED_SEARCH&sortBy=%5B%22date_posted%22%5D"
)

EXCLUDED_EMAIL_DOMAINS = {
    "example.com",
    "domain.com",
    "linkedin.com",
}

EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)

EXPERIENCED_REJECT_REGEX = re.compile(
    r"\b("
    r"[4-9]\+|1[0-9]\+|"
    r"[4-9]\s*\+\s*(?:yrs|years?)|"
    r"1[0-9]\s*\+\s*(?:yrs|years?)|"
    r"(?:3\s*[-–—to]+\s*[5-9]|[4-9]\s*[-–—to]+\s*\d+)\s*(?:yrs|years?)|"
    r"experience\s*:\s*(?:[4-9]|1[0-9])\s*[-–—to+]"
    r"|\bsenior\b|\bsr\b|\bsr\.\b|\blead\b|\bprincipal\b|\bstaff\b"
    r"|(?:engineering|tech|technical|product|project|program|delivery|design)\s+manager|head\s+of"
    r")\b",
    re.IGNORECASE,
)

SPAM_OR_UNPAID_REJECT_REGEX = re.compile(
    r"("
    r"\bunpaid\b|\bno\s+stipend\b|\bwithout\s+stipend\b|"
    r"\b(?:intern|internship|interns|trainee|trainees)\b|"
    r"\bthe\s+entrepreneurship\s+network\b|\bten\b|"
    r"\bb\.?\s*com\b|\bbcom\b|"
    r"\b50\+\s*openings\b|\b100\+\s*openings\b|"
    r"dm\s+on\s+whatsapp|whatsapp\s+group"
    r")",
    re.IGNORECASE,
)

# India-based candidate: onsite-abroad posts can never convert. Fires only
# on STRONG non-India signals (work authorization, clearance, $ pay,
# onsite + foreign country, US staffing markers, US cities). Remote/hybrid/
# location-silent posts pass (fail-open): most recruiter posts name no place.
ABROAD_ONSITE_REJECT_REGEX = re.compile(
    r"("
    r"\b(?:us\s+citizen|us\s+citizens|green\s+card|h1-?b|security\s+clearance|"
    r"public\s+trust\s+clearance|sc\s+clearance|us\s+person)\b|"
    r"\$\s*\d|\b\d+k\s*(?:\/|per|a|\s)\s*(?:yr|year|annum|month)|"
    r"\bon[\s-]?site\s+(?:in|at)\s+(?:the\s+)?"
    r"(?:usa?|united\s+states|uk|united\s+kingdom|london|canada|toronto|"
    r"australia|sydney|europe|germany|berlin|france|singapore|dubai|uae)\b|"
    # US staffing markers: never appear in legitimate India hiring posts.
    r"\b(?:c2c|w2|h-?1b?|ead|opt|stem|usc|eads?)\b|"
    # US cities/metros (full names only — no ambiguous abbreviations).
    r"\b(?:new\s+york|san\s+francisco|los\s+angeles|austin|seattle|chicago|"
    r"boston|atlanta|dallas|houston|denver|miami|arlington|columbus|lisle|"
    r"sunrise|plano|jersey\s+city|edison|charlotte|phoenix|philadelphia|"
    r"san\s+jose|san\s+diego|portland|minneapolis|detroit|tampa|orlando|"
    r"pittsburgh|cleveland|cincinnati|kansas\s+city|st\s+louis|nashville|"
    r"raleigh|durham|richmond|virginia|texas|florida|california|illinois|"
    r"washington\s+dc|new\s+jersey)\b"
    r")",
    re.IGNORECASE,
)

# Poster must look like someone who HIRES. Seeker headlines ("Python
# Developer | Open to Opportunities", "SDE @ X", creators) fail this and
# the post is skipped — emailing fellow job seekers burns cap and brand.
# Empty headline stays fail-open (extraction misses happen).
RECRUITER_SIGNALS = (
    "recruit", "talent", "hr", "human resource", "people ops",
    "hiring manager", "hiring", "staffing", "sourcer", "sourcing",
    "founder", "co-founder", "cofounder", "ceo", "cto", "director",
    "vp ", "vice president", "partner", "manager", "consultant", "owner",
)


def is_likely_hiring_poster(headline: str) -> bool:
    if not (headline or "").strip():
        return True
    low = headline.lower()
    return any(sig in low for sig in RECRUITER_SIGNALS)


def has_profile_skill_overlap(text: str, skills: set[str] | frozenset[str] | None) -> bool:
    """True when the post names at least one skill the candidate verifiably
    has (map years > 0, caller-filtered). Quality-over-volume gate: a post
    with zero skill overlap ("we're hiring developers!") is never worth a
    send, however hiring-flavored its wording. Empty skill set = fail-open
    (misconfiguration must not silence the campaign, it only degrades it).
    """
    if not skills:
        return True
    if not text:
        return False
    blob = text.lower()
    for skill in skills:
        name = (skill or "").strip().lower()
        if not name:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", blob):
            return True
    return False


# Premier-college gates: candidate is non-IIT/NIT (config iit/nit: No), so
# posts restricting to premier institutes can never convert for them.
IIT_ONLY_REJECT_REGEX = re.compile(
    r"\b(?:iits?|nits?)\s*(?:only|ians|graduates|preferred|mandatory)\b|"
    r"\b(?:from|of)\s+(?:iits?|nits?)\b|"
    r"\btier[\s-]?1\s+(?:college|institute|university|b-?school)\b|"
    r"\bpremier\s+(?:institute|college|university|b-?school)\b",
    re.IGNORECASE,
)

# Stacks the candidate verifiably lacks (map years == 0): a post whose CORE
# role is one of these cannot convert, whatever else it mentions.
ZERO_TECH_REJECT_REGEX = re.compile(
    r"\b(?:golang|go\s+(?:developer|engineer)|spring(?:\s+boot|\s+framework)?|"
    r"rust|scala|kotlin|swift|flutter|ruby\s+on\s+rails)\b",
    re.IGNORECASE,
)

TECH_DISQUALIFY_REGEX = re.compile(
    r"\b("
    r"wordpress|wix|shopify|magento|drupal|"
    r"php|laravel|codeigniter|"
    r"dot\s*net|\.net|dotnet|c#|c\s*sharp|asp\.net"
    r")\b",
    re.IGNORECASE,
)

# Keyword lists for role classification. Matched with word boundaries so that
# e.g. "ai" does not match inside "email", and "react" not inside "reaction".
AI_KEYWORDS = [
    "ai", "python", "genai", "llm", "fastapi", "rag", "langchain", "machine learning", "pytorch",
]
FS_KEYWORDS = [
    "react", "full stack", "fullstack", "node.js", "nodejs", "mern", "next.js", "typescript", "frontend",
]


def extract_recruiter_emails(text: str) -> list[str]:
    """Extracts unique, valid recruiter email addresses from post text."""
    if not text:
        return []
    found = EMAIL_REGEX.findall(text)
    valid_emails = set()
    for email in found:
        email_clean = email.strip().lower()
        domain = email_clean.split("@")[-1]
        if domain not in EXCLUDED_EMAIL_DOMAINS and not email_clean.endswith(".png") and not email_clean.endswith(".jpg"):
            valid_emails.add(email_clean)
    return sorted(list(valid_emails))


def is_experience_match(text: str) -> bool:
    """Returns True if the post targets freshers or 0-3 years experience, rejecting senior roles."""
    if not text:
        return False
    if EXPERIENCED_REJECT_REGEX.search(text):
        return False
    return True


def is_spam_or_unpaid(text: str) -> bool:
    """Returns True if the post is an unpaid internship, student program, or generic ad spam."""
    if not text:
        return False
    return bool(SPAM_OR_UNPAID_REJECT_REGEX.search(text))


def is_rate_limit_page(title: str, url: str, body_snippet: str = "") -> bool:
    """Cloudflare 1200 / rate-wall detection (pure).

    When LinkedIn throttles the session/IP, every further search burns
    automation exposure for zero reads — the campaign must stop, not retry
    into the wall. Markers cover the interstitial title, the Ray-ID page,
    and the body copy.
    """
    hay = f"{title or ''}\n{url or ''}\n{body_snippet or ''}".lower()
    # NOTE: challenge markers ("just a moment", "attention required") are
    # deliberately EXCLUDED — a challenge yields zero posts and the search
    # moves on cheaply; only the unrecoverable rate wall stops the run.
    return any(m in hay for m in (
        "temporarily rate limited", "error 1200", "error: 1200",
        "too many requests", "try again later",
    ))


FREE_MAIL_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "yahoo.in", "hotmail.com", "outlook.com",
    "live.com", "live.in", "rediffmail.com", "icloud.com", "protonmail.com",
    "proton.me", "aol.com", "yandex.com", "zoho.com",
})


def is_company_domain(email: str) -> bool:
    """True for employer-domain addresses (higher expected reply value);
    False for free-mailboxes (kept, but deprioritized under the cap)."""
    try:
        domain = email.strip().lower().split("@", 1)[1]
    except (IndexError, AttributeError):
        return False
    return bool(domain) and domain not in FREE_MAIL_DOMAINS


def post_hash(text: str) -> str:
    """Stable identity for overlap measurement (pure): normalized text so
    the same viral post re-surfaced under another keyword hashes equal."""
    blob = " ".join((text or "").lower().split())
    return hashlib.sha1(blob.encode("utf-8", "ignore")).hexdigest()[:16]


def overlap_report(seen: dict[str, list[str]]) -> dict[str, object]:
    """Cross-search duplication stats (pure).

    `seen` maps post hash -> keywords that surfaced it, in encounter order.
    Returns totals + per-keyword novel counts (posts seen ONLY there) so
    redundant keywords can be pruned with data instead of hunches.
    """
    novel: dict[str, int] = {}
    multi = 0
    for _hash, labels in (seen or {}).items():
        if len(labels) > 1:
            multi += 1
        else:
            novel[labels[0]] = novel.get(labels[0], 0) + 1
    total_reads = sum(len(v) for v in (seen or {}).values())
    return {
        "unique_posts": len(seen or {}),
        "total_reads": total_reads,
        "duplicate_reads": total_reads - len(seen or {}),
        "multi_keyword_posts": multi,
        "novel_per_keyword": novel,
    }


def lead_is_addressable(lead: dict) -> bool:
    """Nobody gets an anonymous blast (pure).

    Company-domain inboxes are always addressable (the employer IS the
    address). Free mailboxes only when a real name or company is known —
    otherwise the email opens with a bare "Hi," to a stranger, which reads
    as mass-blast and converts at ~zero while spending cap and reputation.
    """
    email = str(lead.get("email") or "")
    if is_company_domain(email):
        return True
    if str(lead.get("first_name") or "").strip():
        return True
    return bool(str(lead.get("company") or "").strip())


def prioritize_leads(leads: list[dict]) -> list[dict]:
    """Stable company-domain-first, freshest-first ordering (pure).

    Under a daily cap, the scarcest resource is sends. Two converters,
    in order: employer inboxes beat free mailboxes, and a 2-hour-old post
    beats a 23-hour-old one (the poster is still watching replies).
    Original discovery order preserved within each tier.
    """
    def _age(lead: dict) -> float:
        try:
            hours = lead.get("post_age_hours", None)
            return float(hours) if hours is not None else float("inf")
        except (TypeError, ValueError):
            return float("inf")

    return sorted(leads, key=lambda lead: (
        not is_company_domain(lead.get("email", "")),
        _age(lead),
    ))


def parse_post_age_hours(text: str) -> float | None:
    """LinkedIn relative timestamp ('4m', '13h', '2d', '1w', 'just now')
    to hours (pure). None when unparseable — never blocks a lead."""
    if not text:
        return None
    low = text.lower()
    if "just now" in low:
        return 0.0
    m = re.search(r"(\d+)\s*([mhdw])\s*[•·]", low)
    if not m:
        return None
    value = int(m.group(1))
    unit = m.group(2)
    if unit == "m":
        return value / 60.0
    if unit == "h":
        return float(value)
    if unit == "d":
        return float(value) * 24.0
    return float(value) * 24.0 * 7.0


def is_abroad_onsite(text: str) -> bool:
    """Returns True only on strong non-India onsite signals (auth, clearance,
    $ pay, onsite + foreign country). Remote/hybrid/silent posts pass."""
    if not text:
        return False
    return bool(ABROAD_ONSITE_REJECT_REGEX.search(text))


_ROLE_WORDS = frozenset({
    "engineer", "developer", "talent", "team", "role", "roles", "hiring",
    "job", "jobs", "position", "positions", "opportunity", "acquisition",
    "people", "human", "resources", "developers", "engineers",
})

# Clifton-strength style false friends: "great at Python" must never read
# Python as the employer. A candidate made ONLY of these is skipped.
_TECH_WORDS = frozenset({
    "python", "java", "react", "node", "nodejs", "ai", "ml", "llm", "genai",
    "fastapi", "django", "sql", "aws", "azure", "gcp", "docker", "kubernetes",
    "typescript", "javascript", "angular", "vue", "go", "golang", "rust",
    "data", "cloud", "devops", "backend", "frontend", "fullstack", "mern",
})


def _clean_company(cand: str) -> str:
    words = [w for w in cand.split()
             if w.lower() not in _ROLE_WORDS and w.lower() not in _TECH_WORDS]
    if not words:
        return ""
    return " ".join(words)[:60]


def extract_company(post_text: str, headline: str = "") -> str:
    """Employer name behind a hiring post (pure, best-effort).

    Headline first ("Recruiter at Infosys"), then explicit post phrasings
    ("we are hiring at X"). Never returns role words or generic nouns —
    empty string when unsure, so the email says "your company", never a
    wrong one.
    """
    for source in (headline or "", post_text or ""):
        for m in re.finditer(
            r"(?:\bat\b|@|·)\s*([A-Z][\w&.,'\-]*(?:\s+[A-Z][\w&.,'\-]*){0,3})", source
        ):
            cleaned = _clean_company(m.group(1).strip().strip(".,"))
            if cleaned:
                return cleaned
    m = re.search(
        r"we(?:'re|\s+are)\s+hiring\s+(?:at|for)\s+([A-Z][\w&.,'\-]*(?:\s+[A-Z][\w&.,'\-]*){0,3})",
        post_text or "",
    )
    if m:
        cleaned = _clean_company(m.group(1).strip().strip(".,"))
        if cleaned:
            return cleaned
    return ""


def extract_poster(text: str) -> tuple[str, str]:
    """Poster (name, headline) straight from the post snippet (pure).

    Snippets consistently read "Feed post\\n\\n<Name>\\n\\n • 3rd+\\n\\n<Headline>".
    DOM extraction stays primary (it sees loads the snippet truncates), but
    when it misses, this fallback still yields a human "Hi {Name}," instead
    of a bare "Hi,". Empty pair when the shape is absent — never a guess.
    """
    if not text:
        return "", ""
    lines = [ln.strip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        if re.match(r"^[•·]\s*\d*\s*(?:st|nd|rd|th)?\s*\+?$", ln):
            name = ""
            for prev in reversed(lines[:i]):
                if prev and prev.lower() != "feed post":
                    name = prev
                    break
            if not re.match(r"^[A-Z][\w.'\-]*(\s+[A-Z][\w.'\-]*){0,3}$", name):
                return "", ""
            headline = ""
            for following in lines[i + 1:]:
                if following:
                    headline = following[:160]
                    break
            return name[:80], headline
    return "", ""


def extract_first_name(author: str) -> str:
    """Poster first name for the salutation (pure). Empty when unusable."""
    if not author:
        return ""
    first = author.strip().split()[0].strip(".,@")
    if not first or not first[0].isalpha() or len(first) > 20:
        return ""
    return first


def is_disqualified_tech(text: str) -> bool:
    """Returns True if the post is primarily focused on disqualified tech stacks (PHP, WordPress, .NET)."""
    if not text:
        return False
    return bool(TECH_DISQUALIFY_REGEX.search(text))


def _count_keyword_hits(text_lower: str, keywords: list[str]) -> int:
    """Counts keywords present in the text using word-boundary matching."""
    return sum(
        1
        for kw in keywords
        if re.search(rf"\b{re.escape(kw)}\b", text_lower)
    )


def classify_role(text: str) -> str | None:
    """Classifies the post into 'AI / Python Engineer' or 'Full Stack Engineer'."""
    if not text:
        return None
    text_lower = text.lower()

    # Reject disqualified technologies
    if is_disqualified_tech(text):
        return None

    ai_score = _count_keyword_hits(text_lower, AI_KEYWORDS)
    fs_score = _count_keyword_hits(text_lower, FS_KEYWORDS)

    if ai_score >= fs_score and ai_score > 0:
        return "AI / Python Engineer"
    elif fs_score > ai_score:
        return "Full Stack Engineer"
    elif ai_score > 0:
        return "AI / Python Engineer"
    return None

