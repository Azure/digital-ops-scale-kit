// read-spc-objects.bicep
// -------------------------------------------------------------------------------------
// Module: reads the `objects` field off an existing Secret Provider Class.
//
// Deployed as a module so the caller can skip it entirely. A module carrying
// `condition: false` is never deployed, while an ARM `if()` around a `reference()`
// evaluates both branches. The parent sets the module scope from the subscription
// and resource group in the class's resource ID, which may differ from its own.
//
// The caller must not instantiate this when the ID is empty. An `existing` reference
// to an absent resource fails the deployment.
// -------------------------------------------------------------------------------------

@description('Name of the existing Secret Provider Class to read.')
param spcName string

resource spc 'Microsoft.SecretSyncController/azureKeyVaultSecretProviderClasses@2024-08-21-preview' existing = {
  name: spcName
}

@description('Current `objects` YAML document, empty when the field is unset.')
output objects string = spc.properties.?objects ?? ''
