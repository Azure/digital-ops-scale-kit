"""Integration tests for the dataflow-sample manifest.

The sample declares dataflow resources in YAML and deploys them through the
shared catalog templates, so this suite is the live qualification for those
templates. Compiling proves the Bicep is well formed. Only a deploy proves the
declaration reaches the resource provider in a shape it accepts and that the
resources project to the cluster.

Cluster-side assertions are the live source of truth. A dataflow whose
`endpointRef` names an endpoint that does not exist is accepted by ARM, reports
success, and never moves data. The CR checks below are what catch that.
"""

import pytest

from tests.integration.conftest import WORKSPACE_PATH
from tests.integration.helpers.assertions import (
    assert_step_succeeded,
    skip_unless_health_is_reported,
)
from tests.integration.helpers.dataflow_sample import (
    ALERTS_DATAFLOW_NAME as SAMPLE_ALERTS_DATAFLOW_NAME,
)
from tests.integration.helpers.dataflow_sample import (
    ALERTS_ENDPOINT_NAME as SAMPLE_ALERTS_ENDPOINT_NAME,
)
from tests.integration.helpers.dataflow_sample import (
    ALERTS_PROFILE_NAME as SAMPLE_ALERTS_PROFILE_NAME,
)
from tests.integration.helpers.dataflow_sample import (
    DATAFLOW_NAME as SAMPLE_DATAFLOW_NAME,
)
from tests.integration.helpers.dataflow_sample import (
    DATAFLOW_TYPE as _DATAFLOW_TYPE,
)
from tests.integration.helpers.dataflow_sample import (
    ENDPOINT_NAME as SAMPLE_ENDPOINT_NAME,
)
from tests.integration.helpers.dataflow_sample import (
    ENDPOINT_TYPE as _ENDPOINT_TYPE,
)
from tests.integration.helpers.dataflow_sample import (
    PROFILE_NAME as SAMPLE_PROFILE_NAME,
)
from tests.integration.helpers.dataflow_sample import (
    PROFILE_TYPE as _PROFILE_TYPE,
)
from tests.integration.helpers.kube import (
    KubectlError,
    cr_identity,
    wait_for_cr,
    wait_for_cr_health,
)
from tests.integration.helpers.releases import load_aio_release

pytestmark = [pytest.mark.integration]

DATAFLOW_SAMPLE_MANIFEST = WORKSPACE_PATH / "samples" / "dataflow-sample" / "manifest.yaml"

# Names declared by samples/dataflow-sample/dataflows.yaml.
# The endpoint the sample's source reads from, created by the instance template.
INSTANCE_OWNED_ENDPOINT = "default"

CATALOG_STEP = "dataflow-resources"

# Kubernetes resource types the declared resources project to, paired with the
# name each carries on the cluster.
_SAMPLE_CLUSTER_RESOURCES = (
    (_ENDPOINT_TYPE, SAMPLE_ENDPOINT_NAME),
    (_ENDPOINT_TYPE, SAMPLE_ALERTS_ENDPOINT_NAME),
    (_PROFILE_TYPE, SAMPLE_PROFILE_NAME),
    (_PROFILE_TYPE, SAMPLE_ALERTS_PROFILE_NAME),
    (_DATAFLOW_TYPE, SAMPLE_DATAFLOW_NAME),
    (_DATAFLOW_TYPE, SAMPLE_ALERTS_DATAFLOW_NAME),
)


class TestDataflowSampleDeployment:
    """The catalog steps deploy successfully from a declaration."""

    def test_no_failures(self, dataflow_sample_result):
        assert dataflow_sample_result["summary"]["failed"] == 0

    def test_all_sites_succeeded(self, dataflow_sample_result):
        for name, site in dataflow_sample_result["sites"].items():
            assert site["status"] == "success", (
                f"Site '{name}' failed: {site.get('error')}"
            )

    def test_every_catalog_step_succeeds(self, dataflow_sample_result):
        for name in dataflow_sample_result["sites"]:
            assert_step_succeeded(dataflow_sample_result, name, CATALOG_STEP)

    def test_family_deploys_as_one_step(self, dataflow_sample_result):
        """The family contributes a single deployment per site.

        One module per AIO API generation creates every resource kind, selected by
        the family template, so a site pays one round trip rather than one per
        kind. Ordering moved into the template's `dependsOn`, and the cluster
        assertions below are what prove it held.
        """
        for name, site in dataflow_sample_result["sites"].items():
            catalog_steps = [
                s["step"] for s in site["steps"] if s["step"].startswith("dataflow-")
            ]
            assert catalog_steps == [CATALOG_STEP], (
                f"Site '{name}': catalog contributed {catalog_steps}, expected "
                f"exactly ['{CATALOG_STEP}']."
            )


class TestDataflowSampleIdempotency:
    """Re-deploying a declaration is a reconcile, not a recreate."""

    def test_redeploy_does_not_recreate_the_projected_resources(
        self, orchestrator, selector, dataflow_sample_result, aio_namespace, kubectl_available
    ):
        """The cluster resources survive a second deploy as the same objects.

        Two successful deployment results do not distinguish a reconcile from
        a recreate. `metadata.uid` is assigned at creation and stable for the
        object's lifetime, so an unchanged uid proves the distinction. A
        recreate would drop in-flight messages and still report success.

        Projection lags ARM, so a recreate is caught once observed rather than
        the instant it happens.
        """
        from tests.integration.conftest import _resolve_or_fail

        before = {
            (resource_type, name): cr_identity(resource_type, name, aio_namespace)
            for resource_type, name in _SAMPLE_CLUSTER_RESOURCES
        }

        manifest, sites = _resolve_or_fail(orchestrator, DATAFLOW_SAMPLE_MANIFEST, selector)
        second = orchestrator.deploy(
            manifest_path=DATAFLOW_SAMPLE_MANIFEST, manifest=manifest, sites=sites
        )
        assert second["summary"]["failed"] == 0

        for resource_type, name in _SAMPLE_CLUSTER_RESOURCES:
            after = cr_identity(resource_type, name, aio_namespace)
            assert after == before[(resource_type, name)], (
                f"'{name}' was recreated by the redeploy rather than reconciled. "
                f"Before: {before[(resource_type, name)]}. After: {after}."
            )


class TestDataflowSampleClusterProjection:
    """The declared resources reach the cluster as custom resources.

    ARM accepting the PUT only means the control plane stored it. The
    custom-location projection to the cluster is where a malformed declaration
    actually surfaces.
    """

    def test_endpoint_cr_present(
        self, dataflow_sample_result, aio_namespace, kubectl_available
    ):
        for name in dataflow_sample_result["sites"]:
            try:
                wait_for_cr(_ENDPOINT_TYPE, SAMPLE_ENDPOINT_NAME, aio_namespace)
            except KubectlError as e:
                pytest.fail(
                    f"Site '{name}': declared endpoint '{SAMPLE_ENDPOINT_NAME}' "
                    f"is not on the cluster in '{aio_namespace}'. The ARM PUT "
                    f"succeeded, so the declaration reached the resource "
                    f"provider but did not project: {e}"
                )

    def test_profile_cr_present(
        self, dataflow_sample_result, aio_namespace, kubectl_available
    ):
        for name in dataflow_sample_result["sites"]:
            try:
                wait_for_cr(_PROFILE_TYPE, SAMPLE_PROFILE_NAME, aio_namespace)
            except KubectlError as e:
                pytest.fail(
                    f"Site '{name}': declared profile '{SAMPLE_PROFILE_NAME}' "
                    f"is not on the cluster in '{aio_namespace}': {e}"
                )

    def test_dataflow_cr_is_enabled(
        self, dataflow_sample_result, aio_namespace, kubectl_available
    ):
        for name in dataflow_sample_result["sites"]:
            try:
                dataflow = wait_for_cr(_DATAFLOW_TYPE, SAMPLE_DATAFLOW_NAME, aio_namespace)
            except KubectlError as e:
                pytest.fail(
                    f"Site '{name}': declared dataflow '{SAMPLE_DATAFLOW_NAME}' "
                    f"is not on the cluster in '{aio_namespace}': {e}"
                )
            mode = dataflow.get("spec", {}).get("mode")
            assert mode == "Enabled", (
                f"Site '{name}': dataflow '{SAMPLE_DATAFLOW_NAME}' has "
                f"mode={mode!r}, expected 'Enabled'. The declaration's "
                f"`properties.mode` did not survive to the cluster."
            )

    def test_dataflow_references_resolve_on_cluster(
        self, dataflow_sample_result, aio_namespace, kubectl_available
    ):
        """The projected dataflow names the endpoints the declaration selected.

        A mistyped `endpointRef` deploys clean and moves no data, so comparing
        the projected refs against the declared names is the assertion that
        makes the reference contract real rather than documented.
        """
        for name in dataflow_sample_result["sites"]:
            dataflow = wait_for_cr(_DATAFLOW_TYPE, SAMPLE_DATAFLOW_NAME, aio_namespace)
            operations = dataflow.get("spec", {}).get("operations", [])
            refs = {
                op.get("sourceSettings", {}).get("endpointRef")
                or op.get("destinationSettings", {}).get("endpointRef")
                for op in operations
            }
            refs.discard(None)

            assert SAMPLE_ENDPOINT_NAME in refs, (
                f"Site '{name}': projected dataflow does not reference the "
                f"declared destination endpoint '{SAMPLE_ENDPOINT_NAME}'. "
                f"Referenced: {sorted(refs)}"
            )
            assert INSTANCE_OWNED_ENDPOINT in refs, (
                f"Site '{name}': projected dataflow does not reference the "
                f"instance-owned source endpoint '{INSTANCE_OWNED_ENDPOINT}'. "
                f"Referenced: {sorted(refs)}"
            )


class TestDataflowSampleInstanceOwnedEndpointIntact:
    """The catalog deploy leaves the instance-owned `default` endpoint alone.

    The instance template creates a `default` MQTT endpoint, and a declaration
    that reused that name would full-PUT over it and silently break every
    dataflow sourcing from it. A workspace test rejects the name, and this
    confirms the live resource survives a catalog deploy.
    """

    def test_default_endpoint_still_mqtt(
        self, dataflow_sample_result, aio_namespace, kubectl_available
    ):
        for name in dataflow_sample_result["sites"]:
            try:
                endpoint = wait_for_cr(_ENDPOINT_TYPE, INSTANCE_OWNED_ENDPOINT, aio_namespace)
            except KubectlError as e:
                pytest.fail(
                    f"Site '{name}': the instance-owned "
                    f"'{INSTANCE_OWNED_ENDPOINT}' endpoint is missing after a "
                    f"catalog deploy: {e}"
                )
            endpoint_type = endpoint.get("spec", {}).get("endpointType")
            assert endpoint_type == "Mqtt", (
                f"Site '{name}': the instance-owned "
                f"'{INSTANCE_OWNED_ENDPOINT}' endpoint has "
                f"endpointType={endpoint_type!r}, expected 'Mqtt'. A catalog "
                f"declaration overwrote a resource the instance template owns."
            )


class TestDataflowSampleHealth:
    """The declared dataflows report healthy, not merely created.

    Every other assertion in this module reads the projected `spec`, which
    matches the declaration whether or not the dataflow can reach its
    endpoints. AIO aggregates per-instance reports from the data plane and
    writes the result to `status.healthState`, so this is the assertion that
    makes the run evidence the resources work.

    Both declared dataflows are checked, since each names a different endpoint
    and a different profile, so one can run while the other cannot.

    Profiles are not checked. AIO writes health for dataflows and dataflow
    graphs, and a profile is a pool rather than something that runs, so its
    `status.healthState` stays `Unknown`. A profile that is genuinely broken
    surfaces through the dataflows assigned to it.
    """

    @pytest.mark.parametrize(
        "dataflow_name",
        [SAMPLE_DATAFLOW_NAME, SAMPLE_ALERTS_DATAFLOW_NAME],
    )
    def test_declared_dataflow_reports_available(
        self,
        dataflow_sample_result,
        aio_namespace,
        kubectl_available,
        dataflow_name,
        orchestrator,
    ):
        for site in dataflow_sample_result["sites"]:
            assert_step_succeeded(dataflow_sample_result, site, CATALOG_STEP)
            _, release = load_aio_release(orchestrator, site, WORKSPACE_PATH)
            skip_unless_health_is_reported(release.get("aioApiVersion"))
        wait_for_cr_health(_DATAFLOW_TYPE, dataflow_name, aio_namespace)
