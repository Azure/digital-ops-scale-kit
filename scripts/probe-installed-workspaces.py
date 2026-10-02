# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Qualify verified workspace packages with the selected installed engine."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import sys
from pathlib import Path


class QualificationError(ValueError):
    """The selected installed engine did not satisfy workspace qualification."""


def _read(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise QualificationError("A qualification input exceeds its byte limit.")
    return raw


def _json(raw: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise QualificationError("Qualification JSON contains duplicate fields.")
            result[key] = value
        return result

    def invalid(value):
        raise QualificationError("Qualification JSON contains a non-JSON number.")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def qualify(spec: dict) -> dict:
    # Import Site Ops only in this isolated process, never from the controller checkout.
    import siteops

    prefix = Path(sys.prefix).resolve()
    module = Path(siteops.__file__).resolve()
    if (
        sys.prefix == sys.base_prefix
        or not sys.flags.isolated
        or not module.is_relative_to(prefix)
        or importlib.metadata.version("siteops") != spec["engineVersion"]
        or siteops.__version__ != spec["engineVersion"]
        or not Path(importlib.metadata.distribution("siteops").locate_file("siteops"))
        .resolve()
        .is_relative_to(prefix)
    ):
        raise QualificationError(
            "The probe must use the exact selected engine in its isolated installation."
        )
    from siteops.browse import ContentReader
    from siteops.github_attestation import load_github_policy, verify_github_artifact
    from siteops.orchestrator import Orchestrator
    from siteops.workspace_acquisition import WorkspaceAcquisition
    from siteops.workspace_cache import WorkspaceCache
    from siteops.workspace_source import (
        ArtifactIdentity,
        ResolvedReleaseSource,
        ResolvedWorkspaceSource,
    )

    assets = Path(spec["assets"]).resolve()
    state = Path(spec["state"])
    if (
        not state.is_absolute()
        or os.path.lexists(state)
        or state.resolve().is_relative_to(assets)
        or assets.is_relative_to(state.resolve())
    ):
        raise QualificationError(
            "Qualification requires new state outside verified workspace assets."
        )
    source = spec["source"]
    descriptor_id = ArtifactIdentity.from_document(spec["descriptor"])
    release = ResolvedReleaseSource(
        "github-release/v1",
        "github:" + source["repository"],
        source["release"],
        source["commit"],
        descriptor_id,
    )
    descriptor = release.parse_descriptor(_read(assets / descriptor_id.name, 256 * 1024))
    project_workspace = spec.get("projectWorkspace")
    if "projectWorkspace" in spec and (
        not isinstance(project_workspace, str)
        or sum(entry.workspace == project_workspace for entry in descriptor.workspaces) != 1
    ):
        raise QualificationError("Select exactly one declared workspace for the candidate project.")
    if project_workspace is not None:
        from siteops.project import WorkspacePin, write_pin
    policy_path, roots = Path(spec["policy"]).resolve(), Path(spec["trustedRoot"]).resolve()
    if policy_path.is_relative_to(assets) or roots.is_relative_to(assets):
        raise QualificationError(
            "Qualification trust inputs must be independent of workspace assets."
        )
    policy = load_github_policy(policy_path)
    if policy.repository != source["repository"] or policy.source_ref != source["ref"]:
        raise QualificationError("The qualification policy does not approve this workspace source.")
    if hashlib.sha256(_read(roots, 2 * 1024 * 1024)).hexdigest() != policy.trusted_root_sha256:
        raise QualificationError("Qualification roots differ from the selected policy.")
    state.mkdir(mode=0o700)
    cache = WorkspaceCache(state / "cache")
    sites = state / "operator"
    sites.mkdir(mode=0o700)
    (sites / "sites").mkdir(mode=0o700)

    def verify(artifact, proof, selected):
        receipt = verify_github_artifact(
            artifact,
            proof,
            roots,
            policy_path,
            expected_sha256=selected.entry.package.sha256,
            source_commit=source["commit"],
            staging_parent=cache.root / "staging",
        )
        if (
            receipt.policy_sha256 != policy.sha256
            or receipt.root_sha256 != policy.trusted_root_sha256
        ):
            raise QualificationError("Qualification policy changed during workspace use.")
        return receipt

    acquisition = WorkspaceAcquisition(cache, verify=verify)
    packages = manifests = 0
    project_selection = None
    for entry in descriptor.workspaces:
        selected = ResolvedWorkspaceSource(release, entry)
        cache.retain_proof(assets / entry.proof.name, entry.proof)
        acquisition.publish(selected, assets / entry.package.name)
        with acquisition.lease(selected) as content:
            workspace = content.package_root
            if entry.workspace != ".":
                workspace = workspace.joinpath(*entry.workspace.split("/"))
            reader = ContentReader(workspace)
            entries = reader.inventory()
            if not reader.names_complete:
                raise QualificationError(
                    "The installed engine could not enumerate the complete manifest catalog."
                )
            for catalog_entry in entries:
                binding = content.bind("./" + catalog_entry.path)
                orchestrator = Orchestrator(
                    binding.workspace,
                    site_config_root=sites,
                    materialized_package=binding,
                )
                orchestrator.load_manifest(binding.manifest_path)
                manifests += 1
            if entry.workspace == project_workspace:
                project_selection = selected
        packages += 1
    if packages != len(descriptor.workspaces) or packages == 0:
        raise QualificationError("No complete workspace qualification was recorded.")
    for name, imported in tuple(sys.modules.items()):
        if name == "siteops" or name.startswith("siteops."):
            origin = getattr(imported, "__file__", None)
            if origin is None or not Path(origin).resolve().is_relative_to(prefix):
                raise QualificationError(
                    "A workspace operation imported engine code outside its installation."
                )
    result = {
        "apiVersion": "siteops.release.qualification/v1",
        "kind": "WorkspaceEngineQualification",
        "engineVersion": spec["engineVersion"],
        "python": platform.python_version(),
        "platform": sys.platform,
        "workspaceInventorySha256": spec["workspaceInventorySha256"],
        "packages": packages,
        "catalogManifests": manifests,
        "checks": [
            "package-integrity",
            "engine-compatibility",
            "cache-publication",
            "cache-lease",
            "guarded-catalog-load",
        ],
        "deployment": "not-run",
        "workloadHealth": "not-checked",
    }
    if project_workspace is not None:
        if project_selection is None:
            raise QualificationError("The candidate workspace project was not created.")
        project_pin = write_pin(
            sites, WorkspacePin(project_selection), expected_previous=None,
        ).sha256
        result["project"] = {
            "workspace": project_workspace, "pinSha256": project_pin,
            "sourceReleaseObservation": "not-performed",
        }
        result["checks"].append("candidate-project-pin")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, type=Path, metavar="FILE", help="Qualification specification created by the engine installation controller.")
    parser.add_argument("--expected-spec-sha", required=True, metavar="SHA256", help="Independent SHA-256 digest of --spec.")
    args = parser.parse_args()
    try:
        raw = _read(args.spec, 2 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != args.expected_spec_sha:
            raise QualificationError(
                "The qualification specification differs from its expected identity."
            )
        spec = _json(raw)
        required = {
            "engineVersion",
            "assets",
            "state",
            "source",
            "descriptor",
            "policy",
            "trustedRoot",
            "workspaceInventorySha256",
        }
        if (type(spec) is not dict or not required <= spec.keys()
                or spec.keys() - required - {"projectWorkspace"}):
            raise QualificationError(
                "The qualification specification is incomplete or unsupported."
            )
        result = qualify(spec)
    except (ImportError, AttributeError):
        print(
            "probe-installed-workspaces: The selected installed engine lacks the required workspace interfaces.",
            file=sys.stderr,
        )
        return 1
    except (ValueError, OSError, KeyError) as error:
        message = (
            str(error)
            if isinstance(error, ValueError)
            else "Qualification inputs could not be consumed."
        )
        print(f"probe-installed-workspaces: {message}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
