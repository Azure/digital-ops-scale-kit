# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Private text and JSON projections of content inspection."""

import json
import os
import shlex
import textwrap
import unicodedata

from siteops.browse import BrowseResult, ContentEntry
from siteops.manifest_selection import explicit_manifest_reference
from siteops.reporting import _wrap


def _text(value: str) -> str:
    return "".join(
        f"\\u{ord(character):04x}"
        if unicodedata.category(character).startswith("C")
        else character
        for character in value
    )


def _prose(value: str) -> list[str]:
    lines = []
    for paragraph in value.split("\n\n"):
        lines.extend(_wrap(_text(" ".join(paragraph.splitlines()))))
    return lines


def _command_shell() -> str:
    return "PowerShell" if os.name == "nt" else "POSIX shell"


def _quote(value: str) -> str:
    if _command_shell() == "PowerShell":
        return "'" + value.replace("'", "''") + "'"
    return shlex.quote(value)


def serialize_browse_json(result: BrowseResult) -> str:
    """Emit one private document, escaping control and format characters."""
    return json.dumps(result.document(), indent=2, sort_keys=True, allow_nan=False)


def _section(lines: list[str], title: str, values: tuple[str, ...] | None) -> None:
    lines.extend(("", title))
    if values is None:
        lines.append("  Not documented.")
    elif not values:
        lines.append("  The author declares no items. This is not an environment assessment.")
    else:
        for value in values:
            lines.extend(_wrap(_text(value), indent="  - ", hanging="    "))


def _card(
    entry: ContentEntry, workspace: str, *, local: bool = True, project: str | None = None,
) -> list[str]:
    guidance = entry.guidance
    lines = [
        _text(entry.name), f"Path: {_text(entry.path)}",
        f"Role: {guidance.role}. Guidance: {entry.metadata_status}.",
    ]
    if guidance.category:
        lines.append(f"Category: {_text(guidance.category)}")
    if guidance.tags:
        lines.append("Tags: " + ", ".join(_text(tag) for tag in guidance.tags))
    lines.extend(_prose(guidance.outcome or entry.description))
    lines.extend(("", "Authored targeting"))
    if not entry.targeting_known:
        lines.append("  Targeting is not included in the published index.")
    elif entry.selector:
        lines.extend(_wrap("Default selector: " + _text(entry.selector)))
    if entry.sites:
        lines.extend(_wrap("Named Sites: " + ", ".join(_text(site) for site in entry.sites)))
    if entry.targeting_known and not entry.selector and not entry.sites:
        lines.append("  No targets declared. Supply an explicit selector when planning.")
    lines.append("  Targets have not been resolved. A CLI selector replaces manifest targeting.")
    lines.extend(("", "Authored Site input guidance"))
    if guidance.inputs is None:
        lines.append("  Input guidance is not documented. This does not mean no inputs are required.")
    elif not guidance.inputs:
        lines.append("  The author declares no input guidance for this entry.")
    else:
        for item in guidance.inputs:
            lines.extend(_wrap(
                f"{_text(item.field)} ({_text(item.type)}, {item.requirement}, "
                f"sensitivity: {item.sensitivity})",
                indent="  - ", hanging="    ",
            ))
            lines.extend(_wrap(_text(item.description), indent="    "))
            if item.default_behavior:
                lines.extend(_wrap(
                    "Default behavior: " + _text(item.default_behavior), indent="    "
                ))
    if guidance.role == "standalone":
        if local or project is not None:
            lines.extend((
                "", "Input guidance above is descriptive, not the executable input contract.",
            ))
            lines.extend(_wrap(
                "Use `siteops inputs NAME` with this selected workspace or project "
                "for exact typed answers, if the entry declares a contract.",
                indent="",
            ))
        else:
            lines.extend((
                "", "Remote metadata cannot validate typed inputs.",
            ))
            lines.extend(_wrap(
                "Pin an approved workspace or choose reviewed local content before "
                "using `siteops inputs`.",
                indent="",
            ))
    lines.extend(("", "Supplied during deployment"))
    if guidance.supplied is None:
        lines.append("  Step-supplied inputs are not documented.")
    elif not guidance.supplied:
        lines.append("  The author declares no supplied-input guidance.")
    else:
        for item in guidance.supplied:
            lines.extend(_wrap(
                f"{_text(item.step)}.{_text(item.input)}: {_text(item.description)}",
                indent="  - ", hanging="    ",
            ))
    for title, values in (
        ("Prerequisites and permissions", guidance.prerequisites),
        ("Important effects", guidance.effects),
        ("Removal", guidance.removal),
    ):
        _section(lines, title, values)
    lines.extend(("", "Guidance coverage"))
    lines.extend(_prose(guidance.coverage or "Unknown. Missing guidance is not an empty contract."))
    _section(lines, "Read more", guidance.documentation or None)
    shell = _command_shell()
    command_paths_safe = (
        _text(workspace) == workspace and _text(entry.path) == entry.path
        and (
            shell != "PowerShell"
            or not any(character in workspace + entry.path for character in "&|<>%!^")
        )
    )
    if project is not None and guidance.role == "standalone":
        lines.extend((
            "", "Use typed answers where available, or select a configured Site.",
            "Inspect typed answers with `siteops inputs NAME`.",
            "Use plan or deploy with the same project and source/trust options.",
            "For configured Sites, choose an explicit selector and review the executable plan.",
        ))
    elif project is not None:
        lines.extend((
            "", "Use plan or deploy with the same project and source/trust options.",
            "Choose an explicit Site selector and review the executable plan before deployment.",
        ))
    elif not local:
        lines.extend((
            "", "Remote preview only. Deployable workspace content has not been acquired.",
            "Read the pinned guide. Plan and deploy require a complete, reviewed local workspace.",
        ))
    elif guidance.role == "standalone" and command_paths_safe:
        lines.extend((
            "", "Next: use typed answers where available, or select a configured Site.",
            "Inspect typed answers with `siteops inputs NAME`.",
            f"Configured-Site {shell} commands (not Command Prompt):" if shell == "PowerShell"
            else f"Configured-Site {shell} commands:",
            f"  siteops -w {_quote(workspace)} plan "
            f"{_quote(explicit_manifest_reference(entry.path))} -l 'name=<site>'",
            "After review, deploy with the same explicit target. Deployment prepares again.",
            f"  siteops -w {_quote(workspace)} deploy "
            f"{_quote(explicit_manifest_reference(entry.path))} -l 'name=<site>'",
        ))
    elif guidance.role == "standalone":
        lines.extend((
            "", "Copyable commands are withheld for paths containing control or shell metacharacters.",
            "Use a safely named entry/workspace, or the canonical paths in private JSON.",
        ))
    else:
        lines.extend((
            "", "Review the source and its composition context before using its path with plan.",
            "This entry is not declared as a standalone operator choice.",
        ))
    return lines


def render_browse_plain(result: BrowseResult) -> str:
    """Render compact inventory rows or one detailed card without terminal controls."""
    local = result.source is None or result.source.kind == "local"
    project = result.source.project if result.source is not None else None
    verified = result.source is not None and result.source.verification == "verified"
    heading = f"Source: local workspace {_text(result.workspace)}"
    if not local:
        heading = f"Source: {_text(result.source.reference)}"
        if result.source.revision:
            heading += f" @ {_text(result.source.revision)}"
        heading += f"\nWorkspace: {_text(result.workspace) or '(not selected)'}"
        if result.source.index_status:
            heading += f"\nIndex: {_text(result.source.index_status)}"
        if result.source.observation is not None:
            observation = result.source.observation.document()
            origin = "offline cache" if observation["offline"] else observation["origin"]
            heading += f"\nReference: {origin}, observed {_text(observation['observedAt'])}"
            if observation["stale"]:
                heading += " (refresh overdue)"
            elif result.source.observation.refresh_after is not None:
                heading += f"\nRefresh after: {_text(observation['refreshAfter'])}, or use --refresh"
    if project is not None:
        heading += f"\nProject: {_text(project)}"
    lines = [
        heading,
        "Private inspection. Package verified. Preparation and outcomes are not checked."
        if verified else "Private inspection. Package verification, preparation and outcomes are not checked.",
        "",
    ]
    if result.selected and len(result.entries) == 1:
        lines.extend(_card(result.entries[0], result.workspace, local=local, project=project))
    else:
        lines.append(
            f"Deployment content: {len(result.entries)} shown, {result.matched} matches, "
            f"{result.discovered} {'headers read' if local else 'indexed entries'}."
        )
        ambiguous = any(item.code == "lookup.ambiguous" for item in result.diagnostics)
        for entry in result.entries:
            label = entry.guidance.category or entry.guidance.role
            if entry.guidance.category and entry.guidance.role != "standalone":
                label += ", " + entry.guidance.role
            summary = " ".join((entry.guidance.outcome or entry.description).split())
            summary = textwrap.shorten(_text(summary), width=68, placeholder="...")
            lines.append(f"  {_text(entry.name)} [{_text(label)}] {summary}".rstrip())
            if ambiguous or entry.name_ambiguous is not False or entry.guidance.role == "partial":
                lines.append(f"    {_text(explicit_manifest_reference(entry.path))}")
        if not result.entries and not result.diagnostics:
            lines.append(
                "  No matching entries. Use an explicit path for a custom layout."
                if local or project is not None else
                "  No published entries match. Clear filters or select another indexed workspace."
            )
        if result.matched > len(result.entries):
            lines.append("  More matches are available. Omit --limit or narrow the filters.")
        lines.extend((
            "", "Use --search TEXT, --tag TAG or --category CATEGORY to narrow the inventory.",
            "Inspect with browse NAME and the same project and source/trust options."
            if project is not None else
            "Inspect with browse NAME, or use the shown path for ambiguous names and fragments."
            if local else
            "Inspect with browse NAME and the same --source, --ref and source-relative -w options.",
            "Use --include-partials to include declared reusable fragments.",
        ))
        if not local and project is None and result.source.revision:
            lines.append("Use --ref with the displayed revision to select the same source snapshot.")
    if result.diagnostics:
        lines.extend(("", "Inspection is incomplete:"))
        for diagnostic in result.diagnostics:
            location = f" ({_text(diagnostic.path)})" if diagnostic.path else ""
            lines.append(f"  {diagnostic.code}: {diagnostic.summary}{location}")
    return "\n".join(lines) + "\n"
