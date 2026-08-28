"""Locale catalogue integrity (Part I §1, §17.2).

§1 makes Arabic and English equal first-class languages — not one language with
a translation layer bolted on. The failure mode this module exists for is quiet:
:func:`app.core.i18n.translate` falls back to the other language when a key is
missing, which is the right behaviour on a page (readable text beats a raw key)
and a terrible one in review, because a half-translated site looks finished.

So the parity check has to read the catalogs directly.
"""

from __future__ import annotations

import json

import pytest

from app.core.config import settings
from app.core.i18n import LANGUAGES, catalog


def _raw(language: str) -> dict:
    with (settings.locales_dir / f"{language}.json").open(encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------


def test_every_language_has_a_catalogue():
    for language in LANGUAGES:
        assert catalog(language), f"{language}.json is missing or empty"


def test_the_catalogues_hold_exactly_the_same_keys():
    """A key in one file only renders as the *other* language's text — visibly
    wrong to a reader, invisible to a developer."""
    arabic = set(catalog("ar"))
    english = set(catalog("en"))

    assert not arabic - english, f"only in ar.json: {sorted(arabic - english)}"
    assert not english - arabic, f"only in en.json: {sorted(english - arabic)}"


@pytest.mark.parametrize("language", sorted(LANGUAGES))
def test_no_string_is_left_blank(language: str):
    """A blank string is worse than a missing one: the fallback never fires, so
    the page renders an empty button."""
    blank = [key for key, value in catalog(language).items() if not value.strip()]
    assert not blank, f"empty strings in {language}.json: {blank}"


@pytest.mark.parametrize("language", sorted(LANGUAGES))
def test_the_shape_is_the_same_in_both_files(language: str):
    """Where one file nests a section, the other must too — otherwise a later
    edit to the "same" key silently writes to a different place."""
    other = "en" if language == "ar" else "ar"

    def shape(node, prefix=""):
        result = {}
        for key, value in node.items():
            full = f"{prefix}{key}"
            result[full] = isinstance(value, dict)
            if isinstance(value, dict):
                result.update(shape(value, f"{full}."))
        return result

    mine, theirs = shape(_raw(language)), shape(_raw(other))
    mismatched = [k for k in mine.keys() & theirs.keys() if mine[k] != theirs[k]]
    assert not mismatched, f"section vs string mismatch: {mismatched}"


# ---------------------------------------------------------------------------
# Placeholders
# ---------------------------------------------------------------------------


def test_placeholders_match_across_languages():
    """``translate`` formats with keyword arguments and swallows a KeyError by
    returning the unformatted text. So a placeholder present in one language and
    not the other does not raise — it renders ``{amount}`` at the customer."""
    import re

    pattern = re.compile(r"\{(\w+)\}")
    arabic, english = catalog("ar"), catalog("en")

    mismatched = {}
    for key in arabic.keys() & english.keys():
        in_ar = set(pattern.findall(arabic[key]))
        in_en = set(pattern.findall(english[key]))
        if in_ar != in_en:
            mismatched[key] = {"ar": sorted(in_ar), "en": sorted(in_en)}

    assert not mismatched, f"placeholder mismatch: {mismatched}"


# ---------------------------------------------------------------------------
# Script (Part I §1)
# ---------------------------------------------------------------------------


def test_the_arabic_catalogue_is_actually_in_arabic():
    """Guards the commonest way a translation pass goes wrong: a key is copied
    from en.json as a placeholder and never revisited."""
    import re

    arabic_script = re.compile(r"[؀-ۿ]")

    # Names that stay in Latin script in an Arabic interface: file formats,
    # currency codes and the shop's own English name. Listed by value, not by
    # key, so moving a string between sections does not quietly exempt it.
    keeps_latin = {
        "CSV", "Excel", "PDF", "JOD", "$", "JEC Store", "English",
    }

    arabic, english = catalog("ar"), catalog("en")
    untranslated = [
        key
        for key in arabic.keys() & english.keys()
        if arabic[key] == english[key]
        and arabic[key].strip() not in keeps_latin
        and not arabic_script.search(arabic[key])
        and any(character.isalpha() for character in arabic[key])
    ]

    assert not untranslated, f"still English in ar.json: {sorted(untranslated)}"
