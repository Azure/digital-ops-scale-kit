# contracts/

Workspace-owned rules for composing parameter sources before a deployment
template receives them.

`aio-catalog.yaml` declares:

- collection identity for devices, assets, endpoints, profiles, and dataflows
- provider-owned seed identities such as `default`
- provider references such as asset `deviceRef` and dataflow `endpointRef`
- nested lookup from an asset endpoint name into its selected device
- fields recorded with their bound value and reason when the current catalog
  cannot resolve their target

Site Ops interprets only the generic path and identity grammar. Azure IoT
Operations names and reference semantics remain in this workspace.

Resource-set authors normally edit files under `parameters/`, not this
contract. Change the contract when a new resource kind or provider reference
joins the catalog.
