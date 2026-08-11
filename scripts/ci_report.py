#!/usr/bin/env python3
"""Turn pytest's JUnit + coverage XML reports into a CI summary and badge data.

Reads ``reports/junit.xml`` and ``reports/coverage.xml`` (produced by
``make test-cov``) and writes:

* a Markdown summary on stdout (fed to ``$GITHUB_STEP_SUMMARY``),
* ``badges/tests.json`` and ``badges/coverage.json`` in the shields.io
  "endpoint" format, published to the ``badges`` branch by the CI workflow.
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPORTS = Path("reports")
BADGES = Path("badges")


def _color(pct: float, good: float, ok: float) -> str:
    if pct >= good:
        return "brightgreen"
    if pct >= ok:
        return "yellow"
    return "red"


def _write_badge(name: str, label: str, message: str, color: str) -> None:
    BADGES.mkdir(exist_ok=True)
    payload = {"schemaVersion": 1, "label": label, "message": message, "color": color}
    tmp = BADGES / f".{name}.json.tmp"
    tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    tmp.rename(BADGES / f"{name}.json")


def read_tests() -> tuple[int, int, int, int, float]:
    """Return (total, failures, errors, skipped, pass_rate_percent)."""
    root = ET.parse(REPORTS / "junit.xml").getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = sum(int(s.get("tests", 0)) for s in suites)
    failures = sum(int(s.get("failures", 0)) for s in suites)
    errors = sum(int(s.get("errors", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    ran = total - skipped
    passed = ran - failures - errors
    rate = (passed / ran * 100) if ran else 0.0
    return total, failures, errors, skipped, rate


def read_coverage() -> float:
    root = ET.parse(REPORTS / "coverage.xml").getroot()
    return float(root.get("line-rate", 0.0)) * 100


def main() -> int:
    total, failures, errors, skipped, rate = read_tests()
    coverage = read_coverage()
    passed = total - skipped - failures - errors

    _write_badge("tests", "tests", f"{rate:.1f}% ({passed}/{total - skipped})", _color(rate, 100, 90))
    _write_badge("coverage", "coverage", f"{coverage:.1f}%", _color(coverage, 80, 60))

    print("## Test results\n")
    print("| Metric | Value |")
    print("| :--- | ---: |")
    print(f"| Pass rate | **{rate:.1f}%** |")
    print(f"| Passed | {passed} |")
    print(f"| Failed | {failures} |")
    print(f"| Errors | {errors} |")
    print(f"| Skipped | {skipped} |")
    print(f"| Coverage (lines) | **{coverage:.1f}%** |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
