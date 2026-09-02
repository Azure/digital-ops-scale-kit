# resource-set-basic

Start here to see a site select one reusable resource set. The committed
`catalog-basic` site selects only `basic-routing`:

```yaml
properties:
  resourceSets:
    dataflows:
      - basic-routing
```

The sample deploys:

1. The existing AIO instance lookup.
2. One dataflow named `basic-routing` on the instance-owned `default` profile
   and endpoint.

The dataflow reads
`azure-iot-operations/data/siteops-samples/basic/#` and republishes messages to
`siteops-samples/catalog-basic/basic` through the same instance-owned local
MQTT endpoint. The destination sits outside the source wildcard, so
republished messages do not loop.

## Prepare the site

The committed site contains placeholder identity. Create
`workspaces/iot-operations/sites.local/catalog-basic.yaml` with values for the
target cluster:

```yaml
apiVersion: siteops/v1
kind: Site
name: catalog-basic
subscription: "<subscription-id>"
resourceGroup: "<resource-group>"
parameters:
  clusterName: "<arc-cluster-name>"
```

Run `aio-install` against this site first, or set the resolved AIO instance
name in the overlay when using an existing installation with another naming
convention.

The committed site uses `environment=sample`, which keeps it out of ordinary
development fleet deployments. In the GitHub Actions or Azure Pipelines
deployment UI, select the `dev` environment. The workflow uses that environment
for credentials and approvals and adds the sample selector automatically.

## Review and deploy

```bash
siteops -w workspaces/iot-operations \
  validate samples/resource-set-basic/manifest.yaml --plan

siteops -w workspaces/iot-operations \
  deploy samples/resource-set-basic/manifest.yaml
```

Publish a JSON message under
`azure-iot-operations/data/siteops-samples/basic/` and subscribe to
`siteops-samples/catalog-basic/basic` on the local MQTT broker. The republished
message proves the selected set produced a working dataflow rather than only a
valid ARM declaration.

Microsoft's
[MQTT client walkthrough](https://learn.microsoft.com/azure/iot-operations/manage-mqtt-broker/howto-test-connection)
shows how to run an authenticated client inside the cluster. The
`resource-set-samples` E2E phase performs the same proof with a run-specific
payload: it subscribes first, publishes under the sample source prefix, and
requires that payload at the destination.

## Remove the sample

Deselecting the set stops applying it and does not delete the dataflow. Delete
the resource explicitly, then remove the local site overlay:

```bash
az resource delete --ids <basic-routing-dataflow-id>
```

Remove `sites.local/catalog-basic.yaml` when the sample site is no longer
used.

## Authoritative resource shapes

- [Create a data flow using Azure IoT Operations](https://learn.microsoft.com/azure/iot-operations/connect-to-cloud/howto-create-dataflow)
- [AIO dataflow endpoint TypeSpec](https://github.com/Azure/azure-rest-api-specs/blob/main/specification/iotoperations/resource-manager/Microsoft.IoTOperations/IoTOperations/models/dataflows/dataflowEndpoints.tsp)
- [AIO dataflow TypeSpec](https://github.com/Azure/azure-rest-api-specs/blob/main/specification/iotoperations/resource-manager/Microsoft.IoTOperations/IoTOperations/models/dataflows/dataflows.tsp)
