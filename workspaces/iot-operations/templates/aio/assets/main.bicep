// main.bicep
// -------------------------------------------------------------------------------------
// Family entry point for the asset catalog. Creates every Azure Device Registry resource
// kind the effective composition carries, at the ADR API generation the site's release ships.
//
// A resource type and API version is a string literal in Bicep and cannot be computed,
// so each generation has its own module under `modules/`.
//
// Adding a generation:
//   1. Extend @allowed on adrApiVersion below.
//   2. Copy the newest module, changing every API version literal in it.
//   3. Add a `module assets_<YYYY>` block mirroring the existing ones.
//
// See docs/resource-catalog.md for the authoring contract.
// -------------------------------------------------------------------------------------

/*****************************************************************************/
/*                          Deployment Parameters                            */
/*****************************************************************************/

@description('Name of the existing Azure Device Registry namespace the resources are created under.')
param adrNamespaceName string

@description('Name of the custom location bound to the existing AIO instance.')
param customLocationName string

@description('Location for the created devices and assets. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Tags to apply to the created devices and assets.')
param tags object = {}

@description('Devices to create. Each entry is { name: string, properties: object }. An empty array creates nothing.')
param devices array = []

@description('Assets to create. Each entry is { name: string, properties: object }. An empty array creates nothing.')
param assets array = []

// An unlisted version is rejected by ARM. A version added here needs a matching
// module block below.
@description('Azure Device Registry API version, supplied by the site release file at parameters/aio-releases/<release>.yaml.')
@allowed([
  '2025-10-01'
  '2026-04-01'
])
param adrApiVersion string

/*****************************************************************************/
/*                        Per-generation dispatch                            */
/*****************************************************************************/

module assets_2025 './modules/assets-2025-10-01.bicep' = if (adrApiVersion == '2025-10-01') {
  name: 'assets-2025-10-01-${uniqueString(adrNamespaceName)}'
  params: {
    adrNamespaceName: adrNamespaceName
    customLocationName: customLocationName
    location: location
    tags: tags
    devices: devices
    assets: assets
  }
}

module assets_2026_04 './modules/assets-2026-04-01.bicep' = if (adrApiVersion == '2026-04-01') {
  name: 'assets-2026-04-01-${uniqueString(adrNamespaceName)}'
  params: {
    adrNamespaceName: adrNamespaceName
    customLocationName: customLocationName
    location: location
    tags: tags
    devices: devices
    assets: assets
  }
}
