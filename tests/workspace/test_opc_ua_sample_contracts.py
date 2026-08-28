"""Workspace contracts for the OPC UA solution sample.

The sample's data path fails silently when one of these is wrong: every ARM
resource is created, every deployment assertion passes, and no telemetry
arrives. The integration suite proves the path end to end, but it needs a live
cluster, so these run in every lane instead.

Asserted against compiled ARM rather than the Bicep text. A text search cannot
tell `properties.enabled` from an `enabled` key under `tags`, and it reads a
commented-out declaration as a live one, so it can pass against a sample that
regressed. Compiling resolves both, and it is what the deployment itself sends
to ARM.
"""

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.workspace.conftest import az_path

SAMPLE_TEMPLATE = "samples/opc-ua-solution/template.bicep"

DEVICE_TYPE = "Microsoft.DeviceRegistry/namespaces/devices"
ASSET_TYPE = "Microsoft.DeviceRegistry/namespaces/assets"


def _compile(template: Path) -> dict:
    """Compile a Bicep template and return its ARM JSON.

    Raises:
        AssertionError: the compile failed, so nothing was validated.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "template.json"
        result = subprocess.run(
            [az_path(), "bicep", "build", "--file", str(template), "--outfile", str(out)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0, (
            f"`az bicep build` failed on {template.name}, so the contracts "
            f"below validated nothing.\n{(result.stderr or '').strip()[:2000]}"
        )
        return json.loads(out.read_text(encoding="utf-8"))


def _resources_of_type(arm: dict, resource_type: str) -> list[dict]:
    """Every resource of one type, across both compiled resource shapes.

    Bicep emits `resources` as a mapping keyed by symbolic name under
    `languageVersion` 2.0 and as a list before it. Both are handled so a
    toolchain change does not quietly reduce this to matching nothing.
    """
    resources = arm.get("resources", {})
    values = resources.values() if isinstance(resources, dict) else resources
    return [r for r in values if r.get("type") == resource_type]


@pytest.fixture(scope="module")
def sample_arm(workspace: Path) -> dict:
    """The compiled sample, built once for every contract in this module."""
    return _compile(workspace / SAMPLE_TEMPLATE)


@pytest.mark.parametrize(
    ("resource_type", "label"),
    [(DEVICE_TYPE, "device"), (ASSET_TYPE, "asset")],
)
def test_sample_resource_is_enabled(sample_arm, resource_type, label):
    """The device and the asset each resolve to `enabled: true`.

    A device that is not enabled presents no inbound endpoint, so the OPC UA
    supervisor skips every asset that refers to it. An asset that is not
    enabled is not polled. The field defaults to false when absent, which is
    why its absence moves no data while every deployment assertion passes.
    """
    resources = _resources_of_type(sample_arm, resource_type)

    assert len(resources) == 1, (
        f"Expected exactly one `{resource_type}` in the OPC UA sample, found "
        f"{len(resources)}. If the sample now declares several, this contract "
        f"needs to name which one carries the data path."
    )

    enabled = resources[0].get("properties", {}).get("enabled")
    assert enabled is True, (
        f"The OPC UA sample's {label} resolves to `properties.enabled = "
        f"{enabled!r}` rather than True. The supervisor then serves no data "
        f"for it, so the sample deploys clean while no telemetry reaches the "
        f"broker."
    )


def test_contract_reads_properties_rather_than_any_enabled_key(tmp_path):
    """What this contract must NOT accept.

    Compiled ARM distinguishes `properties.enabled` from an `enabled` key
    elsewhere and excludes declarations inside block comments. This proves both
    boundaries rather than assuming them.
    """
    decoy = tmp_path / "decoy.bicep"
    decoy.write_text(
        "resource device 'Microsoft.DeviceRegistry/namespaces/devices@2025-10-01' = {\n"
        "  name: 'ns/dev'\n"
        "  tags: {\n"
        "    enabled: 'true'\n"
        "  }\n"
        "  properties: {\n"
        "    endpoints: {}\n"
        "  }\n"
        "}\n"
        "\n"
        "/*\n"
        "resource commentedOut 'Microsoft.DeviceRegistry/namespaces/assets@2025-10-01' = {\n"
        "  name: 'ns/asset'\n"
        "  properties: {\n"
        "    enabled: true\n"
        "  }\n"
        "}\n"
        "*/\n",
        encoding="utf-8",
    )

    arm = _compile(decoy)

    devices = _resources_of_type(arm, DEVICE_TYPE)
    assert len(devices) == 1
    assert devices[0].get("properties", {}).get("enabled") is None, (
        "An `enabled` key under `tags` was read as `properties.enabled`."
    )

    assert not _resources_of_type(arm, ASSET_TYPE), (
        "A resource declared inside a block comment was treated as live."
    )
