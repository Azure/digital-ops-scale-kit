# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Prepare a release plan from one immutable Git commit."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from siteops_release import (
    ReleaseIntentError,
    discover_release_intent,
    inactive_release_plan,
    load_release_intent,
)

_SUCCESS = "Prepared release candidate."
_NO_INTENT = "No changed release intent."


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--dry-run", action="store_true", help="Prepare a non-publishable rehearsal, allowing committed examples.")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--intent")
    selection.add_argument("--before-sha")
    return parser.parse_args()


def _write_plan(output_dir: Path, plan: dict, notes: str | None) -> None:
    try:
        output_dir.mkdir(mode=0o700)
        plan_bytes = (json.dumps(plan, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        with (output_dir / "plan.json").open("xb") as output:
            output.write(plan_bytes)
        if notes is not None:
            with (output_dir / "release-notes.md").open(
                "x",
                encoding="utf-8",
                newline="",
            ) as output:
                output.write(notes)
    except OSError as error:
        raise ReleaseIntentError(
            "The output directory or release plan files could not be created."
        ) from error


def main() -> int:
    args = _arguments()
    root = Path.cwd()
    output_dir = Path(args.output_dir)
    try:
        if not output_dir.is_absolute():
            raise ReleaseIntentError("--output-dir must be an absolute path.")
        if os.path.lexists(output_dir):
            raise ReleaseIntentError("The output directory already exists.")
        intent_path = args.intent
        if intent_path is None:
            intent_path = discover_release_intent(root, args.before_sha, args.source_sha)

        if intent_path is None:
            plan = inactive_release_plan(
                root,
                args.source_sha,
                args.repository,
                args.source_ref,
                dry_run=args.dry_run,
            )
            notes = None
            status = _NO_INTENT
        else:
            intent = load_release_intent(
                root,
                args.source_sha,
                intent_path,
                args.repository,
                args.source_ref,
                dry_run=args.dry_run,
            )
            plan = intent.to_dict()
            notes = intent.notes
            status = _SUCCESS

        _write_plan(output_dir, plan, notes)
    except ReleaseIntentError as error:
        print(f"prepare-siteops-release: validation error: {error}", file=sys.stderr)
        return 2

    print(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
