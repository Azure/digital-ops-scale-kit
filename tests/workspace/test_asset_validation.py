"""Workspace tests for what an asset declaration means.

The contracts every catalog family shares live in
`test_catalog_family_contracts.py`, which runs against each spec registered in
`catalog_harness.py`, including this family's. What is left here is what only
devices and assets mean, and what no generic check can know:

- The runtime composition contract resolves each `deviceRef` across all
  selected device and asset sources.
- Every generation module creates devices before assets, so the endpoint an
  asset binds to exists when the asset lands.
- The entry point reports the same `deviceRef` values the resources deploy with,
  so the report describes what the asset was actually bound to.
- The worked example the documentation points at still renders per site, which
  is the claim the selection mechanism is sold on.

Malformed, missing, cross-source, and external device references are exercised
through the generic engine contracts in `tests/test_composition.py`.
"""

import re

from tests.workspace import catalog_harness as harness
from tests.workspace.catalog_harness import ASSETS

# The declaration the documentation points at for per-site values. Named rather
# than counted, since another declaration carrying a site value would keep a
# count above zero while this one quietly stopped using one.
_WORKED_EXAMPLE = "parameters/assets/site-assets.yaml"
_WORKED_DEVICE_EXAMPLE = "parameters/devices/site-devices.yaml"

# The keys `properties.deviceRef` carries. A device is reached through one named
# inbound endpoint, so neither half identifies the binding on its own.
_DEVICE_REF_KEYS = ("deviceName", "endpointName")


def _declarations(workspace):
    return harness.declaration_files(workspace, ASSETS)


def _device_ref(asset: dict):
    """The `properties.deviceRef` value of one asset entry, whatever its shape."""
    properties = asset.get("properties")
    if not isinstance(properties, dict):
        return None
    return properties.get("deviceRef")


def device_ref_shape_failures(data, where: str) -> list[str]:
    """`deviceRef` is a mapping carrying both halves as non-empty strings.

    The template passes `properties` to the resource provider unchanged, and the
    compile probe only checks that the keys exist at the pinned generations. A
    `deviceRef` written as a bare string, or carrying an empty `endpointName`,
    satisfies both and reaches the provider as a binding to nothing.
    """
    failures: list[str] = []
    for i, asset in enumerate(harness.entries(data, "assets")):
        label = f"{where}: assets[{i}] ('{asset.get('name')}')"
        if not isinstance(asset.get("properties"), dict):
            continue
        device_ref = _device_ref(asset)
        if not isinstance(device_ref, dict):
            failures.append(
                f"{label} declares deviceRef as {type(device_ref).__name__}, "
                f"where a mapping of {list(_DEVICE_REF_KEYS)} belongs. An asset "
                f"reads through one named endpoint on one device."
            )
            continue
        for key in _DEVICE_REF_KEYS:
            value = device_ref.get(key)
            if not isinstance(value, str) or not value.strip():
                failures.append(
                    f"{label} sets deviceRef.{key} to {value!r}. Both halves "
                    f"name a resource by string, so an empty one binds the "
                    f"asset to nothing and the deploy still succeeds."
                )
    return failures


class TestAssetSemantics:
    """A committed asset declaration describes an asset that can read data."""

    def test_every_asset_declares_a_well_formed_device_ref(self, workspace):
        failures: list[str] = []
        for path in _declarations(workspace):
            failures.extend(
                device_ref_shape_failures(
                    harness.load_yaml(path), str(path.relative_to(workspace))
                )
            )
        assert not failures, "\n\n".join(failures)

    def test_worked_sets_resolve_device_and_endpoint_across_sources(
        self,
        workspace,
        orchestrator,
    ):
        errors = orchestrator.validate(
            workspace / "samples" / "asset-sample" / "manifest.yaml"
        )
        assert not errors, "\n".join(errors)

    def test_contract_resolves_the_endpoint_on_the_selected_device(self, workspace):
        contract = harness.composition_contract(workspace)
        rule = next(
            rule
            for rule in contract.references
            if rule.id == "asset-device-endpoint"
        )
        assert rule.source.collection == "assets"
        assert rule.target is not None
        assert rule.target.collection == "devices"
        assert rule.target.member is not None
        assert rule.target.member.name == "inboundEndpoints"

    def test_every_generation_module_orders_assets_after_devices(self, workspace):
        """Each module creates its devices before the assets that bind to them.

        An asset names its device and endpoint by string, and ARM does not model
        that relationship, so an asset deployed first succeeds and reads nothing.
        Losing this `dependsOn` would be invisible until a live run, and it has
        to hold in each module rather than in one of them, since a module serves
        one generation.
        """
        modules = [
            harness.version_module(workspace, ASSETS, version)
            for version in harness.supported_api_versions(workspace, ASSETS)
        ]
        modules = [module for module in modules if module.is_file()]
        assert len(modules) > 1, (
            f"Found {len(modules)} per-generation asset module(s), so the "
            f"ordering is not being checked on every generation. Either the "
            f"family stopped dispatching or its layout changed."
        )

        asset_type = ASSETS.kind("assets").resource_type
        failures: list[str] = []
        for module in modules:
            text = module.read_text(encoding="utf-8")
            blocks = text.split(f"resource assetResources '{asset_type}")
            if len(blocks) != 2:
                failures.append(
                    f"{module.name} declares no single `assetResources` resource "
                    f"of type '{asset_type}', so ordering cannot be checked."
                )
                continue
            if not re.search(r"dependsOn:[^\]]*\bdeviceResources\b", blocks[1], re.DOTALL):
                failures.append(
                    f"{module.name} does not make its assets depend on "
                    f"deviceResources. An asset would deploy before the device "
                    f"carrying the endpoint it binds to exists."
                )
        assert not failures, "\n".join(failures)

    def test_the_worked_example_still_renders_per_site(self, workspace, orchestrator):
        """The file the documentation points at proves the fan-out claim.

        `test_catalog_gating.py` holds the general rule, that any declaration
        reading a site value renders differently across the fleet. That rule
        checks nothing at all if no declaration reads one, so the worked example
        is named here: it is what the docs send an operator to read, and a
        rewrite that dropped its site variable would leave the claim unproven
        while every other check stayed green.
        """
        rendered = harness.resolved_declarations(workspace, orchestrator, ASSETS)
        worked_example = workspace / _WORKED_EXAMPLE
        assert worked_example in rendered, (
            f"{_WORKED_EXAMPLE} is no longer discovered as an asset declaration, "
            f"so the worked example the documentation points at is unchecked. "
            f"Discovered: {[str(p.relative_to(workspace)) for p in rendered]}"
        )

        source = harness.load_yaml(worked_example)
        assert "{{ site." in str(source), (
            f"{_WORKED_EXAMPLE} carries no site variable, so the per-site "
            f"rendering the documentation points at is no longer demonstrated "
            f"by the file it names."
        )

        per_site = {name: str(value) for name, value in rendered[worked_example].items()}
        assert len(set(per_site.values())) > 1, (
            f"{_WORKED_EXAMPLE} reads a site value but resolved identically for "
            f"every site, so the fleet would share one topic."
        )

    def test_the_worked_example_declares_an_enabled_device_and_asset(self, workspace):
        """The example deploys something that runs, not a disabled placeholder.

        A device that is not enabled presents no endpoint, and the connector then
        skips every asset bound to it. Neither flag defaults in the operator's
        favor, so the example states both, and a rewrite that dropped either
        would produce a deploy that looks healthy and reads nothing.
        """
        device_data = harness.load_yaml(workspace / _WORKED_DEVICE_EXAMPLE) or {}
        asset_data = harness.load_yaml(workspace / _WORKED_EXAMPLE) or {}

        devices = harness.entries(device_data, "devices")
        assets = harness.entries(asset_data, "assets")
        assert devices and assets, (
            f"The worked sets declare {len(devices)} device(s) and "
            f"{len(assets)} asset(s). The sample needs at least one of each."
        )

        failures: list[str] = []
        for entry in devices + assets:
            properties = entry.get("properties") or {}
            if properties.get("enabled") is not True:
                failures.append(
                    f"{_WORKED_EXAMPLE}: '{entry.get('name')}' sets enabled to "
                    f"{properties.get('enabled')!r}. A disabled device presents "
                    f"no endpoint, and a disabled asset is never served."
                )
        assert not failures, "\n".join(failures)


class TestDeviceNameProjectionRules:
    """ARM-legal names must also project as Kubernetes metadata names."""

    def test_uppercase_device_name_is_rejected(self):
        pattern, _, _ = ASSETS.name_rules(ASSETS.kind("devices"))
        assert pattern.fullmatch("Line-3-OPC-UA") is None

    def test_trailing_hyphen_device_name_is_rejected(self):
        pattern, _, _ = ASSETS.name_rules(ASSETS.kind("devices"))
        assert pattern.fullmatch("line-3-") is None
