# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Build the complete unsigned workspace asset set from a reviewed release file."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Workflows may set PYTHONSAFEPATH, so sibling helpers are found through an explicit path entry.
sys.path.insert(1, str(Path(__file__).resolve().parent))

from siteops_release import (  # noqa: E402
    ReleaseIntentError,
    bind_prepared_plan,
    load_release_intent,
)
from source_snapshot import SourceSnapshotError, validate_repository  # noqa: E402
from workspace_producer import build_committed_workspace  # noqa: E402
from workspace_release import BUILD_RECORD, WorkspaceReleaseError  # noqa: E402

from siteops.artifacts import (  # noqa: E402
    ArtifactError,
    require_node,
)
from siteops.browse import BrowseError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, metavar="DIRECTORY", help="Clean source repository.")
    parser.add_argument("--repository", required=True, help="Source repository in owner/repository form.")
    parser.add_argument("--expected-source-sha", required=True, metavar="COMMIT", help="Exact reviewed Git commit.")
    parser.add_argument("--source-ref", required=True, metavar="REF", help="Full Git ref for the reviewed source.")
    parser.add_argument("--release-file", required=True, metavar="PATH", help="Path to the committed release.json, relative to the repository.")
    parser.add_argument("--output-dir", required=True, type=Path, metavar="DIRECTORY", help="New absolute directory for unsigned packages and workspace-builds.json.")
    parser.add_argument("--build-number", type=int, help="Engine build run number. Required when the release builds an engine.")
    parser.add_argument("--build-attempt", type=int, help="Engine build run attempt. Required when the release builds an engine.")
    parser.add_argument("--bicep", type=Path, metavar="FILE", help="Provisioned Bicep executable to use through Azure CLI.")
    parser.add_argument("--dry-run", action="store_true", help="Permit a committed release example and mark the unsigned build record as a preview.")
    parser.add_argument("--prepared-plan", type=Path, metavar="FILE", help="Prepared release plan to compare with the committed declaration.")
    parser.add_argument("--expected-plan-sha", metavar="SHA256", help="Independent SHA-256 digest of --prepared-plan. Supply both options together.")
    parser.add_argument("--release-workspace", metavar="PATH", help="Build only this exact workspace path from the reviewed declaration.")
    args = parser.parse_args()
    if (args.prepared_plan is None) != (args.expected_plan_sha is None):
        parser.error("--prepared-plan and --expected-plan-sha must be supplied together")
    created: list[Path] = []
    output_created = False
    complete = False
    try:
        root = args.root.resolve()
        output = args.output_dir
        if not output.is_absolute() or os.path.lexists(output):
            raise WorkspaceReleaseError("Select a new absolute output directory.")
        require_node(output.parent, directory=True)
        validate_repository(root, args.expected_source_sha)
        intent = load_release_intent(
            root, args.expected_source_sha, args.release_file, args.repository,
            args.source_ref, dry_run=args.dry_run,
        )
        if not intent.workspaces:
            raise WorkspaceReleaseError("The release file does not declare workspace builds.")
        plan_sha = bind_prepared_plan(intent, args.prepared_plan, args.expected_plan_sha)
        requests = intent.workspaces
        if args.release_workspace is not None:
            requests = tuple(request for request in requests if request.workspace == args.release_workspace)
            if len(requests) != 1:
                raise WorkspaceReleaseError("Select an exact workspace from the reviewed release declaration.")
        engine_version = intent.engine_version(args.build_number, args.build_attempt)
        for request in requests:
            request.require_engine(engine_version)
        output.mkdir(mode=0o700)
        output_created = True
        workspaces = []
        for request in requests:
            path = output / request.package_name
            inspection, index = build_committed_workspace(
                root, intent.source_sha, path, workspace=request.workspace,
                kit_id=request.kit_id, version=intent.version, siteops_range=request.siteops_range,
                includes=request.includes or (), licenses=request.licenses,
                required_features=request.required_features or ("manifest/v1",),
                engine_version=engine_version, bicep_path=args.bicep, check_index=True,
            )
            created.append(path)
            record = {
                "workspace": inspection.metadata.workspace_root,
                "kit": {"id": inspection.metadata.kit_id, "version": inspection.metadata.version},
                "package": {"name": path.name, "size": inspection.size, "sha256": inspection.sha256},
                "compatibility": inspection.metadata.document()["compatibility"],
            }
            if index is not None:
                record["index"] = {"sha256": index}
            workspaces.append(record)
        document = {
            "apiVersion": "siteops.release.workspaces/v1", "kind": "WorkspaceBuilds",
            "source": intent.to_dict()["source"],
            "intent": {"path": intent.intent_path, "sha256": intent.intent_sha256},
            "planSha256": plan_sha,
            "dryRun": intent.dry_run, "engineVersion": engine_version,
            "provenance": "not-established", "workspaces": workspaces,
        }
        raw = (json.dumps(document, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
        record_path = output / BUILD_RECORD
        descriptor = os.open(record_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created.append(record_path)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        complete = True
    except (ReleaseIntentError, WorkspaceReleaseError, SourceSnapshotError, ArtifactError, BrowseError) as error:
        print(f"build-workspace-release: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("build-workspace-release: Workspace output could not be created or retained.", file=sys.stderr)
        return 1
    finally:
        if output_created and not complete:
            for path in reversed(created):
                try:
                    path.unlink()
                except OSError:
                    print("build-workspace-release: An incomplete output file could not be removed.", file=sys.stderr)
            try:
                output.rmdir()
            except OSError:
                print("build-workspace-release: The incomplete output directory was retained.", file=sys.stderr)
    print(json.dumps({
        "file": BUILD_RECORD, "sha256": hashlib.sha256(raw).hexdigest(),
        "workspaces": len(workspaces), "provenance": "not-established",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
