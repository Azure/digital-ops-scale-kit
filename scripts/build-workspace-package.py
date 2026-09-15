# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Produce an unsigned workspace package from an exact reviewed Git commit."""

import argparse
import json
import sys
import tempfile
from pathlib import Path

from source_snapshot import (
    SourceSnapshotError,
    export_tracked_source,
    require_complete_export,
    require_workspace_configuration_boundary,
    validate_repository,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from siteops.artifacts import ArtifactError, relative_artifact_path  # noqa: E402
from siteops.package_builder import (  # noqa: E402
    build_package,
    create_producer_compilation_session,
    workspace_bicep_sources,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Clean source repository")
    parser.add_argument("--expected-source-sha", required=True, help="Exact source commit")
    parser.add_argument("--workspace", required=True, help="Source-relative complete workspace")
    parser.add_argument("--id", required=True, help="Provider-neutral kit identifier")
    parser.add_argument("--version", required=True, help="Kit version, independent of the engine version")
    parser.add_argument("--requires-siteops", required=True, help="Bounded PEP 440 engine range")
    parser.add_argument("--require-feature", action="append", default=[], help="Required engine feature")
    parser.add_argument("--include", action="append", default=[], help="Approved companion file or directory")
    parser.add_argument("--license", action="append", required=True, help="Required source-relative license file")
    parser.add_argument(
        "--bicep",
        type=Path,
        help="Existing Azure CLI-managed Bicep executable",
    )
    parser.add_argument("--output", required=True, type=Path, help="New output ZIP path")
    args = parser.parse_args()
    try:
        root = args.root.resolve()
        output = args.output.absolute()
        workspace = "." if args.workspace == "." else relative_artifact_path(args.workspace)
        companions = tuple(relative_artifact_path(value) for value in (*args.include, *args.license))
        paths = (workspace, *companions)
        validate_repository(root, args.expected_source_sha)
        with tempfile.TemporaryDirectory(prefix="siteops-package-source-") as temporary:
            control_root = Path(temporary)
            snapshot = control_root / "source"
            export_tracked_source(root, snapshot, args.expected_source_sha, paths=paths)
            require_complete_export(root, args.expected_source_sha, paths, snapshot)
            bicep_sources = workspace_bicep_sources(snapshot, workspace)
            if bicep_sources:
                require_workspace_configuration_boundary(
                    root,
                    args.expected_source_sha,
                    workspace,
                )
            for license_path in args.license:
                if not snapshot.joinpath(*license_path.split("/")).is_file():
                    raise ArtifactError("A declared package license file is missing.")
            result = build_package(
                snapshot, output, workspace=workspace, kit_id=args.id, version=args.version,
                source_revision=args.expected_source_sha, siteops_range=args.requires_siteops,
                companions=companions,
                required_features=tuple(args.require_feature) or ("manifest/v1",),
                compilation_session_factory=lambda: create_producer_compilation_session(
                    snapshot,
                    control_root,
                    bicep_path=args.bicep,
                ),
            )
    except (ArtifactError, SourceSnapshotError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(json.dumps({
        "file": output.name, "sha256": result.sha256, "size": result.size,
        "kit": result.metadata.kit_id, "version": result.metadata.version,
        "workspace": result.metadata.workspace_root, "files": len(result.metadata.files),
        "templates": len(result.metadata.templates),
        "provenance": "not-established",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
