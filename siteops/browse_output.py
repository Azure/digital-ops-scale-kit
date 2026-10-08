# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Private text and JSON projections of content inspection."""

import json
import os
import re
import shlex
import textwrap

from siteops.browse import BrowseResult, ContentEntry, TypedAnswer
from siteops.manifest_selection import explicit_manifest_reference, is_explicit_manifest_path
from siteops.terminal import BULLET
from siteops.terminal import sanitize as _text
from siteops.terminal import wrap as _wrap

_PLAIN_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_ITEM = f"  {BULLET} "
_RESOURCE_LABELS = {"microsoft.kubernetes/connectedclusters": "Arc-cluster"}


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
            lines.extend(_wrap(_text(value), indent=_ITEM, hanging="    "))


def _safe_command_text(value: str) -> bool:
    return _text(value) == value and (
        _command_shell() != "PowerShell"
        or not any(character in value for character in "&|<>%!^")
    )


def _command_target(entry: ContentEntry) -> str | None:
    """Prefer an exact name known to resolve uniquely, else the explicit manifest path."""
    candidates = [explicit_manifest_reference(entry.path)]
    if (
        entry.name_ambiguous is False and not is_explicit_manifest_path(entry.name)
        and not entry.name.startswith("-")
    ):
        candidates.insert(0, entry.name)
    return next((value for value in candidates if _safe_command_text(value)), None)


def _shell_word(value: str) -> str:
    return value if _PLAIN_WORD.fullmatch(value) else _quote(value)


def _placeholder(answer: TypedAnswer) -> str:
    if answer.type == "azureResourceId":
        label = _RESOURCE_LABELS.get((answer.resource_type or "").casefold(), answer.name)
        return f"<{label}-resource-ID>"
    if answer.type == "boolean":
        return "<true-or-false>"
    return f"<{answer.name}>"


def _typed_inputs_line(entry: ContentEntry, target: str | None) -> list[str]:
    typed = entry.typed_inputs
    if typed is None:
        return []
    command = (
        f"`siteops inputs {_shell_word(target)}`" if target is not None
        else "`siteops inputs` with this entry's path"
    )
    if typed.status == "declared":
        text = f"Typed inputs: declared ({typed.count}). Run {command}."
    elif typed.status == "unreadable":
        text = f"Typed inputs: the contract could not be read. Run {command} for details."
    else:
        text = "Typed inputs: none."
    return ["", *_wrap(text, indent="")]


def _next_commands(entry: ContentEntry, target: str, *, project: bool) -> list[str]:
    """Suggest the shortest typed route first, then the route for a configured Site."""
    shell = _command_shell()
    name = _shell_word(target)
    site = "-l " + _quote("name=<site>")
    where = "PowerShell (not Command Prompt)" if shell == "PowerShell" else f"a {shell}"
    lines = ["", f"Next, in {where}:"]
    typed = entry.typed_inputs
    if typed is not None and typed.status != "none":
        lines.append(f"  siteops inputs {name}")
    if typed is not None and typed.status == "declared" and 0 < len(typed.answers) <= 3:
        answers = " ".join(
            "--input " + _quote(f"{answer.name}={_placeholder(answer)}") for answer in typed.answers
        )
        lines.extend((
            f"  siteops plan {name} {answers}",
            f"  siteops deploy {name} {answers}",
            f"For a configured Site, use {site} instead of --input.",
        ))
    else:
        lines.extend((f"  siteops plan {name} {site}", f"  siteops deploy {name} {site}"))
    lines.append(
        "Add the same project and source options to each command." if project
        else "Add the -w option used here, if any, to each command."
    )
    lines.append("Review the plan before deploying. Deploy prepares it again.")
    return lines


def _card(
    entry: ContentEntry, *, local: bool = True, project: str | None = None,
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
    lines.extend(("", "Authored Site input guidance (descriptive only)"))
    if guidance.inputs is None:
        lines.append("  Input guidance is not documented. This does not mean no inputs are required.")
    elif not guidance.inputs:
        lines.append("  The author declares no input guidance for this entry.")
    else:
        for item in guidance.inputs:
            details = [_text(item.type), item.requirement]
            if item.sensitivity != "unknown":
                details.append(item.sensitivity)
            lines.extend(_wrap(
                f"{_text(item.field)} ({', '.join(details)})", indent=_ITEM, hanging="    ",
            ))
            lines.extend(_wrap(_text(item.description), indent="    "))
            if item.default_behavior:
                lines.extend(_wrap(
                    "Default behavior: " + _text(item.default_behavior), indent="    "
                ))
    target = _command_target(entry)
    if local or project is not None:
        if guidance.role != "partial":
            lines.extend(_typed_inputs_line(entry, target))
    elif guidance.role == "standalone":
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
                indent=_ITEM, hanging="    ",
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
    if not local and project is None:
        lines.extend((
            "", "Remote preview only. Deployable workspace content has not been acquired.",
            "Read the pinned guide. Plan and deploy require a complete, reviewed local workspace.",
        ))
    elif project is not None and guidance.role != "standalone":
        lines.extend((
            "", "Use plan or deploy with the same project and source/trust options.",
            "Choose an explicit Site selector and review the executable plan before deployment.",
        ))
    elif guidance.role != "standalone":
        lines.extend((
            "", "Review the source and its composition context before using its path with plan.",
            "This entry is not declared as a standalone operator choice.",
        ))
    elif target is None:
        lines.extend((
            "", "Copyable commands are withheld for paths containing control or shell metacharacters.",
            "Use a safely named entry, or the canonical paths in private JSON.",
        ))
    else:
        lines.extend(_next_commands(entry, target, project=project is not None))
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
        "Private inspection. Package verified. No plan is prepared."
        if verified else "Private inspection. Package not verified. No plan is prepared.",
        "",
    ]
    if result.selected and len(result.entries) == 1:
        lines.extend(_card(result.entries[0], local=local, project=project))
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
            "Inspect with browse NAME, or use the shown path for ambiguous names and partials."
            if local else
            "Inspect with browse NAME and the same --source, --ref and source-relative -w options.",
            "Partials are shown with their paths. Omit --include-partials to hide them."
            if result.partials_included else
            "Use --include-partials to show partials.",
        ))
        if not local and project is None and result.source.revision:
            lines.append("Use --ref with the displayed revision to select the same source snapshot.")
    if result.diagnostics:
        lines.extend(("", "Inspection is incomplete:"))
        for diagnostic in result.diagnostics:
            location = f" ({_text(diagnostic.path)})" if diagnostic.path else ""
            lines.append(f"  {diagnostic.code}: {diagnostic.summary}{location}")
    return "\n".join(lines) + "\n"
