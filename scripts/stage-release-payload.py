# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Stage one complete publication payload from qualified engine and workspace inputs."""

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

# Workflows may set PYTHONSAFEPATH, so sibling helpers are found through an explicit path entry.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from siteops_release_assets import (  # noqa: E402
    ENGINE_REFERENCE_FILES,
    ENGINE_REFERENCE_NAME,
    MAX_ENGINE_REFERENCE_BYTES,
    EngineReference,
    FrozenReleaseAssets,
    ReleaseAsset,
    ReleaseAssetsError,
    engine_reference,
    publication_assets,
)


def read_expected(path: Path, expected: str, maximum: int = 2 * 1024 * 1024) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ReleaseAssetsError("A publication input must be a regular file.")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum or hashlib.sha256(raw).hexdigest() != expected:
        raise ReleaseAssetsError("A publication input differs from its qualified identity.")
    return raw


def stage(
    plan: dict, plan_sha: str, native: FrozenReleaseAssets, output: Path, *,
    engine_directory: Path | None, workspace_directory: Path | None = None,
    workspace_sha: str | None = None, selected_engine: Path | None = None,
    selected_engine_sha: str | None = None,
    reference_directory: Path | None = None, reference_sha: str | None = None,
) -> FrozenReleaseAssets:
    if native.source != plan["source"]:
        raise ReleaseAssetsError("The native inventory describes a different candidate.")
    publication_assets({key: value for key, value in plan.items() if key != "workspaces"}, native)
    assets = list(native.assets)
    if plan.get("workspaces"):
        if (workspace_directory is None or workspace_sha is None or selected_engine is None
                or selected_engine_sha is None or reference_directory is None or reference_sha is None):
            raise ReleaseAssetsError("Workspace publication requires its qualified asset and engine identities.")
        workspace = FrozenReleaseAssets.from_bytes(read_expected(workspace_directory / "release-assets.json", workspace_sha))
        engine = json.loads(read_expected(selected_engine, selected_engine_sha))
        if (
            workspace.source != plan["source"] or workspace.engine is not None
            or engine.get("candidate") != plan["source"] or engine.get("planSha256") != plan_sha
        ):
            raise ReleaseAssetsError("The qualified workspaces and engine describe another candidate.")
        qualified_native = FrozenReleaseAssets.from_document(engine["native"])
        actual = {asset.name: asset.document() for asset in (native.assets if plan["siteops"]["bundle"] else native.engine.assets)}
        if actual != {asset.name: asset.document() for asset in qualified_native.assets}:
            raise ReleaseAssetsError("The engine assets changed after workspace qualification.")
        if plan["siteops"]["bundle"]:
            if qualified_native.source != plan["source"] or engine["reference"] is not None:
                raise ReleaseAssetsError("The qualified engine is not the selected build.")
        elif native.engine.document() != engine["reference"] or engine["version"] != plan["siteops"]["releaseTag"].removeprefix("siteops/v"):
            raise ReleaseAssetsError("The referenced engine changed after workspace qualification.")
        if {path.name for path in reference_directory.iterdir()} != ENGINE_REFERENCE_FILES:
            raise ReleaseAssetsError("The engine reference requires exactly its subject and proof.")
        raw = read_expected(reference_directory / ENGINE_REFERENCE_NAME, reference_sha, MAX_ENGINE_REFERENCE_BYTES)
        if EngineReference.from_bytes(raw) != engine_reference(plan, engine):
            raise ReleaseAssetsError("The public engine reference differs from qualified engine selection.")
        proof_path = reference_directory / (ENGINE_REFERENCE_NAME + ".attestation.jsonl")
        if proof_path.is_symlink() or not proof_path.is_file():
            raise ReleaseAssetsError("The engine reference proof must be a regular file.")
        with proof_path.open("rb") as stream:
            proof = stream.read(2 * 1024 * 1024 + 1)
        if not 0 < len(proof) <= 2 * 1024 * 1024:
            raise ReleaseAssetsError("The engine reference proof exceeds its size limit or is empty.")
        assets.extend(workspace.assets)
        assets.extend((
            ReleaseAsset(ENGINE_REFERENCE_NAME, len(raw), reference_sha),
            ReleaseAsset(proof_path.name, len(proof), hashlib.sha256(proof).hexdigest()),
        ))
    combined = FrozenReleaseAssets(native.repository, native.commit, native.source_ref, tuple(assets), native.engine)
    native_assets, workspace_assets = publication_assets(plan, combined)
    sources = {}
    for entries, root in ((native_assets, engine_directory), (workspace_assets, workspace_directory)):
        if entries and root is None:
            raise ReleaseAssetsError("The publication payload is missing an input directory.")
        sources.update({
            asset.name: (reference_directory if asset.name in ENGINE_REFERENCE_FILES else root) / asset.name
            for asset in entries
        })
    output.mkdir(mode=0o700)
    created = []
    complete = False
    try:
        for asset in combined.assets:
            source = sources[asset.name]
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != asset.size:
                raise ReleaseAssetsError("A publication input changed before payload staging.")
            target = output / asset.name
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created.append(target)
            size = 0
            digest = hashlib.sha256()
            with source.open("rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
                while chunk := incoming.read(min(1024 * 1024, asset.size - size + 1)):
                    size += len(chunk)
                    if size > asset.size:
                        raise ReleaseAssetsError("A publication input exceeds its expected size.")
                    digest.update(chunk)
                    outgoing.write(chunk)
            if (size, digest.hexdigest()) != (asset.size, asset.sha256):
                raise ReleaseAssetsError("A publication input differs from its qualified bytes.")
        complete = True
        return combined
    finally:
        if not complete:
            for path in reversed(created):
                try:
                    path.unlink()
                except OSError:
                    print("stage-release-payload: Incomplete output was retained.", file=sys.stderr)
            try:
                output.rmdir()
            except OSError:
                print("stage-release-payload: The incomplete payload directory was retained.", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name, metavar, help_text in (
        ("plan", "FILE", "Prepared release plan for the approved asset selection."),
        ("engine-inventory", "FILE", "Frozen inventory of built or referenced engine assets."),
        ("output", "DIRECTORY", "New directory for the complete publication payload."),
        ("output-inventory", "FILE", "Final inventory file within a new parent directory."),
    ):
        parser.add_argument("--" + name, required=True, type=Path, metavar=metavar, help=help_text)
    parser.add_argument("--expected-plan-sha", required=True, metavar="SHA256", help="Independent SHA-256 digest of --plan.")
    parser.add_argument("--expected-engine-inventory-sha256", required=True, metavar="SHA256", help="Independent SHA-256 digest of --engine-inventory.")
    parser.add_argument("--engine-directory", type=Path, metavar="DIRECTORY", help="Built engine assets to publish. Referenced engine assets remain in their own release.")
    parser.add_argument("--workspace-directory", type=Path, metavar="DIRECTORY", help="Qualified workspace assets and their release-assets.json inventory.")
    parser.add_argument("--expected-workspace-inventory-sha256", metavar="SHA256", help="Independent SHA-256 digest of release-assets.json in --workspace-directory.")
    parser.add_argument("--engine-selection", type=Path, metavar="FILE", help="Engine selection record used to qualify the workspaces.")
    parser.add_argument("--expected-engine-selection-sha256", metavar="SHA256", help="Independent SHA-256 digest of --engine-selection.")
    parser.add_argument("--engine-reference-directory", type=Path, metavar="DIRECTORY",
                        help="Exact engine-reference subject and detached proof from the signing job.")
    parser.add_argument("--expected-engine-reference-sha256", metavar="SHA256",
                        help="Independent SHA-256 digest of the generated engine reference.")
    args = parser.parse_args()
    try:
        plan = json.loads(read_expected(args.plan, args.expected_plan_sha))
        inventory = stage(
            plan, args.expected_plan_sha,
            FrozenReleaseAssets.from_bytes(read_expected(args.engine_inventory, args.expected_engine_inventory_sha256)), args.output,
            engine_directory=args.engine_directory, workspace_directory=args.workspace_directory,
            workspace_sha=args.expected_workspace_inventory_sha256, selected_engine=args.engine_selection,
            selected_engine_sha=args.expected_engine_selection_sha256,
            reference_directory=args.engine_reference_directory, reference_sha=args.expected_engine_reference_sha256,
        )
        args.output_inventory.parent.mkdir(mode=0o700)
        with args.output_inventory.open("xb") as stream:
            stream.write(inventory.serialized())
    except (ReleaseAssetsError, OSError, ValueError) as error:
        message = str(error) if isinstance(error, ValueError) else "Publication payload inputs or output could not be accessed."
        print(f"stage-release-payload: {message}", file=sys.stderr)
        return 1
    print("asset-list-sha=" + hashlib.sha256(inventory.serialized()).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
