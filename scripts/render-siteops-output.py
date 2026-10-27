# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Validate and render publishable Site Ops results without engine imports."""

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

UNAVAILABLE = "Diagnostic details are unavailable."
MISMATCH = (
    "Site Ops engine and template releases do not match. "
    "Use matching engine, setup, templates and helpers."
)
_CODE = re.compile(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*\Z", re.ASCII)
_FIELDS = {"code", "severity", "summary", "guidance", "documentation", "subject", "schemaField", "occurrences"}
_STATUSES = {"succeeded", "failed", "skipped", "not-run", "cancelled", "unknown"}
_COMPOSITION_COUNTS = {
    "selectedSourceCount", "appliedResourceCount", "externalAssertionCount",
    "verifiedReferenceCount", "recordedReferenceCount", "requirementCount",
}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Repeated key.")
        result[key] = value
    return result


def _integer(value):
    if len(value) > 19:
        raise ValueError("Integer is too long.")
    return int(value)


def _reject_constant(value):
    raise ValueError("Non-finite numbers are not permitted.")


def _count(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def _text(value, limit, *, multiline=False):
    if type(value) is not str:
        return False
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        return False
    return (
        bool(value.strip()) and size <= limit
        and not any(
            unicodedata.category(char) in {"Cc", "Cf", "Cs"}
            and not (multiline and char == "\n") for char in value
        )
    )


def _core(document, kind, code):
    if type(code) is not int or not 0 <= code <= 255 or not isinstance(document, dict) or (
        document.get("apiVersion") != "siteops/v1alpha1"
        or document.get("kind") != kind or document.get("projection") != "publishable"
    ):
        return None
    status, summary = document.get("status"), document.get("summary")
    engine = document.get("engine")
    if not isinstance(summary, dict) or not isinstance(engine, dict) or (
        engine.get("name") != "siteops" or not _text(engine.get("version"), 128)
    ):
        return None
    if kind == "ValidationResult":
        count = summary.get("diagnosticCount")
        if (
            type(status) is not str or status not in {"valid", "invalid"} or not _count(count)
            or summary.get("targetResolution") not in ("not-requested", "complete", "incomplete")
            or type(document.get("exitCode")) is not int or document["exitCode"] != code
            or code != (0 if status == "valid" else 1)
            or (code == 0) != (status == "valid")
        ):
            return None
        return f"Validation result: {status}. Diagnostic records: {count}."
    if kind == "DeploymentPlan":
        targets, operations = summary.get("targetCount"), summary.get("operationCount")
        dispositions = summary.get("dispositions")
        if (
            status not in ("planned", "invalid") or not _count(targets) or not _count(operations)
            or type(dispositions) is not dict or set(dispositions) != {"execute", "skip", "blocked"}
            or not all(_count(value) for value in dispositions.values()) or sum(dispositions.values()) != operations
            or document.get("intent") not in ("describe", "executable")
            or type(document.get("executable")) is not bool
            or document["executable"] != (status == "planned" and document["intent"] == "executable")
            or (code == 0) != (status == "planned")
            or not _count(summary.get("diagnosticCount"))
        ):
            return None
        if "composition" in summary:
            composition = summary["composition"]
            if type(composition) is not dict or set(composition) != _COMPOSITION_COUNTS or (
                not all(_count(value) for value in composition.values())
            ):
                return None
        return f"Plan result: {status}. Sites: {targets}. Operations: {operations}."
    if kind == "DeploymentRun":
        interrupted = summary.get("interrupted")
        if (
            type(status) is not str or status not in _STATUSES | {"invalid"}
            or type(document.get("exitCode")) is not int or document["exitCode"] != code
            or type(interrupted) is not bool or (code == 130) != interrupted
            or (not interrupted and (code == 0) != (status in {"succeeded", "skipped"}))
        ):
            return None
        totals = []
        for name in ("sites", "operations"):
            section = summary.get(name)
            if type(section) is not dict or not _count(section.get("total")):
                return None
            counts = section.get("counts")
            if type(counts) is not dict or set(counts) != _STATUSES or (
                not all(_count(value) for value in counts.values()) or sum(counts.values()) != section["total"]
            ):
                return None
            totals.append(section["total"])
        site_counts = summary["sites"]["counts"]
        if status == "invalid":
            if any(summary[name]["counts"][outcome] for name in ("sites", "operations")
                   for outcome in _STATUSES - {"not-run", "skipped"}):
                return None
        else:
            expected = next(
                (outcome for outcome in ("failed", "unknown", "cancelled", "not-run") if site_counts[outcome]),
                "skipped" if not totals[0] or site_counts["skipped"] == totals[0] else "succeeded",
            )
            if interrupted and (expected == "not-run" or not totals[0]):
                expected = "cancelled"
            if status != expected:
                return None
        flag = "yes" if interrupted else "no"
        return f"Status {status}. Sites {totals[0]}. Operations {totals[1]}. Interrupted {flag}."
    return None


def _diagnostics(document):
    diagnostics = document.get("diagnostics", [])
    if not isinstance(diagnostics, list):
        return None
    result = []
    for item in diagnostics:
        if not isinstance(item, dict):
            return None
        if (
            not _text(item.get("code"), 128) or not _CODE.fullmatch(item["code"])
            or type(item.get("severity")) is not str or item["severity"] not in {"error", "warning", "info"}
            or not _text(item.get("summary"), 512)
        ):
            return None
        for field in ("guidance", "documentation", "subject", "schemaField"):
            limit = 128 if field in {"subject", "schemaField"} else 2048
            if field in item and not _text(item[field], limit, multiline=field == "guidance"):
                return None
        if "occurrences" in item and (not _count(item["occurrences"]) or item["occurrences"] < 1):
            return None
        if "documentation" in item:
            try:
                url = urlsplit(item["documentation"])
            except ValueError:
                return None
            if url.scheme != "https" or url.netloc != "github.com" or url.username or url.password:
                return None
        result.append({key: value for key, value in item.items() if key in _FIELDS})
    count = document.get("summary", {}).get("diagnosticCount")
    if count is not None and (not _count(count) or count != len(result)):
        return None
    return result


def _markdown(value):
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", value)


def _failure_groups(document, kind):
    if kind != "DeploymentRun":
        return []
    section = document["summary"]["operations"]
    groups = section.get("failureDiagnostics", [])
    if type(groups) is not list:
        return None
    result, seen = [], set()
    sums = {"failed": 0, "unknown": 0}
    for group in groups:
        if type(group) is not dict or group.get("status") not in ("failed", "unknown"):
            return None
        category, count = group.get("category"), group.get("count")
        code = group.get("azureCode", "")
        if not _text(category, 128) or not _CODE.fullmatch(category) or not _count(count) or count == 0:
            return None
        if type(code) is not str or (
            "azureCode" in group and not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", code, re.ASCII)
        ):
            return None
        if not all(field in group for field in ("summary", "guidance", "documentation")):
            return None
        public = {key: group[key] for key in ("summary", "guidance", "documentation")}
        if _diagnostics({"diagnostics": [{"code": "provider", "severity": "error", **public}]}) is None:
            return None
        key = (group["status"], category, code)
        if key in seen:
            return None
        seen.add(key)
        sums[group["status"]] += count
        result.append({"status": group["status"], "category": category, "count": count, **public,
                       **({"azureCode": code} if code else {})})
    if "failureDiagnostics" in section and any(sums[status] != section["counts"][status] for status in sums):
        return None
    return result


def _annotation(value, platform, severity):
    value = value.replace("%", "%AZP25" if platform == "ado" else "%25").replace("\r", "%0D").replace("\n", "%0A")
    if platform == "ado":
        value = value.replace("]", "%5D").replace(";", "%3B")
        return f"##vso[task.logissue type={'error' if severity == 'error' else 'warning'}]{value}"
    return f"::{'error' if severity == 'error' else 'warning'}::{value}"


def render(document, kind, code, platform):
    try:
        core = _core(document, kind, code)
    except (TypeError, ValueError, RecursionError):
        core = None
    if core is None:
        message = MISMATCH if code == 2 else UNAVAILABLE
        return [_annotation(message, platform, "error")], [message], False
    logs, summary = [core], [core]
    diagnostics = _diagnostics(document)
    groups = _failure_groups(document, kind)
    if diagnostics is None or groups is None:
        logs.append(_annotation(UNAVAILABLE, platform, "warning"))
        summary.append(UNAVAILABLE)
        return logs, summary, True
    records, counts = {}, Counter()
    for diagnostic in diagnostics:
        record = {key: value for key, value in diagnostic.items() if key != "occurrences"}
        key = json.dumps(record, sort_keys=True)
        records.setdefault(key, record)
        counts[key] += diagnostic.get("occurrences", 1)
    for key, item in records.items():
        occurrence = f" ({counts[key]} occurrences)" if counts[key] > 1 else ""
        text = f"{item['code']}{occurrence}: {item['summary']}"
        if "schemaField" in item:
            text += "\nField: " + item["schemaField"] + "."
        if "guidance" in item:
            text += "\n" + item["guidance"]
        if "documentation" in item:
            text += "\n" + item["documentation"]
        logs.append(_annotation(text, platform, item["severity"]))
        summary.extend("- " + _markdown(line) for line in text.splitlines())
    for group in groups:
        code = f" ({group['azureCode']})" if "azureCode" in group else ""
        noun = "operation" if group["count"] == 1 else "operations"
        text = f"{group['status']}: {group['category']}{code}, {group['count']} {noun}\n{group['summary']}"
        text += "\n" + group["guidance"] + "\n" + group["documentation"]
        logs.append(_annotation(text, platform, "error" if group["status"] == "failed" else "warning"))
        summary.extend("- " + _markdown(line) for line in text.splitlines())
    return logs, summary, True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    parser.add_argument("--kind", choices=("ValidationResult", "DeploymentPlan", "DeploymentRun"), required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--platform", choices=("ado", "github"), required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--require-executable", action="store_true")
    args = parser.parse_args()
    try:
        with args.result.open("rb") as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Result exceeds its bound.")
        document = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs, parse_int=_integer,
            parse_constant=_reject_constant,
        )
    except (OSError, ValueError, UnicodeError, RecursionError):
        document = None
    if args.require_executable and (
        type(document) is not dict or document.get("intent") != "executable"
    ):
        document = None
    try:
        logs, summary, valid = render(document, args.kind, args.exit_code, args.platform)
        if args.summary is not None:
            with args.summary.open("a", encoding="utf-8") as stream:
                stream.write("\n".join(summary) + "\n")
        print("\n".join(logs))
    except (OSError, ValueError, TypeError, RecursionError):
        print(_annotation(UNAVAILABLE, args.platform, "error"))
        return 1
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
