#!/usr/bin/env python3
"""Generate sites.local/ overlay files from a SITE_OVERRIDES JSON string.

Expands dot-notation keys into nested YAML structures:
    {"munich-dev": {"parameters.clusterName": "arc-01"}}
  becomes sites.local/munich-dev.yaml:
    parameters:
      clusterName: arc-01

Usage:
    # From environment variable
    python scripts/generate-site-overrides.py <workspace> --from-env SITE_OVERRIDES

    # From stdin
    echo "$SITE_OVERRIDES" | python scripts/generate-site-overrides.py <workspace>

    # From file
    python scripts/generate-site-overrides.py <workspace> --file overrides.json

Returns exit code 0 on success (or if input is empty), 1 on error.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

import yaml

SITE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def expand_dot_notation(flat: dict) -> dict:
    """Expand dot-notation keys into a nested dictionary.

    Example:
        {"parameters.broker.memoryProfile": "Low", "subscription": "abc"}
      → {"parameters": {"broker": {"memoryProfile": "Low"}}, "subscription": "abc"}
    """
    nested: dict = {}
    for key, value in flat.items():
        parts = key.split(".")
        current = nested
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = value
    return nested


def generate_overlays(overrides: dict, sites_local: Path) -> tuple[list[Path], list[str]]:
    """Generate sites.local/ YAML overlay files from an overrides dict.

    Pre-existing overlays are preserved (not overwritten): a hand-authored
    site in sites.local/ always wins over a CI-supplied override of the
    same name. The caller receives both the generated paths and the names
    of any skipped sites so it can surface a diagnostic if SITE_OVERRIDES
    silently has no effect.

    Args:
        overrides: Dict mapping site names to their override key/value pairs.
        sites_local: Path to the sites.local/ directory.

    Returns:
        Tuple of (generated file paths, skipped site names).

    Raises:
        ValueError: If a site name contains invalid characters.
    """
    sites_local.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []
    skipped: list[str] = []

    for site_name, values in overrides.items():
        if not SITE_NAME_PATTERN.match(site_name):
            raise ValueError(
                f"Invalid site name: '{site_name}' (must match {SITE_NAME_PATTERN.pattern})"
            )

        output_path = sites_local / f"{site_name}.yaml"
        if output_path.exists():
            skipped.append(site_name)
            continue

        expanded = expand_dot_notation(values)
        output_path.write_text(yaml.safe_dump(expanded, default_flow_style=False))
        generated.append(output_path)

    return generated, skipped


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate sites.local/ overlays from SITE_OVERRIDES JSON"
    )
    parser.add_argument("workspace", help="Workspace directory path")
    parser.add_argument("--from-env", metavar="VAR", help="Read JSON from environment variable")
    parser.add_argument("--file", metavar="PATH", help="Read JSON from file")
    args = parser.parse_args()

    if args.from_env:
        raw = os.environ.get(args.from_env, "")
    elif args.file:
        raw = Path(args.file).read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        print("No input provided. Use --from-env, --file, or pipe JSON to stdin.", file=sys.stderr)
        sys.exit(1)

    if not raw.strip():
        print("No site overrides configured, using committed sites")
        sys.exit(0)

    try:
        overrides = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in site overrides: {e}", file=sys.stderr)
        sys.exit(1)

    sites_local = Path(args.workspace) / "sites.local"

    try:
        generated, skipped = generate_overlays(overrides, sites_local)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    for path in generated:
        print(f"  {path.name}")
    print(f"Generated {len(generated)} site override(s)")
    if skipped:
        print(
            f"Skipped {len(skipped)} site(s) with pre-existing overlays in "
            f"sites.local/: {', '.join(sorted(skipped))} "
            f"(hand-authored overlays are never overwritten)"
        )


if __name__ == "__main__":
    main()
