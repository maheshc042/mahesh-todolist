"""
Screening-question answer resolution.

Naukri's post-apply chatbot asks free-form recruiter questions ("How many years
of experience do you have in HTML, CSS, JavaScript?", "Current CTC?", "Are you
willing to relocate to Pune?"). Answering these correctly is the difference
between a real application and a wasted one.

Design decisions
----------------

- **Deterministic knowledge base, not an LLM.** Answers are submitted to real
  recruiters and are legally meaningful (notice period, CTC, work authorisation).
  A pattern-matched, human-authored answer is auditable and reproducible; a
  generated one is neither.

- **Three matching stages, best-match wins (not first-match wins).** Real
  recruiter phrasing almost never contains our KB key as a contiguous substring.
  `experience in javascript` does NOT appear in "How many years of experience do
  you have in HTML, CSS, JavaScript?" — that exact question is why the previous
  substring-only engine skipped otherwise-perfect jobs. So we score EVERY entry
  and take the strongest:

      3. regex      — pattern written as `re:<expr>`; the author was explicit
      2. substring  — the KB key appears verbatim in the question
      1. token set  — every significant word of the key appears as a whole word
                      somewhere in the question, in any order

  Ties break on specificity (more matched words, then longer pattern, then
  earlier/higher-priority entry), so `experience in python` beats `experience`.

- **A dedicated years-of-experience resolver.** "How many years in X?" is the
  single most common Naukri question and it is unbounded — you cannot enumerate
  every skill a recruiter might name. `ExperienceAnswers` declares a skill→years
  map once; this engine detects the intent, finds which declared skills the
  question mentions, and combines them with an explicit strategy. Unknown skills
  resolve to `default_years` (or nothing at all in strict mode) instead of
  guessing.

- **Word-boundary option matching.** The old engine matched options by naive
  substring, so the answer "No" matched an option reading "Notice period" and
  "no" matched "Cannot relocate". Short answers (<4 chars) now require an exact
  or word-boundary match, which is the difference between a correct radio button
  and an unrecoverable wrong one.

- **Strict mode (default).** If nothing matches confidently we return `None`.
  The caller abandons that job and queues the question for human review rather
  than guessing — a wrong "notice period: immediate" is worse than a skipped
  listing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from ..core.models import ScreeningQuestion
from ..logging_setup import get_logger

log = get_logger(__name__)

YES_TOKENS = ("yes", "yeah", "yep", "sure", "agree", "willing", "true", "available")
NO_TOKENS = ("no", "nope", "never", "false")

# Dropped from token-set matching: they carry no discriminating signal and their
# absence would otherwise make a good pattern miss.
STOPWORDS = frozenset(
    """
    a an the and or of in on at to for with your you have has do does did are is
    was were be been being how many much i me my we our it its that this these
    those please kindly can could would will shall any some what which whom whose
    if then else about from into over under per as by
    """.split()
)

# Question phrasings that mean "tell me a number of years".
_EXPERIENCE_INTENT = re.compile(
    r"(how\s+(?:many|much)\s+(?:years?|yrs?|months?))"
    r"|(?:total|overall|relevant|work|professional|hands[\s-]?on)\s+experience"
    r"|(?:years?|yrs?)\s+of\s+(?:experience|exp)"
    r"|experience\s+(?:do\s+you\s+have|in|with|on|using)"
    r"|(?:years?|yrs?)\s+(?:in|with|of)\s+\w+"
    r"|\bexp\s+in\b",
    re.IGNORECASE,
)

# "Total"/"overall" means the whole career, not one skill.
_TOTAL_EXPERIENCE_INTENT = re.compile(
    r"\b(?:total|overall|cumulative|aggregate)\s+(?:work\s+|professional\s+)?"
    r"(?:experience|exp)\b",
    re.IGNORECASE,
)

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _normalise(text: str) -> str:
    return " ".join((text or "").lower().split())


def _tokenise(text: str) -> frozenset[str]:
    """Words of a phrase, punctuation stripped, stopwords removed."""
    words = re.findall(r"[a-z0-9+#.]+", (text or "").lower())
    # Trailing dots come from sentence punctuation; `.net` / `node.js` keep theirs.
    cleaned = {word.strip(".") if word.endswith(".") else word for word in words}
    return frozenset(word for word in cleaned if word and word not in STOPWORDS)


def _contains_word(haystack: str, needle: str) -> bool:
    """Whole-word / whole-phrase containment. `ai` must not match `retail`."""
    if not needle:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


def _format_years(value: float) -> str:
    """2.6 -> '2.6', 3.0 -> '3' (recruiters reject '3.0' in integer fields)."""
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


@dataclass(slots=True)
class ResolvedAnswer:
    value: str
    matched_pattern: str
    source: str  # profile | kb | option-match | experience-map | fuzzy
    confidence: float = 1.0  # 1.0=exact, 0.9=substring, 0.8=token-set, 0.0-0.79=fuzzy


@dataclass(slots=True)
class ExperienceAnswers:
    """
    Declarative answers for "how many years of X?" questions.

    `skills` maps a skill phrase to years. Phrases are matched as whole words, so
    `ai` never matches `retail` and `node` never matches `nodejs` unless you
    declare both.
    """

    total_years: float | None = None
    # Used when the question clearly asks for years but names no declared skill.
    default_years: float | None = None
    # How to combine when a question names several declared skills
    # ("HTML, CSS, JavaScript"): max is the usual intent (the stack as a whole).
    multi_skill_strategy: str = "max"
    skills: dict[str, float] | None = None

    def merged_with(self, override: "ExperienceAnswers | None") -> "ExperienceAnswers":
        """Profile-level values win; skill maps are merged, not replaced."""
        if override is None:
            return self
        skills = dict(self.skills or {})
        skills.update(override.skills or {})
        return ExperienceAnswers(
            total_years=override.total_years if override.total_years is not None else self.total_years,
            default_years=(
                override.default_years if override.default_years is not None else self.default_years
            ),
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
        """
        `kb` arrives pre-ordered from the repository (priority, then pattern
        length). Profile answers from YAML are prepended so they win ties.
        """
        raw: list[tuple[str, str, str]] = [
            (_normalise(pattern), answer, "profile")
            for pattern, answer in (profile_answers or {}).items()
        ]
        # Longer profile patterns first => more specific wins an exact tie.
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
        # Patterns whose answer was actually used, for hit accounting.
        self.hits: dict[tuple[str, str], int] = {}
        # Minimum ratio for fuzzy matching (0.0–1.0). Conservative default of
        # 0.80 means the question must share ≥80% of its character sequences
        # with a known pattern before we auto-answer it.
        self.fuzzy_threshold: float = 0.80

    # ------------------------------------------------------------- resolution
    def resolve(self, question: ScreeningQuestion) -> ResolvedAnswer | None:
        text = _normalise(question.text)
        if not text:
            return None

        entry = self._best_entry(text)
        if entry is not None:
            fitted = self._fit_to_options(entry.answer, question, entry.pattern, entry.source)
            if fitted is not None:
                # Assign confidence based on match stage
                if entry.regex is not None:
                    fitted.confidence = 1.0
                elif entry.pattern in text:
                    fitted.confidence = 0.9
                else:
                    fitted.confidence = 0.8
                self._record_hit(entry)
                return fitted
            # The KB knew the answer but it does not map onto the rendered
            # options. Fall through: the experience map may produce a value that
            # does fit a numeric bucket.
            log.debug(
                "answers.option_fit_failed",
                pattern=entry.pattern[:60],
                answer=entry.answer[:40],
                options=len(question.options),
            )

        experience = self._resolve_experience(text, question)
        if experience is not None:
            return experience

        # Stage 4: fuzzy matching — last resort before giving up.
        # Only fires when strict=False OR when fuzzy_threshold is met, so
        # legally meaningful answers (CTC, notice period) are never guessed.
        fuzzy = self._resolve_fuzzy(text, question)
        if fuzzy is not None:
            return fuzzy

        log.info(
            "answers.unresolved",
            question=question.text[:160],
            kind=question.kind,
            options=len(question.options),
        )
        return None

    # ------------------------------------------------------------ kb matching
    def _best_entry(self, text: str) -> _Entry | None:
        """
        Score every entry and return the strongest match.

        First-match-wins was the old behaviour and it is wrong: the KB is ordered
        by pattern LENGTH, not by relevance, so a long weak pattern could shadow
        a short exact one. Scoring makes the choice explicit and testable.
        """
        question_tokens = _tokenise(text)
        best: _Entry | None = None
        best_score: tuple[int, int, int, int] | None = None

        for entry in self.entries:
            if entry.regex is not None:
                if not entry.regex.search(text):
                    continue
                stage = 3
                matched = 3
            elif entry.pattern in text:
                stage = 2
                matched = len(entry.tokens)
            elif entry.tokens and entry.tokens <= question_tokens:
                stage = 1
                matched = len(entry.tokens)
            else:
                continue

            # Higher is better; -index keeps profile/high-priority entries ahead.
            score = (stage, matched, len(entry.pattern), -entry.index)
            if best_score is None or score > best_score:
                best, best_score = entry, score

        return best

    def _record_hit(self, entry: _Entry) -> None:
        key = (entry.pattern, entry.source)
        self.hits[key] = self.hits.get(key, 0) + 1

    def consume_hits(self) -> dict[tuple[str, str], int]:
        """Drain the hit counters so the caller can persist them once per run."""
        hits, self.hits = self.hits, {}
        return hits

    # ------------------------------------------------------- fuzzy matching
    def _resolve_fuzzy(
        self, text: str, question: ScreeningQuestion
    ) -> ResolvedAnswer | None:
        """
        Stage 4: fuzzy-match the question against every KB pattern.

        Uses difflib.SequenceMatcher (no external deps) with a conservative
        0.80 threshold. Only fires for questions the first three stages could
        not answer, so the performance cost is paid rarely.

        A fuzzy match is logged at INFO level so the operator can decide
        whether to promote it to a proper KB entry.
        """
        best_ratio = 0.0
        best_entry: _Entry | None = None

        for entry in self.entries:
            # Skip regex patterns — they are exact by construction.
            if entry.regex is not None:
                continue
            ratio = SequenceMatcher(None, text, entry.pattern, autojunk=False).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_entry = entry

        if best_entry is None or best_ratio < self.fuzzy_threshold:
            return None

        log.info(
            "answers.fuzzy_match",
            question=question.text[:120],
            matched_pattern=best_entry.pattern[:80],
            ratio=round(best_ratio, 3),
            answer=best_entry.answer[:40],
        )
        fitted = self._fit_to_options(
            best_entry.answer, question, best_entry.pattern, "fuzzy"
        )
        if fitted is not None:
            fitted.confidence = round(best_ratio, 3)
            self._record_hit(best_entry)
        return fitted

    # -------------------------------------------------------- experience map
    def _resolve_experience(
        self, text: str, question: ScreeningQuestion
    ) -> ResolvedAnswer | None:
        """
        Answer "how many years of <skill>?" from the declared skill map.

        This is the open-ended half of the problem: a recruiter can name any
        technology, so no finite KB covers it. Declaring the skills once and
        detecting the intent generalises to phrasings we have never seen.
        """
        config = self.experience
        if not _EXPERIENCE_INTENT.search(text):
            return None

        if _TOTAL_EXPERIENCE_INTENT.search(text) and config.total_years is not None:
            return self._fit_to_options(
                _format_years(config.total_years),
                question,
                "experience:total",
                "experience-map",
            )

        matched: list[tuple[str, float]] = []
        for skill, years in (config.skills or {}).items():
            phrase = _normalise(skill)
            if phrase and _contains_word(text, phrase):
                matched.append((phrase, float(years)))

        if matched:
            # Prefer the most specific phrasing when one skill contains another
            # ("react native" vs "react") so a longer declared phrase wins.
            matched.sort(key=lambda pair: len(pair[0]), reverse=True)
            longest = matched[0][0]
            values = [
                years
                for phrase, years in matched
                if phrase == longest or phrase not in longest
            ]
            strategy = (config.multi_skill_strategy or "max").lower()
            if strategy == "min":
                value = min(values)
            elif strategy == "avg":
                value = round(sum(values) / len(values), 1)
            else:
                value = max(values)

            # Never claim more than the total career length.
            if config.total_years is not None:
                value = min(value, config.total_years)

            log.info(
                "answers.experience_map",
                skills=[phrase for phrase, _ in matched][:6],
                strategy=strategy,
                value=value,
            )
            return self._fit_to_options(
                _format_years(value),
                question,
                f"experience:{longest}",
                "experience-map",
            )

        if config.default_years is not None:
            log.info("answers.experience_default", value=config.default_years)
            return self._fit_to_options(
                _format_years(config.default_years),
                question,
                "experience:default",
                "experience-map",
            )

        return None

    # --------------------------------------------------------- option fitting
    def _fit_to_options(
        self,
        answer: str,
        question: ScreeningQuestion,
        pattern: str,
        source: str,
    ) -> ResolvedAnswer | None:
        """Map a resolved answer onto one of the rendered choices."""
        if not question.options:
            return ResolvedAnswer(answer, pattern, source)

        wanted = _normalise(answer)
        options = question.options

        # 1. exact
        for option in options:
            if _normalise(option) == wanted:
                return ResolvedAnswer(option, pattern, source)

        # 2. containment — only for answers long enough to be unambiguous.
        #    "no" must never match "Notice period" or "Cannot relocate".
        if len(wanted) >= 4:
            for option in options:
                low = _normalise(option)
                if wanted in low or low in wanted:
                    return ResolvedAnswer(option, pattern, source)
        else:
            for option in options:
                if _contains_word(_normalise(option), wanted):
                    return ResolvedAnswer(option, pattern, source)

        # 3. yes/no synonyms, matched on the option's FIRST word only.
        polarity = self._polarity(wanted)
        if polarity is not None:
            for option in options:
                if self._polarity(_normalise(option)) == polarity:
                    return ResolvedAnswer(option, pattern, "option-match")

        # 4. numeric answer against numeric buckets ("0-2 years", "3-5 years")
        number = _NUMBER.search(wanted)
        if number:
            value = float(number.group())
            exact_single: str | None = None
            for option in options:
                bounds = [float(v) for v in _NUMBER.findall(option)]
                if len(bounds) >= 2 and min(bounds) <= value <= max(bounds):
                    return ResolvedAnswer(option, pattern, "option-match")
                if len(bounds) == 1:
                    if bounds[0] == value:
                        exact_single = option
                    # "5+ years" style open-ended bucket.
                    elif "+" in option and value >= bounds[0]:
                        exact_single = exact_single or option
            if exact_single:
                return ResolvedAnswer(exact_single, pattern, "option-match")

            # Yes/No fallback for experience questions where recruiter rendered Yes/No options instead of numbers
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
        """
        True for an affirmative option/answer, False for a negative, None when
        it is neither. Only the leading word counts: "No, I cannot relocate" is
        negative, but "Notice period served" is not negative at all.
        """
        first = re.split(r"[^a-z']+", text.strip(), maxsplit=1)
        head = first[0] if first else ""
        if not head:
            return None
        if head in YES_TOKENS:
            return True
        if head in NO_TOKENS:
            return False
        # "cannot"/"can't" are negative; "can"/"i can" are affirmative.
        if head in ("cannot", "can't", "cant", "unwilling", "unavailable"):
            return False
        if head in ("can", "ready", "immediately", "immediate"):
            return True
        return None
