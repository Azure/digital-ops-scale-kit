// =====================================================================================
// Secret Provider Class objects derivation. Import-only library.
//
// The `objects` field lists the Key Vault objects the Secret Sync controller
// materializes. Both the enablement template and the sync template PUT the same
// Secret Provider Class, so both derive this field here rather than each computing
// its own. A full PUT replaces the field, so two writers with different derivations
// would take turns clearing each other's value.
// =====================================================================================

@description('Renders the Secret Provider Class `objects` YAML for a set of Key Vault secrets. Each entry contributes one `objectName` of type `secret`.')
@export()
func spcObjectsYaml(secrets array) string =>
  'array:\n${join(map(secrets, s => '  - |\n    objectName: ${s.secretName}\n    objectType: secret'), '\n')}\n'
