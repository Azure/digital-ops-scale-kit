# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Command-line interface for Azure Site Ops.

Commands:
    deploy   - Deploy a manifest to target sites
    validate - Validate manifest (use -v to show deployment plan)
    sites    - List available sites
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

from siteops import __version__
from siteops.models import _merge_selector_strings
from siteops.orchestrator import Orchestrator


def setup_logging(verbose: bool = False) -> None:
    """Configure logging based on verbosity level."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
    )
    if not verbose:
        logging.getLogger("siteops.executor").setLevel(logging.WARNING)


def resolve_manifest_path(manifest: Path, workspace: Path) -> Path:
    """Resolve manifest path - if relative, make it relative to workspace."""
    if manifest.is_absolute():
        return manifest
    return workspace / manifest


def cmd_deploy(args: argparse.Namespace, orchestrator: Orchestrator) -> int:
    """Execute deployment."""
    manifest_path = resolve_manifest_path(args.manifest, args.workspace)

    if not manifest_path.exists():
        print(f"Error: Manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    parallel_override = getattr(args, "parallel", None)

    import yaml as _yaml

    from siteops.models import Manifest

    cli_selector = getattr(args, "selector", None)
    try:
        manifest = Manifest.from_file(manifest_path, workspace_root=args.workspace)
        sites = orchestrator.resolve_sites(manifest, cli_selector)
    except (ValueError, OSError, _yaml.YAMLError) as e:
        # ValueError: selector parse, no-targeting, overlay-rename, etc.
        # OSError: missing or unreadable file (includes FileNotFoundError).
        # YAMLError: malformed manifest YAML.
        print(f"\nError: {e}\n", file=sys.stderr)
        return 1

    if not sites:
        if cli_selector:
            # Operator explicitly asked for a target set and got
            # nothing. Surface the diagnostic and exit non-zero so the
            # condition is not silently masked in CI.
            print(
                f"\nError: {orchestrator.explain_no_match(cli_selector)}\n",
                file=sys.stderr,
            )
            return 1
        print("\n⚠ No sites matched. Nothing to deploy.\n")
        return 0

    if not manifest.steps:
        print("\n⚠ Manifest has no steps. Nothing to deploy.\n")
        return 0

    # Execute deployment
    result = orchestrator.deploy(
        manifest_path,
        selector=getattr(args, "selector", None),
        parallel_override=parallel_override,
        manifest=manifest,
        sites=sites,
    )

    # Return exit code based on results
    if result["summary"]["failed"] > 0:
        return 1
    return 0


def cmd_validate(args: argparse.Namespace, orchestrator: Orchestrator) -> int:
    """Validate manifest and optionally show deployment plan."""
    manifest_path = resolve_manifest_path(args.manifest, args.workspace)

    if not manifest_path.exists():
        print(f"Error: Manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    selector = getattr(args, "selector", None)
    verbose = getattr(args, "verbose", False)
    errors = orchestrator.validate(manifest_path, selector=selector)

    if errors:
        print(f"\n✗ Validation failed with {len(errors)} error(s):\n")
        for err in errors:
            print(f"  • {err}")
        print()
        return 1

    print(f"\n✓ Manifest is valid: {manifest_path.name}\n")

    # Heads-up when the manifest is a library/partial (no `sites:` and
    # no `selector:`) and no `-l` was provided. Validation passes, but
    # `deploy` will hard-error without targeting. Surfacing this here
    # eliminates the validate-passes-then-deploy-fails confusion class.
    is_library_no_selector = False
    import yaml as _yaml

    from siteops.models import Manifest as _Manifest
    try:
        _m = _Manifest.from_file(manifest_path, workspace_root=args.workspace)
        is_library_no_selector = (
            not selector and not _m.sites and not _m.site_selector
        )
        if is_library_no_selector:
            print(
                "  Note: library manifest (no `sites:` or `selector:`). "
                "Pass `-l <key>=<value>` at deploy time, or run "
                "`siteops validate <manifest> -l ...` to exercise resolution.\n"
            )
    except (ValueError, OSError, _yaml.YAMLError):
        # Manifest parse already passed in `validate` above; any failure
        # here is best-effort and should not change the exit code.
        # Programmer errors (AttributeError, RuntimeError) still propagate.
        pass

    # Skip the plan render for a library manifest with no selector.
    # show_plan re-resolves and would re-raise NoTargetingError.
    if verbose and not is_library_no_selector:
        orchestrator.show_plan(manifest_path, selector=selector)

    return 0


def _origin_suffix(prov: dict[str, str] | None, key: str) -> str:
    """Format the `# <origin>` suffix for a leaf line.

    Returns an empty string when `prov` is None or the key is not in
    the map (e.g., a scalar within a list element); the leaf renders
    as today.
    """
    if prov is None:
        return ""
    origin = prov.get(key)
    if origin is None:
        return ""
    return f"  # {origin}"


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


def cmd_sites(args: argparse.Namespace, orchestrator: Orchestrator) -> int:
    """List available sites in the workspace.

    A bare `siteops sites` lists every site. Pass a positional `name`
    (filename without extension, or the internal `name:` field) to
    scope to one site, equivalent to `-l name=<NAME>`. With `--render`,
    emits the merged YAML for each matched site instead of the
    human-readable summary, useful for confirming what an overlay or
    extras-dir file actually changed.
    """
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
            print(f"\nError: {e}\n", file=sys.stderr)
            return 1
        # Use filter_sites for parity with deploy: trusted-file fast
        # path resolves path-form names like `regions/eu/munich-dev`.
        sites = orchestrator.filter_sites(selector)
    else:
        sites = orchestrator.load_all_sites()

    if not sites:
        if selector_str:
            # Operator explicitly asked for a target set (positional
            # `name` or `-l`) and got nothing. Exit non-zero so wrapper
            # scripts and `&&`-chained commands surface the failure
            # instead of silently treating "0 sites" as success.
            print(f"\nNo sites matched selector: {selector_str}\n", file=sys.stderr)
            return 1
        print("\nNo sites found in workspace\n")
        return 0

    if getattr(args, "render", False) is True:
        import yaml

        for i, site in enumerate(sorted(sites, key=lambda s: s.name)):
            resolved = {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "name": site.name,
                "subscription": site.subscription,
            }
            # Subscription-scoped sites have no resourceGroup; emitting "" would
            # falsely imply RG-scoped behavior on a round-trip.
            if site.resource_group:
                resolved["resourceGroup"] = site.resource_group
            resolved["location"] = site.location
            if site.labels:
                resolved["labels"] = site.labels
            if site.parameters:
                resolved["parameters"] = site.parameters
            if site.properties:
                resolved["properties"] = site.properties
            if i > 0:
                print("---")
            print(yaml.safe_dump(resolved, sort_keys=False, default_flow_style=False), end="")
        return 0

    verbose = getattr(args, "verbose", False)

    # Display header
    print()
    print("═" * 60)
    print(f"  Available Sites ({len(sites)})")
    if selector_str:
        print(f"  (filtered by: {selector_str})")
    print("═" * 60)
    print()

    for site in sorted(sites, key=lambda s: s.name):
        # In verbose mode, re-load with provenance so each leaf line
        # can be annotated with the source file the value came from
        # (after inherits + overlay merge). Skipped in non-verbose
        # mode to keep the bare listing fast.
        prov: dict[str, str] | None = None
        if verbose:
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
            _print_value(site.properties, indent=6, prov=prov, key_prefix="properties")

        if site.parameters:
            print("    parameters:")
            _print_value(site.parameters, indent=6, prov=prov, key_prefix="parameters")

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
    "Duplicate `name=` values OR-combine; any other duplicate key is an "
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
  siteops -w workspaces/iot-operations sites
  siteops -w workspaces/iot-operations sites munich-dev --render
  siteops -w workspaces/iot-operations validate manifests/aio-install.yaml
  siteops -w workspaces/iot-operations deploy manifests/aio-install.yaml
  siteops -w workspaces/iot-operations deploy manifests/aio-install.yaml --dry-run
  siteops -w workspaces/iot-operations deploy manifests/aio-install.yaml -l environment=prod -p max
""",
    )
    parser.add_argument("--version", action="version", version=f"siteops {__version__}")
    parser.add_argument(
        "-w",
        "--workspace",
        type=Path,
        default=None,
        help=(
            "Workspace directory. When omitted, siteops auto-discovers a "
            "single workspace under ./workspaces/ or uses the current "
            "directory if it has the workspace shape."
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

    subparsers = parser.add_subparsers(dest="command", required=True)

    # deploy command
    p_deploy = subparsers.add_parser(
        "deploy",
        help="Deploy manifest to target sites",
        description="Execute deployment of a manifest to one or more sites.",
    )
    p_deploy.add_argument("manifest", type=Path, help="Path to manifest file")
    p_deploy.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deployed without executing (default: false)",
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

    # validate command
    p_validate = subparsers.add_parser(
        "validate",
        help="Validate manifest and show plan",
        description="Validate manifest syntax, files, and references. Use -v to show deployment plan.",
    )
    p_validate.add_argument("manifest", type=Path, help="Path to manifest file")
    p_validate.add_argument(
        "-l",
        "--selector",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help=_SELECTOR_HELP,
    )
    p_validate.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show deployment plan after validation (default: false)",
    )

    # sites command
    p_sites = subparsers.add_parser(
        "sites",
        help="List available sites",
        description=(
            "List sites in the workspace. Pass a positional name "
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
        "-v",
        "--verbose",
        "--show-sources",
        action="store_true",
        help=(
            "Annotate every leaf with the source file the value came from "
            "after inherits + overlay merge. `--show-sources` is an alias "
            "for `-v` on `sites` (default: false)."
        ),
    )
    p_sites.add_argument(
        "--render",
        action="store_true",
        help=(
            "Emit the merged YAML for each matched site instead of the summary. "
            "Useful with a single-site scope to inspect resolved config "
            "(default: false)."
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

    # Workspace resolution. Explicit -w wins. Otherwise auto-discover
    # from cwd; if discovery is ambiguous or finds nothing, fall back
    # to cwd (the prior default).
    if args.workspace is None:
        discovered = _auto_discover_workspace(Path.cwd())
        args.workspace = discovered if discovered is not None else Path.cwd()
    args.workspace = Path(args.workspace).resolve()

    if not args.workspace.is_dir():
        print(f"Error: Workspace directory not found: {args.workspace}", file=sys.stderr)
        sys.exit(1)

    extra_sites_dirs = _resolve_extra_sites_dirs(args.extra_sites_dirs)

    try:
        orchestrator = Orchestrator(
            workspace=args.workspace,
            dry_run=getattr(args, "dry_run", False),
            extra_trusted_sites_dirs=extra_sites_dirs,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    commands = {
        "deploy": cmd_deploy,
        "validate": cmd_validate,
        "sites": cmd_sites,
    }

    exit_code = commands[args.command](args, orchestrator)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
