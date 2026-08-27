"""Release behavior that differs across AIO versions and API generations."""

from pathlib import Path

import yaml


def _read(workspace: Path, relative_path: str) -> str:
    return (workspace / relative_path).read_text(encoding="utf-8")


def _release(workspace: Path, release: str) -> dict:
    path = workspace / "parameters" / "aio-releases" / f"{release}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_security_pki_defaults_follow_released_aio_behavior(workspace):
    create_2025 = _read(
        workspace,
        "templates/aio/modules/instance-2025-10-01.bicep",
    )
    create_2026_03 = _read(
        workspace,
        "templates/aio/modules/instance-2026-03-01.bicep",
    )
    create_2026_07 = _read(
        workspace,
        "templates/aio/modules/instance-2026-07-01.bicep",
    )
    update_extensions = _read(
        workspace,
        "templates/aio/upgrade/update-extensions.bicep",
    )

    application_uri = "connectors.values.securityPki.applicationUri"
    subject_name = "connectors.values.securityPki.subjectName"

    # The application URI starts with the 2026-03-01 generation. Subject name
    # starts in releases 2605 and 2606, then becomes a 2026-07-01 baseline.
    assert application_uri not in create_2025
    assert subject_name not in create_2025
    assert application_uri in create_2026_03
    assert subject_name in create_2026_03
    assert (
        "var configureSecurityPkiSubjectName = "
        "bool(releaseExtensionConfiguration.?securityPkiSubjectName ?? false)"
        in create_2026_03
    )
    assert application_uri in create_2026_07
    assert subject_name in create_2026_07
    assert "deriveAioExtensionSuffix(clusterResourceId)" in create_2026_03

    for release in ("2603", "2604"):
        assert _release(workspace, release)["aioReleaseConfiguration"] == {}
    for release in ("2605", "2606"):
        extension = _release(workspace, release)["aioReleaseConfiguration"]["extension"]
        assert extension["securityPkiSubjectName"] is True
    for release in ("2607", "2608"):
        extension = (
            _release(workspace, release)["aioReleaseConfiguration"].get("extension", {})
        )
        assert "securityPkiSubjectName" not in extension

    assert (
        "var aioApplicationUriDefault = aioApiVersion == '2025-10-01'\n  ? {}"
        in update_extensions
    )
    assert (
        "var configureSecurityPkiSubjectName = aioApiVersion == '2026-07-01' "
        "|| bool(releaseExtensionConfiguration.?securityPkiSubjectName ?? false)"
        in update_extensions
    )
    assert application_uri in update_extensions
    assert subject_name in update_extensions
    assert "contains(aio.configurationSettings" in update_extensions
    assert "aio.extensionSuffix" in update_extensions


def test_release_contract_does_not_opt_in_to_gds_manager(workspace):
    release_files = sorted(
        (workspace / "parameters" / "aio-releases").glob("*.yaml")
    )
    instance_modules = sorted(
        (workspace / "templates" / "aio" / "modules").glob("instance-*.bicep")
    )
    checked = release_files + instance_modules + [
        workspace / "templates" / "aio" / "upgrade" / "update-extensions.bicep"
    ]
    assert release_files and instance_modules

    for path in checked:
        assert "gdsManager" not in path.read_text(encoding="utf-8"), (
            f"{path}: Scale Kit configures published release requirements and "
            "does not opt in to GDS Manager"
        )
