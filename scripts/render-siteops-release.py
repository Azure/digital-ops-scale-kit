# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Render installation notes and an approval preview from a verified candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class RenderingError(ValueError):
    """Candidate presentation metadata is incomplete or invalid."""


def render_notes(
    plan: dict[str, Any],
    authored: str,
    assets: dict[str, Any],
    *,
    engine_version: str,
    archive_name: str,
    attestation_suffix: str,
) -> str:
    """Append release-specific installation guidance to the authored notes."""
    source, engine = plan["source"], plan["siteops"]
    repository = source["repository"]
    home = "https://github.com/" + repository
    notes = authored.rstrip() + "\n\n## Install Site Ops\n\n"
    if not engine["bundle"]:
        tag = engine["releaseTag"]
        url = home + "/releases/tag/" + urllib.parse.quote(tag, safe="")
        return (
            notes + f"Use [{tag}]({url}) and its installation instructions. "
            "The referenced engine release has its own source commit and native installation assets.\n"
        )

    wheels = [item["name"] for item in assets["assets"] if item["name"].endswith(".whl")]
    if assets.get("mode") != "publish" or len(wheels) != 1:
        raise RenderingError("The release asset list cannot render installation guidance.")
    wheel = wheels[0]
    downloads = home + "/releases/download/" + urllib.parse.quote(plan["release"]["tag"], safe="") + "/"
    guide = home + "/blob/" + source["commit"] + "/docs/install-siteops.md#install-the-verified-bundle"
    command = (
        f'pipx install "{downloads}{wheel}" --backend pip --fetch-python never '
        '--skip-maintenance --app siteops --pip-args "--only-binary=:all: --no-cache-dir"'
    )
    paragraphs = [
        f"Package version: `{engine_version}`.",
        "**Prerequisites:** standard 64-bit CPython 3.10-3.14 and pipx 1.17.2. "
        "Use Windows x64 or Linux x64 with glibc 2.17 or newer. "
        "Configure an approved package index that serves the runtime dependencies as wheels.",
        "Install the versioned wheel from this release. Runtime dependencies come from your configured package index as wheels. "
        "To name it explicitly, add `--index-url <your approved index>` inside `--pip-args`.",
        f"```console\n{command}\n```",
        "To replace an existing online installation, review any pipx pin and rerun the command with `--force`. "
        "Confirm the result with `siteops --version` and `siteops --help`.",
        "pipx does not automatically verify GitHub attestations for the online command. "
        "For external verification before extraction and a hash-locked native install from stable private storage, "
        f"follow the [verified installation guide]({guide}). "
        "That path downloads only the ZIP and its detached proof, authenticates the ZIP before extraction, "
        "then installs from the authenticated `pylock.toml` with stock pipx.",
        f"Expected publisher: `{repository}`. Source commit: `{source['commit']}`. "
        f"Source ref: `{source['ref']}`. Use these values with the guide verification policy. "
        "The guide also describes switching between online and locked installations.",
        "The locked path is qualified with pipx 1.17.2 and its shared pip 26.2.1. "
        "pip support for `pylock.toml` remains experimental.",
        f"Release assets: [{wheel}]({downloads}{wheel}), "
        f"[{wheel}{attestation_suffix}]({downloads}{wheel}{attestation_suffix}), "
        f"[{archive_name}]({downloads}{archive_name}), and "
        f"[{archive_name}{attestation_suffix}]({downloads}{archive_name}{attestation_suffix}). "
        "Use these assets instead of the generated source archives.",
        "Installing the CLI does not authenticate to Azure or deploy resources.",
    ]
    return notes + "\n\n".join(paragraphs) + "\n"


def embedded_notes(text: str) -> str:
    """Nest Markdown headings below the summary without changing code fences."""
    lines = text.splitlines()
    headings = {}
    omitted = set()
    fence = None
    previous_text = False
    for index, line in enumerate(lines):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                fence = None
            previous_text = False
            continue
        if marker:
            fence = (marker[1][0], len(marker[1]))
            previous_text = False
            continue
        heading = re.match(r"^( {0,3})(#{1,6})(?=[ \t]|$)(.*)$", line)
        if heading:
            headings[index] = (len(heading[2]), heading[1], heading[3])
        elif previous_text and re.fullmatch(r" {0,3}(=+|-+)[ \t]*", line):
            headings[index - 1] = (
                1 if line.lstrip().startswith("=") else 2, "", " " + lines[index - 1].strip(),
            )
            omitted.add(index)
        previous_text = bool(
            line.strip() and not heading
            and not re.match(r"^( {4}|\t| {0,3}(?:>|<|[-+*] |[0-9]+[.)] ))", line)
            and not re.fullmatch(r" {0,3}(?:=+|-+|(?:[-*_][ \t]*){3,})[ \t]*", line)
            and index not in omitted
        )
    shift = max(0, 3 - min((value[0] for value in headings.values()), default=3))
    for index, (level, indent, content) in headings.items():
        lines[index] = indent + "#" * min(6, level + shift) + content
    return "\n".join(line for index, line in enumerate(lines) if index not in omitted)


def render_summary(plan: dict[str, Any], notes: str, values: Mapping[str, str]) -> str:
    """Render the complete approval preview before publishing any summary text."""
    release, engine = plan["release"], plan["siteops"]
    dry_run = values.get("DRY_RUN") == "true"
    action = "Reuse the matching tag" if values["TAG_EXISTS"] == "true" else "Create the missing tag"
    lines = ["# Release preview (no publication)\n" if dry_run else "# Ready for release approval\n"]
    if dry_run:
        lines.append("Preview only. No tag, GitHub Release, or approval request was created.\n")
    for label, value in (
        ("Release", release["tag"]), ("Title", release["title"]),
        ("Source commit", plan["source"]["commit"]), ("Release file", plan["intent"]["path"]),
        ("Stream", release["stream"]), ("Prerelease", release["prerelease"]),
        ("Latest", release["latest"]), ("Proposed tag action" if dry_run else "Tag action", action),
        ("Site Ops", engine["releaseTag"] or values["ENGINE_VERSION"]),
    ):
        lines.append(f"- {label}: `{value}`")
    if engine["bundle"]:
        matrix = json.loads(values["MATRIX"])
        expected = ["3.10", "3.11", "3.12", "3.13", "3.14"]
        if (
            not isinstance(matrix, list)
            or any(not isinstance(row, dict) for row in matrix)
            or [row.get("python") for row in matrix] != expected
        ):
            raise RenderingError("Incomplete installation qualification summary.")
        lines.extend(["\n## Installation checks\n", "| Python | Linux | Windows |", "|---|---|---|"])
        allowed = {"passed", "failed", "cancelled", "skipped", "not-run", "unknown"}
        for row in matrix:
            if row.get("linux") not in allowed or row.get("windows") not in allowed:
                raise RenderingError("Invalid qualification summary.")
            lines.append(f"| {row['python']} | {row['linux']} | {row['windows']} |")
        lines.extend([
            f"\n[Download the attested release assets]({values['ARTIFACT_URL']})\n",
            "The Actions download contains the installation ZIP, standalone wheel, and a detached proof for each. "
            "For a verified installation, use the ZIP and its proof.\n",
            "<details><summary>Artifact identity</summary>\n",
            f"- `{values['ARCHIVE_NAME']}` SHA-256: `{values['BUNDLE_SHA']}`",
            f"- `{values['WHEEL_NAME']}` SHA-256: `{values['WHEEL_SHA']}`",
            f"- Frozen asset list SHA-256: `{values['ASSET_LIST_SHA']}`\n\n</details>",
        ])
    lines.extend([
        "\nCI passed for this candidate. Installation qualification passed when a bundle is included.",
        "CI evidence: " + values["CI_URL"],
        "For content releases, confirm the applicable content/AIO evidence before approving.",
    ])
    if dry_run:
        lines.append("Next: review this preview. A declaration merged under releases/ starts the real approval flow.")
    else:
        lines.extend([
            "Next: select Review deployments, then siteops-release, then Approve and deploy.",
            "Approval authorizes the listed tag action at this commit and publication of these release assets.",
        ])
    if engine["bundle"]:
        lines.append(
            "\nThe installation guidance below is for the published release. To try a candidate before publication, "
            "use the Actions download above and the installation guide for manually downloaded files.",
        )
    lines.append("\n## Release notes\n\n" + embedded_notes(notes))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("notes", "summary"))
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root
    try:
        plan = json.loads((root / "release-plan" / "plan.json").read_text(encoding="utf-8"))
        if args.mode == "notes":
            authored = (root / "release-plan" / "release-notes.md").read_text(encoding="utf-8")
            assets = json.loads((root / "release-assets" / "release-assets.json").read_text(encoding="utf-8"))
            raw = render_notes(
                plan, authored, assets, engine_version=os.environ.get("ENGINE_VERSION", ""),
                archive_name=os.environ["ARCHIVE_NAME"], attestation_suffix=os.environ["ATTESTATION_SUFFIX"],
            ).encode("utf-8")
            (root / "publish-notes.md").write_bytes(raw)
            print("sha256=" + hashlib.sha256(raw).hexdigest())
        else:
            notes = (root / "publish-notes.md").read_text(encoding="utf-8")
            summary = render_summary(plan, notes, os.environ)
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
                output.write(summary)
    except RenderingError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError):
        print("::error::Release rendering requires readable candidate files and complete metadata.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
