# resource-set-composition

This sample shows how a fleet composes provider-shaped resources without
copying them into one declaration file. The `catalog-composition` site inherits
its device selections from `sites/shared/catalog-composition.yaml`, then adds
independent asset and dataflow selections:

```text
shared/catalog-composition.yaml
  devices
    composition-opc-ua
    external-opc-ua-device

catalog-composition.yaml
  assets
    composition-oven-assets
    boiler-assets
    external-oven-assets
  dataflows
    shared-mqtt-endpoint
    shared-dataflow-profile
    advanced-routing
```

The composition demonstrates:

- `composition-oven-assets` and `boiler-assets` sharing the managed
  `composition-opc-ua` device
- `external-oven-assets` consuming `external-opc-ua`, which a Bicep step owns
  and `_siteops.external` asserts
- a dataflow resolving its `profileRef` and destination `endpointRef` from two
  separately selected sets
- site inheritance and source provenance in the local deployment plan
- three selected resource areas deployed through two ordered families

The boiler telemetry nodes follow the OPC PLC boiler simulation used by
[Azure-Samples/explore-iot-operations](https://github.com/Azure-Samples/explore-iot-operations/blob/main/samples/process-control/boiler-simulation.bicep).

## Prerequisites

- AIO is installed on the target cluster. The committed sample site uses
  release `2608`.
- cert-manager is enabled because the simulator creates a local certificate.
- Arc Cluster Connect is enabled.
- The deployment identity has Kubernetes permission to create Deployments,
  Services, Issuers, Certificates, Secrets, Jobs, ConfigMaps,
  ServiceAccounts, Roles, and RoleBindings in `azure-iot-operations`. See
  [Kubernetes RBAC for Arc proxy operations](../../../../docs/ci-cd-setup.md#kubernetes-rbac-for-arc-proxy-operations).

## Prepare the site

Create `workspaces/iot-operations/sites.local/catalog-composition.yaml`:

```yaml
apiVersion: siteops/v1
kind: Site
name: catalog-composition
subscription: "<subscription-id>"
resourceGroup: "<resource-group>"
parameters:
  clusterName: "<arc-cluster-name>"
```

Deploy AIO on this site first:

```bash
siteops -w workspaces/iot-operations \
  deploy manifests/aio-install.yaml -l name=catalog-composition
```

The committed site uses `environment=sample`, which keeps it out of ordinary
development fleet deployments. In the GitHub Actions or Azure Pipelines
deployment UI, select the `dev` environment. The workflow uses that environment
for credentials and approvals and adds the sample selector automatically.

The advanced manifest applies the included OPC PLC simulator and creates
`external-opc-ua` through its own Bicep step before the catalog steps run. The
simulator manifest is adapted from a pinned upstream commit and pins every
container image by multi-architecture digest. The device is external to the
catalog even though the sample owns its lifecycle. An external assertion
validates the declared identity and expected endpoint shape during
composition. It does not query Azure to prove that the device currently
exists.

`external-provider.bicep` pins the oldest supported Device Registry API, which
is the workspace policy for sample-owned Bicep. The catalog-managed assets
continue to follow the API generation selected by the site's AIO release. This
is a deliberate cross-lifecycle example: the external assertion binds by
provider identity and endpoint shape rather than requiring both owners to use
one deployment template or API version.

## Review the composition

```bash
siteops -w workspaces/iot-operations \
  validate samples/resource-set-composition/manifest.yaml --plan
```

The local plan identifies which site file selected each source, distinguishes
managed resources from the external device assertion, and shows the resolved
asset, endpoint, and profile references.

## Deploy and exercise the result

```bash
siteops -w workspaces/iot-operations \
  deploy samples/resource-set-composition/manifest.yaml
```

The two managed assets publish to:

```text
azure-iot-operations/data/catalog-composition/resource-set-composition/oven
azure-iot-operations/data/catalog-composition/resource-set-composition/boiler
```

The externally connected oven publishes to:

```text
azure-iot-operations/data/catalog-composition/resource-set-composition/external-oven
```

The composed dataflow preserves each source topic under:

```text
catalog/catalog-composition/azure-iot-operations/data/catalog-composition/resource-set-composition/oven
catalog/catalog-composition/azure-iot-operations/data/catalog-composition/resource-set-composition/boiler
catalog/catalog-composition/azure-iot-operations/data/catalog-composition/resource-set-composition/external-oven
```

Use an in-cluster MQTT client to observe those topics. The boiler asset also
demonstrates OPC UA node identifiers from the simulator's boiler model.

The `resource-set-samples` E2E phase automates this proof on a fresh cluster.
It waits for the simulator trust job, checks the projected device, asset,
endpoint, profile, and dataflow resources, waits for dataflow health where the
API reports it, and subscribes independently to all three routed topics.

## Remove the sample

Deselecting a resource set stops applying it and does not delete resources.
Delete catalog resources explicitly, in dependency order:

```bash
az resource delete --ids \
  <catalog-routing-dataflow-id> \
  <catalog-mqtt-out-endpoint-id> \
  <catalog-profile-id> \
  <composition-oven-asset-id> \
  <composition-boiler-asset-id> \
  <composition-external-oven-asset-id> \
  <composition-opc-ua-device-id> \
  <external-opc-ua-device-id>

kubectl delete -f \
  workspaces/iot-operations/samples/resource-set-composition/opc-plc.k8s \
  --ignore-not-found=true

kubectl patch secret aio-opc-ua-broker-trust-list \
  -n azure-iot-operations \
  --type=merge \
  -p '{"data":{"resource-set-opc-plc.crt":null}}'
```

Remove `sites.local/catalog-composition.yaml` when the sample site is no
longer used.

## Authoritative resource shapes

- [Assets and devices in Azure IoT Operations](https://learn.microsoft.com/azure/iot-operations/discover-manage-assets/concept-assets-devices)
- [Device reference TypeSpec](https://github.com/Azure/azure-rest-api-specs/blob/main/specification/deviceregistry/resource-manager/Microsoft.DeviceRegistry/DeviceRegistry/common/deviceRef.tsp)
- [Device properties TypeSpec](https://github.com/Azure/azure-rest-api-specs/blob/main/specification/deviceregistry/resource-manager/Microsoft.DeviceRegistry/DeviceRegistry/properties/ns_deviceProperties.tsp)
- [Asset properties TypeSpec](https://github.com/Azure/azure-rest-api-specs/blob/main/specification/deviceregistry/resource-manager/Microsoft.DeviceRegistry/DeviceRegistry/properties/ns_assetProperties.tsp)
- [AIO dataflow profile TypeSpec](https://github.com/Azure/azure-rest-api-specs/blob/main/specification/iotoperations/resource-manager/Microsoft.IoTOperations/IoTOperations/models/dataflows/dataflowProfiles.tsp)
- [Dynamic dataflow destination topics](https://learn.microsoft.com/azure/iot-operations/connect-to-cloud/howto-configure-dataflow-destination#dynamic-destination-topics)
