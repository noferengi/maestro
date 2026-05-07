"""Parse pytest output into structured test evidence.

Extracts total/passed/failed/skipped counts and coverage percentage
from raw pytest --tb=short -q output (with or without pytest-cov).
"""

from __future__ import annotations

import re


def parse_pytest_output(output: str) -> dict:
    """Parse pytest output and return structured test evidence.

    Handles both plain pytest (-q) and pytest with pytest-cov output.

    Returns:
        {
            "total": int | None,
            "passed": int | None,
            "failed": int | None,
            "skipped": int | None,
            "error": int | None,
            "coverage_pct": float | None,
            "summary": str | None,          # last line of pytest summary
            "all_passed": bool,
        }
    """
    result = {
        "total": None,
        "passed": None,
        "failed": None,
        "skipped": None,
        "error": None,
        "coverage_pct": None,
        "summary": None,
        "all_passed": False,
    }

    if not output:
        return result

    # ── Summary line patterns ──────────────────────────────────
    # "12 passed, 3 skipped, 2 failed in 1.23s"
    # "12 passed in 1.23s"
    # "2 failed, 12 passed in 1.23s"
    summary_patterns = [
        # Full pattern: N passed, N failed, N skipped, N error in Xs
        re.compile(
            r"(?P<failed>\d+)\s+failed,\s+(?P<passed>\d+)\s+passed"
            r"(?:,\s+(?P<skipped>\d+)\s+skipped)?"
            r"(?:,\s+(?P<error>\d+)\s+error)?",
        ),
        # Short pattern: N passed in Xs
        re.compile(
            r"(?P<passed>\d+)\s+passed"
            r"(?:,\s+(?P<skipped>\d+)\s+skipped)?"
            r"(?:,\s+(?P<failed>\d+)\s+failed)?"
            r"(?:,\s+(?P<error>\d+)\s+error)?",
        ),
    ]

    for pattern in summary_patterns:
        m = pattern.search(output)
        if m:
            result["passed"] = int(m.group("passed"))
            result["failed"] = int(m.group("failed") or 0)
            result["skipped"] = int(m.group("skipped") or 0)
            result["error"] = int(m.group("error") or 0)
            result["total"] = (
                result["passed"]
                + result["failed"]
                + result["skipped"]
                + result["error"]
            )
            result["all_passed"] = (
                result["failed"] == 0 and result["error"] == 0
            )
            break

    # If we found counts but no total explicitly, compute it
    if result["total"] is None:
        total = sum(v or 0 for v in [
            result["passed"], result["failed"],
            result["skipped"], result["error"],
        ])
        if total > 0:
            result["total"] = total
            result["all_passed"] = (result["failed"] == 0 and result["error"] == 0)

    # ── Coverage percentage ────────────────────────────────────
    # "coverage.py: XXXX  YY%"
    # "Name                  Stmts   Miss  Cover"
    # "----------------------------------------"
    # "total                 XXXX      0   100%"
    coverage_patterns = [
        # Total line: "total                 XXXX      0   100%"
        re.compile(r"total\s+\d+\s+\d+\s+(?P<pct>\d+)%"),
        # Coverage line: "coverage.py: 1234  56%"
        re.compile(r"coverage\.py[:\s]+(?:\d+\s+)?(?P<pct>\d+)%"),
    ]

    for pattern in coverage_patterns:
        m = pattern.search(output)
        if m:
            pct = int(m.group("pct"))
            if 0 <= pct <= 100:
                result["coverage_pct"] = float(pct)
                break

    # ── Summary text (last non-empty line before coverage table)
    lines = output.strip().split("\n")
    for line in reversed(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("Name") and not stripped.startswith("-"):
            result["summary"] = stripped
            break

    return result


def format_test_evidence(parsed: dict) -> str:
    """Format parsed test evidence into a human-readable string.

    Used for display in the Stage Journal.
    """
    parts = []

    total = parsed.get("total")
    passed = parsed.get("passed")
    failed = parsed.get("failed")
    skipped = parsed.get("skipped")
    error = parsed.get("error")

    if total is not None:
        status = "PASS" if parsed["all_passed"] else "FAIL"
        parts.append(f"[{status}] {passed}/{total} tests passed")

    if failed and failed > 0:
        parts.append(f"{failed} failed")
    if skipped and skipped > 0:
        parts.append(f"{skipped} skipped")
    if error and error > 0:
        parts.append(f"{error} errors")

    coverage = parsed.get("coverage_pct")
    if coverage is not None:
        parts.append(f"coverage: {coverage:.1f}%")

    return " · ".join(parts) if parts else "no test data"
