# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Command-line interface for Azure Site Ops.

Commands:
    browse   - Discover and inspect deployment content
    index    - Build approved public descriptions and source bindings
    sites    - Inspect sites as plain text, YAML, or JSON
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
            if args.trust_policy is not None or args.trusted_root is not None:
                raise ProjectError("Project show reads a selection without applying trust options.")
            root = project_root(args.directory)
            pin = read_pin(root).pin
        else:
            from siteops.github_source import GitHubClient, GitHubReference
            from siteops.github_workspace_acquisition import GitHubWorkspaceAcquirer
            from siteops.workspace_cache import WorkspaceCache, default_cache_root

            cache_root = default_cache_root()
            policy, trusted_root = require_trust_inputs(cache_root, args.trust_policy, args.trusted_root)
            reference = GitHubReference.parse(args.source, ref=args.release)
            if reference.ref is None:
                raise ProjectError("Project pin requires an explicit published release with --release.")
            require_separate_cache(Path(args.directory).absolute(), cache_root)
            root = project_root(args.directory, create=True)
            require_separate_cache(root, cache_root)
            previous = read_pin(root) if pin_exists(root) else None
            cache = WorkspaceCache(cache_root)
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
            if args.project is not None or args.trust_policy is not None or args.trusted_root is not None:
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
        if args.project is not None or args.trust_policy is not None or args.trusted_root is not None:
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
) -> PlanBuildResult:
    return PlanBuildResult(
        status=PlanStatus.INVALID,
        executable=False,
        plan=None,
        diagnostics=tuple(
            PlanDiagnostic(
                code="validation.failed",
                severity=DiagnosticSeverity.ERROR,
                summary="Manifest validation failed.",
                detail=error,
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
        if intent is PlanIntent.EXECUTABLE:
            print(
                "Preparing executable deployment plan...",
                file=sys.stderr,
            )
        result = orchestrator.build_plan(
            manifest_path,
            selector,
            intent=intent,
            parallel_override=getattr(args, "parallel", None),
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

    stop_requested = threading.Event()
    restore_signal_handler = _install_stop_handler(stop_requested)
    try:
        print(
            "Preparing executable deployment plan...",
            file=sys.stderr,
        )
        result = orchestrator.deploy(
            manifest_path,
            selector=getattr(args, "selector", None),
            parallel_override=getattr(args, "parallel", None),
            progress=TextProgressReporter(
                sys.stderr,
                redacted=is_redaction_enabled(),
            ),
            stop_requested=stop_requested,
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
    except (ValueError, OSError, yaml.YAMLError) as error:
        _write_plain_validation_errors([report_site_load_error(error)])
        return 1

    errors = orchestrator.validate(
        manifest_path,
        selector=selector,
        manifest=manifest,
    )
    if errors:
        _write_plain_validation_errors(errors)
        return 1

    print(f"\n✓ Manifest is valid: {manifest_path.name}\n")
    if not selector and not manifest.sites and not manifest.site_selector:
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
    parser = argparse.ArgumentParser(
        prog="siteops",
        description="Azure Site Ops: multi-site Azure IaC orchestration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  siteops -w workspaces/iot-operations browse
  siteops -w workspaces/iot-operations browse aio-install
  siteops -w workspaces/iot-operations sites
  siteops -w workspaces/iot-operations sites munich-dev --output yaml
  siteops -w workspaces/iot-operations validate aio-install
  siteops -w workspaces/iot-operations plan aio-install
  siteops -w workspaces/iot-operations deploy aio-install
  siteops -w workspaces/iot-operations plan aio-install -l environment=prod
  siteops --project ./factory sites
  siteops --project ./factory -w ./clone plan storage -l name=one
  siteops --trust-policy policy.json --trusted-root trusted-root.json project pin ./factory --source github:OWNER/REPO --release RELEASE
  siteops project show ./factory
""",
    )
    parser.add_argument("--version", action="version", version=f"siteops {__version__}")
    parser.add_argument(
        "-w",
        "--workspace",
        type=Path,
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
        "--project", type=Path, metavar="DIRECTORY",
        help="Operator project directory containing Sites and an optional workspace pin (a path, not a name)",
    )
    parser.add_argument(
        "--trust-policy", type=Path, metavar="FILE",
        help="Independent local artifact verification policy, required for pinning and pinned package use",
    )
    parser.add_argument(
        "--trusted-root", type=Path, metavar="FILE",
        help="Independent local trusted root snapshot, required for pinning and pinned package use",
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
                "replace DIRECTORY/siteops.pin without changing Sites. Supply the independent "
                "global --trust-policy FILE and --trusted-root FILE options before 'project pin'."
            )
        command = project_commands.add_parser(name, help=help_text, description=description)
        command.add_argument("directory", nargs="?", type=Path, default=Path("."),
                             metavar="DIRECTORY", help="Operator project directory (default: current directory)")
        command.add_argument("--output", choices=("plain", "json"), default="plain")
        if name == "pin":
            command.add_argument("--source", required=True,
                                 help="Workspace release source: github:OWNER/REPO")
            command.add_argument("--release", metavar="RELEASE", help="Explicit published release tag")
            command.add_argument("--release-workspace", metavar="PATH",
                                 help="Workspace path listed in the release descriptor, required when several are listed")

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
        "--source", help="Published descriptive index source: github:OWNER/REPO[@REF] or repository URL"
    )
    p_browse.add_argument("--ref", help="Source branch, tag or commit (default: repository default branch)")
    p_browse.add_argument(
        "--auth", choices=("anonymous", "cli"), default="anonymous",
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
    for command in (p_plan, p_deploy, p_validate):
        command.add_argument(
            "--offline", action="store_true",
            help="Use the pinned package and proof already in cache, without source requests",
        )

    # sites command
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

    args = parser.parse_args()

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

    extra_sites_dirs = _resolve_extra_sites_dirs(args.extra_sites_dirs)
    commands = {
        "deploy": cmd_deploy,
        "plan": cmd_plan,
        "validate": cmd_validate,
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
