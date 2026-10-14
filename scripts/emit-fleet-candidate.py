# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Render the exact candidate selection for Site and fleet acceptance without secrets."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Workflows may set PYTHONSAFEPATH, so sibling helpers are found through an explicit path entry.
sys.path.insert(1, str(Path(__file__).resolve().parent))

from fleet_workflow import FleetCandidate  # noqa: E402


def main() -> int:
    try:
        source = {
            "repository": os.environ["GITHUB_REPOSITORY"],
            "commit": os.environ["GITHUB_SHA"], "ref": os.environ["GITHUB_REF"],
        }
        mode = os.environ["CANDIDATE_PREVIEW"]
        if mode not in {"true", "false"}:
            raise ValueError("Invalid candidate mode.")
        value = {
            "apiVersion": "siteops.release.acceptance/v1", "kind": "FleetCandidate",
            "source": source,
            "producer": {
                "run": int(os.environ["GITHUB_RUN_ID"]), "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
                "caller": os.environ["CANDIDATE_CALLER"], "preview": mode == "true",
            },
            "artifacts": {
                role: {"id": int(os.environ[f"FLEET_{role.upper()}_ID"]),
                       "sha256": os.environ[f"FLEET_{role.upper()}_SHA"]}
                for role in ("admission", "plan", "inventory", "engine", "workspaces")
            },
        }
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
        FleetCandidate.parse(raw.encode(), repository=source["repository"], commit=source["commit"], ref=source["ref"])
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("candidate=" + raw + "\n")
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write("\n### Exact candidate selection\n\n")
            stream.write("This identifies the frozen candidate. It does not authorize Azure use or publication.\n\n")
            stream.write("```json\n" + json.dumps(value, indent=2, sort_keys=True) + "\n```\n")
        # The log copy lets a maintainer read the same selection through the jobs API. It holds no secret.
        print("Exact candidate selection: " + raw)
    except (ValueError, OSError, KeyError, TypeError):
        print("The exact candidate selection could not be rendered.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
