// main.bicep
// -------------------------------------------------------------------------------------
// Creates a Microsoft.Edge/sites resource at resource-group scope.
// Pair with subscription.bicep when an edge site needs subscription scope instead.
//
// Inputs:  siteName, optional displayName/siteDescription/siteAddress/labels.
// Outputs: site { id, name, displayName }.
// -------------------------------------------------------------------------------------

metadata description = 'Creates an Azure Edge site resource at resource group scope.'

import { siteAddressType } from './types.bicep'

/*****************************************************************************/
/*                          Deployment Parameters                            */
/*****************************************************************************/

@description('Name of the site resource.')
param siteName string

@description('Display name for the site. Defaults to the resource name.')
param displayName string = siteName

@description('Description of the site.')
param siteDescription string = ''

@description('Site address information.')
param siteAddress siteAddressType?

@description('Labels for categorizing the site.')
param labels object = {}

/*****************************************************************************/
/*                          Site Resource                                    */
/*****************************************************************************/

resource site 'Microsoft.Edge/sites@2025-06-01' = {
  name: siteName
  properties: {
    displayName: displayName
    description: !empty(siteDescription) ? siteDescription : null
    siteAddress: !empty(siteAddress) ? siteAddress : null
    labels: !empty(labels) ? labels : null
  }
}

/*****************************************************************************/
/*                          Deployment Outputs                               */
/*****************************************************************************/

output site object = {
  id: site.id
  name: site.name
  displayName: site.properties.displayName
}
