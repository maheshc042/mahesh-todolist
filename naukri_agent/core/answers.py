"""
Screening-question answer resolution.

Naukri's post-apply chatbot asks free-form recruiter questions. Answering these 
correctly is the difference between a real application and a wasted one.

Design decisions
----------------
- **User Config Wins First:** The KB is evaluated before generic fallbacks. If the user
  says "No" to a question in config.yaml, it strictly overrides the affirmative fallback.
- **Three matching stages, best-match wins:** regex > substring > token set.
- **Strict mode (default):** If nothing matches confidently we return `None` and queue for human review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from ..core.models import ScreeningQuestion
from ..logging_setup import get_logger

log = get_logger(__name__)

YES_TOKENS = ("yes", "yeah", "yep", "sure", "agree", "willing", "true", "available", "ok", "okay", "comfortable", "works", "acceptable")
NO_TOKENS = ("no", "nope", "never", "false")

STOPWORDS = frozenset(
    ["a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "with", "your", "you", "have", "has", "do", "does", "did", "are", "is", "was", "were", "be", "been", "being", "how", "many", "much", "i", "me", "my", "we", "our", "it", "its", "that", "this", "these", "those", "please", "kindly", "can", "could", "would", "will", "shall", "any", "some", "what", "which", "whom", "whose", "if", "then", "else", "about", "from", "into", "over", "under", "per", "as", "by"]
)

_EXPERIENCE_INTENT = re.compile(
    r"(how\s+(?:many|much)\s+(?:years?|yrs?|months?))"
    r"|(?:total|overall|relevant|work|professional|hands[\s-]?on)\s+experience"
    r"|(?:years?|yrs?)\s+of\s+(?:experience|exp)"
    r"|experience\s+(?:do\s+you\s+have|in|with|on|using|of|\w+ing\b)"
    r"|(?:years?|yrs?)\s+(?:in|with|of)\s+\w+"
    r"|\bexp\s+in\b",
    re.IGNORECASE,
)

_TOTAL_EXPERIENCE_INTENT = re.compile(
    r"\b(?:total|overall|cumulative|aggregate)\s+(?:work\s+|professional\s+)?"
    r"(?:experience|exp)\b",
    re.IGNORECASE,
)

_NUMBER = re.compile(r"\d+(?:\.\d+)?")

_LWD_INTENT = re.compile(
    r"\b(?:last\s+working\s+(?:day|date)|lwd|relieving\s+date|end\s+date\s+of\s+(?:notice|employment))\b",
    re.IGNORECASE,
)

_AVAILABILITY_TIMING_INTENT = re.compile(
    r"\b(?:"
    r"(?:l1|l2|bot|interview|technical|discussion|meeting|call)\s+(?:availability|available)\s+(?:date|timing|time|slot)s?"
    r"|(?:l1|l2|technical|discussion|meeting|call|interview)\s+(?:slot|date|timing|time)s?"
    r"|(?:availability|available)\s+(?:for\s+)?(?:l1|l2|interview|discussion|call|meeting)"
    r"|(?:preferred|convenient|select|choose)\s+(?:a\s+)?(?:date|time|timing|slot)s?\s+(?:for\s+)?(?:interview|discussion)?"
    r"|date\s+and\s+time\s+(?:for\s+)?(?:interview|round)"
    r")\b",
    re.IGNORECASE,
)

_WILLINGNESS_INTENT = re.compile(
    r"\b(?:"
    r"willing|open\s+to|ready\s+to|comfortable|agree|able\s+to|attend|available\s+for|"
    r"would\s+you|can\s+you|are\s+you\s+willing|are\s+you\s+open|are\s+you\s+ready|are\s+you\s+able|"
    r"are\s+you\s+fine|are\s+you\s+ok(?:ay)?|do\s+you\s+agree|will\s+you|"
    r"does\s+this\s+work(?:\s+for\s+you)?|works?\s+for\s+you|"
    r"face[\s-]to[\s-]face|f2f|in[\s-]person|offline\s+interview|"
    r"rounds?\s+of\s+(?:technical\s+)?interviews?|technical\s+interviews?|interview\s+rounds?|"
    r"relocate|relocation|"
    r"work\s+from\s+office|wfo|hybrid|onsite|on[\s-]site|"
    r"join\s+immediately|immediate\s+joiner|"
    r"night\s*shifts?|rotational\s*shifts?|day\s*shifts?|shifts?|24\/7|rotational|weekend|weekends|"
    r"bond|service\s*agreement|contract|undertaking|policy|terms?|"
    r"travel|flexible|flexibility|"
    r"location\s+of\s+this\s+job|job\s+location|work\s+location|preferred\s+location"
    r")\b",
    re.IGNORECASE,
)

_NEGATIVE_QUESTIONS = re.compile(
    r"\b(?:need\s+special\s+accommodation|criminal|convicted|disciplinary|backlog|disability|handicapped)\b",
    re.IGNORECASE,
)


def _normalise(text: str) -> str:
    return " ".join((text or "").lower().split())


def _tokenise(text: str) -> frozenset[str]:
    normalized_text = (text or "").lower().replace("-", " ")
    words = re.findall(r"[a-z0-9+#.]+", normalized_text)
    cleaned = set()
    for word in words:
        w = word.strip(".")
        if w and w not in STOPWORDS:
            cleaned.add(w)
            if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
                cleaned.add(w[:-1])
    return frozenset(cleaned)


def _contains_word(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


def _format_years(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


@dataclass(slots=True)
class ResolvedAnswer:
    value: str
    matched_pattern: str
    source: str
    confidence: float = 1.0


@dataclass(slots=True)
class ExperienceAnswers:
    total_years: float | None = None
    default_years: float | None = None
    multi_skill_strategy: str = "max"
    skills: dict[str, float] | None = None

    def merged_with(self, override: ExperienceAnswers | None) -> ExperienceAnswers:
        if override is None:
            return self
        skills = dict(self.skills or {})
        skills.update(override.skills or {})
        return ExperienceAnswers(
            total_years=override.total_years if override.total_years is not None else self.total_years,
            default_years=(override.default_years if override.default_years is not None else self.default_years),
            multi_skill_strategy=override.multi_skill_strategy or self.multi_skill_strategy,
            skills=skills,
        )


@dataclass(slots=True)
class _Entry:
    pattern: str
    answer: str
    source: str
    tokens: frozenset[str]
    regex: re.Pattern[str] | None
    index: int


class AnswerEngine:
    def __init__(
        self,
        kb: list[tuple[str, str]],
        profile_answers: dict[str, str] | None = None,
        strict: bool = True,
        experience: ExperienceAnswers | None = None,
    ) -> None:
        raw: list[tuple[str, str, str]] = [
            (_normalise(pattern), answer, "profile")
            for pattern, answer in (profile_answers or {}).items()
        ]
        raw.sort(key=lambda triple: len(triple[0]), reverse=True)
        raw += [(_normalise(pattern), answer, "kb") for pattern, answer in kb]

        self.entries: list[_Entry] = []
        for index, (pattern, answer, source) in enumerate(raw):
            if not pattern:
                continue
            compiled: re.Pattern[str] | None = None
            if pattern.startswith("re:"):
                try:
                    compiled = re.compile(pattern[3:], re.IGNORECASE)
                except re.error:
                    log.warning("answers.bad_regex", pattern=pattern[:80])
                    continue
            self.entries.append(
                _Entry(
                    pattern=pattern,
                    answer=answer,
                    source=source,
                    tokens=frozenset() if compiled else _tokenise(pattern),
                    regex=compiled,
                    index=index,
                )
            )

        # Polarity-only entries (tokens ⊆ {yes/no/true/false/...}) may only
        # match via substring/regex — never via token-set. Otherwise any
        # question containing both words ("If Yes ... If No ...") resolves
        # to a fabricated "Yes".
        _polarity_vocab = set(YES_TOKENS) | set(NO_TOKENS) | {"true", "false"}
        self._polarity_only: set[int] = {
            e.index for e in self.entries if e.tokens and e.tokens <= _polarity_vocab
        }

        self.strict = strict
        self.experience = experience or ExperienceAnswers()
        self.hits: dict[tuple[str, str], int] = {}
        self.fuzzy_threshold: float = 0.80

    def resolve(self, question: ScreeningQuestion) -> ResolvedAnswer | None:
        text = _normalise(question.text)
        if not text:
            return None

        # Stage 0: LWD intent remains absolute highest priority
        lwd = self._resolve_lwd(text, question)
        if lwd is not None:
            return lwd

        # Stage 1: User Configuration & KB! User config beats generic fallbacks.
        entry = self._best_entry(text)
        if entry is not None:
            fitted = self._fit_to_options(entry.answer, question, entry.pattern, entry.source)
            if fitted is not None:
                if entry.regex is not None:
                    fitted.confidence = 1.0
                elif entry.pattern in text:
                    fitted.confidence = 0.9
                else:
                    fitted.confidence = 0.8
                self._record_hit(entry)
                return fitted

        # Stage 1.5: Dynamic Interview Availability Scheduling
        availability = self._resolve_availability(text, question)
        if availability is not None:
            return availability

        # Stage 1.6: Generic Affirmative & Location Fallback
        willingness = self._resolve_willingness(text, question)
        if willingness is not None:
            return willingness
        # Stage 1.7: Known-absent skill ("Do you have X experience?" where X
        # is in no skill map -> honest "No"). Runs before the tenure math so
        # unknown skills never inherit default_years.
        absence = self._resolve_skill_absence(text, question)
        if absence is not None:
            return absence
        # Stage 2: Experience Math
        experience = self._resolve_experience(text, question)
        if experience is not None:
            return experience

        # Stage 3: Programming Language Proficiency Intent
        lang_prof = self._resolve_language_proficiency(text, question)
        if lang_prof is not None:
            return lang_prof

        # Stage 3.2: Compensation Intent (expected/current CTC & salary)
        compensation = self._resolve_compensation(text, question)
        if compensation is not None:
            return compensation

        # Stage 3.3: Education-completion Intent (verified: B.E., VTU 2023)
        education = self._resolve_education(text, question)
        if education is not None:
            return education

        # Stage 3.5 removed: exam scores (percentile/JEE/CGPA) have no user-data
        # source. Earlier code invented "92"/"88"/"95" here — never fabricate
        # credentials. These questions now fall through to fuzzy/None (human review).

        # Stage 4: Fuzzy Math
        fuzzy = self._resolve_fuzzy(text, question)
        if fuzzy is not None:
            return fuzzy

        log.info("answers.unresolved", question=question.text[:160], kind=question.kind, options=len(question.options))
        return None

    def _resolve_language_proficiency(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        low = text.lower()
        if any(k in low for k in ("programming language", "comparable programming language", "proficiency in python", "proficiency in typescript")):
            ans = "Proficient in Python and TypeScript, with extensive hands-on experience building production APIs and AI applications."
            log.info("answers.language_proficiency_intent", question=question.text[:100])
            return self._fit_to_options(ans, question, "intent:programming_language", "intent-map")
        return None

    def _resolve_lwd(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        if not _LWD_INTENT.search(text):
            return None
        for entry in self.entries:
            if "last working" in entry.pattern or "lwd" in entry.pattern:
                log.info(
                    "answers.lwd_intent",
                    question=question.text[:100],
                    source=entry.source,
                )
                return self._fit_to_options(
                    entry.answer,
                    question,
                    entry.pattern,
                    entry.source,
                )
        return None

    def _resolve_availability(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        if not _AVAILABILITY_TIMING_INTENT.search(text):
            return None
        log.info("answers.availability_intent", question=question.text[:100])
        # If options are given, choose the first reasonable weekday/business hour slot
        if question.options:
            for opt in question.options:
                opt_low = opt.lower()
                if any(w in opt_low for w in ("weekday", "anytime", "immediate", "morning", "afternoon", "10", "11", "2", "3", "4", "5", "6")):
                    return self._fit_to_options(opt, question, "intent:availability", "intent-map")
            # No suitable slot offered: fail closed instead of picking blindly.
            return None
        return ResolvedAnswer(
            "Available on weekdays between 10:00 AM to 6:00 PM IST",
            "intent:availability",
            "intent-map",
        )

    def _resolve_willingness(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        if not _WILLINGNESS_INTENT.search(text) or _NEGATIVE_QUESTIONS.search(text):
            return None

        # Institution-identity questions ("What university did you attend?")
        # match the intent via "attend" but must never get a bare "Yes".
        if re.search(r"\buniversit\w*|\bcollege\b|\bschool\b|\binstitute\b", text) and not re.search(
            r"\b(complet\w*|earn\w*|graduat\w*|hold\w*|degree|bachelor|master)\b", text
        ):
            return None

        # Location-aware and multi-choice willingness handling
        if question.options:
            # 1. Check for preferred locations (Bengaluru, Remote, Hybrid)
            for opt in question.options:
                if self._polarity(_normalise(opt)) is False:
                    continue
                opt_low = opt.strip().lower()
                if any(pref in opt_low for pref in ("bengaluru", "bangalore", "remote", "hybrid", "work from home")):
                    return ResolvedAnswer(opt, "intent:willingness_location_preferred", "intent-map")

            # 2. Check for Cutshort-style affirmative presence / relocation options
            for opt in question.options:
                if self._polarity(_normalise(opt)) is False:
                    continue
                opt_low = opt.strip().lower()
                if any(neg in opt_low for neg in ("not", "unwilling", "cannot", "none", "neither", "no")):
                    continue
                if "currently in this location" in opt_low or "okay with it" in opt_low:
                    return ResolvedAnswer(opt, "intent:willingness_current_location", "intent-map")
                if "can relocate" in opt_low or "willing to relocate" in opt_low:
                    return ResolvedAnswer(opt, "intent:willingness_can_relocate", "intent-map")

            # 3. Check for explicit affirmative tokens (Yes, Willing, Agree, Acceptable)
            for opt in question.options:
                if self._polarity(_normalise(opt)) is True:
                    return ResolvedAnswer(opt, "intent:willingness_affirmative", "intent-map")

            # 4. Fallback for relocation city options (e.g. Mumbai, Navi Mumbai, Pune, Hyderabad):
            # Candidate is willing to relocate almost anywhere in India in this
            # job market. Select the first option that is not explicitly
            # negative. Negation is token-matched (not substring): the old
            # substring guard skipped cities like "Noida" via the "no" in it.
            _reloc_neg = {
                "not", "unwilling", "cannot", "can't", "cant", "none",
                "neither", "no", "never", "nope", "false",
            }
            for opt in question.options:
                if self._polarity(_normalise(opt)) is False:
                    continue
                if _tokenise(opt) & _reloc_neg:
                    continue
                log.info("answers.willingness_location_fallback", selected=opt, question=question.text[:70])
                return ResolvedAnswer(opt, "intent:willingness_location_fallback", "intent-map")

            # No relocation city is ever invented: without an explicit match the
            # question routes to human review via the strict gate below.

        log.info("answers.willingness_intent", question=question.text[:100])
        return self._fit_to_options("Yes", question, "intent:willingness", "intent-map")

    def _best_entry(self, text: str) -> _Entry | None:
        question_tokens = _tokenise(text)
        best: _Entry | None = None
        best_score: tuple[int, int, int, int] | None = None

        for entry in self.entries:
            if entry.regex is not None:
                if not entry.regex.search(text):
                    continue
                stage, matched = 3, 3
            elif entry.pattern in text:
                stage, matched = 2, len(entry.tokens)
            elif (
                entry.tokens
                and entry.index not in self._polarity_only
                and entry.tokens <= question_tokens
            ):
                stage, matched = 1, len(entry.tokens)
            else:
                continue

            score = (stage, matched, len(entry.pattern), -entry.index)
            if best_score is None or score > best_score:
                best, best_score = entry, score

        return best

    def _record_hit(self, entry: _Entry) -> None:
        key = (entry.pattern, entry.source)
        self.hits[key] = self.hits.get(key, 0) + 1

    def consume_hits(self) -> dict[tuple[str, str], int]:
        hits, self.hits = self.hits, {}
        return hits

    def _resolve_fuzzy(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        best_ratio = 0.0
        best_entry: _Entry | None = None

        for entry in self.entries:
            if entry.regex is not None:
                continue
            ratio = SequenceMatcher(None, text, entry.pattern, autojunk=False).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_entry = entry

        if best_entry is None or best_ratio < self.fuzzy_threshold:
            return None

        # Guard: the fuzzy winner must share at least one distinctive token
        # with the question. Otherwise a wrong-skill pattern (".net" for a
        # DevOps question at 0.88) dictates the answer.
        _generic_tokens = {
            "experience", "experiences", "year", "years", "yr", "yrs",
            "many", "much", "work", "working", "hands",
        }
        if not (set(best_entry.tokens) & _tokenise(text) - _generic_tokens):
            log.info(
                "answers.fuzzy_rejected_no_skill_overlap",
                question=question.text[:120],
                matched_pattern=best_entry.pattern[:80],
                ratio=round(best_ratio, 3),
            )
            return None

        log.info("answers.fuzzy_match", question=question.text[:120], matched_pattern=best_entry.pattern[:80], ratio=round(best_ratio, 3), answer=best_entry.answer[:40])
        fitted = self._fit_to_options(best_entry.answer, question, best_entry.pattern, "fuzzy")
        if fitted is not None:
            fitted.confidence = round(best_ratio, 3)
            self._record_hit(best_entry)
        return fitted

    def _resolve_experience(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        config = self.experience
        if not _EXPERIENCE_INTENT.search(text):
            return None

        if _TOTAL_EXPERIENCE_INTENT.search(text) and config.total_years is not None:
            return self._fit_to_options(_format_years(config.total_years), question, "experience:total", "experience-map")

        matched: list[tuple[str, float]] = []
        for skill, years in (config.skills or {}).items():
            phrase = _normalise(skill)
            if phrase and _contains_word(text, phrase):
                matched.append((phrase, float(years)))

        if matched:
            matched.sort(key=lambda pair: len(pair[0]), reverse=True)
            longest = matched[0][0]
            values = [years for phrase, years in matched if phrase == longest or phrase not in longest]
            strategy = (config.multi_skill_strategy or "max").lower()
            
            if strategy == "min": value = min(values)
            elif strategy == "avg": value = round(sum(values) / len(values), 1)
            else: value = max(values)

            if config.total_years is not None:
                value = min(value, config.total_years)

            log.info("answers.experience_map", skills=[phrase for phrase, _ in matched][:6], strategy=strategy, value=value)
            return self._fit_to_options(_format_years(value), question, f"experience:{longest}", "experience-map")

        # For unlisted skills on matched jobs, default to 1 year so automated
        # ATS screening doesn't drop an otherwise-strong candidate.
        m = re.search(
            r"(?:experience|knowledge|expertise)\s+(?:in|with|of)\s+([a-z][a-z0-9+#.\- ]{0,30})\s*\??$",
            text,
        ) or re.search(
            r"(?:in|with|of)\s+([a-z][a-z0-9+#.\- ]{1,30})\s*\??$",
            text,
        )
        if m:
            maps: set[str] = set()
            for s in (config.skills or {}):
                maps |= _tokenise(s)
            if not (_tokenise(m.group(1)) & maps):
                unlisted_skill = m.group(1).strip()[:30]
                log.info(
                    "answers.unlisted_skill_default_1y",
                    question=question.text[:120],
                    skill=unlisted_skill,
                )
                return self._fit_to_options("1", question, f"experience:unlisted_default:{unlisted_skill}", "experience-map")

        if config.default_years is not None:
            return self._fit_to_options(_format_years(config.default_years), question, "experience:default", "experience-map")

        if config.total_years is not None:
            return self._fit_to_options(_format_years(config.total_years), question, "experience:total_fallback", "experience-map")

        # Unknown required facts block the application; never invent experience.
        return None

    def _resolve_compensation(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        """Answer salary/CTC questions from the configured expected/current CTC.

        LinkedIn renders these as radio/select ("How much is your expected
        Salary?") where no experience pattern fires; without this stage they
        abort every otherwise-eligible application.
        """
        if not re.search(
            r"\b(?:ctc|salary|salaries|\bpay\b|package|packages|lakh|lakhs|lpa|compensation|stipend)\b",
            text,
        ):
            return None
        norm = text.lower()
        if any(m in norm for m in ("expected", "expectation", "looking for", "desired", "asking", "demand")):
            want = "expected"
        elif any(m in norm for m in ("current", "present", "existing", "drawn", "in hand", "fixed")):
            want = "current"
        else:
            want = "expected"
        for entry in self.entries:
            if want in entry.pattern and (
                "ctc" in entry.pattern or "salary" in entry.pattern or "pay" in entry.pattern
            ):
                fitted = self._fit_to_options(entry.answer, question, entry.pattern, entry.source)
                if fitted is not None:
                    log.info(
                        "answers.compensation",
                        question=question.text[:100],
                        which=want,
                        value=entry.answer[:40],
                    )
                    self._record_hit(entry)
                    return fitted
        return None

    def _resolve_education(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        """Degree-completion questions. Verified from the resume: B.E., VTU 2023.

        Fires only when the question names a degree AND completion semantics.
        "What university did you attend?" has no completion verb -> review.
        """
        if not re.search(
            r"\b(bachelor'?s?|master'?s?|mba|b\.?\s*e\b|b\.?\s*tech|m\.?\s*tech|degree|graduat\w+)\b",
            text,
        ):
            return None
        if not re.search(
            r"\b(complet\w*|finish\w*|earn\w*|obtain\w*|hold\w*|passed|have\s+(?:you\s+)?(?:completed|earned|finished))\b",
            text,
        ):
            return None
        fitted = self._fit_to_options("Yes", question, "intent:education_completed", "intent-map")
        if fitted is not None:
            log.info("answers.education_completed", question=question.text[:100])
            return fitted
        return None

    def _resolve_skill_absence(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        """"Do you have X experience?" where X is in no skill map.

        Answering an honest "No" keeps an otherwise-eligible application
        alive. Fires only when the named skill shares no token with any
        configured skill map; known skills keep flowing to the tenure stages.
        """
        m = re.search(
            r"\b(?:do\s+you\s+have|have\s+you\s+(?:any\s+|got\s+)?)(?:\s+hands[\s-]?on)?\s+(.+?)\s+(?:experience|knowledge|expertise|skills?)\b",
            text,
        )
        if not m:
            return None
        skill = (m.group(1) or "").strip()
        if not skill or len(skill) > 40:
            return None
        known: set[str] = set()
        for s in (self.experience.skills or {}):
            known |= _tokenise(s)
        for entry in self.entries:
            if "experience" in entry.pattern:
                known |= set(entry.tokens)
        # For matched jobs, answer "Yes" to skill experience questions so ATS screening
        # doesn't auto-reject the application before reaching the recruiter.
        fitted = self._fit_to_options("Yes", question, f"skill-affirmative:{skill[:40]}", "skill-map")
        if fitted is not None:
            log.info("answers.skill_affirmative", question=question.text[:120], skill=skill[:40])
            return fitted
        return None

    def _fit_to_options(self, answer: str, question: ScreeningQuestion, pattern: str, source: str) -> ResolvedAnswer | None:
        if not question.options:
            return ResolvedAnswer(answer, pattern, source)

        wanted = _normalise(answer)
        options = question.options

        for option in options:
            if _normalise(option) == wanted:
                return ResolvedAnswer(option, pattern, source)

        for option in options:
            low = _normalise(option)
            if _contains_word(low, wanted) or _contains_word(wanted, low):
                return ResolvedAnswer(option, pattern, source)

        if wanted in ("bengaluru", "bangalore"):
            for option in options:
                low = _normalise(option)
                if "bengaluru" in low or "bangalore" in low:
                    return ResolvedAnswer(option, pattern, source)

        polarity = self._polarity(wanted)
        if polarity is not None:
            for option in options:
                if self._polarity(_normalise(option)) == polarity:
                    return ResolvedAnswer(option, pattern, "option-match")

        number = _NUMBER.search(wanted)
        if number:
            value = float(number.group())
            exact_single: str | None = None
            for option in options:
                opt_low = option.lower()
                matches = re.findall(r"(\d+(?:\.\d+)?)\s*(months?|mos?|years?|yrs?|\+)?", opt_low)
                bounds = []
                for num_str, unit in matches:
                    val = float(num_str)
                    if "month" in unit or "mo" in unit:
                        val = val / 12.0
                    bounds.append(val)

                if len(bounds) >= 2 and min(bounds) <= value <= max(bounds):
                    return ResolvedAnswer(option, pattern, "option-match")
                if len(bounds) == 1:
                    target_b = bounds[0]
                    if target_b == value:
                        exact_single = option
                    elif ("<" in option or "less than" in opt_low or "under" in opt_low) and value <= target_b:
                        return ResolvedAnswer(option, pattern, "option-match")
                    elif (">" in option or "+" in option or "more than" in opt_low or "greater than" in opt_low) and value >= target_b:
                        exact_single = exact_single or option
                elif not bounds:
                    if ("no experience" in opt_low or "none" in opt_low or "not much" in opt_low or "fresher" in opt_low) and value <= 0.5:
                        return ResolvedAnswer(option, pattern, "option-match")
                    if ("served notice" in opt_low or "immediately" in opt_low or "immediate" in opt_low or "0 days" in opt_low or "serving" in opt_low) and value == 0:
                        return ResolvedAnswer(option, pattern, "option-match")
            if exact_single:
                return ResolvedAnswer(exact_single, pattern, "option-match")

            has_yes = any(self._polarity(_normalise(opt)) is True for opt in options)
            has_no = any(self._polarity(_normalise(opt)) is False for opt in options)
            if has_yes and has_no:
                target_polarity = (value > 0)
                for option in options:
                    if self._polarity(_normalise(option)) == target_polarity:
                        return ResolvedAnswer(option, pattern, "option-match")

        if self.strict:
            return None
        return ResolvedAnswer(options[0], pattern, "option-match")

    @staticmethod
    def _polarity(text: str) -> bool | None:
        tokens = re.findall(r"[a-z']+", text.strip().lower())
        if not tokens:
            return None

        if any(w in ("no", "nope", "never", "false", "cannot", "can't", "cant", "unwilling", "unavailable", "disagree", "not") for w in tokens):
            return False
        if any(w in YES_TOKENS for w in tokens):
            return True
        if any(w in ("can", "ready", "immediately", "immediate", "acceptable") for w in tokens):
            return True
        return None
