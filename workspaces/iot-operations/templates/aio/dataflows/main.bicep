// main.bicep
// -------------------------------------------------------------------------------------
// Family entry point for the dataflow catalog. Creates every dataflow resource kind a
// declaration carries, at the AIO API generation the site's release ships.
//
// A resource type and API version is a string literal in Bicep and cannot be computed,
// so each generation has its own module under `modules/`.
//
// Adding a generation:
//   1. Extend @allowed on aioApiVersion below.
//   2. Copy the newest module, changing every API version literal in it.
//   3. Add a `module dataflows_<YYYY>` block mirroring the existing ones.
//
// See docs/resource-catalog.md for the authoring contract.
// -------------------------------------------------------------------------------------

/*****************************************************************************/
/*                          Deployment Parameters                            */
/*****************************************************************************/

@description('Name of the existing AIO instance the resources are created under.')
param aioInstanceName string

@description('Name of the custom location bound to the existing AIO instance.')
param customLocationName string

@description('Dataflow endpoints to create. Each entry is { name: string, properties: object }. An empty array creates nothing.')
param dataflowEndpoints array = []

@description('Dataflow profiles to create. Each entry is { name: string, properties: object }. An empty array creates nothing.')
param dataflowProfiles array = []

@description('Dataflows to create. Each entry is { name: string, profileRef?: string, properties: object }. An empty array creates nothing.')
param dataflows array = []

@description('Profile a dataflow runs in when its entry declares no profileRef. AIO creates this profile alongside the instance.')
param defaultProfileName string = 'default'

// An unlisted version is rejected by ARM. A version added here needs a matching
// module block below.
@description('AIO control-plane API version, supplied by the site release file at parameters/aio-releases/<release>.yaml.')
@allowed([
  '2025-10-01'
  '2026-03-01'
  '2026-07-01'
])
param aioApiVersion string

/*****************************************************************************/
/*                        Per-generation dispatch                            */
/*****************************************************************************/

module dataflows_2025 './modules/dataflows-2025-10-01.bicep' = if (aioApiVersion == '2025-10-01') {
  name: 'dataflows-2025-10-01-${uniqueString(aioInstanceName)}'
  params: {
    aioInstanceName: aioInstanceName
    customLocationName: customLocationName
    dataflowEndpoints: dataflowEndpoints
    dataflowProfiles: dataflowProfiles
    dataflows: dataflows
    defaultProfileName: defaultProfileName
  }
}

module dataflows_2026_03 './modules/dataflows-2026-03-01.bicep' = if (aioApiVersion == '2026-03-01') {
  name: 'dataflows-2026-03-01-${uniqueString(aioInstanceName)}'
  params: {
    aioInstanceName: aioInstanceName
    customLocationName: customLocationName
    dataflowEndpoints: dataflowEndpoints
    dataflowProfiles: dataflowProfiles
    dataflows: dataflows
    defaultProfileName: defaultProfileName
  }
}

module dataflows_2026_07 './modules/dataflows-2026-07-01.bicep' = if (aioApiVersion == '2026-07-01') {
  name: 'dataflows-2026-07-01-${uniqueString(aioInstanceName)}'
  params: {
    aioInstanceName: aioInstanceName
    customLocationName: customLocationName
    dataflowEndpoints: dataflowEndpoints
    dataflowProfiles: dataflowProfiles
    dataflows: dataflows
    defaultProfileName: defaultProfileName
  }
}

/*****************************************************************************/
/*                                  Outputs                                  */
/*****************************************************************************/

// Derived from the declaration rather than from whichever module ran, so they stay
// correct as generations are added or retired. `apiVersion` is the exception: it
// comes from the module that ran, so a generation allowed without a module fails
// the deploy rather than reporting a declaration it never created.

@description('Endpoint names this deploy declared, in declaration order. An empty array means the declaration reached the family with no endpoints.')
output endpointNames array = [for endpoint in dataflowEndpoints: endpoint.name]

@description('Profile names this deploy declared, in declaration order.')
output profileNames array = [for profile in dataflowProfiles: profile.name]

@description('Dataflow names this deploy declared, in declaration order.')
output dataflowNames array = [for dataflow in dataflows: dataflow.name]

@description('Profile each dataflow was created under, in declaration order. An entry declaring no profileRef reports the default it fell back to. This is the same expression the resource name is built from, so a successful deploy places each dataflow under the profile reported here.')
output dataflowProfileRefs array = [
  for dataflow in dataflows: dataflow.?profileRef ?? defaultProfileName
]

@description('AIO API generation these resources were written at, reported by the module that ran.')
output apiVersion string = aioApiVersion == '2025-10-01'
  ? dataflows_2025!.outputs.deployedApiVersion
  : aioApiVersion == '2026-03-01'
      ? dataflows_2026_03!.outputs.deployedApiVersion
      : dataflows_2026_07!.outputs.deployedApiVersion
