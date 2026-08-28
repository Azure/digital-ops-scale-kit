// deploy-release-resources.bicep
// -------------------------------------------------------------------------------------
// Routes upgrade-time AIO child resources to a typed module for the selected
// control-plane API generation. Extension configuration remains in
// update-extensions.bicep.
//
// Adding a new API version:
//   1. Extend @allowed on aioApiVersion.
//   2. Add one conditional module with the same parameter surface.
//   3. Add the matching version module under upgrade/modules/.
// -------------------------------------------------------------------------------------

@description('Name of the existing IoT Operations instance.')
param aioInstanceName string

@description('Full resource ID of the custom location associated with the instance.')
param customLocationId string

@description('IoT Operations API version for release-required resource deployment.')
@allowed([
  '2025-10-01'
  '2026-03-01'
  '2026-07-01'
])
param aioApiVersion string

@description('Release-owned AIO settings that distinguish releases sharing an ARM API generation.')
param aioReleaseConfiguration object = {}

module releaseResources_2025 './modules/deploy-release-resources-2025-10-01.bicep' = if (aioApiVersion == '2025-10-01') {
  name: 'deploy-release-resources-2025-10-${uniqueString(aioInstanceName)}'
  params: {
    aioInstanceName: aioInstanceName
    aioReleaseConfiguration: aioReleaseConfiguration
    customLocationId: customLocationId
  }
}

module releaseResources_2026_03 './modules/deploy-release-resources-2026-03-01.bicep' = if (aioApiVersion == '2026-03-01') {
  name: 'deploy-release-resources-2026-03-${uniqueString(aioInstanceName)}'
  params: {
    aioInstanceName: aioInstanceName
    aioReleaseConfiguration: aioReleaseConfiguration
    customLocationId: customLocationId
  }
}

module releaseResources_2026_07 './modules/deploy-release-resources-2026-07-01.bicep' = if (aioApiVersion == '2026-07-01') {
  name: 'deploy-release-resources-2026-07-${uniqueString(aioInstanceName)}'
  params: {
    aioInstanceName: aioInstanceName
    aioReleaseConfiguration: aioReleaseConfiguration
    customLocationId: customLocationId
  }
}
