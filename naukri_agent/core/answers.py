"""
Screening-question answer resolution.

Naukri's post-apply chatbot asks free-form recruiter questions ("How many years
of Python?", "Current CTC?", "Are you willing to relocate to Pune?"). Answering
these correctly is the difference between a real application and a wasted one.

Design decisions:

- **Deterministic knowledge base, not an LLM.** Answers are submitted to real
  recruiters and are legally meaningful (notice period, CTC, work authorisation).
  A pattern-matched, human-authored answer is auditable and reproducible; a
  generated one is neither. The KB is ordered: human-resolved review answers
  (priority 10) beat YAML seeds (priority 100), profile-specific beat global,
  and longer patterns beat shorter ones so "years of python" wins over "years".
- **Option-aware resolution.** For radio/checkbox/dropdown questions we match
  the resolved answer against the rendered options (exact -> substring ->
  yes/no synonyms) so "yes" still works when the button says "Yes, I can".
- **Strict mode (default).** If nothing matches confidently we return `None`.
  The caller then abandons that job and queues the question for human review
  rather than guessing — a wrong "notice period: immediate" is worse than a
  skipped listing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..core.models import ScreeningQuestion
from ..logging_setup import get_logger

log = get_logger(__name__)

YES_TOKENS = ("yes", "yeah", "yep", "sure", "agree", "willing", "can do", "i can", "true")
NO_TOKENS = ("no", "nope", "not", "cannot", "can't", "false")


@dataclass(slots=True)
class ResolvedAnswer:
    value: str
    matched_pattern: str
    source: str  # kb | profile | option-match | numeric-heuristic


class AnswerEngine:
    def __init__(
        self,
        kb: list[tuple[str, str]],
        profile_answers: dict[str, str] | None = None,
        strict: bool = True,
    ) -> None:
        """
        `kb` arrives pre-ordered from the repository (priority, then pattern
        length). Profile answers from YAML are prepended so they win.
        """
        profile_pairs = [
            (pattern.strip().lower(), answer)
            for pattern, answer in (profile_answers or {}).items()
        ]
        # Longer patterns first => more specific match wins.
        profile_pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
        self.entries: list[tuple[str, str, str]] = [
            (pattern, answer, "profile") for pattern, answer in profile_pairs
        ] + [(pattern.strip().lower(), answer, "kb") for pattern, answer in kb]
        self.strict = strict

    # ------------------------------------------------------------- resolution
    def resolve(self, question: ScreeningQuestion) -> ResolvedAnswer | None:
        text = " ".join(question.text.lower().split())
        if not text:
            return None

        for pattern, answer, source in self.entries:
            if not pattern:
                continue
            if pattern in text:
                return self._fit_to_options(answer, question, pattern, source)
            # Patterns starting with `re:` are treated as regexes.
            if pattern.startswith("re:"):
                try:
                    if re.search(pattern[3:], text):
                        return self._fit_to_options(answer, question, pattern, source)
                except re.error:
                    continue

        heuristic = self._heuristic(text, question)
        if heuristic is not None:
            return heuristic

        log.info("answers.unresolved", question=question.text[:160], kind=question.kind)
        return None

    def _fit_to_options(
        self,
        answer: str,
        question: ScreeningQuestion,
        pattern: str,
        source: str,
    ) -> ResolvedAnswer | None:
        """Map a KB answer onto one of the rendered choices."""
        if not question.options:
            return ResolvedAnswer(answer, pattern, source)

        wanted = answer.strip().lower()
        options = question.options

        for option in options:  # exact
            if option.strip().lower() == wanted:
                return ResolvedAnswer(option, pattern, source)
        for option in options:  # substring both ways
            low = option.strip().lower()
            if wanted and (wanted in low or low in wanted):
                return ResolvedAnswer(option, pattern, source)

        # yes/no synonym mapping
        if any(token == wanted or wanted.startswith(token) for token in YES_TOKENS):
            for option in options:
                if option.strip().lower().startswith(YES_TOKENS):
                    return ResolvedAnswer(option, pattern, "option-match")
        if any(token == wanted or wanted.startswith(token) for token in NO_TOKENS):
            for option in options:
                low = option.strip().lower()
                if low.startswith(NO_TOKENS) and not low.startswith("not sure"):
                    return ResolvedAnswer(option, pattern, "option-match")

        # numeric answer against numeric buckets ("0-2 years", "3-5 years")
        number = re.search(r"\d+(?:\.\d+)?", wanted)
        if number:
            value = float(number.group())
            for option in options:
                bounds = [float(v) for v in re.findall(r"\d+(?:\.\d+)?", option)]
                if len(bounds) >= 2 and bounds[0] <= value <= bounds[1]:
                    return ResolvedAnswer(option, pattern, "option-match")
                if len(bounds) == 1 and bounds[0] == value:
                    return ResolvedAnswer(option, pattern, "option-match")

        if self.strict:
            return None
        return ResolvedAnswer(options[0], pattern, "option-match")

    def _heuristic(self, text: str, question: ScreeningQuestion) -> ResolvedAnswer | None:
        """
        Last-resort heuristics, only for questions whose semantics are
        unambiguous. Disabled entirely in strict mode for option questions
        because picking a wrong radio button is a silent, unrecoverable mistake.
        """
        if self.strict:
            return None
        if question.kind in ("radio", "checkbox", "dropdown") and question.options:
            for option in question.options:
                if option.strip().lower().startswith(YES_TOKENS):
                    return ResolvedAnswer(option, "heuristic:yes", "numeric-heuristic")
            return ResolvedAnswer(question.options[0], "heuristic:first", "numeric-heuristic")
        if re.search(r"how many years|years of experience|total experience", text):
            return ResolvedAnswer("3", "heuristic:experience", "numeric-heuristic")
        return None
