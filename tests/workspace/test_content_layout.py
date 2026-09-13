"""Public entry ownership and shared resource-set layout."""

import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml


def _entries(workspace: Path) -> list[Path]:
    core = sorted((workspace / "manifests").glob("*/manifest.yaml"))
    samples = sorted((workspace / "samples").glob("*/manifest.yaml"))
    assert core and samples, "Both core operations and samples must be discoverable."
    return core + samples


def test_public_entries_own_their_guide_and_identity(workspace):
    names = set()
    for manifest in _entries(workspace):
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        assert data["name"] == manifest.parent.name
        assert data["name"] not in names
        names.add(data["name"])
        guide = manifest.with_name("README.md")
        assert guide.is_file(), f"{manifest.relative_to(workspace)} has no operator guide."
        for match in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", guide.read_text(encoding="utf-8")):
            target = urlsplit(match.group(1))
            if not target.scheme and target.path:
                assert (guide.parent / target.path).exists(), (
                    f"{guide.relative_to(workspace)} links to missing {target.path}"
                )


def test_shared_sets_have_one_library_owner(workspace):
    for area in ("devices", "assets", "dataflows"):
        directory = workspace / "resource-sets" / area
        assert list(directory.glob("*.yaml")), f"No resource sets discovered in {area}."
        assert not list((workspace / "parameters" / area).glob("*.yaml"))
    assert (workspace / "samples" / "dataflow-sample" / "dataflows.yaml").is_file()


def test_shared_and_implementation_partials_keep_their_owners(workspace):
    shared = list((workspace / "manifests" / "_partials").glob("_*.yaml"))
    assert shared
    assert not list((workspace / "manifests").glob("*.yaml"))
    for relative in (
        "templates/host-bootstrap/aksee",
        "templates/host-ops/aksee-upgrade",
        "samples/opc-ua-solution",
        "samples/secretsync-sample",
    ):
        assert (workspace / relative / "_partial.yaml").is_file()
