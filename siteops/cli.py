# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Command-line interface for Azure Site Ops.

Commands:
    browse   - Discover and inspect deployment content
    index    - Build approved public descriptions and source bindings
    sites    - Inspect sites as plain text, YAML, or JSON
    inputs   - Inspect typed answers for a selected deployment
    validate - Validate manifest structure and references
    plan     - Prepare and preflight a deployment plan
    deploy   - Deploy a manifest to target sites

Global flags:
    -v/--verbose controls log verbosity only. Use `plan` to prepare a
    deployment plan.
"""

import argparse
import json
import logging
import os
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path
from types import FrameType
from typing import Any, Callable

import yaml

from siteops import __version__
from siteops.arm_resources import ArmResourceError, new_arm_reader
from siteops.artifacts import ArtifactError
from siteops.browse import (
    BrowseError,
    BrowseResult,
    BrowseSource,
    ContentReader,
    inspect_content,
    validate_browse_options,
)
from siteops.browse_output import _text as _content_text
from siteops.browse_output import render_browse_plain, serialize_browse_json
from siteops.command_context import open_command_context, require_trust_inputs
from siteops.composition import CompositionError, report_composition_error
from siteops.guided_inputs import (
    GuidedInputError,
    InputContract,
    ResourceInputError,
    load_contract,
    load_direct_site,
    write_yaml_exclusive,
)
from siteops.manifest_selection import (
    ManifestSelectionError,
    explicit_manifest_reference,
    is_explicit_manifest_path,
    select_manifest_path,
)
from siteops.models import (
    MultipleSubscriptionSitesError,
    NoTargetingError,
    ParameterSelectionError,
    Site,
    _merge_selector_strings,
)
from siteops.orchestrator import Orchestrator
from siteops.planning import (
    DiagnosticSeverity,
    PlanBuildResult,
    PlanDiagnostic,
    PlanIntent,
    PlanNotExecutableError,
    PlanProjection,
    PlanStatus,
    render_plain_plan,
    serialize_plan_json,
)
from siteops.project import (
    PIN_NAME,
    ProjectError,
    WorkspacePin,
    pin_exists,
    project_root,
    read_pin,
    require_separate_cache,
    write_pin,
)
from siteops.reporting import (
    TextProgressReporter,
    _wrap,
    render_plain_run,
    serialize_run_json,
)
from siteops.results import RunResult, preparation_failure_result
from siteops.sanitize import (
    is_redaction_enabled,
    report_parameter_selection_error,
    report_site_load_error,
)


def setup_logging(verbose: bool = False) -> None:
    """Configure logging based on verbosity level."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
    )
    if not verbose:
        logging.getLogger("siteops.executor").setLevel(logging.WARNING)


def resolve_manifest_path(manifest: str | Path, workspace: Path) -> Path:
    """Resolve an exact name, root filename, or trusted explicit local path."""
    if is_explicit_manifest_path(manifest):
        path = Path(str(manifest).replace("\\", "/"))
        return path if path.is_absolute() else workspace / path
    reader = ContentReader(workspace)
    entries = reader.inventory()
    selection = str(manifest)
    path = select_manifest_path(
        selection,
        ((entry.name, entry.path) for entry in entries if entry.guidance.role != "partial"),
        names_complete=reader.names_complete,
        filename_match=reader.filename_candidate(selection),
    )
    return reader.workspace / path


def _command_manifest(args: argparse.Namespace) -> Path | None:
    try:
        binding = getattr(args, "package_binding", None)
        if binding is not None:
            path = binding.require_manifest(binding.manifest_path)
            if not is_redaction_enabled():
                print(f"Manifest: {_content_text(binding.manifest_relative_path)}", file=sys.stderr)
            return path
        path = resolve_manifest_path(args.manifest, args.workspace)
        if not path.is_file():
            raise ManifestSelectionError(
                "lookup.missing", "Manifest not found.", (str(path),)
            )
    except (ManifestSelectionError, BrowseError) as error:
        print(f"Error: {error}", file=sys.stderr)
        if isinstance(error, ManifestSelectionError) and error.paths and not is_redaction_enabled():
            print("Explicit paths:", file=sys.stderr)
            for choice in error.paths:
                print(f"  {_content_text(explicit_manifest_reference(choice))}", file=sys.stderr)
        return None
    if isinstance(args.manifest, str) and not is_explicit_manifest_path(args.manifest):
        if not is_redaction_enabled():
            print(f"Manifest: {_content_text(str(path))}", file=sys.stderr)
    return path


def _context_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "workspace": args.workspace,
        "project": getattr(args, "project", None),
        "command": args.command,
        "policy": getattr(args, "trust_policy", None),
        "trusted_root": getattr(args, "trusted_root", None),
        "approved_source": getattr(args, "approved_source", None),
        "offline": getattr(args, "offline", False),
        "discover": _auto_discover_workspace,
    }


def cmd_project(args: argparse.Namespace) -> int:
    """Inspect or explicitly change a project's complete workspace selection."""
    if is_redaction_enabled():
        print("Project source details are private. Use SITEOPS_REDACT_OUTPUT=0 for an authorized destination.",
              file=sys.stderr)
        return 1
    try:
        if args.project is not None or args.workspace is not None:
            raise ProjectError("Project commands take their target directory as a positional argument.")
        if args.project_command == "show":
            if args.trust_policy is not None or args.trusted_root is not None or args.approved_source is not None:
                raise ProjectError("Project show reads a selection without applying trust options.")
            root = project_root(args.directory)
            pin = read_pin(root).pin
        else:
            from siteops.github_source import GitHubClient, GitHubReference
            from siteops.github_workspace_acquisition import GitHubWorkspaceAcquirer
            from siteops.source_profiles import read_source
            from siteops.workspace_cache import WorkspaceCache, default_cache_root

            profile = read_source(args.approved_source) if args.approved_source is not None else None
            selected_source = args.source if args.source is not None else profile.reference if profile else None
            if selected_source is None:
                raise ProjectError("Project pin requires --source or --approved-source.")
            reference = GitHubReference.parse(selected_source, ref=args.release)
            if reference.ref is None:
                raise ProjectError("Project pin requires an explicit published release with --release.")
            cache_root = default_cache_root()
            policy, trusted_root = require_trust_inputs(
                cache_root, args.trust_policy, args.trusted_root,
                approved_source=args.approved_source,
                source_reference=f"github:{reference.owner}/{reference.repository}",
            )
            require_separate_cache(Path(args.directory).absolute(), cache_root)
            root = project_root(args.directory, create=True)
            require_separate_cache(root, cache_root)
            previous = read_pin(root) if pin_exists(root) else None
            cache = WorkspaceCache(cache_root)
            print(
                "Resolving and verifying the selected workspace release; "
                "fetching missing package bytes may take time.",
                file=sys.stderr, flush=True,
            )
            acquired = GitHubWorkspaceAcquirer(
                cache, policy_file=policy, trusted_root=trusted_root,
            ).acquire(GitHubClient(reference), workspace=args.release_workspace)
            pin = WorkspacePin(acquired.resolved)
            write_pin(root, pin, expected_previous=previous.sha256 if previous else None)
        if args.output == "json":
            print(pin.serialized().decode("ascii"), end="")
        else:
            selection = pin.selection
            print(f"Project: {_content_text(str(root))}")
            print(f"Pin: {PIN_NAME}")
            print(f"Source: {_content_text(selection.source.reference)} @ {_content_text(selection.source.release)}")
            print(f"Workspace: {_content_text(selection.entry.workspace)}")
            print(f"Kit: {_content_text(selection.entry.kit_id)} {_content_text(selection.entry.kit_version)}")
            print(f"Revision: {_content_text(selection.source.revision)}")
            print(f"Package SHA-256: {selection.entry.package.sha256}")
            print("The pin records selection. Package use revalidates current consumer policy.")
        return 0
    except (ArtifactError, BrowseError) as error:
        print(f"Error: {error}", file=sys.stderr)
        for choice in getattr(error, "choices", ()):
            print(f"Workspace choice: {_content_text(choice)}", file=sys.stderr)
        return 1


def cmd_source(args: argparse.Namespace) -> int:
    """Manage consumer-approved source trust independently of projects."""
    from siteops.github_attestation import load_github_policy
    from siteops.source_profiles import enroll_source, list_sources, read_source, remove_source

    redacted = is_redaction_enabled()
    if redacted and args.source_command in {"show", "list"}:
        print("Source approval details are private. Use an authorized private destination.", file=sys.stderr)
        return 1
    try:
        if args.project is not None or args.workspace is not None or args.approved_source is not None:
            raise ProjectError("Source enrollment is user configuration, not a project or workspace selection.")
        if args.source_command == "enroll":
            if args.trust_policy is None or args.trusted_root is None:
                raise ProjectError("Source enrollment requires --trust-policy and --trusted-root.")
            from siteops.workspace_cache import default_cache_root

            policy_file, root_file = require_trust_inputs(
                default_cache_root(), args.trust_policy, args.trusted_root,
            )
            result = enroll_source(args.name, args.source, policy_file, root_file)
            if redacted:
                print("Approved source enrolled.")
            else:
                print(f"Approved source {_content_text(result.name)}: {_content_text(result.reference)}.")
            return 0
        if args.trust_policy is not None or args.trusted_root is not None:
            raise ProjectError("Trust file options apply to source enroll, not inspection or removal.")
        if args.source_command == "list":
            for name in list_sources():
                print(_content_text(name))
        elif args.source_command == "show":
            result = read_source(args.name, require_valid=False)
            policy = load_github_policy(result.policy)
            print(f"Approved source: {_content_text(result.name)}.")
            print(f"Publisher: {_content_text(result.reference)}.")
            print(f"Policy SHA-256: {result.policy_sha256}.")
            print(f"Trusted root SHA-256: {result.root_sha256}.")
            print(f"Signing workflow: {_content_text(policy.signer_workflow)} @ "
                  f"{_content_text(policy.source_ref)}.")
            print(f"Builder: {_content_text(policy.builder_workflow)}. "
                  f"Runner: {policy.runner_environment}.")
            print(f"Valid until: {policy.valid_until.isoformat()}.")
            print("Package use checks current policy validity and artifact provenance.")
        else:
            remove_source(args.name)
            print("Approved source removed." if redacted else
                  f"Removed approved source {_content_text(args.name)}.")
        return 0
    except (ArtifactError, BrowseError) as error:
        print("Error: Approved source operation failed. Check private source configuration."
              if redacted else f"Error: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("Error: The approved source files could not be accessed.", file=sys.stderr)
        return 1


def cmd_cache(args: argparse.Namespace) -> int:
    """Inspect private cached storage or remove one explicitly selected entry."""
    from siteops.cache_management import CacheManagement

    if is_redaction_enabled():
        print("Cache details are private. Use SITEOPS_REDACT_OUTPUT=0 for an authorized destination.",
              file=sys.stderr)
        return 1
    try:
        if any((
            args.project is not None, args.workspace is not None,
            args.trust_policy is not None, args.trusted_root is not None,
            getattr(args, "approved_source", None) is not None,
            args.extra_sites_dirs,
        )):
            raise ProjectError("Cache commands use SITEOPS_CACHE_DIR or the platform default, not project or trust options.")
        cache = CacheManagement()
        if args.cache_command == "list":
            listing = cache.list(kind=args.kind, identity=args.id, limit=args.limit)
            if args.output == "json":
                print(json.dumps(listing.document(), ensure_ascii=True, sort_keys=True, indent=2))
            else:
                print(f"Cache: {_content_text(str(listing.root))}")
                print("Storage inventory only. Integrity and publisher trust are not evaluated.")
                if not listing.initialized:
                    print("The cache has not been initialized.")
                for entry in listing.entries:
                    size = str(entry.stored_bytes) if entry.stored_bytes is not None else "unknown"
                    print(f"{entry.kind} {entry.identity}")
                    print(f"  Storage: {entry.state}. Bytes: {size}.")
                    if entry.issue is not None:
                        print(f"  Issue: {entry.issue}")
                print(f"{len(listing.entries)} shown, {listing.matched} matching entries.")
                if listing.matched > len(listing.entries):
                    print("Use --kind, --id or --limit to inspect the remaining entries.")
            return 1 if any(entry.state == "unavailable" for entry in listing.entries) else 0
        removed = cache.remove(args.kind, args.id)
        if args.output == "json":
            print(json.dumps({
                "apiVersion": "siteops/v1alpha1", "kind": "CacheRemoval",
                "entry": removed.document(),
            }, ensure_ascii=True, sort_keys=True, indent=2))
        else:
            print(f"Removed cached {removed.kind} {removed.identity}. Bytes: {removed.stored_bytes}.")
            print("Workspace pins, Site configuration and trust inputs are unchanged.")
        return 0
    except ArtifactError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        return 1


def cmd_browse(args: argparse.Namespace) -> int:
    """Inspect content before any Site configuration or Orchestrator is loaded."""
    if is_redaction_enabled():
        print(
            "Content inspection output is private. Use SITEOPS_REDACT_OUTPUT=0 "
            "only for an authorized private destination.",
            file=sys.stderr,
        )
        return 1
    try:
        validate_browse_options(args.name, args.search, tuple(args.tag), args.category, args.limit)
        if args.source:
            if (args.project is not None or args.trust_policy is not None
                    or args.trusted_root is not None or args.approved_source is not None):
                raise ProjectError("Choose metadata --source browsing or project content, not both.")
            from siteops.github_catalog import inspect_github

            result = inspect_github(
                args.source, args.name, ref=args.ref, workspace=args.workspace, auth=args.auth,
                search=args.search, tags=tuple(args.tag), category=args.category,
                include_partials=args.include_partials, limit=args.limit,
                refresh=args.refresh, offline=args.offline,
            )
        else:
            if args.ref or args.auth != "anonymous" or args.refresh:
                raise ValueError("--ref, --auth and --refresh apply only to --source.")
            with open_command_context(**_context_options(args)) as context:
                result = inspect_content(
                    context.workspace, args.name, search=args.search, tags=tuple(args.tag),
                    category=args.category, include_partials=args.include_partials, limit=args.limit,
                )
                if context.project is not None:
                    selected = context.pin.selection if context.pin else None
                    result = replace(result, source=BrowseSource(
                        "package" if selected else "local",
                        selected.source.reference if selected else str(context.workspace),
                        revision=selected.source.revision if selected else None,
                        provider=selected.source.provider if selected else None,
                        project=str(context.project),
                        verification="verified" if selected else "not-performed",
                    ))
                    if selected:
                        result = replace(result, workspace=selected.entry.workspace)
                rendered = serialize_browse_json(result) if args.output == "json" else render_browse_plain(result)
            print(rendered, end="\n" if args.output == "json" else "")
            return 1 if result.diagnostics else 0
    except BrowseError as error:
        result = BrowseResult(str(args.workspace or ""), diagnostics=(error.diagnostic,))
    except ArtifactError as error:
        from siteops.browse import BrowseDiagnostic

        result = BrowseResult("", diagnostics=(BrowseDiagnostic(error.code, str(error)),))
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    if args.output == "json":
        print(serialize_browse_json(result))
    else:
        print(render_browse_plain(result), end="")
    return 1 if result.diagnostics else 0


def cmd_index(args: argparse.Namespace) -> int:
    """Build public descriptions without publishing private inspection output."""
    from siteops.content_index import build_content_index, write_content_index

    try:
        if not args.public:
            raise BrowseError("index.approval", "Use --public to approve the authored publication.")
        if (args.project is not None or args.trust_policy is not None
                or args.trusted_root is not None or args.approved_source is not None):
            raise BrowseError("index.project", "Index generation uses local workspace input, not project or trust options.")
        if args.workspace is None and pin_exists(Path.cwd()):
            raise BrowseError("index.project", "Select a local workspace with -w before generating an index from a project.")
        workspace = args.workspace or _auto_discover_workspace(Path.cwd()) or Path.cwd()
        digests = None
        if args.for_source == "github":
            from siteops.github_catalog import github_input_digests

            digests = github_input_digests
        bundle = build_content_index(workspace, approve_public=True, additional_digests=digests)
        write_content_index(workspace, bundle, check=args.check)
    except BrowseError as error:
        print(f"{error.diagnostic.code}: {error.diagnostic.summary}", file=sys.stderr)
        return 1
    action = "Current" if args.check else "Generated"
    print(f"{action} index: {bundle.published} published entries, "
          f"{bundle.unclassified} unclassified candidates omitted.")
    print("Commit both generated files next to the workspace. "
          "Publish only siteops-index.json, not siteops-index.inputs.json, to a gallery.")
    return 0


def _output_settings(
    args: argparse.Namespace,
    *,
    require_plan_flag: bool = False,
) -> tuple[bool, PlanProjection]:
    output_format = getattr(args, "output", "plain")
    requested_projection = getattr(args, "projection", None)
    json_output = output_format == "json"
    if requested_projection is not None and not json_output:
        raise ValueError("--projection requires --output json.")
    if (
        require_plan_flag
        and json_output
        and not getattr(args, "plan", False)
    ):
        raise ValueError("--output json requires --plan.")
    projection = (
        PlanProjection(requested_projection)
        if requested_projection is not None
        else (
            PlanProjection.PUBLISHABLE
            if is_redaction_enabled()
            else PlanProjection.LOCAL_PRIVATE
        )
    )
    if (
        json_output
        and projection is PlanProjection.LOCAL_PRIVATE
        and is_redaction_enabled()
    ):
        raise ValueError(
            "local-private output is unavailable while output "
            "redaction is enabled. Use --projection publishable."
        )
    return json_output, projection


def _validation_failure_result(
    errors: list[str],
    *,
    intent: PlanIntent,
    code: str = "validation.failed",
    summary: str = "Manifest validation failed.",
    public_summary: str | None = None,
) -> PlanBuildResult:
    return PlanBuildResult(
        status=PlanStatus.INVALID,
        executable=False,
        plan=None,
        diagnostics=tuple(
            PlanDiagnostic(
                code=code,
                severity=DiagnosticSeverity.ERROR,
                summary=summary,
                detail=error,
                public_summary=public_summary,
            )
            for error in errors
        ),
        intent=intent,
    )


def _write_plain_validation_errors(errors: list[str]) -> None:
    print(
        f"\n✗ Validation failed with {len(errors)} error(s):\n",
        file=sys.stderr,
    )
    for error in errors:
        print(f"  • {error}", file=sys.stderr)
    print(file=sys.stderr)


def _write_plan_result(
    result: PlanBuildResult,
    *,
    json_output: bool,
    projection: PlanProjection,
) -> None:
    if json_output:
        print(
            serialize_plan_json(
                result,
                projection,
                engine_version=__version__,
            )
        )
        return
    print(
        render_plain_plan(
            result,
            redacted=is_redaction_enabled(),
        ),
        end="",
    )


class ExplicitSiteConflict(GuidedInputError):
    """An explicit Site conflicts with another target-selection form."""


class ResourceReadError(GuidedInputError):
    """Value-safe resource admission failure with a stable public code."""

    def __init__(self, code: str, message: str):
        self.code = f"inputs.resource.{code}"
        super().__init__(f"{self.code}: {message}")


def _guided_error_detail(error: Exception) -> str:
    return str(error) if isinstance(error, GuidedInputError) else report_site_load_error(error)


def _is_cancelled_read(error: Exception) -> bool:
    return isinstance(error, ResourceReadError) and error.code == "inputs.resource.cancelled"


def _explicit_site_failure(
    args: argparse.Namespace,
    error: Exception,
    *,
    intent: PlanIntent,
) -> PlanBuildResult:
    if isinstance(error, (ResourceReadError, ResourceInputError)):
        code = error.code
        summary = str(error)
    elif isinstance(error, ExplicitSiteConflict):
        code = "plan.targeting.conflict"
        summary = str(error)
    elif getattr(args, "site_file", None):
        code = "site.invalid"
        summary = (
            str(error) if isinstance(error, GuidedInputError)
            else "The supplied Site file is invalid. Check its structure and required fields."
        )
    else:
        code = "inputs.invalid"
        summary = (
            f"{error} Run `siteops inputs` for required values."
            if isinstance(error, GuidedInputError)
            else "Typed Site inputs are unavailable or invalid. Inspect this "
            "manifest with `siteops inputs` and correct the answers."
        )
    return _validation_failure_result(
        [_guided_error_detail(error)],
        intent=intent,
        code=code,
        summary=summary,
        public_summary=summary if isinstance(error, GuidedInputError) else None,
    )


def _explicit_site(
    args: argparse.Namespace, manifest_path: Path, orchestrator: Orchestrator,
) -> Site | None:
    site_file = getattr(args, "site_file", None)
    input_file = getattr(args, "input_file", None)
    inline = getattr(args, "input_values", None)
    read_resources = getattr(args, "read_resources", False)
    if read_resources and (site_file or getattr(args, "selector", None)):
        raise ExplicitSiteConflict(
            "--read-resources requires typed answers, not --site-file or -l/--selector."
        )
    if not (site_file or input_file or inline):
        if read_resources:
            raise ResourceReadError(
                "nothing-to-read", "Supply a declared resource ID with --input or --input-file."
            )
        return None
    if getattr(args, "selector", None):
        raise ExplicitSiteConflict("An explicit Site cannot be combined with -l/--selector.")
    if site_file:
        if input_file or inline:
            raise ExplicitSiteConflict(
                "--site-file cannot be combined with --input-file or --input."
            )
        _require_operator_file_path(site_file, args)
        return load_direct_site(site_file)
    if input_file is not None:
        _require_operator_file_path(input_file, args)
    contract = load_contract(
        manifest_path,
        binding=getattr(args, "package_binding", None),
    )
    if contract is None:
        raise GuidedInputError(
            "This manifest has no typed input contract. Use --site-file with "
            "a complete Site, or configure Sites in your project."
        )
    site, _ = _resolve_typed_site(
        contract, args, manifest_path=manifest_path, orchestrator=orchestrator,
    )
    return site


def _announce_explicit_site(args: argparse.Namespace, site: Site) -> None:
    identity = "a private Site" if is_redaction_enabled() else _content_text(site.name)
    source = "a Site file" if getattr(args, "site_file", None) else "typed inputs"
    print(
        f"Target: {identity} from {source} (replaces manifest targeting).",
        file=sys.stderr,
    )


def _site_for_file(site: Site) -> dict[str, Any]:
    document: dict[str, Any] = {
        "apiVersion": "siteops/v1",
        "kind": "Site",
        "name": site.name,
        "subscription": site.subscription,
    }
    if site.resource_group:
        document["resourceGroup"] = site.resource_group
    document["location"] = site.location
    if site.labels:
        document["labels"] = site.labels
    if site.parameters:
        document["parameters"] = site.parameters
    if site.properties:
        document["properties"] = site.properties
    return document


def _require_operator_file_path(path: Path, args: argparse.Namespace) -> None:
    from siteops.workspace_cache import default_cache_root

    destination = path.resolve()
    if destination.is_relative_to(default_cache_root().resolve()):
        raise GuidedInputError("Keep operator Site and answer files outside the Site Ops content cache.")
    binding = getattr(args, "package_binding", None)
    if binding is not None and destination.is_relative_to(binding.package_root.resolve()):
        raise GuidedInputError("Do not write operator files into a verified workspace package.")


def _resolve_typed_site(
    contract: InputContract,
    args: argparse.Namespace,
    *,
    manifest_path: Path,
    orchestrator: Orchestrator,
) -> tuple[Site, dict[str, Any] | None]:
    bound = contract.bind(values_file=args.input_file, inline=args.input_values)
    if not bound.resources:
        if getattr(args, "read_resources", False):
            raise ResourceReadError("nothing-to-read", "No active resource ID input was supplied.")
        return contract.build_site(bound), None
    if not getattr(args, "read_resources", False):
        if getattr(args, "command", None) == "validate":
            raise ResourceReadError(
                "read-required",
                "Validation does not read Azure resources. Use "
                "`siteops plan MANIFEST --describe --read-resources` "
                "with the same answers.",
            )
        raise ResourceReadError(
            "read-required", "Resource ID inputs need --read-resources before planning."
        )
    orchestrator.load_manifest(manifest_path)
    try:
        reader = new_arm_reader()
    except ArmResourceError as error:
        if error.code == "CANCELLED":
            raise ResourceReadError("cancelled", "The resource read was cancelled.") from None
        raise ResourceReadError(
            "provider-unavailable", f"The selected resource reader is unavailable ({error.code})."
        ) from None
    observations = {}
    for index, resource in enumerate(bound.resources, start=1):
        name = resource.field.name
        print(
            f"Reading declared resource {_content_text(name)} "
            f"{index}/{len(bound.resources)} using {_content_text(reader.identity.name)}.",
            file=sys.stderr,
        )
        try:
            observations[name] = reader.read(resource.ref, facts=resource.required_facts)
        except ArmResourceError as error:
            raise ResourceReadError(error.code.lower().replace("_", "-"), f"Input '{name}' read failed.") from None
    site = contract.build_site(bound, observations)
    return site, {
        "provider": {"name": reader.identity.name, "version": reader.identity.version},
        "resourceCount": len(observations),
    }


def cmd_inputs(args: argparse.Namespace, orchestrator: Orchestrator) -> int:
    """Inspect an authored input contract or emit a completed ordinary Site."""
    manifest_path = _command_manifest(args)
    if manifest_path is None:
        return 1
    try:
        contract = load_contract(
            manifest_path,
            binding=getattr(args, "package_binding", None),
        )
        if contract is None:
            raise GuidedInputError(
                "This manifest has no typed input contract. Use a complete "
                "Site file or inspect the manifest's authored guidance."
            )
        if args.example and (
            args.save_site or args.input_file or args.input_values or args.read_resources
        ):
            raise GuidedInputError(
                "--example cannot be combined with answers, --save-site or --read-resources."
            )
        if args.example:
            _require_operator_file_path(args.example, args)
        resolved_site: Site | None = None
        resource_summary: dict[str, Any] | None = None
        if args.input_file or args.input_values or args.save_site or args.read_resources:
            if args.input_file is not None:
                _require_operator_file_path(args.input_file, args)
            if args.read_resources and not (args.input_file or args.input_values):
                raise ResourceReadError(
                    "nothing-to-read", "Supply a declared resource ID before requesting a read."
                )
            resolved_site, resource_summary = _resolve_typed_site(
                contract, args, manifest_path=manifest_path, orchestrator=orchestrator,
            )
            errors = orchestrator.validate(
                manifest_path,
                sites=[resolved_site],
            )
            if errors:
                raise ValueError("Site validation failed: " + "; ".join(errors))
        description = contract.describe()
        preview: str | None = None
        if resolved_site is not None:
            description["resolution"] = {
                "status": "ready",
                "site": None if is_redaction_enabled() else _site_document(resolved_site),
            }
            if resource_summary is not None:
                description["resolution"]["resourceReads"] = resource_summary
            if args.output == "plain" and not is_redaction_enabled():
                document = yaml.safe_dump(
                    description["resolution"]["site"],
                    sort_keys=False,
                    allow_unicode=True,
                ).rstrip()
                preview = "\n".join(_content_text(line) for line in document.splitlines())
        if args.output == "json":
            rendered_json = json.dumps(description, ensure_ascii=False, indent=2, allow_nan=False)
        else:
            manifest_name = _content_text(orchestrator.load_manifest(manifest_path).name)
        if args.example:
            write_yaml_exclusive(args.example, contract.example())
            destination = (
                "a private file"
                if is_redaction_enabled()
                else _content_text(str(args.example))
            )
            print(f"Incomplete answer file written: {destination}", file=sys.stderr)
        if args.save_site:
            if resolved_site is None:
                raise GuidedInputError("Complete typed answers are required to save a Site.")
            _require_operator_file_path(args.save_site, args)
            orchestrator.ensure_new_site_identity(resolved_site.name, args.save_site)
            write_yaml_exclusive(args.save_site, _site_for_file(resolved_site))
            destination = (
                "a private file"
                if is_redaction_enabled()
                else _content_text(str(args.save_site))
            )
            print(f"Site written: {destination}", file=sys.stderr)
        if args.output == "json":
            print(rendered_json)
        else:
            if resolved_site is not None:
                if preview is not None:
                    print(f"Effective Site for {manifest_name} (private preview):\n{preview}")
                else:
                    print(f"One Site resolved for {manifest_name}. Private Site values are withheld.")
                if not args.save_site:
                    print("No Site file written. Use --save-site FILE to keep it.")
            else:
                print(f"Inputs for {manifest_name}:")
                required = [
                    field["name"] for field in description["inputs"]
                    if field["status"] == "required"
                ]
                example_values = contract.example()["values"]
                for resource in description["inputs"]:
                    derived = set(resource.get("derive", {}).values())
                    if not derived.intersection(required) or resource["name"] not in example_values:
                        continue
                    supplied = [name for name in required if name not in derived] + [resource["name"]]
                    for line in _wrap(
                        f"Resource route: fill {_content_text(', '.join(supplied))}. "
                        f"Leave {_content_text(', '.join(name for name in required if name in derived))} "
                        "empty. Use --read-resources to read the ID.",
                    ):
                        print(line)
                for field in description["inputs"]:
                    status = field["status"]
                    if field.get("derivableFrom"):
                        status += " or derived from " + ", ".join(field["derivableFrom"])
                    for line in _wrap(
                        f"{_content_text(field['name'])} "
                        f"({_content_text(field['type'])}, {_content_text(status)})"
                        f": {_content_text(field['description'])}",
                        hanging="    ",
                    ):
                        print(line)
                    if "when" in field:
                        condition = field["when"]
                        for line in _wrap(
                            "Active when "
                            + _content_text(condition["input"])
                            + "="
                            + _content_text(json.dumps(condition["equals"])),
                            indent="    ",
                        ):
                            print(line)
                    if "resource" in field:
                        for line in _wrap(
                            "ARM type: "
                            + _content_text(field["resource"]["type"])
                            + ". Read only with --read-resources.",
                            indent="    ",
                        ):
                            print(line)
                        if field.get("derive"):
                            for line in _wrap(
                                "Derives: " + _content_text(", ".join(field["derive"].values())),
                                indent="    ",
                            ):
                                print(line)
                        for requirement in field.get("requires", []):
                            condition = requirement.get("when")
                            condition_text = (
                                f" when {condition['input']}="
                                f"{json.dumps(condition['equals'])}"
                                if condition is not None else ""
                            )
                            for line in _wrap(
                                "Prerequisite"
                                + _content_text(condition_text)
                                + ": "
                                + _content_text(requirement["description"]),
                                indent="    ",
                            ):
                                print(line)
                    if "default" in field:
                        value = field["default"]
                        shown = json.dumps(value) if isinstance(value, bool) else str(value)
                        for line in _wrap(f"Default: {_content_text(shown)}", indent="    "):
                            print(line)
                if not args.example:
                    print("Use --example FILE to write an incomplete answer file.")
            print("Review prerequisites and effects with `siteops browse`.")
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"Error: {_guided_error_detail(error)}", file=sys.stderr)
        return 130 if _is_cancelled_read(error) else 1
    return 0


def cmd_plan(args: argparse.Namespace, orchestrator: Orchestrator) -> int:
    """Validate and prepare a deployment plan without executing it."""
    manifest_path = _command_manifest(args)
    if manifest_path is None:
        return 1

    try:
        json_output, projection = _output_settings(args)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    selector = getattr(args, "selector", None)
    intent = (
        PlanIntent.DESCRIBE
        if getattr(args, "describe", False)
        else PlanIntent.EXECUTABLE
    )
    try:
        explicit_site = _explicit_site(args, manifest_path, orchestrator)
    except (OSError, ValueError, yaml.YAMLError) as error:
        result = _explicit_site_failure(args, error, intent=intent)
        _write_plan_result(result, json_output=json_output, projection=projection)
        return 130 if _is_cancelled_read(error) else 1
    if explicit_site is not None:
        _announce_explicit_site(args, explicit_site)
    try:
        if intent is PlanIntent.EXECUTABLE:
            print(
                "Preparing executable deployment plan...",
                file=sys.stderr,
            )
        site_options = {"sites": [explicit_site]} if explicit_site is not None else {}
        result = orchestrator.build_plan(
            manifest_path,
            selector,
            intent=intent,
            parallel_override=getattr(args, "parallel", None),
            **site_options,
        )
    except (CompositionError, ParameterSelectionError) as error:
        detail = (
            report_composition_error(error)
            if isinstance(error, CompositionError)
            else report_parameter_selection_error(error)
        )
        print(f"\nError: {detail}\n", file=sys.stderr)
        return 1
    except NoTargetingError:
        result = PlanBuildResult(
            status=PlanStatus.INVALID,
            executable=False,
            plan=None,
            diagnostics=(
                PlanDiagnostic(
                    code="plan.targeting.required",
                    severity=DiagnosticSeverity.ERROR,
                    summary=(
                        "Add `sites:` or `selector:` to the manifest, or "
                        "pass `-l <key>=<value>`."
                    ),
                    detail=(
                        "The manifest has no `sites:` or `selector:`. Pass "
                        "`-l <key>=<value>` to build a plan."
                    ),
                ),
            ),
            intent=intent,
        )
    except MultipleSubscriptionSitesError as error:
        result = _validation_failure_result(
            [str(error)],
            intent=intent,
        )

    _write_plan_result(
        result,
        json_output=json_output,
        projection=projection,
    )
    return 0 if result.status is PlanStatus.PLANNED else 1


_STOP_GUIDANCE = (
    "Stopping after the operations already in progress finish. A call already "
    "running waits for its own timeout, and work already accepted by Azure is "
    "not cancelled."
)


def _install_stop_handler(
    stop_requested: threading.Event,
) -> Callable[[], None]:
    """Ask a running deployment to stop when the terminal sends SIGINT.

    The handler records the request and says what will happen. It does not
    raise, because a raised `KeyboardInterrupt` would unwind through workers
    that are still using scratch files and would abandon outcomes already
    observed. A repeated interrupt repeats the same bounded expectation
    rather than forcing an unsafe exit.

    `signal.signal` only works on the main thread, so an embedded caller on
    another thread keeps its own signal disposition and passes an explicit
    `stop_requested` event instead. Returns the callable that restores the
    previous disposition.

    The guidance is written straight to stderr rather than through the
    progress reporter, because taking that reporter's lock inside a signal
    handler could deadlock the thread the signal interrupted.
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None

    def request_stop(signum: int, frame: FrameType | None) -> None:
        stop_requested.set()
        print(_STOP_GUIDANCE, file=sys.stderr, flush=True)

    try:
        previous = signal.signal(signal.SIGINT, request_stop)
    except (OSError, ValueError):  # pragma: no cover - host restriction
        return lambda: None

    def restore() -> None:
        signal.signal(signal.SIGINT, previous)

    return restore


def cmd_deploy(args: argparse.Namespace, orchestrator: Orchestrator) -> int:
    """Execute deployment."""
    if getattr(args, "dry_run", False):
        return cmd_plan(args, orchestrator)
    manifest_path = _command_manifest(args)
    if manifest_path is None:
        return 1

    try:
        json_output, projection = _output_settings(args)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    try:
        explicit_site = _explicit_site(args, manifest_path, orchestrator)
    except (OSError, ValueError, yaml.YAMLError) as error:
        if _is_cancelled_read(error):
            failed = _explicit_site_failure(args, error, intent=PlanIntent.EXECUTABLE)
            result = RunResult.from_sites(
                (), elapsed=0.0, interrupted=True,
                diagnostics=preparation_failure_result(failed).diagnostics,
            )
            _write_run_result(result, json_output=json_output, projection=projection)
            return result.exit_code
        if json_output:
            failed = _explicit_site_failure(args, error, intent=PlanIntent.EXECUTABLE)
            result = preparation_failure_result(failed)
            _write_run_result(result, json_output=True, projection=projection)
            return result.exit_code
        print(f"Error: {_guided_error_detail(error)}", file=sys.stderr)
        return 1
    if explicit_site is not None:
        _announce_explicit_site(args, explicit_site)

    stop_requested = threading.Event()
    restore_signal_handler = _install_stop_handler(stop_requested)
    try:
        print(
            "Preparing executable deployment plan...",
            file=sys.stderr,
        )
        site_options = {"sites": [explicit_site]} if explicit_site is not None else {}
        result = orchestrator.deploy(
            manifest_path,
            selector=getattr(args, "selector", None),
            parallel_override=getattr(args, "parallel", None),
            progress=TextProgressReporter(
                sys.stderr,
                redacted=is_redaction_enabled(),
            ),
            stop_requested=stop_requested,
            **site_options,
        )
    except (CompositionError, ParameterSelectionError) as e:
        detail = (
            report_composition_error(e)
            if isinstance(e, CompositionError)
            else report_parameter_selection_error(e)
        )
        print(f"\nError: {detail}\n", file=sys.stderr)
        return 1
    except PlanNotExecutableError as e:
        if json_output:
            result = preparation_failure_result(e.result)
            _write_run_result(result, json_output=True, projection=projection)
            return result.exit_code
        print(
            f"\nError: "
            f"{e.message(redacted=is_redaction_enabled())}\n",
            file=sys.stderr,
        )
        return 1
    except MultipleSubscriptionSitesError as e:
        detail = (
            "Only one subscription-level site per subscription is allowed."
            if is_redaction_enabled()
            else str(e)
        )
        print(f"\nError: {detail}\n", file=sys.stderr)
        return 1
    finally:
        restore_signal_handler()

    _write_run_result(result, json_output=json_output, projection=projection)
    return result.exit_code


def _write_run_result(
    result: RunResult,
    *,
    json_output: bool,
    projection: PlanProjection,
) -> None:
    if json_output:
        print(
            serialize_run_json(result, projection, engine_version=__version__),
            end="",
        )
    else:
        print(
            render_plain_run(result, redacted=is_redaction_enabled()),
            end="",
        )


def _note_superseded_verbose(
    args: argparse.Namespace, requested: bool, command: str, flag: str, output: str
) -> None:
    """Say where the output moved when `-v` is used to ask for it.

    `-v` used to select this output and now controls log verbosity only, so the
    obvious retry after the old spelling fails is `-v`, which succeeds and
    prints nothing. Written to stderr so a pipeline capturing stdout is
    unaffected, and printed on the way in so it is visible before the run's own
    output.
    """
    if getattr(args, "verbose", False) and not requested:
        print(
            f"Note: `-v` sets log verbosity. To print {output}, "
            f"run `siteops {command} ... {flag}`.",
            file=sys.stderr,
        )


def cmd_validate(args: argparse.Namespace, orchestrator: Orchestrator) -> int:
    """Validate manifest and optionally show deployment plan."""
    selector = getattr(args, "selector", None)
    show_plan = getattr(args, "plan", False)
    try:
        json_output, projection = _output_settings(
            args,
            require_plan_flag=True,
        )
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    _note_superseded_verbose(args, show_plan, "validate", "--plan", "the deployment plan")
    if show_plan:
        plan_args = argparse.Namespace(**vars(args))
        plan_args.describe = True
        return cmd_plan(plan_args, orchestrator)

    manifest_path = _command_manifest(args)
    if manifest_path is None:
        return 1
    try:
        manifest = orchestrator.load_manifest(manifest_path)
        explicit_site = _explicit_site(args, manifest_path, orchestrator)
    except (ValueError, OSError, yaml.YAMLError) as error:
        _write_plain_validation_errors([_guided_error_detail(error)])
        return 1

    site_options = {"sites": [explicit_site]} if explicit_site is not None else {}
    errors = orchestrator.validate(
        manifest_path,
        selector=selector,
        manifest=manifest,
        **site_options,
    )
    if errors:
        _write_plain_validation_errors(errors)
        return 1

    print(f"\n✓ Manifest is valid: {manifest_path.name}\n")
    if explicit_site is None and not selector and not manifest.sites and not manifest.site_selector:
        print(
            "  Note: library manifest (no `sites:` or `selector:`). "
            "Pass `-l <key>=<value>` at deploy time, or run "
            "`siteops validate <manifest> -l ...` to exercise resolution.\n"
        )
    return 0


def _origin_suffix(prov: dict[str, str] | None, key: str) -> str:
    """Format the `# <origin>` suffix for a leaf line.

    Returns an empty string when `prov` is None or the key is not in
    the map, such as a scalar within a list element. The leaf renders as
    today.
    """
    if prov is None:
        return ""
    origin = prov.get(key)
    if origin is None:
        return ""
    return f"  # {origin}"


# Substrings (case-insensitive) that mark a config key as carrying a secret.
# Values under a matching key are redacted in `siteops sites` output so a
# secret supplied via sites.local / SITE_OVERRIDES (e.g. an SP password) does
# not print to the terminal. Display-only: deploy still uses the real value.
_SENSITIVE_KEY_SUBSTRINGS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "credential",
    "apikey",
    "accountkey",
    "accesskey",
    "privatekey",
    "connectionstring",
    "sastoken",
)
_REDACTED = "***"


def _is_sensitive_key(key: str) -> bool:
    """True if a config key name indicates its value is a secret."""
    lowered = key.lower()
    return any(token in lowered for token in _SENSITIVE_KEY_SUBSTRINGS)


def _redact_sensitive(value: Any, key: Any = None) -> Any:
    """Return a copy of `value` with secret-keyed entries replaced by `***`.

    Display-only redaction for all `siteops sites` output formats.
    A value is redacted when its own key matches `_is_sensitive_key`, except
    booleans: a sensitive-looking key with a bool value is a toggle, not a
    secret (e.g. `enableSecretSync: false`), so it is left as-is. The whole
    subtree under a sensitive key is replaced so a nested credential object is
    fully masked. The real value is never mutated.

    Args:
        value: The value to redact (dict, list, or scalar).
        key: The key this value sits under, or None at the root / in a list.

    Returns:
        A redacted deep copy.
    """
    if isinstance(key, str) and _is_sensitive_key(key) and not isinstance(value, bool):
        return _REDACTED
    if isinstance(value, dict):
        return {k: _redact_sensitive(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _print_value(
    value: Any,
    indent: int = 6,
    prov: dict[str, str] | None = None,
    key_prefix: str = "",
) -> None:
    """Recursively print a value with proper indentation.

    When `prov` is provided, every leaf line is appended with a
    `# <origin>` comment showing the source file the value came from.

    Args:
        value: The value to print (can be dict, list, or scalar)
        indent: Number of spaces for indentation
        prov: Optional provenance map (dotted key to origin label).
        key_prefix: Dotted-key prefix accumulated through recursion.
    """
    prefix = " " * indent
    if isinstance(value, dict):
        for k, v in value.items():
            sub_key = f"{key_prefix}.{k}" if key_prefix else k
            if isinstance(v, dict):
                print(f"{prefix}{k}:")
                _print_value(v, indent + 2, prov=prov, key_prefix=sub_key)
            elif isinstance(v, list):
                origin = _origin_suffix(prov, sub_key)
                if len(v) == 0:
                    print(f"{prefix}{k}: []{origin}")
                elif all(isinstance(item, (str, int, float, bool, type(None))) for item in v):
                    # Simple list - print inline
                    print(f"{prefix}{k}: {v}{origin}")
                else:
                    # Complex list - print each item
                    print(f"{prefix}{k}:{origin}")
                    for i, item in enumerate(v):
                        if isinstance(item, dict):
                            print(f"{prefix}  [{i}]:")
                            _print_value(item, indent + 4, prov=prov, key_prefix=f"{sub_key}.{i}")
                        else:
                            print(f"{prefix}  - {item}")
            else:
                origin = _origin_suffix(prov, sub_key)
                print(f"{prefix}{k}: {v}{origin}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            if isinstance(item, dict):
                print(f"{prefix}[{i}]:")
                _print_value(item, indent + 2)
            else:
                print(f"{prefix}- {item}")
    else:
        print(f"{prefix}{value}")


def _site_document(site: Site) -> dict[str, Any]:
    """Build the private inspection document without exporting hidden model fields."""
    resolved: dict[str, Any] = {
        "apiVersion": "siteops/v1",
        "kind": "Site",
        "name": site.name,
        "subscription": site.subscription,
    }
    if site.resource_group:
        resolved["resourceGroup"] = site.resource_group
    resolved["location"] = site.location
    if site.labels:
        resolved["labels"] = site.labels
    if site.parameters:
        resolved["parameters"] = _redact_sensitive(site.parameters)
    if site.properties:
        resolved["properties"] = _redact_sensitive(site.properties)
    return resolved


def _require_json_mapping_keys(value: Any) -> None:
    """Reject YAML keys that JSON would silently coerce to different identities."""
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings.")
        for item in value.values():
            _require_json_mapping_keys(item)
    elif isinstance(value, list):
        for item in value:
            _require_json_mapping_keys(item)


def cmd_sites(args: argparse.Namespace, orchestrator: Orchestrator) -> int:
    """List available sites in the workspace.

    A bare `siteops sites` lists every site. Pass a positional `name`
    (filename without extension, or the internal `name:` field) to
    scope to one site, equivalent to `-l name=<NAME>`. Every format uses the
    same inheritance and overlay resolution. YAML emits one Site document
    per match, while JSON emits one array regardless of the match count.
    These are private inspection views with sensitive-key masking, not
    publication projections or lossless exports.
    """
    output_format = getattr(args, "output", "plain")
    show_sources = getattr(args, "show_sources", False)
    if show_sources and output_format != "plain":
        print("Error: --show-sources requires --output plain.", file=sys.stderr)
        return 1

    # Positional `name` is sugar for `-l name=<NAME>`. Combining the two
    # forms is rejected so a confusing override path cannot exist.
    name_arg = getattr(args, "name", None)
    selector_str = getattr(args, "selector", None)
    if name_arg and selector_str:
        print(
            "Error: pass either the positional `name` or `-l name=<value>`, not both.",
            file=sys.stderr,
        )
        return 1
    if name_arg:
        selector_str = f"name={name_arg}"

    # Filter by selector if provided
    if selector_str:
        from siteops.models import parse_selector

        try:
            selector = parse_selector(selector_str)
        except ValueError as e:
            detail = "Invalid site selector." if is_redaction_enabled() else str(e)
            print(f"\nError: {detail}\n", file=sys.stderr)
            return 1
        # Use filter_sites for parity with deploy: trusted-file fast
        # path resolves path-form names like `regions/eu/munich-dev`.
        try:
            sites = orchestrator.filter_sites(selector)
        except (ValueError, FileNotFoundError) as e:
            # `sites` is the command an operator reaches for to find out why a
            # site was rejected, so it has to report that rather than raise
            # through as a traceback.
            print(f"\nError: {report_site_load_error(e)}\n", file=sys.stderr)
            return 1
    else:
        try:
            sites = orchestrator.load_all_sites()
        except (ValueError, FileNotFoundError) as e:
            print(f"\nError: {report_site_load_error(e)}\n", file=sys.stderr)
            return 1

    if not sites:
        if orchestrator.skipped_sites:
            # Reported before the two cases below, since both of those describe
            # a workspace that has nothing to offer. Here it has site files and
            # every one of them was rejected, which is a different problem with
            # a different fix.
            names = (
                "<site identities omitted>"
                if is_redaction_enabled()
                else ", ".join(name for name, _ in orchestrator.skipped_sites)
            )
            print(
                f"\nError: no site could be loaded. "
                f"{len(orchestrator.skipped_sites)} site file(s) were rejected "
                f"({names}). Fix those files rather than adding a new site.\n",
                file=sys.stderr,
            )
            return 1
        if selector_str:
            # Operator explicitly asked for a target set (positional
            # `name` or `-l`) and got nothing. Exit non-zero so wrapper
            # scripts and `&&`-chained commands surface the failure
            # instead of silently treating "0 sites" as success.
            message = (
                "No sites matched selector."
                if is_redaction_enabled()
                else f"No sites matched selector: {selector_str}"
            )
            print(f"\n{message}\n", file=sys.stderr)
            return 1
        if output_format == "plain":
            print("\nNo sites found in workspace\n")
            return 0

    if orchestrator.skipped_sites:
        # A site that does not load is one the operator expected to be here.
        # The names are already reported above. This makes the shortfall
        # visible to a wrapper script and to CI rather than only to a reader.
        print(
            f"Error: {len(orchestrator.skipped_sites)} site(s) could not be "
            f"loaded, so this listing is incomplete. Fix the files named above.",
            file=sys.stderr,
        )
        return 1

    if sites and is_redaction_enabled():
        print(
            "Error: Site inspection output is private and unavailable while output "
            "redaction is enabled. For an authorized private destination, set "
            "SITEOPS_REDACT_OUTPUT=0.",
            file=sys.stderr,
        )
        return 1

    if output_format in {"yaml", "json"}:
        documents = [_site_document(site) for site in sorted(sites, key=lambda s: s.name)]
        if output_format == "json":
            try:
                _require_json_mapping_keys(documents)
                serialized = json.dumps(documents, indent=2, allow_nan=False) + "\n"
            except (TypeError, ValueError):
                print(
                    "Error: Resolved site values cannot be represented as JSON. "
                    "Use --output yaml to inspect YAML values.",
                    file=sys.stderr,
                )
                return 1
        else:
            serialized = yaml.safe_dump_all(
                documents, sort_keys=False, default_flow_style=False,
            )
        print(serialized, end="")
        return 0

    _note_superseded_verbose(
        args, show_sources, "sites", "--show-sources", "the source file of each value"
    )

    # Display header
    print()
    print("═" * 60)
    print(f"  Available Sites ({len(sites)})")
    if selector_str:
        print(f"  (filtered by: {selector_str})")
    print("═" * 60)
    print()

    for site in sorted(sites, key=lambda s: s.name):
        # With --show-sources, re-load with provenance so each leaf line
        # can be annotated with the source file the value came from
        # (after inherits + overlay merge). Skipped otherwise
        # to keep the bare listing fast.
        prov: dict[str, str] | None = None
        if show_sources:
            try:
                _, prov = orchestrator.load_site_with_provenance(site.name)
            except (FileNotFoundError, ValueError) as e:
                print(f"  {site.name}  # provenance unavailable: {e}")
                continue

        print(f"  {site.name}")
        print(f"    subscription:   {site.subscription}{_origin_suffix(prov, 'subscription')}")
        print(
            f"    resourceGroup:  {site.resource_group}"
            f"{_origin_suffix(prov, 'resourceGroup')}"
        )
        print(f"    location:       {site.location}{_origin_suffix(prov, 'location')}")

        if site.labels:
            print("    labels:")
            for key, value in sorted(site.labels.items()):
                print(f"      {key}: {value}{_origin_suffix(prov, f'labels.{key}')}")

        if site.properties:
            print("    properties:")
            _print_value(_redact_sensitive(site.properties), indent=6, prov=prov, key_prefix="properties")

        if site.parameters:
            print("    parameters:")
            _print_value(_redact_sensitive(site.parameters), indent=6, prov=prov, key_prefix="parameters")

        print()

    return 0


_EXTRA_SITES_DIRS_ENV = "SITEOPS_EXTRA_SITES_DIRS"


def _resolve_extra_sites_dirs(cli_dirs: list[Path] | None) -> list[Path]:
    """Resolve extra trusted site dirs from CLI flag and/or env var.

    Precedence: `--extra-sites-dir` wins over `SITEOPS_EXTRA_SITES_DIRS`.
    When both are provided, an INFO log records that the env var was ignored.

    The env var is parsed using `os.pathsep` (`;` on Windows, `:` on
    Unix) to match platform conventions for `PATH`-style variables. Empty
    segments are skipped so trailing separators are tolerated.

    Args:
        cli_dirs: Directories supplied via the `--extra-sites-dir` flag,
            or `None` if the flag was not used.

    Returns:
        List of paths to pass to `Orchestrator`. Empty list when neither
        source provides a value.
    """
    env_raw = os.environ.get(_EXTRA_SITES_DIRS_ENV, "")
    env_dirs = [Path(p) for p in env_raw.split(os.pathsep) if p]

    if cli_dirs:
        if env_dirs:
            print(
                f"Note: {_EXTRA_SITES_DIRS_ENV} env var ignored "
                f"(`--extra-sites-dir` takes precedence).",
                file=sys.stderr,
            )
        return list(cli_dirs)
    return env_dirs


def _parse_parallel(value: str) -> int:
    """Parse the `--parallel` value, accepting friendly aliases for unlimited.

    Accepts:
        max, auto, 0   -> 0 (unlimited)
        any positive int -> that int
        negative ints  -> argparse error

    The `0` form is preserved for backward compatibility but `max` reads
    more naturally for the no-cap case (the integer 0 is easy to misread
    as "no parallelism").
    """
    lowered = value.lower()
    if lowered in ("max", "auto"):
        return 0
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--parallel must be a non-negative integer or 'max' / 'auto', got {value!r}"
        )
    if n < 0:
        raise argparse.ArgumentTypeError("--parallel must be >= 0")
    return n


class _ExactArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, allow_abbrev=False, **kwargs)


class _SingleValueOption(argparse.Action):
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        seen = getattr(namespace, "_single_values_seen", None)
        if seen is None:
            seen = set()
            namespace._single_values_seen = seen
        if self.dest in seen:
            raise argparse.ArgumentError(self, "may be supplied only once")
        seen.add(self.dest)
        setattr(namespace, self.dest, values)


def _auto_discover_workspace(start: Path) -> Path | None:
    """Auto-discover a workspace from `start` when -w was not supplied.

    Two cases siteops can resolve unambiguously:

      1. `start` itself looks like a workspace (has `sites/` and
         `manifests/` subdirs).
      2. `start` contains a `workspaces/` subdir with exactly one entry
         that has the workspace shape.

    Returns the resolved workspace Path on success. Returns None when
    the discovery is ambiguous or no workspace shape is found, and the
    caller falls back to using `start` directly (preserving the prior
    "default to cwd" behavior).
    """
    if (start / "sites").is_dir() and (start / "manifests").is_dir():
        return start
    workspaces_dir = start / "workspaces"
    if not workspaces_dir.is_dir():
        return None
    candidates = [
        d for d in sorted(workspaces_dir.iterdir())
        if d.is_dir() and (d / "sites").is_dir() and (d / "manifests").is_dir()
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


_SELECTOR_HELP = (
    "Filter sites by labels (e.g., `environment=prod`, `name=munich-dev`). "
    "Repeatable: multiple `-l` flags AND-combine across distinct keys. "
    "Duplicate `name=` values OR-combine. Any other duplicate key is an "
    "error. `name=` accepts the basename, the relative path under a trusted "
    "`sites/` dir, or the file's internal `name:` field."
)


def main() -> None:
    """Main entry point for the Site Ops CLI."""
    # Reconfigure stdout/stderr to UTF-8 so the status glyphs render on
    # Windows consoles defaulting to cp1252. `reconfigure` is a no-op
    # when the stream is already UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    parser = _ExactArgumentParser(
        prog="siteops",
        description="Azure Site Ops: multi-site Azure IaC orchestration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Global options such as --project and --approved-source precede the command.

Examples:
  siteops -w workspaces/iot-operations browse aio-install
  siteops -w workspaces/iot-operations inputs aio-install --example ./aio-inputs.yaml
  # Fill siteName, environment, country and cluster in aio-inputs.yaml.
  siteops -w workspaces/iot-operations plan aio-install --input-file ./aio-inputs.yaml --read-resources
  siteops -w workspaces/iot-operations deploy aio-install --input-file ./aio-inputs.yaml --read-resources
  # After source enrollment, pin an identified release and select only new fleet Sites.
  siteops --approved-source NAME project pin ./factory --release RELEASE
  siteops --approved-source NAME --project ./factory plan aio-install -l name=plant-two,name=plant-three
  siteops --approved-source NAME --project ./factory deploy aio-install -l name=plant-two,name=plant-three
""",
    )
    parser.add_argument("--version", action="version", version=f"siteops {__version__}")
    parser.add_argument(
        "-w",
        "--workspace",
        type=Path,
        action=_SingleValueOption,
        metavar="PATH",
        default=None,
        help=(
            "Local content directory. Overrides project package content without "
            "changing project Sites or its workspace pin. With browse --source, "
            "selects a path inside that source instead. Without a project or -w, "
            "uses local workspace discovery from the current directory."
        ),
    )
    parser.add_argument(
        "--extra-sites-dir",
        dest="extra_sites_dirs",
        action="append",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Additional trusted sites/ directory (repeatable). Also accepts "
            "the SITEOPS_EXTRA_SITES_DIRS env var. See "
            "docs/site-configuration.md for trust rules and precedence."
        ),
    )
    parser.add_argument(
        "--project", type=Path, action=_SingleValueOption, metavar="DIRECTORY",
        help="Operator project directory for Sites and an optional workspace pin (a path, not a name)",
    )
    parser.add_argument(
        "--trust-policy", type=Path, action=_SingleValueOption, metavar="FILE",
        help="Independent local artifact verification policy, required to create or use a workspace pin",
    )
    parser.add_argument(
        "--trusted-root", type=Path, action=_SingleValueOption, metavar="FILE",
        help="Independent local trusted root snapshot, required to create or use a workspace pin",
    )
    parser.add_argument(
        "--approved-source", action=_SingleValueOption, metavar="NAME",
        help="Explicitly selected consumer source enrollment for a project pin or packaged command",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help=(
            "Raise log verbosity to DEBUG. Controls logging only. To see a "
            "deployment plan use `siteops plan`."
        ),
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_browse = subparsers.add_parser(
        "browse",
        help="Discover and inspect deployment content",
        description=(
            "Inspect local manifest headers and optional authored guidance without "
            "loading Sites, compiling templates or contacting deployment services."
        ),
    )
    p_browse.add_argument(
        "name", nargs="?", help="Exact entry name or explicit workspace-relative manifest path"
    )
    p_browse.add_argument("--search", help="Case-insensitive text filter")
    p_browse.add_argument("--tag", action="append", default=[], help="Required tag (repeatable)")
    p_browse.add_argument("--category", help="Exact authored category")
    p_browse.add_argument(
        "--include-partials", action="store_true", help="Include declared reusable fragments"
    )
    p_browse.add_argument("--limit", type=int, help="Maximum inventory rows (positive integer)")
    p_browse.add_argument(
        "--output", choices=("plain", "json"), default="plain", help="Private output format"
    )
    p_browse.add_argument(
        "--source", action=_SingleValueOption,
        help="Published descriptive index source: github:OWNER/REPO[@REF] or repository URL"
    )
    p_browse.add_argument(
        "--ref", action=_SingleValueOption,
        help="Source branch, tag or commit (default: repository default branch)",
    )
    p_browse.add_argument(
        "--auth", choices=("anonymous", "cli"), action=_SingleValueOption, default="anonymous",
        help="Remote read access: anonymous or configured GitHub CLI authentication",
    )
    cache_mode = p_browse.add_mutually_exclusive_group()
    cache_mode.add_argument(
        "--refresh", action="store_true", help="Resolve the remote reference again before browsing",
    )
    cache_mode.add_argument(
        "--offline", action="store_true",
        help="Make no source requests: use cached index metadata with --source, or a cached project package and proof",
    )
    p_inputs = subparsers.add_parser(
        "inputs",
        help="Inspect typed inputs for a selected deployment",
        description=(
            "Inspect the declared input contract. Complete answers preview "
            "one resolved Site without writing it. --example writes an "
            "incomplete answer file, and --save-site explicitly keeps a Site."
        ),
    )
    p_inputs.add_argument("manifest", help="Exact manifest name or explicit manifest path")
    p_inputs.add_argument(
        "--output", choices=("plain", "json"), default="plain",
        help="Input contract and optional private Site preview format (default: plain)",
    )
    p_inputs.add_argument(
        "--example", type=Path, action=_SingleValueOption, metavar="FILE",
        help="Write incomplete required answers and any resource ID that can derive them",
    )
    p_inputs.add_argument(
        "--input-file", type=Path, action=_SingleValueOption, metavar="FILE",
        help="Typed answers to preview or save one Site (no file written without --save-site)",
    )
    p_inputs.add_argument(
        "--input", dest="input_values", action="append", metavar="NAME=VALUE",
        help="Typed non-secret answer (repeatable, overrides --input-file)",
    )
    p_inputs.add_argument(
        "--save-site", type=Path, action=_SingleValueOption, metavar="FILE",
        help="Write a validated Site to a new file, checking selected Site identities before saving",
    )
    p_inputs.add_argument(
        "--read-resources", action="store_true",
        help="Read only supplied, declared ARM resource IDs with the selected Azure provider before previewing the Site",
    )
    p_inputs.add_argument(
        "--offline", action="store_true",
        help="Use the pinned package and proof already in cache, without source requests",
    )

    p_sites = subparsers.add_parser(
        "sites",
        help="List available sites",
        description=(
            "List selected Site configuration from the operator project or local workspace. Pass a positional name "
            "(filename or internal `name:`) to scope to one site."
        ),
    )
    p_sites.add_argument(
        "name",
        nargs="?",
        default=None,
        help=(
            "Optional site name to scope to (filename without extension, "
            "or the internal `name:` field). Equivalent to `-l name=<NAME>`."
        ),
    )
    p_sites.add_argument(
        "-l",
        "--selector",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help=_SELECTOR_HELP,
    )
    p_sites.add_argument(
        "--show-sources",
        action="store_true",
        help=(
            "Annotate every leaf with the source file the value came from "
            "after inheritance and overlays. Plain output only (default: false)."
        ),
    )
    p_sites.add_argument(
        "--output",
        choices=("plain", "yaml", "json"),
        default="plain",
        help=(
            "Private inspection format: plain display, YAML Site documents, or "
            "a JSON array. Sensitive-key masking is not publication safety "
            "(default: plain)."
        ),
    )

    # validate command
    p_validate = subparsers.add_parser(
        "validate",
        help="Validate manifest structure and static references",
        description=(
            "Validate manifest syntax, files, and static references. "
            "Use `siteops plan <manifest> --describe` for the compile-free "
            "plan shape."
        ),
    )
    p_validate.add_argument("manifest", help="Exact manifest name or explicit manifest path")
    p_validate.add_argument(
        "-l",
        "--selector",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help=_SELECTOR_HELP,
    )
    p_validate.add_argument(
        "--plan",
        action="store_true",
        help=(
            "Compatibility alias for `siteops plan --describe` "
            "(default: false)"
        ),
    )
    p_validate.add_argument(
        "--output",
        choices=("plain", "json"),
        default="plain",
        help="Plan output format. JSON requires --plan (default: plain).",
    )
    p_validate.add_argument(
        "--projection",
        choices=("local-private", "publishable"),
        default=None,
        help=(
            "JSON plan projection, valid with --output json. Defaults to "
            "publishable when output redaction is enabled, otherwise "
            "local-private."
        ),
    )

    # plan command
    p_plan = subparsers.add_parser(
        "plan",
        help="Prepare and preflight a deployment plan",
        description=(
            "Validate, resolve, compile, and preflight a deployment plan "
            "without executing it."
        ),
    )
    p_plan.add_argument("manifest", help="Exact manifest name or explicit manifest path")
    p_plan.add_argument(
        "-l",
        "--selector",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help=_SELECTOR_HELP,
    )
    p_plan.add_argument(
        "--describe",
        action="store_true",
        help=(
            "Show the compile-free plan shape without executable preflight "
            "(default: false)."
        ),
    )
    p_plan.add_argument(
        "-p",
        "--parallel",
        type=_parse_parallel,
        action=_SingleValueOption,
        default=None,
        metavar="N",
        help=(
            "Max concurrent sites recorded in the plan. Accepts a positive "
            "integer, or 'max' / 'auto' / '0' for unlimited. Overrides the "
            "manifest setting."
        ),
    )
    p_plan.add_argument(
        "--output",
        choices=("plain", "json"),
        default="plain",
        help="Plan output format (default: plain).",
    )
    p_plan.add_argument(
        "--projection",
        choices=("local-private", "publishable"),
        default=None,
        help=(
            "JSON plan projection, valid with --output json. Defaults to "
            "publishable when output redaction is enabled, otherwise "
            "local-private."
        ),
    )

    # deploy command
    p_deploy = subparsers.add_parser(
        "deploy",
        help="Deploy manifest to target sites",
        description=(
            "Execute deployment of a manifest to one or more sites. "
            "Ctrl-C asks the run to stop and waits for the calls already in "
            "progress to return or reach their own timeout."
        ),
    )
    p_deploy.add_argument("manifest", help="Exact manifest name or explicit manifest path")
    p_deploy.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Compatibility alias for executable planning. Prepares and shows "
            "the plan without executing it (default: false)."
        ),
    )
    p_deploy.add_argument(
        "-l",
        "--selector",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help=_SELECTOR_HELP,
    )
    p_deploy.add_argument(
        "-p",
        "--parallel",
        type=_parse_parallel,
        action=_SingleValueOption,
        default=None,
        metavar="N",
        help=(
            "Max concurrent sites. Accepts a positive integer, or 'max' / "
            "'auto' / '0' for unlimited. Overrides the manifest setting."
        ),
    )
    p_deploy.add_argument(
        "--output",
        choices=("plain", "json"),
        default="plain",
        help="Final result format. A dry run emits a plan instead (default: plain).",
    )
    p_deploy.add_argument(
        "--projection",
        choices=("local-private", "publishable"),
        default=None,
        help=(
            "JSON projection, valid with --output json. Defaults to publishable "
            "when output redaction is enabled, otherwise local-private."
        ),
    )
    for command in (p_plan, p_deploy, p_validate):
        command.add_argument(
            "--site-file", type=Path, action=_SingleValueOption, metavar="FILE",
            help="Complete standalone Site file selecting exactly one target",
        )
        command.add_argument(
            "--input-file", type=Path, action=_SingleValueOption, metavar="FILE",
            help="Typed answers for the selected manifest's input contract",
        )
        command.add_argument(
            "--input", dest="input_values", action="append", metavar="NAME=VALUE",
            help="Typed non-secret answer (repeatable, overrides --input-file)",
        )
        command.add_argument(
            "--offline", action="store_true",
            help="Use the pinned package and proof already in cache, without source requests",
        )
    for command in (p_plan, p_deploy):
        command.add_argument(
            "--read-resources", action="store_true",
            help="Read only supplied, declared ARM resource IDs with the selected Azure provider before preparing the Site",
        )

    p_project = subparsers.add_parser("project", help="Inspect or explicitly pin a project's workspace source")
    project_commands = p_project.add_subparsers(dest="project_command", required=True)
    for name, help_text in (
        ("pin", "Acquire an explicit release and atomically record its workspace selection"),
        ("show", "Show the recorded workspace selection without acquiring or verifying package content"),
    ):
        description = help_text
        if name == "pin":
            description = (
                "Acquire and verify an explicit workspace release, then atomically create or "
                "replace DIRECTORY/siteops.pin without changing Sites. Supply global "
                "--approved-source NAME, or independent global --trust-policy FILE and "
                "--trusted-root FILE options before 'project pin'."
            )
        command = project_commands.add_parser(name, help=help_text, description=description)
        command.add_argument("directory", nargs="?", type=Path, default=Path("."),
                             metavar="DIRECTORY", help="Operator project directory (default: current directory)")
        command.add_argument("--output", choices=("plain", "json"), default="plain")
        if name == "pin":
            command.add_argument("--source",
                                 action=_SingleValueOption,
                                 help="Workspace source: github:OWNER/REPO[@RELEASE]. Omit when global --approved-source NAME selects it")
            command.add_argument("--release", metavar="RELEASE",
                                 action=_SingleValueOption,
                                 help="Published release tag (or include @RELEASE in --source)")
            command.add_argument("--release-workspace", metavar="PATH",
                                 action=_SingleValueOption,
                                 help="Workspace path listed in the release descriptor, required when several are listed")

    p_source = subparsers.add_parser(
        "source", help="Inspect, enroll or remove consumer-approved sources",
        description="Manage independent consumer source approval outside projects and workspace packages.",
    )
    source_commands = p_source.add_subparsers(dest="source_command", required=True)
    for name, help_text in (
        ("enroll", "Enroll a consumer-approved source using independent trust files"),
        ("show", "Inspect one approved source in a private destination"),
        ("list", "List approved source names in a private destination"),
        ("remove", "Remove one approval without changing a workspace pin"),
    ):
        description = help_text
        if name == "enroll":
            description += (
                ". Supply global --trust-policy FILE and --trusted-root FILE "
                "before 'source enroll'."
            )
        command = source_commands.add_parser(name, help=help_text, description=description)
        if name != "list":
            command.add_argument(
                "name", metavar="NAME", help="Lowercase name in private user configuration",
            )
        if name == "enroll":
            command.add_argument(
                "--source", required=True, action=_SingleValueOption,
                help="Approved repository: github:OWNER/REPO",
            )

    p_index = subparsers.add_parser(
        "index", help="Build a public content index and separate source bindings",
        description="Generate deterministic index files in the selected workspace. No remote publication.",
    )
    p_index.add_argument(
        "--public", action="store_true",
        help="Approve the selected authored descriptions for public indexing (required)",
    )
    p_index.add_argument(
        "--for-source", choices=("github",), help="Include optional freshness identities for a source adapter"
    )
    p_index.add_argument(
        "--check", action="store_true", help="Compare generated files without writing them"
    )

    p_cache = subparsers.add_parser("cache", help="Inspect cached storage or remove a selected entry")
    cache_commands = p_cache.add_subparsers(dest="cache_command", required=True)
    for name, help_text in (
        ("list", "List cached storage without verifying content or contacting sources"),
        ("remove", "Remove one cached entry, refusing active use and preserving project configuration"),
    ):
        command = cache_commands.add_parser(name, help=help_text, description=help_text)
        command.add_argument("--output", choices=("plain", "json"), default="plain")
        if name == "list":
            command.add_argument("--kind", choices=("package", "proof", "metadata"),
                                 help="Limit inspection to one cache entry kind")
            command.add_argument("--id", help="Complete cache entry ID, used with --kind")
            command.add_argument("--limit", type=int, default=100, help="Maximum rows to inspect (default: 100)")
        else:
            command.add_argument("kind", choices=("package", "proof", "metadata"))
            command.add_argument("id", help="Complete lowercase entry ID from cache list")

    args = parser.parse_args()
    if hasattr(args, "_single_values_seen"):
        del args._single_values_seen

    # Flatten repeatable -l/--selector (action="append" gives a list) into
    # a single comma-joined string. Joining is safe because parse_selector
    # enforces the name-OR and non-name-duplicate rules over the merged
    # input, and every downstream caller consumes a string.
    if hasattr(args, "selector"):
        args.selector = _merge_selector_strings(getattr(args, "selector", None))

    # Setup logging - use verbose from subcommand if available, otherwise False
    verbose = getattr(args, "verbose", False)
    setup_logging(verbose)

    if args.command == "browse":
        sys.exit(cmd_browse(args))
    if args.command == "index":
        sys.exit(cmd_index(args))
    if args.command == "project":
        sys.exit(cmd_project(args))
    if args.command == "source":
        sys.exit(cmd_source(args))
    if args.command == "cache":
        sys.exit(cmd_cache(args))

    extra_sites_dirs = _resolve_extra_sites_dirs(args.extra_sites_dirs)
    commands = {
        "deploy": cmd_deploy,
        "plan": cmd_plan,
        "validate": cmd_validate,
        "inputs": cmd_inputs,
        "sites": cmd_sites,
    }
    if args.command in {"plan", "deploy", "validate"}:
        try:
            _output_settings(args, require_plan_flag=args.command == "validate")
        except ValueError as error:
            print(f"Error: {error}", file=sys.stderr)
            sys.exit(1)
    try:
        with open_command_context(**_context_options(args)) as context:
            args.workspace = context.workspace
            binding = context.package.bind(args.manifest) if context.package is not None else None
            args.package_binding = binding
            if context.project is not None and args.command != "sites":
                if context.pin is not None:
                    selected = context.pin.selection
                    if is_redaction_enabled():
                        print("Source: verified pinned workspace.", file=sys.stderr)
                    else:
                        print(
                            f"Source: {_content_text(selected.source.reference)} "
                            f"@ {_content_text(selected.source.release)}\n"
                            f"Workspace: {_content_text(selected.entry.workspace)}\n"
                            f"Kit: {_content_text(selected.entry.kit_id)} {_content_text(selected.entry.kit_version)}\n"
                            f"Revision: {_content_text(selected.source.revision)}\n"
                            f"Package SHA-256: {selected.entry.package.sha256}",
                            file=sys.stderr,
                        )
                else:
                    print("Source: local workspace override. Operator configuration comes from the project.",
                          file=sys.stderr)
            options: dict[str, Any] = {}
            if context.project is not None:
                options["site_config_root"] = context.site_root
            if binding is not None:
                options["materialized_package"] = binding
            orchestrator = Orchestrator(
                workspace=context.workspace, dry_run=getattr(args, "dry_run", False),
                extra_trusted_sites_dirs=extra_sites_dirs, **options,
            )
            exit_code = commands[args.command](args, orchestrator)
    except (FileNotFoundError, ValueError) as error:
        detail = str(error) if isinstance(error, ArtifactError) or not is_redaction_enabled() else "Command preparation failed."
        print(f"Error: {detail}", file=sys.stderr)
        if isinstance(error, ManifestSelectionError) and not is_redaction_enabled():
            for choice in error.paths:
                print(f"Explicit path: {_content_text(explicit_manifest_reference(choice))}", file=sys.stderr)
        exit_code = 1
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
