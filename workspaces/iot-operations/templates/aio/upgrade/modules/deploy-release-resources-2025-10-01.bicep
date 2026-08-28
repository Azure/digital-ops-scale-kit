// deploy-release-resources-2025-10-01.bicep
// -------------------------------------------------------------------------------------
// Releases on this API generation require no upgrade-time child resources.
// The shared parameter surface keeps dispatcher arms interchangeable and gives
// a later release a typed place to add a requirement.
// -------------------------------------------------------------------------------------

@description('Name of the existing IoT Operations instance.')
#disable-next-line no-unused-params
param aioInstanceName string

@description('Full resource ID of the custom location associated with the instance.')
#disable-next-line no-unused-params
param customLocationId string

@description('Release-owned AIO settings that distinguish releases sharing this ARM API generation.')
#disable-next-line no-unused-params
param aioReleaseConfiguration object = {}
