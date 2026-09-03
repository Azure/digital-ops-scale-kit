"""Committed samples for beginner and advanced resource-set composition."""

from pathlib import Path

import yaml

from siteops.models import Manifest

_BASIC_MANIFEST = Path("samples/resource-set-basic/manifest.yaml")
_ADVANCED_MANIFEST = Path("samples/resource-set-composition/manifest.yaml")


def _source_paths(composition) -> list[str]:
    return [source.path.as_posix() for source in composition.sources]


class TestSharedCatalogPartial:
    """Every fleet-selection entry point consumes one catalog definition."""

    def test_entry_points_include_the_same_catalog_partial(self, workspace):
        manifests = [
            workspace / "manifests" / "aio-resources.yaml",
            workspace / _BASIC_MANIFEST,
            workspace / _ADVANCED_MANIFEST,
        ]

        includes = {}
        for path in manifests:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            includes[path.relative_to(workspace).as_posix()] = [
                step.get("include")
                for step in raw.get("steps") or []
                if isinstance(step, dict) and step.get("include")
            ]

        assert includes == {
            "manifests/aio-resources.yaml": ["_aio-resources.yaml"],
            "samples/resource-set-basic/manifest.yaml": [
                "../../manifests/_aio-resources.yaml"
            ],
            "samples/resource-set-composition/manifest.yaml": [
                "_external-provider.yaml",
                "../../manifests/_aio-resources.yaml"
            ],
        }

    def test_sample_sites_do_not_join_the_default_development_fleet(
        self,
        workspace,
        orchestrator,
    ):
        manifest = Manifest.from_file(
            workspace / "manifests" / "aio-resources.yaml",
            workspace_root=workspace,
        )

        selected = {
            site.name for site in orchestrator.resolve_sites(manifest)
        }

        assert "catalog-basic" not in selected
        assert "catalog-composition" not in selected


class TestBeginnerResourceSetSample:
    """One site selection produces one useful dataflow composition."""

    def test_basic_sample_resolves_one_selected_set(
        self,
        workspace,
        orchestrator,
    ):
        manifest_path = workspace / _BASIC_MANIFEST
        manifest = Manifest.from_file(manifest_path, workspace_root=workspace)
        sites = orchestrator.resolve_sites(manifest)

        assert [site.name for site in sites] == ["catalog-basic"]
        site = sites[0]
        assert site.properties["resourceSets"] == {
            "dataflows": ["basic-routing"]
        }

        errors = orchestrator.validate(manifest_path)
        assert not errors, "\n".join(errors)

        _, composition, _ = orchestrator._resolve_manifest_parameters(
            manifest,
            site,
        )
        assert composition is not None
        assert _source_paths(composition) == [
            "parameters/dataflows/basic-routing.yaml"
        ]
        assert [entry.identity for entry in composition.entries["dataflows"]] == [
            ("default", "basic-routing")
        ]
        dataflow = composition.entries["dataflows"][0].value
        operations = dataflow["properties"]["operations"]
        assert operations[0]["sourceSettings"]["dataSources"] == [
            "azure-iot-operations/data/siteops-samples/basic/#"
        ]
        assert operations[-1]["destinationSettings"] == {
            "endpointRef": "default",
            "dataDestination": "siteops-samples/catalog-basic/basic",
        }
        assert not composition.entries["devices"]
        assert not composition.entries["assets"]
        assert not composition.entries["dataflowEndpoints"]
        assert not composition.entries["dataflowProfiles"]


class TestAdvancedResourceSetSample:
    """Inheritance composes managed, external, and cross-source resources."""

    def test_advanced_site_preserves_selection_provenance(
        self,
        orchestrator,
    ):
        site, provenance = orchestrator.load_site_with_provenance(
            "catalog-composition"
        )

        assert site.properties["resourceSets"] == {
            "devices": ["composition-opc-ua", "external-opc-ua-device"],
            "assets": [
                "composition-oven-assets",
                "boiler-assets",
                "external-oven-assets",
            ],
            "dataflows": [
                "shared-mqtt-endpoint",
                "shared-dataflow-profile",
                "advanced-routing",
            ],
        }
        assert provenance["properties.resourceSets.devices"].replace("\\", "/").endswith(
            "sites/shared/catalog-composition.yaml"
        )
        assert provenance["properties.resourceSets.assets"].replace("\\", "/").endswith(
            "sites/catalog-composition.yaml"
        )
        assert provenance["properties.resourceSets.dataflows"].replace("\\", "/").endswith(
            "sites/catalog-composition.yaml"
        )

    def test_advanced_sample_resolves_the_complete_graph(
        self,
        workspace,
        orchestrator,
    ):
        manifest_path = workspace / _ADVANCED_MANIFEST
        manifest = Manifest.from_file(manifest_path, workspace_root=workspace)
        sites = orchestrator.resolve_sites(manifest)

        assert [site.name for site in sites] == ["catalog-composition"]
        errors = orchestrator.validate(manifest_path)
        assert not errors, "\n".join(errors)

        _, composition, _ = orchestrator._resolve_manifest_parameters(
            manifest,
            sites[0],
        )
        assert composition is not None
        assert _source_paths(composition) == [
            "parameters/devices/composition-opc-ua.yaml",
            "parameters/devices/external-opc-ua-device.yaml",
            "parameters/assets/composition-oven-assets.yaml",
            "parameters/assets/boiler-assets.yaml",
            "parameters/assets/external-oven-assets.yaml",
            "parameters/dataflows/shared-mqtt-endpoint.yaml",
            "parameters/dataflows/shared-dataflow-profile.yaml",
            "parameters/dataflows/advanced-routing.yaml",
        ]

        assert len(composition.entries["devices"]) == 1
        assert len(composition.external["devices"]) == 1
        assert len(composition.entries["assets"]) == 3
        assert len(composition.entries["dataflowEndpoints"]) == 1
        assert len(composition.entries["dataflowProfiles"]) == 1
        assert len(composition.entries["dataflows"]) == 1

        asset_refs = [
            reference
            for reference in composition.references
            if reference.rule_id == "asset-device-endpoint"
        ]
        assert len(asset_refs) == 3
        assert sum(reference.external for reference in asset_refs) == 1

        profile_ref = next(
            reference
            for reference in composition.references
            if reference.rule_id == "dataflow-profile"
        )
        destination_ref = next(
            reference
            for reference in composition.references
            if reference.rule_id == "dataflow-destination-endpoint"
        )
        assert profile_ref.target_source == Path(
            "parameters/dataflows/shared-dataflow-profile.yaml"
        )
        assert destination_ref.target_source == Path(
            "parameters/dataflows/shared-mqtt-endpoint.yaml"
        )
        route = composition.entries["dataflows"][0].value
        source = route["properties"]["operations"][0]["sourceSettings"]
        destination = route["properties"]["operations"][-1][
            "destinationSettings"
        ]
        assert source["dataSources"] == [
            "azure-iot-operations/data/catalog-composition/"
            "resource-set-composition/#"
        ]
        assert destination["dataDestination"] == (
            "catalog/catalog-composition/${inputTopic}"
        )

        advanced_topics = {
            target["configuration"]["topic"]
            for entry in composition.entries["assets"]
            for dataset in entry.value["properties"].get("datasets", [])
            for target in dataset.get("destinations", [])
        }
        existing = yaml.safe_load(
            (
                workspace
                / "parameters"
                / "assets"
                / "site-assets.yaml"
            ).read_text(encoding="utf-8")
        )
        existing_topics = {
            target["configuration"]["topic"]
            for asset in existing["assets"]
            for dataset in asset["properties"].get("datasets", [])
            for target in dataset.get("destinations", [])
        }
        assert advanced_topics
        assert all(
            "/resource-set-composition/" in topic
            for topic in advanced_topics
        )
        assert advanced_topics.isdisjoint(existing_topics)

        assert [step.name for step in manifest.steps] == [
            "external-opc-plc-simulator",
            "external-opc-ua-device",
            "resolve-aio",
            "asset-resources",
            "dataflow-resources",
        ]

    def test_simulator_source_and_image_are_immutable(self, workspace):
        partial = (
            workspace
            / "samples"
            / "resource-set-composition"
            / "_external-provider.yaml"
        ).read_text(encoding="utf-8")
        simulator = (
            workspace
            / "samples"
            / "resource-set-composition"
            / "opc-plc.k8s"
        ).read_text(encoding="utf-8")

        assert "samples/resource-set-composition/opc-plc.k8s" in partial
        images = [
            line.split("image:", 1)[1].strip()
            for line in simulator.splitlines()
            if line.strip().startswith("image:")
        ]
        documents = list(yaml.safe_load_all(simulator))
        assert [document["kind"] for document in documents] == [
            "Deployment",
            "Service",
            "Issuer",
            "Certificate",
            "Secret",
            "Job",
            "ConfigMap",
            "ServiceAccount",
            "Role",
            "RoleBinding",
        ]
        assert images
        assert all("@sha256:" in image for image in images)
        assert "set -eu" in simulator
        assert "wait_for_certificate" in simulator
        assert "wait_for_secret" in simulator
        assert "backoffLimit: 6" in simulator

    def test_local_plan_explains_provenance_and_external_ownership(
        self,
        workspace,
        orchestrator,
        capsys,
    ):
        orchestrator.show_plan(workspace / _ADVANCED_MANIFEST)

        output = capsys.readouterr().out
        assert "selected by sites/shared/catalog-composition.yaml" in output
        assert "selected by sites/catalog-composition.yaml" in output
        assert "external  devices[name='external-opc-ua']" in output
        assert (
            "assets[name='composition-oven'] -> "
            "devices[name='composition-opc-ua']"
        ) in output
        assert (
            "assets[name='composition-boiler'] -> "
            "devices[name='composition-opc-ua']"
        ) in output
        assert (
            "assets[name='composition-external-oven'] -> "
            "devices[name='external-opc-ua']"
        ) in output
        assert "dataflows[profileRef='catalog-profile', name='catalog-routing']" in output
        assert "-> dataflowEndpoints[name='catalog-mqtt-out']" in output
