// opcua-connector-template-2026-07-01.bicep
// -------------------------------------------------------------------------------------
// Deploys the supervisor-managed OPC UA ConnectorTemplate for an existing
// IoT Operations instance.
// -------------------------------------------------------------------------------------

@description('Name of the existing IoT Operations instance.')
param aioInstanceName string

@description('Full resource ID of the custom location associated with the instance.')
param customLocationId string

@description('Released OPC UA connector version used by the metadata and supervisor image.')
param connectorVersion string

resource aioInstance 'Microsoft.IoTOperations/instances@2026-07-01' existing = {
  name: aioInstanceName
}

var connectorTemplateName = 'azureiotoperationsconnectorforopcua-${substring(uniqueString(aioInstance.id), 0, 4)}'

resource connectorTemplate 'Microsoft.IoTOperations/instances/akriConnectorTemplates@2026-07-01' = {
  parent: aioInstance
  name: connectorTemplateName
  extendedLocation: {
    name: customLocationId
    type: 'CustomLocation'
  }
  properties: {
    connectorMetadataRef: 'mcr.microsoft.com/azureiotoperations/aio-connectors/opcua-metadata:${connectorVersion}'
    aioMetadata: {
      aioMinVersion: '1.2.100'
    }
    runtimeConfiguration: {
      runtimeConfigurationType: 'ManagedConfiguration'
      managedConfigurationSettings: {
        managedConfigurationType: 'ImageConfiguration'
        imageConfigurationSettings: {
          registrySettings: {
            registrySettingsType: 'ContainerRegistry'
            containerRegistrySettings: {
              registry: 'mcr.microsoft.com'
            }
          }
          imageName: 'azureiotoperations/aio-connectors/supervisor'
          tagDigestSettings: {
            tagDigestType: 'Tag'
            tag: connectorVersion
          }
        }
      }
    }
    deviceInboundEndpointTypes: [
      {
        endpointType: 'Microsoft.OpcUa'
      }
    ]
  }
}
