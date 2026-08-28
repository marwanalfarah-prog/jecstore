"""Keep the requirements coverage matrix aligned with the source spec."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "JEC_Store_Requirements.md"
COVERAGE = ROOT / "REQUIREMENTS_COVERAGE.md"


def _top_level_requirement_sections() -> list[str]:
    text = SPEC.read_text(encoding="utf-8")
    headings = re.findall(r"^## (.+)$", text, flags=re.MULTILINE)
    return [
        heading.strip()
        for heading in headings
        if heading.strip() != "Website Requirements Specification (Consolidated)"
    ]


def _coverage_section(text: str, heading: str) -> str:
    match = re.search(
        rf"^## {re.escape(heading)}\n(?P<body>.*?)(?=^## |\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group("body") if match else ""


def test_every_top_level_requirements_section_is_tracked() -> None:
    coverage = COVERAGE.read_text(encoding="utf-8")

    missing = [
        heading
        for heading in _top_level_requirement_sections()
        if f"## {heading}" not in coverage
    ]

    assert not missing, (
        "REQUIREMENTS_COVERAGE.md must track every top-level section from "
        f"JEC_Store_Requirements.md. Missing: {missing}"
    )


def test_each_tracked_section_has_status_evidence_and_remaining_work() -> None:
    coverage = COVERAGE.read_text(encoding="utf-8")

    incomplete = []
    for heading in _top_level_requirement_sections():
        body = _coverage_section(coverage, heading)
        missing_labels = [
            label for label in ("Status:", "Evidence:", "Remaining:") if label not in body
        ]
        if missing_labels:
            incomplete.append((heading, missing_labels))

    assert not incomplete, (
        "Every tracked requirements section needs Status, Evidence, and "
        f"Remaining labels. Incomplete: {incomplete}"
    )
