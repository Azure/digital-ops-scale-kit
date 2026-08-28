"""AIO release configuration helpers for live integration tests."""

from pathlib import Path
from typing import Any

import yaml

from siteops.orchestrator import Orchestrator


def load_aio_release(
    orchestrator: Orchestrator,
    site_name: str,
    workspace_path: Path,
) -> tuple[str, dict[str, Any]]:
    """Load the release selected by a site and return its key and mapping."""
    site = orchestrator.load_site(site_name)
    release_key = site.properties.get("aioRelease")
    if not isinstance(release_key, str) or not release_key:
        raise AssertionError(
            f"Site '{site_name}' does not declare properties.aioRelease."
        )

    path = workspace_path / "parameters" / "aio-releases" / f"{release_key}.yaml"
    if not path.is_file():
        raise AssertionError(
            f"Site '{site_name}': release '{release_key}' has no configuration file."
        )

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AssertionError(
            f"Site '{site_name}': release '{release_key}' is not a mapping."
        )
    return release_key, data
