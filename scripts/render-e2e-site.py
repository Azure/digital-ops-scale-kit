#!/usr/bin/env python3
"""Render the E2E site template into a writable directory.

Reads `tests/e2e/sites/e2e-test.yaml.tmpl`, substitutes `${E2E_*}` placeholders
from the environment, and writes the result to a target directory suitable for
use with `SITEOPS_EXTRA_SITES_DIRS`.

Required environment variables:
    E2E_RESOURCE_GROUP   Resource group (operator-supplied in persistent mode; workflow-created in ephemeral)
    E2E_CLUSTER_NAME     Arc-connected cluster name
    E2E_AIO_RELEASE      AIO release selector

Auto-computed when unset (local developer convenience; CI sets these explicitly):
    E2E_SITE_NAME        Defaults to `e2e-local-<unix_epoch>`
    E2E_SUBSCRIPTION     Defaults to `az account show --query id -o tsv`
    E2E_LOCATION         Defaults to `az group show -n $E2E_RESOURCE_GROUP --query location -o tsv`

Usage:
    python scripts/render-e2e-site.py \
        --template tests/e2e/sites/e2e-test.yaml.tmpl \
        --output-dir "$RUNNER_TEMP/e2e-sites"

Exits non-zero if any required variable is missing, if an `az` fallback fails,
or if any `${...}` placeholder remains un-substituted after rendering.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import string
import subprocess
import time
from pathlib import Path

REQUIRED_VARS = ("E2E_RESOURCE_GROUP", "E2E_CLUSTER_NAME", "E2E_AIO_RELEASE")
OPTIONAL_VARS = ("E2E_SITE_NAME", "E2E_SUBSCRIPTION", "E2E_LOCATION")
ALL_VARS = REQUIRED_VARS + OPTIONAL_VARS

UNRESOLVED_PATTERN = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def _run_az(args: list[str]) -> str:
    """Run an `az` command and return stripped stdout, or raise RuntimeError.

    Resolves `az` via `shutil.which` so Windows (where the CLI ships as a
    `.cmd` and `subprocess.run` cannot resolve it through PATHEXT when
    `shell=False`) behaves identically to Linux and macOS.
    """
    az_exe = shutil.which("az")
    if az_exe is None:
        raise RuntimeError("Azure CLI (`az`) not found on PATH; cannot auto-compute defaults.")
    try:
        result = subprocess.run(
            [az_exe, *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        raise RuntimeError(f"`az {' '.join(args)}` failed: {stderr}") from e
    return result.stdout.strip()


def compute_defaults(values: dict[str, str]) -> dict[str, str]:
    """Fill in optional vars using safe defaults when unset."""
    out = dict(values)

    if not out.get("E2E_SITE_NAME"):
        out["E2E_SITE_NAME"] = f"e2e-local-{int(time.time())}"

    if not out.get("E2E_SUBSCRIPTION"):
        out["E2E_SUBSCRIPTION"] = _run_az(["account", "show", "--query", "id", "-o", "tsv"])

    if not out.get("E2E_LOCATION"):
        rg = out["E2E_RESOURCE_GROUP"]
        out["E2E_LOCATION"] = _run_az(
            ["group", "show", "--name", rg, "--query", "location", "-o", "tsv"]
        )

    return out


def collect_values() -> dict[str, str]:
    """Read env vars, fail fast on missing required, compute defaults."""
    values = {name: os.environ.get(name, "").strip() for name in ALL_VARS}

    missing = [name for name in REQUIRED_VARS if not values[name]]
    if missing:
        raise SystemExit(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + "\nSet them and retry. See scripts/render-e2e-site.py docstring."
        )

    try:
        return compute_defaults(values)
    except RuntimeError as e:
        raise SystemExit(f"Error auto-computing defaults: {e}") from e


def render(template_path: Path, values: dict[str, str]) -> str:
    template = string.Template(template_path.read_text(encoding="utf-8"))
    # `safe_substitute` lets bare `$` characters (e.g. shell-style `$VAR` in
    # comments, or `$$` literals) pass through unchanged. The
    # `UNRESOLVED_PATTERN` check below still catches genuine missing
    # `${...}` placeholders, which is the failure mode that matters.
    rendered = template.safe_substitute(values)

    leftovers = UNRESOLVED_PATTERN.findall(rendered)
    if leftovers:
        raise SystemExit(
            "Rendered output still contains placeholders: "
            + ", ".join(sorted(set(leftovers)))
        )

    return rendered


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render the E2E site template into an extra-trusted-sites dir."
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=Path("tests/e2e/sites/e2e-test.yaml.tmpl"),
        help="Path to the E2E site template (default: %(default)s)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory to write the rendered site into. Created if missing.",
    )
    parser.add_argument(
        "--output-name",
        default=None,
        help="Rendered file name (default: <E2E_SITE_NAME>.yaml). "
             "Using the site name as the filename matches siteops' "
             "convention that file stem equals `name:` field, which lets "
             "orchestrator.load_site(name) resolve the site directly.",
    )
    args = parser.parse_args()

    if not args.template.is_file():
        raise SystemExit(f"Template not found: {args.template}")

    values = collect_values()
    rendered = render(args.template, values)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_name = args.output_name or f"{values['E2E_SITE_NAME']}.yaml"
    output_path = args.output_dir / output_name
    output_path.write_text(rendered, encoding="utf-8")

    print(f"Rendered E2E site -> {output_path}")


if __name__ == "__main__":
    main()
