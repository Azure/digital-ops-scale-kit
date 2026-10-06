#!/usr/bin/env python3
"""Summarize native ADO job outcomes without credentials or child-run polling."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

CHECKS = {
    "preview": "PREVIEW_RESULT",
    "validate_selector": "SELECTOR_RESULT",
    "validate_site_file": "SITE_FILE_RESULT",
}
RESULTS = {"Succeeded", "SucceededWithIssues", "Failed", "Canceled", "Skipped"}
MAX_RECEIPT = 1024 * 1024
NOT_EXERCISED = [
    "verified-release-installation", "separate-repository-checkout",
    "executable-planning", "azure-deployment", "wif-renewal",
]


class QualificationError(Exception):
    """A fixed diagnostic safe for qualification logs."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise QualificationError("The preview receipt contains duplicate fields.")
        result[key] = value
    return result


def _preview_cases(path: Path, commit: str) -> list[dict]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_RECEIPT + 1)
    if len(raw) > MAX_RECEIPT:
        raise QualificationError("The preview receipt is too large.")
    document = json.loads(raw, object_pairs_hook=_unique_object)
    if (not isinstance(document, dict)
            or set(document) != {"sourceCommit", "status", "expectedCases", "cases"}
            or document["sourceCommit"] != commit or document["status"] != "passed"):
        raise QualificationError("The preview receipt does not qualify this source.")
    expected, cases = document["expectedCases"], document["cases"]
    if (not isinstance(expected, list) or not expected
            or any(not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,99}", name)
                   for name in expected)
            or len(expected) != len(set(expected))
            or not isinstance(cases, list) or len(cases) != len(expected)):
        raise QualificationError("The preview receipt has an incomplete case inventory.")
    for name, case in zip(expected, cases, strict=True):
        if (not isinstance(case, dict)
                or set(case) != {"case", "status", "inputSha256", "expandedSha256"}
                or case["case"] != name or case["status"] != "passed"
                or any(not isinstance(case[field], str)
                       or not re.fullmatch(r"[0-9a-f]{64}", case[field])
                       for field in ("inputSha256", "expandedSha256"))):
            raise QualificationError("The preview receipt has an invalid case result.")
    return cases


def summarize(commit: str, results: dict[str, str], preview: Path, preparation: str) -> dict:
    """Require every native job and the exact-source preview receipt to pass."""
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise QualificationError("Qualification requires the exact full source commit.")
    if not isinstance(preparation, str) or preparation not in RESULTS | {""}:
        raise QualificationError("Qualification report preparation is unsupported.")
    if not isinstance(results, dict) or set(results) != set(CHECKS) or any(
        not isinstance(value, str) or value not in RESULTS | {""} for value in results.values()
    ):
        raise QualificationError("Qualification job results are unsupported.")
    checks = [{"case": name, "result": results[name] or "Missing"} for name in CHECKS]
    cases, diagnostics = [], []
    if preparation != "Succeeded":
        diagnostics.append("report-preparation-failed")
    elif results["preview"] == "Succeeded":
        try:
            cases = _preview_cases(preview, commit)
        except (QualificationError, OSError, ValueError, RecursionError):
            diagnostics.append("preview-receipt-invalid")
    passed = all(result == "Succeeded" for result in results.values()) and not diagnostics
    return {
        "sourceCommit": commit,
        "status": "passed" if passed else "failed",
        "engineSelection": {"kind": "source-checkout", "sourceCommit": commit},
        "checks": checks,
        "reportPreparation": preparation or "Missing",
        "templatePreviews": cases,
        "diagnostics": diagnostics,
        "notExercised": NOT_EXERCISED,
    }


def render_markdown(report: dict) -> str:
    """Render only the fixed labels and admitted receipt fields."""
    lines = [
        "# ADO maintainer qualification", "",
        f"**Result: {report['status']}**", "",
        f"Source: `{report['sourceCommit']}`", "",
        "Selected consumer engine route: ordinary noneditable source installation.", "",
        "| Check | Native result |", "|---|---|",
        *(f"| {check['case']} | {check['result']} |" for check in report["checks"]),
        f"| report_preparation | {report['reportPreparation']} |",
        "", f"Completed template previews: {len(report['templatePreviews'])}.",
    ]
    if "preview-receipt-invalid" in report["diagnostics"]:
        lines += ["", "The preview receipt is missing, invalid or from another source."]
    if "report-preparation-failed" in report["diagnostics"]:
        lines += ["", "The reporting job did not complete its preparation successfully."]
    lines += [
        "", "Not exercised by this run: verified-release installation, separate-repository",
        "checkout, executable planning, Azure deployment and WIF renewal.", "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="New directory for safe reports.")
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise QualificationError("Choose a new qualification report directory.")
        report = summarize(
            os.environ["BUILD_SOURCEVERSION"],
            {name: os.environ.get(variable, "") for name, variable in CHECKS.items()},
            args.preview, os.environ.get("REPORT_PREPARATION_RESULT", ""),
        )
        args.output.mkdir(mode=0o700)
        with (args.output / "qualification.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        summary = args.output / "qualification.md"
        with summary.open("x", encoding="utf-8") as stream:
            stream.write(render_markdown(report))
    except (QualificationError, KeyError, OSError):
        print("Qualification reporting failed. Check the source, native results and new output path.",
              file=sys.stderr)
        return 1
    summary_path = str(summary.resolve()).replace("%", "%AZP25").replace("\r", "%0D").replace("\n", "%0A")
    print("##vso[task.setvariable variable=ADO_QUALIFICATION_REPORT_READY]true")
    print(f"##vso[task.uploadsummary]{summary_path}")
    print(f"ADO maintainer qualification {report['status']}.")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
