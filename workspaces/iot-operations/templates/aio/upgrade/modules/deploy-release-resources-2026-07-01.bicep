// deploy-release-resources-2026-07-01.bicep
// -------------------------------------------------------------------------------------
// Deploys child resources required when an existing AIO instance moves to a
// release using the 2026-07-01 control-plane generation.
// -------------------------------------------------------------------------------------

@description('Name of the existing IoT Operations instance.')
param aioInstanceName string

@description('Full resource ID of the custom location associated with the instance.')
param customLocationId string

@description('Release-owned AIO settings that distinguish releases sharing this ARM API generation.')
param aioReleaseConfiguration object = {}

// Keep resource interpretation aligned with instance-2026-07-01.bicep so
// greenfield install and upgrade deploy the same release requirements.
var releaseResourceConfiguration = aioReleaseConfiguration.?resources ?? {}
var opcUaConnectorConfiguration = releaseResourceConfiguration.?opcUaConnector ?? {}
var opcuaConnectorVersion = string(opcUaConnectorConfiguration.?version ?? '')

module opcUaConnectorTemplate '../../modules/opcua-connector-template-2026-07-01.bicep' = if (!empty(opcuaConnectorVersion)) {
  name: 'opcua-connector-template-${uniqueString(aioInstanceName)}'
  params: {
    aioInstanceName: aioInstanceName
    connectorVersion: opcuaConnectorVersion
    customLocationId: customLocationId
  }
}
