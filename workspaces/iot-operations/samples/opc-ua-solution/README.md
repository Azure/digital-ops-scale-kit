# opc-ua-solution

Reference sample that adds a simulated OPC UA asset, an Event Hub destination, and a dataflow that routes oven telemetry from the AIO broker to the Event Hub. Demonstrates the full asset-to-cloud data path against an existing AIO instance.

## What this sample deploys

1. **resolve-aio**: reads custom location and ADR namespace names from the existing AIO instance.
2. **opc-ua-solution** (`template.bicep`): an OPC UA `device` and `asset` in the resolved ADR namespace, an Event Hub namespace + hub, an Event Hubs Data Sender role assignment for the AIO extension principal, and a dataflow that maps the oven's data points (Temperature, EnergyUse, Weight) from the broker to the Event Hub.
3. **opc-plc-simulator** (`kubectl`): applies Microsoft's OPC PLC simulator deployment to the cluster so the asset has something to read.

Once running, the OPC UA connector polls the simulator over `opc.tcp://opcplc-000000:50000`, publishes oven telemetry to the broker on `azure-iot-operations/data/oven`, and the dataflow forwards each message to the Event Hub.

## Prerequisites

- AIO must be installed on the target cluster. Run `aio-install` first, or use the composed `samples/aio-with-opc-ua/manifest.yaml` for a single-command install + sample.
- The site's `aioRelease` must point to a release config under `parameters/aio-releases/`.
- Your principal needs role-assignment permissions (Owner, or `User Access Administrator` plus `Contributor`) on the deployment resource group so the Event Hubs Data Sender role can be granted to the AIO extension principal. Skip this requirement by setting `createRoleAssignment: false` if the role is already granted at a higher scope.

## Configure before deploying

Default values in `samples/opc-ua-solution/inputs.yaml` are wired from `resolve-aio` outputs, so a stock deploy works out of the box. Override the Event Hub name or skip the role assignment via a `sites.local/` overlay or CI parameter:

```yaml
# sites.local/<site>-opc.yaml or parameters override
eventHubName: "my-existing-eh"      # default: aio-eh-<resourceSuffix>
createRoleAssignment: false         # default: true. Disable if the role exists at a higher scope.
```

See `template.bicep` for the full parameter list.

## Deploy

```bash
siteops -w workspaces/iot-operations deploy samples/opc-ua-solution/manifest.yaml -l environment=dev
```

For a fresh-cluster combined install + sample, use the composed wrapper:

```bash
siteops -w workspaces/iot-operations deploy samples/aio-with-opc-ua/manifest.yaml -l environment=dev
```

## Verifying the result

Check the dataflow CR is projected to the cluster:

```bash
kubectl get dataflows.connectivity.iotoperations.azure.com -n azure-iot-operations
```

Subscribe to the topic with an in-cluster MQTT client (see Microsoft's [`mqtt-client.yaml` reference](https://learn.microsoft.com/azure/iot-operations/manage-mqtt-broker/howto-test-connection)) and watch for messages on `azure-iot-operations/data/oven`. End-to-end telemetry flow lags the deploy: after Bicep returns, the OPC UA connector still needs to reconcile the asset, establish the OPC UA session, and warm up polling before the first MQTT publish lands.

For Event Hub egress, inspect incoming messages in the Azure portal under the deployed Event Hub namespace, or query via the Event Hubs SDK.

## Removing the sample

The Bicep deploy is incremental. To remove the OPC UA resources after a deploy:

```bash
# Remove the asset, device, and dataflow ARM resources
az resource delete --ids <assetResourceId> <deviceResourceId> <dataflowResourceId>

# Remove the simulator pod
kubectl delete -f https://raw.githubusercontent.com/Azure-Samples/explore-iot-operations/0072eb6cdd3602bbd48858f21cf097e36a6b0b7e/samples/quickstarts/opc-plc-deployment.yaml

# Optional: delete the Event Hub namespace if no other workloads use it
az eventhubs namespace delete --name <eventHubName> --resource-group <rg>
```

## Writing your own sample

See `../README.md` for sample bundle conventions and how to add a new sample to this workspace.
