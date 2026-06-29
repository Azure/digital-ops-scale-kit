// samples/opc-ua-solution/template.bicep
// -------------------------------------------------------------------------------------
// Sample solution that runs on top of an existing AIO instance: OPC UA device + asset,
// Event Hub destination, role assignment, and a dataflow mapping oven telemetry
// from the broker to the Event Hub.
//
// Inputs:  cluster + AIO instance refs (custom location, instance name, default
//          dataflow endpoint/profile names), ADR namespace, Event Hub name.
// Outputs: created Event Hub identity, resolved AIO extension name.
//
// Sample API-version policy: pinned to the oldest supported AIO/ADR API
// versions, relying on RP backward-compatibility to work across all
// supported releases. See docs/aio-releases.md.
// -------------------------------------------------------------------------------------

metadata description = 'This template deploys components that are required to show data flowing after cluster provisioning and AIO deployment.'

import { aioExtensionName as deriveAioExtensionName } from '../../templates/common/extension-names.bicep'

/*****************************************************************************/
/*                          Deployment Parameters                            */
/*****************************************************************************/

@description('Name of the existing Arc-connected Kubernetes cluster hosting the AIO instance.')
param clusterName string

@description('Region of the existing Arc-connected cluster hosting AIO. The Event Hub namespace created by this template is co-located in this region. Defaults to the resource group location.')
param clusterLocation string = resourceGroup().location

@description('Name of the AIO custom location bound to the existing AIO instance.')
param customLocationName string

@description('Name of the AIO extension. If empty, derived from cluster ID using convention.')
param aioExtensionName string = ''

@description('Name of the existing AIO instance the sample binds to.')
param aioInstanceName string

@description('Short hash appended to created resource names to keep them globally unique. Defaults to a stable hash of subscription + RG + cluster.')
param resourceSuffix string = substring(uniqueString(subscription().id, resourceGroup().id, clusterName), 0, 10)

@description('Name of the Event Hub namespace created by this template (also used as the Event Hub name).')
param eventHubName string = 'aio-eh-${resourceSuffix}'

@description('Name of the default dataflow endpoint child resource on the AIO instance.')
param defaultDataflowEndpointName string = 'default'

@description('Name of the default dataflow profile child resource on the AIO instance.')
param defaultDataflowProfileName string = 'default'

@description('Whether to create the Event Hubs Data Sender role assignment for the AIO extension principal. Disable when the principal already has the role at a higher scope.')
param createRoleAssignment bool = true
@description('Name of the ADR namespace where assets will be created.')
param adrNamespaceName string

@description('Tags to apply to created resources.')
param tags object = {}

/*****************************************************************************/
/*                          Existing AIO cluster                             */
/*****************************************************************************/

resource connectedCluster 'Microsoft.Kubernetes/connectedClusters@2024-07-15-preview' existing = {
  name: clusterName
}

resource customLocation 'Microsoft.ExtendedLocation/customLocations@2021-08-31-preview' existing = {
  name: customLocationName
}

// Derive extension name from cluster ID if not provided (matches the aio/instance.bicep convention)
var resolvedExtensionName = !empty(aioExtensionName)
  ? aioExtensionName
  : deriveAioExtensionName(connectedCluster.id)

resource aioExtension 'Microsoft.KubernetesConfiguration/extensions@2023-05-01' existing = {
  name: resolvedExtensionName
  scope: connectedCluster
}

resource aioInstance 'Microsoft.IoTOperations/instances@2025-10-01' existing = {
  name: aioInstanceName
}

resource defaultDataflowEndpoint 'Microsoft.IoTOperations/instances/dataflowEndpoints@2025-10-01' existing = {
  name: defaultDataflowEndpointName
  parent: aioInstance
}

resource defaultDataflowProfile 'Microsoft.IoTOperations/instances/dataflowProfiles@2025-10-01' existing = {
  name: defaultDataflowProfileName
  parent: aioInstance
}

resource namespace 'Microsoft.DeviceRegistry/namespaces@2025-10-01' existing = {
  name: adrNamespaceName
}

/*****************************************************************************/
/*                                    Asset                                  */
/*****************************************************************************/

var assetName = 'oven'
var opcUaEndpointName = 'opc-ua-connector-0'
var deviceName = 'opc-ua-connector'

resource device 'Microsoft.DeviceRegistry/namespaces/devices@2025-10-01' = {
  name: deviceName
  parent: namespace
  location: clusterLocation
  tags: tags
  extendedLocation: {
    type: 'CustomLocation'
    name: customLocation.id
  }
  properties: {
    endpoints: {
      outbound: {
        assigned: {}
      }
      inbound: {
        '${opcUaEndpointName}': {
          endpointType: 'Microsoft.OpcUa'
          address: 'opc.tcp://opcplc-000000:50000'
          authentication: {
            method: 'Anonymous'
          }
        }
      }
    }
  }
}

resource asset 'Microsoft.DeviceRegistry/namespaces/assets@2025-10-01' = {
  name: assetName
  parent: namespace
  location: clusterLocation
  tags: tags
  extendedLocation: {
    type: 'CustomLocation'
    name: customLocation.id
  }
  properties: {
    displayName: assetName
    deviceRef: {
      deviceName: device.name
      endpointName: opcUaEndpointName
    }
    description: 'Multi-function large oven for baked goods.'

    enabled: true
    attributes: {
      manufacturer: 'Contoso'
      manufacturerUri: 'http://www.contoso.com/ovens'
      model: 'Oven-003'
      productCode: '12345C'
      hardwareRevision: '2.3'
      softwareRevision: '14.1'
      serialNumber: '12345'
      documentationUri: 'http://docs.contoso.com/ovens'
    }

    datasets: [
      {
        name: 'Oven telemetry'
        dataPoints: [
          {
            name: 'Temperature'
            dataSource: 'ns=3;s=SpikeData'
            dataPointConfiguration: '{"samplingInterval":500,"queueSize":1}'
          }
          {
            name: 'EnergyUse'
            dataSource: 'ns=3;s=FastUInt10'
            dataPointConfiguration: '{"samplingInterval":500,"queueSize":1}'
          }
          {
            name: 'Weight'
            dataSource: 'ns=3;s=FastUInt9'
            dataPointConfiguration: '{"samplingInterval":500,"queueSize":1}'
          }
        ]
        destinations: [
          {
            target: 'Mqtt'
            configuration: {
              topic: 'azure-iot-operations/data/oven'
              retain: 'Never'
              qos: 'Qos1'
            }
          }
        ]
      }
    ]

    defaultDatasetsConfiguration: '{"publishingInterval":1000,"samplingInterval":500,"queueSize":1}'
    defaultEventsConfiguration: '{"publishingInterval":1000,"samplingInterval":500,"queueSize":1}'
  }
}

/*****************************************************************************/
/*                                  Event Hub                                */
/*****************************************************************************/

resource eventHubNamespace 'Microsoft.EventHub/namespaces@2024-01-01' = {
  name: eventHubName
  location: clusterLocation
  tags: tags
  properties: {
    disableLocalAuth: true
  }
}

// Role assignment for Event Hubs Data Sender role
resource roleAssignmentDataSender 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (createRoleAssignment) {
  name: guid(eventHubNamespace.id, aioExtension.id, '2b629674-e913-4c01-ae53-ef4638d8f975')
  scope: eventHubNamespace
  properties: {
    // ID for Event Hubs Data Sender role is 2b629674-e913-4c01-ae53-ef4638d8f975
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2b629674-e913-4c01-ae53-ef4638d8f975')
    // Safe-access in case identity was not provisioned on the AIO extension.
    principalId: aioExtension.?identity.?principalId ?? ''
    principalType: 'ServicePrincipal'
  }
}

resource eventHub 'Microsoft.EventHub/namespaces/eventhubs@2024-01-01' = {
  name: 'destinationeh'
  parent: eventHubNamespace
  properties: {
    messageRetentionInDays: 1
    partitionCount: 1
  }
}

/*****************************************************************************/
/*                                    Data flow                              */
/*****************************************************************************/

resource dataflowEndpointEventHub 'Microsoft.IoTOperations/instances/dataflowEndpoints@2025-10-01' = {
  parent: aioInstance
  name: 'opc-ua-solution-eh-endpoint'
  extendedLocation: {
    name: customLocation.id
    type: 'CustomLocation'
  }
  properties: {
    endpointType: 'Kafka'
    kafkaSettings: {
      host: '${eventHubName}.servicebus.windows.net:9093'
      batching: {
        latencyMs: 0
        maxMessages: 100
      }
      tls: {
        mode: 'Enabled'
      }
      authentication: {
        method: 'SystemAssignedManagedIdentity'
        systemAssignedManagedIdentitySettings: {
          audience: 'https://${eventHubName}.servicebus.windows.net'
        }
      }
    }
  }
  dependsOn: [
    eventHubNamespace
  ]
}

resource dataflowCToF 'Microsoft.IoTOperations/instances/dataflowProfiles/dataflows@2025-10-01' = {
  parent: defaultDataflowProfile
  name: 'opc-ua-solution-oven-dataflow'
  extendedLocation: {
    name: customLocation.id
    type: 'CustomLocation'
  }
  properties: {
    mode: 'Enabled'
    operations: [
      {
        operationType: 'Source'
        sourceSettings: {
          endpointRef: defaultDataflowEndpoint.name
          assetRef: asset.name
          serializationFormat: 'Json'
          dataSources: ['azure-iot-operations/data/${asset.name}']
        }
      }
      {
        operationType: 'BuiltInTransformation'
        builtInTransformationSettings: {
          serializationFormat: 'Json'
          map: [
            {
              type: 'PassThrough'
              inputs: [
                '*'
              ]
              output: '*'
            }
            {
              type: 'Compute'
              description: 'Temperature in F'
              inputs: [
                'Temperature.Value ? $last'
              ]
              expression: '$1 * 9/5 + 32'
              output: 'TemperatureF'
            }
            {
              type: 'Compute'
              description: 'Weight Offset'
              inputs: [
                'Weight.Value ? $last'
              ]
              expression: '$1 - 150'
              output: 'FillWeight'
            }
            {
              type: 'Compute'
              inputs: [
                'Temperature.Value ? $last'
              ]
              expression: '$1 > 225'
              output: 'Spike'
            }
            {
              inputs: [
                '$metadata.user_property.externalAssetId'
              ]
              output: 'AssetId'
            }
          ]
        }
      }
      {
        operationType: 'Destination'
        destinationSettings: {
          endpointRef: dataflowEndpointEventHub.name
          dataDestination: 'destinationeh'
        }
      }
    ]
  }
  dependsOn: [
    eventHub
  ]
}

output eventHub object = {
  name: eventHub.name
  namespace: eventHubNamespace.name
}

output resolvedExtensionName string = resolvedExtensionName
