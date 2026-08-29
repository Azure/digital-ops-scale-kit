"""Workspace tests for what a dataflow declaration means.

The contracts every catalog family shares live in
`test_catalog_family_contracts.py`, which runs against each spec registered in
`catalog_harness.py`, including this family's. What is left here is what only
dataflows mean, and what no generic check can know:

- Every `endpointRef` resolves to a declared endpoint or to the endpoint the
  instance owns, and every `profileRef` resolves to a declared profile or to the
  profile the instance owns. A mistyped reference deploys clean and never moves
  data.
- Each generation builds the dataflow resource name from `profileRef` or the
  instance-owned default.
- The fallback profile names the profile AIO creates alongside the instance, so
  an entry that omits `profileRef` lands somewhere real.
- `endpointType` names a real variant and carries the settings object that
  variant needs. An unknown value compiles clean and turns off the resource
  provider's own property validation for the whole entry.
- The worked example the documentation points at still renders per site, which
  is the claim the selection mechanism is sold on.
"""

import re

from siteops.orchestrator import Orchestrator
from tests.workspace import catalog_harness as harness
from tests.workspace.catalog_harness import DATAFLOWS

# `endpointType` is the discriminator that selects which settings shape the
# resource provider validates. Derived from the settings variants Bicep lists for
# `DataflowEndpointProperties`: dataExplorerSettings, dataLakeStorageSettings,
# fabricOneLakeSettings, kafkaSettings, localStorageSettings, mqttSettings,
# openTelemetrySettings.
_ENDPOINT_TYPES = frozenset(
    {
        "DataExplorer",
        "DataLakeStorage",
        "FabricOneLake",
        "Kafka",
        "LocalStorage",
        "Mqtt",
        "OpenTelemetry",
    }
)

# The declaration the documentation points at for per-site values. Named rather
# than counted, since another declaration carrying a site value would keep a
# count above zero while this one quietly stopped using one.
_WORKED_EXAMPLE = "parameters/dataflows/site-telemetry.yaml"


def _settings_key(endpoint_type: str) -> str:
    """The settings property paired with an endpoint type.

    The provider names them mechanically, lowercasing the first character of
    the type and appending `Settings`.
    """
    return f"{endpoint_type[0].lower()}{endpoint_type[1:]}Settings"


def _declarations(workspace):
    return harness.declaration_files(workspace, DATAFLOWS)


class TestDataflowSemantics:
    """A committed dataflow declaration describes a pipeline that can run."""

    def test_endpoint_and_profile_references_use_the_composed_graph(
        self,
        workspace,
        orchestrator,
    ):
        contract = harness.composition_contract(workspace)
        targets = {
            rule.id: rule.target.collection if rule.target else None
            for rule in contract.references
        }
        assert targets["dataflow-profile"] == "dataflowProfiles"
        assert targets["dataflow-source-endpoint"] == "dataflowEndpoints"
        assert targets["dataflow-destination-endpoint"] == "dataflowEndpoints"

        errors = orchestrator.validate(
            workspace / "samples" / "dataflow-sample" / "manifest.yaml"
        )
        assert not errors, "\n".join(errors)

    def test_selectable_worked_set_validates_through_the_catalog(
        self,
        workspace,
        tmp_path,
    ):
        extra_sites = tmp_path / "sites"
        extra_sites.mkdir()
        (extra_sites / "catalog-reference-test.yaml").write_text(
            """
apiVersion: siteops/v1
kind: Site
name: catalog-reference-test
inherits: base-site.yaml
subscription: "00000000-0000-0000-0000-000000000000"
resourceGroup: rg-test
location: eastus
labels:
  environment: dev
properties:
  resourceSets:
    dataflows:
      - site-telemetry
"""
        )
        orchestrator = Orchestrator(
            workspace,
            extra_trusted_sites_dirs=[extra_sites],
        )

        errors = orchestrator.validate(
            workspace / "manifests" / "aio-resources.yaml",
            selector="name=catalog-reference-test",
        )

        assert not errors, "\n".join(errors)

    def test_profile_ref_builds_the_resource_parent(self, workspace):
        """Each generation uses the selected profile in the resource name."""
        module = harness.version_module(
            workspace, DATAFLOWS, harness.supported_api_versions(workspace, DATAFLOWS)[0]
        ).read_text(encoding="utf-8")

        placement = r"dataflow\.\?profileRef\s*\?\?\s*defaultProfileName"
        assert re.search(placement, module), (
            "A generation module no longer builds the dataflow name from "
            "`dataflow.?profileRef ?? defaultProfileName`, so it would create "
            "the resource under a different profile."
        )

    def test_the_default_profile_is_the_one_the_instance_owns(self, workspace):
        """The fallback profile names a profile that exists without being declared.

        A dataflow entry may omit `profileRef`, and the templates then place it
        in the profile AIO creates alongside the instance. A typo in that
        default sends every such dataflow to a profile nothing creates, which
        includes the worked example the documentation points at.

        Checked in the entry point and in every module, since each carries its
        own copy of the default.
        """
        profile_collection = harness.composition_contract(workspace).collections[
            "dataflowProfiles"
        ]
        assert profile_collection.seeds == (("default",),)
        instance_owned_profile = profile_collection.seeds[0][0]

        templates = [harness.entry_point(workspace, DATAFLOWS)] + [
            harness.version_module(workspace, DATAFLOWS, version)
            for version in harness.supported_api_versions(workspace, DATAFLOWS)
        ]
        failures: list[str] = []
        for template in templates:
            if not template.is_file():
                continue
            defaults = re.findall(
                r"param\s+defaultProfileName\s+string\s*=\s*'([^']*)'",
                template.read_text(encoding="utf-8"),
            )
            if not defaults:
                failures.append(
                    f"{template.name} declares no defaultProfileName default, so "
                    f"an entry omitting profileRef has no profile to fall back to."
                )
                continue
            wrong = [d for d in defaults if d != instance_owned_profile]
            if wrong:
                failures.append(
                    f"{template.name} falls back to {wrong}, but the profile the "
                    f"instance owns is '{instance_owned_profile}'. A dataflow "
                    f"declaring no profileRef would name a profile nothing creates."
                )
        assert not failures, "\n".join(failures)

    def test_endpoint_types_are_real_and_carry_matching_settings(self, workspace):
        """`endpointType` selects which settings shape the provider validates.

        Bicep types `properties` as `DataflowEndpointProperties` and rejects an
        unknown property inside it, but only once `endpointType` names a real
        variant. An endpoint whose `endpointType` is `NotARealEndpointType`
        compiles with no diagnostic at all, so a typo there turns off validation
        for everything else in that entry.

        The paired settings key is checked with it. `endpointType: Mqtt`
        alongside `kafkaSettings` names a variant whose settings are absent,
        which the compile probe cannot catch either.
        """
        failures: list[str] = []
        for path in _declarations(workspace):
            data = harness.load_yaml(path)
            for i, entry in enumerate(harness.entries(data, "dataflowEndpoints")):
                props = entry.get("properties")
                if not isinstance(props, dict):
                    continue
                endpoint_type = props.get("endpointType")
                if not endpoint_type:
                    continue
                label = f"{path}: dataflowEndpoints[{i}] ('{entry.get('name')}')"

                if endpoint_type not in _ENDPOINT_TYPES:
                    failures.append(
                        f"{label} declares endpointType '{endpoint_type}', "
                        f"which is not one of {sorted(_ENDPOINT_TYPES)}. An "
                        f"unknown value compiles clean and stops the resource "
                        f"provider's own property validation from running."
                    )
                    continue

                settings_key = _settings_key(endpoint_type)
                if settings_key not in props:
                    failures.append(
                        f"{label} declares endpointType '{endpoint_type}' but "
                        f"carries no `{settings_key}`. The endpoint would be "
                        f"created with none of the settings that type needs."
                    )
        assert not failures, "\n\n".join(failures)

    def test_the_worked_example_still_renders_per_site(self, workspace, orchestrator):
        """The file the documentation points at proves the fan-out claim.

        `test_catalog_gating.py` holds the general rule, that any declaration
        reading a site value renders differently across the fleet. That rule
        checks nothing at all if no declaration reads one, so the worked example
        is named here: it is what the docs send an operator to read, and a
        rewrite that dropped its site variable would leave the claim unproven
        while every other check stayed green.
        """
        rendered = harness.resolved_declarations(workspace, orchestrator, DATAFLOWS)
        worked_example = workspace / _WORKED_EXAMPLE
        assert worked_example in rendered, (
            f"{_WORKED_EXAMPLE} is no longer discovered as a dataflow "
            f"declaration, so the worked example the documentation points at is "
            f"unchecked. Discovered: "
            f"{[str(p.relative_to(workspace)) for p in rendered]}"
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
            f"every site, so the fleet would share one destination."
        )
