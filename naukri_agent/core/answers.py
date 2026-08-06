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

YES_TOKENS = ("yes", "yeah", "yep", "sure", "agree", "willing", "true", "available")
NO_TOKENS = ("no", "nope", "never", "false")

STOPWORDS = frozenset(
    """
    a an the and or of in on at to for with your you have has do does did are is
    was were be been being how many much i me my we our it its that this these
    those please kindly can could would will shall any some what which whom whose
    if then else about from into over under per as by
    """.split()
)

_EXPERIENCE_INTENT = re.compile(
    r"(how\s+(?:many|much)\s+(?:years?|yrs?|months?))"
    r"|(?:total|overall|relevant|work|professional|hands[\s-]?on)\s+experience"
    r"|(?:years?|yrs?)\s+of\s+(?:experience|exp)"
    r"|experience\s+(?:do\s+you\s+have|in|with|on|using)"
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

# Shift keywords removed so user config in config.yaml can properly control them.
_WILLINGNESS_INTENT = re.compile(
    r"\b(?:"
    r"willing|open\s+to|ready\s+to|comfortable|agree|able\s+to|attend|available\s+for|"
    r"would\s+you|can\s+you|are\s+you\s+willing|are\s+you\s+open|are\s+you\s+ready|are\s+you\s+able|"
    r"are\s+you\s+fine|are\s+you\s+ok|do\s+you\s+agree|will\s+you|"
    r"face[\s-]to[\s-]face|f2f|in[\s-]person|offline\s+interview|"
    r"rounds?\s+of\s+(?:technical\s+)?interviews?|technical\s+interviews?|interview\s+rounds?|"
    r"relocate|relocation|"
    r"work\s+from\s+office|wfo|hybrid|onsite|on[\s-]site|"
    r"join\s+immediately|immediate\s+joiner"
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

    def merged_with(self, override: "ExperienceAnswers | None") -> "ExperienceAnswers":
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

        # Stage 2: Generic Affirmative Fallback (Runs AFTER user config is checked)
        willingness = self._resolve_willingness(text, question)
        if willingness is not None:
            return willingness

        # Stage 3: Experience Math
        experience = self._resolve_experience(text, question)
        if experience is not None:
            return experience

        # Stage 4: Fuzzy Math
        fuzzy = self._resolve_fuzzy(text, question)
        if fuzzy is not None:
            return fuzzy

        log.info("answers.unresolved", question=question.text[:160], kind=question.kind, options=len(question.options))
        return None

    def _resolve_lwd(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        if not _LWD_INTENT.search(text):
            return None
        target_date = "30/04/2026"
        for entry in self.entries:
            if "last working" in entry.pattern or "lwd" in entry.pattern:
                target_date = entry.answer
                break
        log.info("answers.lwd_intent", question=question.text[:100], answer=target_date)
        return self._fit_to_options(target_date, question, "intent:lwd", "intent-map")

    def _resolve_willingness(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        if not _WILLINGNESS_INTENT.search(text) or _NEGATIVE_QUESTIONS.search(text):
            return None
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
            elif entry.tokens and entry.tokens <= question_tokens:
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

        if config.default_years is not None:
            return self._fit_to_options(_format_years(config.default_years), question, "experience:default", "experience-map")

        if config.total_years is not None:
            return self._fit_to_options(_format_years(config.total_years), question, "experience:total_fallback", "experience-map")

        return self._fit_to_options("2", question, "experience:universal_fallback", "experience-map")

    def _fit_to_options(self, answer: str, question: ScreeningQuestion, pattern: str, source: str) -> ResolvedAnswer | None:
        if not question.options:
            return ResolvedAnswer(answer, pattern, source)

        wanted = _normalise(answer)
        options = question.options

        for option in options:
            if _normalise(option) == wanted:
                return ResolvedAnswer(option, pattern, source)

        if len(wanted) >= 4:
            for option in options:
                low = _normalise(option)
                if wanted in low or low in wanted:
                    return ResolvedAnswer(option, pattern, source)
        else:
            for option in options:
                if _contains_word(_normalise(option), wanted):
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
                bounds = [float(v) for v in _NUMBER.findall(option)]
                if len(bounds) >= 2 and min(bounds) <= value <= max(bounds):
                    return ResolvedAnswer(option, pattern, "option-match")
                if len(bounds) == 1:
                    target_b = bounds[0]
                    if target_b == value:
                        exact_single = option
                    elif ("<" in option or "less than" in opt_low or "under" in opt_low) and value < target_b:
                        return ResolvedAnswer(option, pattern, "option-match")
                    elif (">" in option or "+" in option or "more than" in opt_low or "greater than" in opt_low) and value >= target_b:
                        exact_single = exact_single or option
                elif not bounds:
                    if ("no experience" in opt_low or "none" in opt_low or "fresher" in opt_low) and value == 0:
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

        meaningful = [t for t in tokens if t not in ("i", "am", "is", "are", "be", "do", "will", "would")]
        head = meaningful[0] if meaningful else tokens[0]

        if any(w in ("no", "nope", "never", "false", "cannot", "can't", "cant", "unwilling", "unavailable", "disagree") for w in tokens):
            return False
        if head in YES_TOKENS or any(w in YES_TOKENS for w in tokens):
            return True
        if head in ("can", "ready", "immediately", "immediate", "acceptable"):
            return True
        return None
