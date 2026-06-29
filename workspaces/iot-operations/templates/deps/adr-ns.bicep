// adr-ns.bicep
// -------------------------------------------------------------------------------------
// Creates an Azure Device Registry namespace. Devices and assets created later
// (e.g. by samples/opc-ua-solution/template.bicep) live under this namespace.
//
// Routes to a per-API-version module under ./modules; ADR namespace API can
// move per AIO release.
//
// Inputs:  adrNamespaceName, adrApiVersion (sourced from aio-releases YAML),
//          optional location/tags.
// Outputs: adrNamespace { id, name, principalId }.
// -------------------------------------------------------------------------------------

metadata description = 'Creates an Azure Device Registry namespace for use with Azure IoT Operations.'

/*****************************************************************************/
/*                          Deployment Parameters                            */
/*****************************************************************************/

@description('Name of the ADR namespace to create.')
param adrNamespaceName string

@description('Location for the namespace. Defaults to resource group location.')
param location string = resourceGroup().location

@description('Tags to apply to resources')
param tags object = {}

@description('Azure Device Registry API version for the namespace deployment. Sourced from parameters/aio-releases/<release>.yaml.')
@allowed([
  '2025-10-01'
  '2026-04-01'
])
param adrApiVersion string

/*****************************************************************************/
/*                  ADR Namespace (API-versioned dispatch)                   */
/*****************************************************************************/

// --- Adding a new API version ----------------------------------------------
// Convention: the newest API version is always the else-branch (falsy fallback);
// every older version is an explicit positive equality check. Mirror this in
// any future ADR-versioned templates that join this dispatch.
// ---------------------------------------------------------------------------

module ns_2025 './modules/adr-ns-2025-10-01.bicep' = if (adrApiVersion == '2025-10-01') {
  name: 'adr-ns-2025-10-01'
  params: {
    adrNamespaceName: adrNamespaceName
    location: location
    tags: tags
  }
}

module ns_2026 './modules/adr-ns-2026-04-01.bicep' = if (adrApiVersion == '2026-04-01') {
  name: 'adr-ns-2026-04-01'
  params: {
    adrNamespaceName: adrNamespaceName
    location: location
    tags: tags
  }
}

var active = adrApiVersion == '2025-10-01'
  ? {
      id: ns_2025!.outputs.id
      name: ns_2025!.outputs.name
      principalId: ns_2025!.outputs.principalId
    }
  : {
      id: ns_2026!.outputs.id
      name: ns_2026!.outputs.name
      principalId: ns_2026!.outputs.principalId
    }

/*****************************************************************************/
/*                          Deployment Outputs                               */
/*****************************************************************************/

output adrNamespace object = {
  id: active.id
  name: active.name
  principalId: active.principalId
}

