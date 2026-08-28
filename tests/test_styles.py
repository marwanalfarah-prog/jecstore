"""The built stylesheet matches the templates.

Tailwind emits only the classes it finds by scanning source files, and the
result is committed as ``app/static/css/app.css``. So adding a colour token to
``tailwind.config.js`` and using it in a template does nothing at all until
somebody runs ``npm run css:build``.

That failure is silent and ugly: the page renders, the element is simply
invisible or unstyled, and no test that checks HTML notices. This module reads
the built file directly.

If one of these fails, the fix is ``npm run css:build``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.config import settings

STYLESHEET = settings.static_dir / "css" / "app.css"
TEMPLATES = settings.templates_dir

#: Colour tokens defined by the project rather than by Tailwind's defaults.
#: A rule for these can only exist if the stylesheet was rebuilt after the
#: token was added.
PROJECT_TOKENS = (
    "primary", "secondary", "navy", "success", "warning", "danger", "info", "star",
)

#: A class as written in a template, variants and all — ``hover:bg-primary-700``
#: is emitted as ``.hover\:bg-primary-700:hover``, so the variants have to be
#: carried through or every hover state reads as missing.
_CLASS_PATTERN = re.compile(
    r"((?:[a-z0-9-]+:)*"
    r"(?:text|bg|border|ring|fill|stroke|from|via|to)-(?:"
    + "|".join(PROJECT_TOKENS)
    + r")(?:-[a-z0-9]+)?)(?![\w-])"
)


@pytest.fixture(scope="module")
def stylesheet() -> str:
    assert STYLESHEET.is_file(), f"{STYLESHEET} has never been built"
    return STYLESHEET.read_text(encoding="utf-8")


def _escaped(class_name: str) -> str:
    """How Tailwind writes the selector: dots, slashes and colons are escaped."""
    for character in (".", "/", ":"):
        class_name = class_name.replace(character, "\\" + character)
    return "." + class_name


def _template_files() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def test_the_stylesheet_has_been_built(stylesheet: str):
    assert len(stylesheet) > 1000, "app.css looks empty or truncated"


def test_every_project_colour_class_used_in_a_template_exists_in_the_css(
    stylesheet: str,
):
    """The check that would have caught the review stars shipping invisible."""
    missing: dict[str, list[str]] = {}

    for path in _template_files():
        source = path.read_text(encoding="utf-8")
        for class_name in sorted(set(_CLASS_PATTERN.findall(source))):
            if _escaped(class_name) not in stylesheet:
                missing.setdefault(
                    str(path.relative_to(TEMPLATES)), []
                ).append(class_name)

    assert not missing, (
        "classes used in templates but absent from app.css "
        f"(run `npm run css:build`): {missing}"
    )


def test_no_template_composes_a_colour_class_from_a_variable():
    """Tailwind scans for whole class names, so ``bg-{{ tone }}-50`` is never
    emitted — the element renders unstyled with no error anywhere."""
    composed = re.compile(
        r"\b(?:text|bg|border|ring|fill)-\{\{|\b(?:text|bg|border|ring|fill)-\{%"
    )

    offenders = [
        str(path.relative_to(TEMPLATES))
        for path in _template_files()
        if composed.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"interpolated Tailwind colour classes in: {offenders}"


# ---------------------------------------------------------------------------
# Project component classes (Part I §17.6, §17.7)
# ---------------------------------------------------------------------------
#
# The colour checks above cover Tailwind utilities. They do not cover the
# project's own `@layer components` classes — `.admin-flash--success`,
# `.badge-warning`, `.stat-tile__hint` — and Tailwind purges those exactly the
# same way: a class defined in input.css but never found as a whole string in a
# template is simply not emitted.
#
# The bug this pins shipped: the admin flash banner wrote
# `class="admin-flash admin-flash--{{ tone }}"`, so none of the four tone
# modifiers were ever emitted and every banner rendered with no colour and no
# border. The markup was correct, the CSS was correct, and the two never met.

_COMPONENT_PATTERN = re.compile(r"^\s*\.([a-z][\w-]*(?:__[\w-]+)?(?:--[\w-]+)?)\s*\{", re.M)

#: Composed in a template from a variable — the whole failure mode.
_COMPOSED = re.compile(
    r'class="[^"]*\b([a-z][\w-]*(?:__[\w-]+)?)--\{\{',
)


def _declared_components() -> set[str]:
    source = (settings.static_dir / "css" / "input.css").read_text(encoding="utf-8")
    return set(_COMPONENT_PATTERN.findall(source))


def test_every_project_component_class_a_template_uses_exists_in_the_css(
    stylesheet: str,
):
    declared = _declared_components()
    missing: dict[str, list[str]] = {}

    for path in _template_files():
        source = path.read_text(encoding="utf-8")
        used = {
            name
            for name in re.findall(r'[\s"\']([a-z][\w-]*(?:__[\w-]+)?(?:--[\w-]+)?)(?=[\s"\'])', source)
            if name in declared
        }
        for name in sorted(used):
            if f".{name}" not in stylesheet:
                missing.setdefault(str(path.relative_to(TEMPLATES)), []).append(name)

    assert not missing, (
        "component classes used in templates but absent from app.css "
        f"(run `npm run css:build`): {missing}"
    )


def test_no_template_composes_a_component_modifier_from_a_variable():
    """`admin-flash--{{ tone }}` is never emitted by Tailwind, so the element
    renders unstyled with nothing failing anywhere. Write the modifier out."""
    offenders: dict[str, list[str]] = {}
    for path in _template_files():
        found = _COMPOSED.findall(path.read_text(encoding="utf-8"))
        if found:
            offenders[str(path.relative_to(TEMPLATES))] = sorted(set(found))

    assert not offenders, f"interpolated component modifiers in: {offenders}"


# ---------------------------------------------------------------------------
# JSON dropped into an HTML attribute
# ---------------------------------------------------------------------------


def test_no_template_puts_unescaped_json_in_a_double_quoted_attribute():
    """``tojson`` escapes ``<``, ``>``, ``&`` and ``'`` for HTML but
    deliberately not ``"`` — it is meant for a ``<script>`` block or a
    single-quoted attribute. Inside a double-quoted one, its own JSON string
    delimiters close the attribute early.

    The bug this pins shipped on the product page: ``x-data="{ … images:
    {{ gallery_urls | tojson }} … }"`` meant the browser read the attribute as
    ``{ images: [`` and treated the remainder as stray attributes. Alpine got a
    syntax error, ``images`` never existed, and the ``<template x-if>`` holding
    the ``<img>`` never rendered — every product showed an empty white box. The
    markup was correct, the data was correct, and nothing anywhere failed.

    The fix is ``| forceescape`` after ``tojson``, which escapes the quotes to
    ``&#34;`` for the parser to decode back.
    """
    offenders: dict[str, list[str]] = {}
    pattern = re.compile(r'="[^"]*\{\{[^}]*\|\s*tojson\s*\}\}', re.S)

    for path in _template_files():
        source = path.read_text(encoding="utf-8")
        for match in pattern.finditer(source):
            if "forceescape" not in match.group(0):
                offenders.setdefault(str(path.relative_to(TEMPLATES)), []).append(
                    match.group(0)[:60]
                )

    assert not offenders, (
        "tojson inside a double-quoted attribute without forceescape — the "
        f"attribute will be truncated at the first JSON quote: {offenders}"
    )


def test_the_product_gallery_attribute_survives_an_html_parser():
    """The end-to-end version of the check above, on the template that broke:
    parse the attribute back out the way a browser would and require the whole
    expression to still be there."""
    from html.parser import HTMLParser

    source = (TEMPLATES / "storefront" / "product.html").read_text(encoding="utf-8")
    # Stand in for the render: one image URL, as `tojson | forceescape` emits it.
    rendered = source.replace(
        "{{ gallery_urls | tojson | forceescape }}", "[&#34;/media/a.jpg&#34;]"
    )
    block = re.search(r'<div x-data="\{.*?\}"', rendered, re.S)
    assert block, "the gallery x-data block is no longer recognisable"

    class Grab(HTMLParser):
        value = None

        def handle_starttag(self, tag, attrs):
            if self.value is None:
                self.value = dict(attrs).get("x-data")

    parser = Grab()
    parser.feed(block.group(0) + ">")
    assert parser.value and "images:" in parser.value
    assert parser.value.rstrip().endswith("}"), (
        "the attribute was truncated by the parser — the gallery will not render"
    )
