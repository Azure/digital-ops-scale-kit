"""End-to-end integrity of what a deployment reports.

Two contracts here hold only end to end, so each is asserted at the boundary a
consumer actually reads rather than at a helper behind it.

The exit code reflects the fleet. A site whose step fails is counted as failed,
and any failure makes `siteops deploy` exit non-zero. Everything from the step
result through the site status and the summary count to the exit code runs for
real, with only the calls that reach Azure replaced.

A step output reaches the next step. A `{{ steps.<name>.outputs.<key> }}`
reference in a later step's parameter file arrives at the executor carrying the
producing step's value. Asserting on the parameters the executor receives covers
recording the output and forwarding it as well as substituting it.

Templates are ARM JSON so these tests do not depend on a Bicep compiler.
"""

import json
from argparse import Namespace

import pytest
import yaml

from siteops.cli import cmd_deploy
from siteops.executor import DeploymentResult
from siteops.orchestrator import Orchestrator

SUBSCRIPTION = "00000000-0000-0000-0000-000000000000"


def _write_arm_template(path, parameters):
    """Write a minimal ARM template declaring the named parameters."""
    path.write_text(
        json.dumps(
            {
                "$schema": (
                    "https://schema.management.azure.com/schemas/2019-04-01/"
                    "deploymentTemplate.json#"
                ),
                "contentVersion": "1.0.0.0",
                "parameters": {name: {"type": "string"} for name in parameters},
                "resources": [],
            }
        ),
        encoding="utf-8",
    )


def _write_site(workspace, name):
    """Write a site file with the fields a deployment requires."""
    (workspace / "sites" / f"{name}.yaml").write_text(
        yaml.dump(
            {
                "apiVersion": "siteops/v1",
                "kind": "Site",
                "name": name,
                "subscription": SUBSCRIPTION,
                "resourceGroup": f"rg-{name}",
                "location": "eastus",
            }
        ),
        encoding="utf-8",
    )


def _deploy_args(workspace, manifest_path):
    """Build the namespace `cmd_deploy` reads from argparse."""
    args = Namespace()
    args.manifest = manifest_path
    args.workspace = workspace
    args.selector = None
    args.parallel = None
    return args


class TestAFailedFleetIsReportedAsFailed:
    """`siteops deploy` exits non-zero whenever a site fails.

    Only `deploy_resource_group` is replaced, since it is the one call that
    reaches Azure. The step result, the site status, the summary counts, and the
    exit code are all produced by the engine.
    """

    @pytest.fixture
    def fleet(self, tmp_workspace):
        """A two-site manifest with one deployment step."""
        for name in ("plant-east", "plant-west"):
            _write_site(tmp_workspace, name)
        _write_arm_template(tmp_workspace / "templates" / "fleet.json", [])

        manifest_path = tmp_workspace / "manifests" / "fleet.yaml"
        manifest_path.write_text(
            yaml.dump(
                {
                    "apiVersion": "siteops/v1",
                    "kind": "Manifest",
                    "name": "fleet",
                    "sites": ["plant-east", "plant-west"],
                    "steps": [{"name": "deploy", "template": "templates/fleet.json"}],
                }
            ),
            encoding="utf-8",
        )
        return tmp_workspace, manifest_path

    @staticmethod
    def _orchestrator(workspace, monkeypatch, failing_sites):
        """Return an orchestrator whose step fails for the named sites."""
        orchestrator = Orchestrator(workspace)

        def fake_deploy_resource_group(*, site_name, step_name, deployment_name, **kwargs):
            failed = site_name in failing_sites
            return DeploymentResult(
                success=not failed,
                step_name=step_name,
                site_name=site_name,
                deployment_name=deployment_name,
                error="BadRequest: the template was rejected" if failed else None,
            )

        monkeypatch.setattr(
            orchestrator.executor, "deploy_resource_group", fake_deploy_resource_group
        )
        return orchestrator

    def test_every_site_failing_exits_nonzero(self, fleet, monkeypatch):
        workspace, manifest_path = fleet
        orchestrator = self._orchestrator(
            workspace, monkeypatch, {"plant-east", "plant-west"}
        )

        assert cmd_deploy(_deploy_args(workspace, manifest_path), orchestrator) == 1

    def test_one_site_failing_among_several_exits_nonzero(self, fleet, monkeypatch):
        """A single failure carries the exit code for the whole run."""
        workspace, manifest_path = fleet
        orchestrator = self._orchestrator(workspace, monkeypatch, {"plant-west"})

        assert cmd_deploy(_deploy_args(workspace, manifest_path), orchestrator) == 1

    def test_the_summary_counts_what_the_sites_reported(self, fleet, monkeypatch):
        """The counts themselves, which the summary and any artifact reader use."""
        workspace, manifest_path = fleet
        orchestrator = self._orchestrator(workspace, monkeypatch, {"plant-west"})

        result = orchestrator.deploy(manifest_path)

        assert result["summary"]["total"] == 2
        assert result["summary"]["succeeded"] == 1
        assert result["summary"]["failed"] == 1
        assert result["sites"]["plant-west"]["status"] == "failed"
        assert result["sites"]["plant-east"]["status"] == "success"

    def test_a_healthy_fleet_exits_zero(self, fleet, monkeypatch):
        """A clean run exits 0, which is what gives the non-zero cases meaning."""
        workspace, manifest_path = fleet
        orchestrator = self._orchestrator(workspace, monkeypatch, set())

        assert cmd_deploy(_deploy_args(workspace, manifest_path), orchestrator) == 0


class TestAStepOutputReachesTheNextStep:
    """A value one step produces arrives as the next step's parameter.

    Asserted on what the executor is called with, which is where the value has
    to be correct for the deployment to be right. Catalog families chain their
    steps this way.
    """

    @pytest.fixture
    def chaining(self, tmp_workspace):
        """A two-step manifest whose second step consumes the first's output."""
        _write_site(tmp_workspace, "plant-east")
        _write_arm_template(tmp_workspace / "templates" / "producer.json", [])
        # The consumer declares the parameter, since resolution is followed by a
        # filter that keeps only what the template accepts.
        _write_arm_template(tmp_workspace / "templates" / "consumer.json", ["chainedId"])
        (tmp_workspace / "parameters" / "chain.yaml").write_text(
            yaml.dump({"chainedId": "{{ steps.produce.outputs.storageId }}"}),
            encoding="utf-8",
        )

        manifest_path = tmp_workspace / "manifests" / "chain.yaml"
        manifest_path.write_text(
            yaml.dump(
                {
                    "apiVersion": "siteops/v1",
                    "kind": "Manifest",
                    "name": "chain",
                    "sites": ["plant-east"],
                    "steps": [
                        {"name": "produce", "template": "templates/producer.json"},
                        {
                            "name": "consume",
                            "template": "templates/consumer.json",
                            "parameters": ["parameters/chain.yaml"],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        return tmp_workspace, manifest_path

    def test_the_consumer_is_called_with_the_producers_value(self, chaining, monkeypatch):
        workspace, manifest_path = chaining
        orchestrator = Orchestrator(workspace)
        seen: dict[str, dict] = {}

        def fake_deploy_resource_group(
            *, site_name, step_name, deployment_name, parameters, **kwargs
        ):
            seen[step_name] = parameters
            return DeploymentResult(
                success=True,
                step_name=step_name,
                site_name=site_name,
                deployment_name=deployment_name,
                outputs={"storageId": "storage-from-produce"} if step_name == "produce" else {},
            )

        monkeypatch.setattr(
            orchestrator.executor, "deploy_resource_group", fake_deploy_resource_group
        )

        result = orchestrator.deploy(manifest_path)

        assert result["summary"]["failed"] == 0
        assert seen["consume"]["chainedId"] == "storage-from-produce"
