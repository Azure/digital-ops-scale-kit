# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Render installation notes and a release candidate summary."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from siteops_release_assets import (
    FrozenReleaseAssets,
    native_engine_wheel,
    publication_assets,
)


class RenderingError(ValueError):
    """Candidate presentation metadata is incomplete or invalid."""


def render_notes(
    plan: dict[str, Any],
    authored: str,
    assets: FrozenReleaseAssets,
    *,
    engine_version: str,
    archive_name: str,
    attestation_suffix: str,
    runner_environment: str,
) -> str:
    """Append installation guidance for this release to the authored notes."""
    if runner_environment not in ("github-hosted", "self-hosted"):
        raise RenderingError("The expected provenance runner class is unsupported.")
    source, engine = plan["source"], plan["siteops"]
    native, workspace = publication_assets(plan, assets)
    if assets.source != source:
        raise RenderingError("The release asset inventory describes a different source.")
    repository = source["repository"]
    home = "https://github.com/" + repository
    notes = authored.rstrip() + "\n\n## Install Site Ops\n\n"
    workspace_notes = ""
    if workspace:
        downloads = home + "/releases/download/" + urllib.parse.quote(plan["release"]["tag"], safe="") + "/"
        project_guide = (
            home + "/blob/" + source["commit"] + "/docs/projects.md#run-project-pin"
        )
        workspace_notes = (
            "\n\n## Workspace content\n\n"
            "This release contains complete workspace packages. Each package has a detached "
            "attestation proof containing signed provenance evidence. "
            f"Follow the [workspace pin guidance]({project_guide}) to run "
            "`siteops project pin` with `--release` for this release. Supply the trust "
            "policy and trusted roots independently. The routing descriptor cannot select "
            "them.\n\n"
            + "\n".join(f"- [{asset.name}]({downloads}{asset.name})" for asset in workspace)
            + "\n\nWorkspace qualification used the selected installed engine to check package "
            "compatibility, protected cache use, and guarded catalog loading. It did not compare "
            "executable deployment plans, authorize targets, deploy resources, or evaluate "
            "workload health.\n"
        )
    if not engine["bundle"]:
        tag = engine["releaseTag"]
        if assets.engine is None or assets.engine.tag != tag or native:
            raise RenderingError("The release asset inventory does not match the engine selection.")
        url = home + "/releases/tag/" + urllib.parse.quote(tag, safe="")
        return (
            notes + f"Use [{tag}]({url}) and its installation instructions. "
            "The referenced engine release has its own source commit and native installation assets.\n"
            + workspace_notes
        )

    if assets.engine is not None:
        raise RenderingError("The release asset list cannot render installation guidance.")
    wheel = native_engine_wheel(native).name
    downloads = home + "/releases/download/" + urllib.parse.quote(plan["release"]["tag"], safe="") + "/"
    native_names = {asset.name for asset in native}
    scripts = ("siteops-bootstrap.ps1", "siteops-bootstrap.sh")
    bootstrap_links = (
        " Bootstrap scripts: " + ", ".join(
            f"[{name}]({downloads}{name}) and "
            f"[{name}{attestation_suffix}]({downloads}{name}{attestation_suffix})"
            for name in scripts
        ) + "."
        if all(name in native_names for name in scripts) else ""
    )
    guide = home + "/blob/" + source["commit"] + "/docs/install-siteops.md#install-the-verified-bundle"
    script_guide = (
        home + "/blob/" + source["commit"]
        + "/docs/install-siteops.md#verify-the-bootstrap-script"
    )
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
        "The installation ZIP and standalone wheel each have a detached attestation proof "
        "containing signed provenance evidence. pipx does not automatically verify GitHub "
        "attestations for the online command. "
        "For external verification before extraction and a native installation locked to hashes "
        "from stable private storage, "
        f"follow the [verified installation guide]({guide}). "
        "That path downloads only the ZIP and its detached proof, authenticates the ZIP before extraction, "
        "then installs from the authenticated `pylock.toml` with stock pipx.",
        f"Expected publisher: `{repository}`. Source commit: `{source['commit']}`. "
        f"Source ref: `{source['ref']}`. Use these values with the guide verification policy. "
        "The guide also describes switching between online and locked installations.",
        "For a bootstrap installation, review this release tag, publisher, source commit and "
        "source ref against your approved selection. "
        f"[Verify the versioned script and its detached proof]({script_guide}) "
        "with those identities before running it. HTTPS download alone does not authenticate "
        "the publisher. If the guide's example publisher, source ref, workflows or runner "
        "differ from this release, use this release's reviewed provenance values instead.",
        f"Expected provenance runner class: `{runner_environment}`. "
        "The runner class does not identify a particular pool.",
        "The locked path is qualified with pipx 1.17.2 and its shared pip 26.2.1. "
        "pip support for `pylock.toml` remains experimental.",
        f"Release assets: [{wheel}]({downloads}{wheel}), "
        f"[{wheel}{attestation_suffix}]({downloads}{wheel}{attestation_suffix}), "
        f"[{archive_name}]({downloads}{archive_name}), and "
        f"[{archive_name}{attestation_suffix}]({downloads}{archive_name}{attestation_suffix})."
        + bootstrap_links + " Use these assets instead of the generated source archives.",
        "Installing the CLI does not authenticate to Azure or deploy resources.",
        "Existing local `-w` workspaces and configured-Site fleet selectors remain supported. "
        "`siteops inputs` and explicit typed answers are optional for manifests with a typed "
        "input contract; a project pin selects content, not operator Site configuration.",
    ]
    return notes + "\n\n".join(paragraphs) + "\n" + workspace_notes


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
    """Render the complete candidate summary before publishing any summary text."""
    release, engine = plan["release"], plan["siteops"]
    dry_run = values.get("DRY_RUN") == "true"
    components = {"siteops": "Site Ops only", "content": "Content only", "both": "Both"}
    mode = release["components"]
    expected_mode = "siteops" if release["stream"] == "siteops" else (
        "both" if engine["bundle"] else "content"
    )
    if type(mode) is not str or mode not in components or mode != expected_mode:
        raise RenderingError("The release components disagree with the engine selection.")
    action = "Reuse the matching tag" if values["TAG_EXISTS"] == "true" else "Create the missing tag"
    lines = ["# Release preview (no publication)\n" if dry_run else "# Ready for release approval\n"]
    if dry_run:
        lines.append("Preview only. No tag, GitHub Release, or approval request was created.\n")
    for label, value in (
        ("Release", release["tag"]), ("Title", release["title"]),
        ("Source commit", plan["source"]["commit"]), ("Release file", plan["intent"]["path"]),
        ("Components", components[mode]), ("Stream", release["stream"]),
        ("Content version", release["version"] if mode != "siteops" else "Not included"),
        ("Engine selection", "Build from this commit" if engine["bundle"] else "Use an existing release"),
        ("Prerelease", release["prerelease"]),
        ("Latest", release["latest"]), ("Proposed tag action" if dry_run else "Tag action", action),
        ("Site Ops", engine["releaseTag"] or values["ENGINE_VERSION"]),
    ):
        lines.append(f"- {label}: `{value}`")
    if plan.get("workspaces"):
        lines.extend([
            "\n## Workspace builds\n",
            "| Workspace | Kit ID | Package | Required Site Ops |",
            "|---|---|---|---|",
        ])
        for request in plan["workspaces"]:
            cells = [
                html.escape(value, quote=False).translate(str.maketrans({
                    character: f"&#{ord(character)};" for character in "\\`*_[]()|~"
                }))
                for value in (
                    request["workspace"], request["id"], request["package"],
                    request["compatibility"]["siteops"],
                )
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.extend([
            "\nEach workspace package has a proof containing signed provenance evidence. "
            "The public routing descriptor routes packages and proofs. "
            "It does not select trust policy or trusted roots.\n",
            "The selected installed engine consumed the frozen workspace packages on every "
            "declared target. Qualification checked package compatibility, protected cache use, "
            "and guarded catalog loading. It did not compare executable deployment plans, "
            "authorize targets, deploy resources, or evaluate workload health.\n",
            f"[Download the complete release payload]({values['ARTIFACT_URL']})\n",
            f"Frozen publication inventory SHA-256: `{values['ASSET_LIST_SHA']}`",
        ])
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
            "The Actions download contains the installation ZIP and standalone wheel. Each has "
            "a detached attestation proof containing signed provenance evidence."
            + (" It also contains the declared workspace assets. " if plan.get("workspaces") else " ")
            +
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
    parser.add_argument(
        "--inventory",
        type=Path,
        metavar="FILE",
        help="Final frozen publication inventory. Defaults to release-assets/release-assets.json under --root.",
    )
    args = parser.parse_args(argv)
    root = args.root
    try:
        plan = json.loads((root / "release-plan" / "plan.json").read_text(encoding="utf-8"))
        if args.mode == "notes":
            authored = (root / "release-plan" / "release-notes.md").read_text(encoding="utf-8")
            assets = FrozenReleaseAssets.read(args.inventory or root / "release-assets" / "release-assets.json")
            raw = render_notes(
                plan, authored, assets, engine_version=os.environ.get("ENGINE_VERSION", ""),
                archive_name=os.environ["ARCHIVE_NAME"], attestation_suffix=os.environ["ATTESTATION_SUFFIX"],
                runner_environment=os.environ["EXPECTED_RUNNER_ENVIRONMENT"],
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
