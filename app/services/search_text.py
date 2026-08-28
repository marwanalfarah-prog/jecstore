"""Text normalisation and fuzzy matching for search (Part I §3.1, §15).

§3.1 requires the site-wide search bar to "account for Arabic/English text
normalization", and §15 asks for typo-tolerant suggestions. Both come down to
one idea: **compare normalised forms, not raw strings.**

Arabic needs more normalisation than Latin because the same word is routinely
written several ways:

* **Diacritics (tashkeel)** are optional — ``الكِتاب`` and ``الكتاب`` are the
  same word, and shoppers almost never type them.
* **Alef forms** ``أ إ آ ٱ`` are interchanged freely with bare ``ا``.
* **Taa marbuta** ``ة`` is often typed as ``ه`` at the end of a word.
* **Alef maqsura** ``ى`` is often typed as ``ي``.
* **Tatweel** ``ـ`` is decoration and carries no meaning.
* **Arabic-Indic digits** ``٠١٢`` and Western ``012`` are the same numbers.

Without this, a shopper typing the most natural spelling of a word gets nothing
back — which is the single most damaging search failure a store can have.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

#: Tashkeel (fatha, damma, kasra, shadda, sukun…) plus the tatweel elongation.
_ARABIC_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")

#: Letter forms shoppers interchange freely.
_ARABIC_FOLD = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ٲ": "ا", "ٳ": "ا",
    "ة": "ه",
    "ى": "ي", "ئ": "ي",
    "ؤ": "و",
    "ک": "ك", "ﻙ": "ك",
    "ی": "ي",
    "ۀ": "ه",
})

#: Arabic-Indic and Eastern Arabic-Indic digits → Western.
_DIGIT_FOLD = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

#: Anything that is not a letter, digit or space becomes a space.
_NON_WORD = re.compile(r"[^\w؀-ۿ]+", re.UNICODE)

#: Arabic definite article — stripped so "الكتاب" matches a search for "كتاب".
_AL_PREFIX = re.compile(r"\bال(?=\w{3,})")

#: Latin words too short to be worth indexing on their own.
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "and", "or", "for", "to", "in", "on", "with",
    "من", "في", "على", "عن", "الى", "و",
})


def normalize(value: str | None) -> str:
    """Reduce text to its comparable form.

    Applied to **both** the indexed text and the query — normalising only one
    side is worse than normalising neither, because the mismatch becomes
    invisible.
    """
    if not value:
        return ""

    text = unicodedata.normalize("NFKC", value)
    text = _ARABIC_DIACRITICS.sub("", text)
    text = text.translate(_ARABIC_FOLD)
    text = text.translate(_DIGIT_FOLD)
    text = text.casefold()
    text = _NON_WORD.sub(" ", text)
    return " ".join(text.split())


def index_text(*parts: str | None) -> str:
    """Build the searchable blob for a record.

    The definite article is stripped from a copy of each Arabic word and kept
    alongside it, so both "كتاب" and "الكتاب" find the same product without the
    query needing to guess which form was indexed.
    """
    normalized = normalize(" ".join(part for part in parts if part))
    if not normalized:
        return ""

    words = normalized.split()
    expanded: list[str] = []
    seen: set[str] = set()

    for word in words:
        for form in (word, _AL_PREFIX.sub("", word)):
            if form and form not in seen:
                seen.add(form)
                expanded.append(form)

    return " ".join(expanded)


def tokens(value: str | None) -> list[str]:
    """Meaningful search tokens, stopwords removed."""
    return [
        word
        for word in normalize(value).split()
        if len(word) > 1 and word not in _STOPWORDS
    ]


# ---------------------------------------------------------------------------
# Typo tolerance (Part I §15)
# ---------------------------------------------------------------------------


def max_edits(term: str) -> int:
    """How many typos to forgive, scaled to the word's length.

    Allowing two edits on a three-letter word would match almost anything, so
    the budget grows with length — the same rule Meilisearch and Elasticsearch
    use, and for the same reason.
    """
    length = len(term)
    if length <= 3:
        return 0
    if length <= 6:
        return 1
    return 2


def similarity(a: str, b: str) -> float:
    """0–1 similarity between two normalised strings."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def is_fuzzy_match(term: str, candidate: str) -> bool:
    """Whether ``candidate`` is within the typo budget for ``term``.

    A prefix match always counts: someone typing half a word is not making a
    mistake, they are still typing.
    """
    if not term or not candidate:
        return False
    if candidate.startswith(term) or term in candidate:
        return True

    budget = max_edits(term)
    if budget == 0:
        return False

    # SequenceMatcher is close enough to an edit ratio for ranking, and needs
    # no dependency. Convert the ratio into an approximate edit allowance.
    longest = max(len(term), len(candidate))
    allowed_ratio = 1 - (budget / longest)
    return similarity(term, candidate) >= allowed_ratio


def score(query: str, text: str) -> float:
    """Rank a record against a query. Higher is better; 0 means no match.

    Scoring rewards, in order: the whole phrase appearing, every token being
    present, tokens matching as prefixes, and finally fuzzy matches. That order
    is deliberate — an exact phrase hit should always outrank a lucky typo
    match.
    """
    query_norm = normalize(query)
    if not query_norm or not text:
        return 0.0

    text_tokens = text.split()
    if not text_tokens:
        return 0.0

    total = 0.0

    # Whole phrase present: the strongest signal.
    if query_norm in text:
        total += 10.0

    for term in query_norm.split():
        if not term:
            continue

        if term in text_tokens:
            total += 4.0
            continue

        prefix_hit = next((t for t in text_tokens if t.startswith(term)), None)
        if prefix_hit:
            total += 3.0
            continue

        substring_hit = any(term in t for t in text_tokens)
        if substring_hit:
            total += 2.0
            continue

        best = max((similarity(term, t) for t in text_tokens), default=0.0)
        if any(is_fuzzy_match(term, t) for t in text_tokens):
            total += 1.5 * best

    return total
