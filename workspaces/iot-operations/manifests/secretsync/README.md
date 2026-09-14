# Enable Secret Sync

Enable workload-identity-backed Secret Sync on an existing AIO instance.
This standalone operation performs enablement directly. The optional
Secret Sync path inside [aio-install](../aio-install/README.md) is controlled
by that entry's Site capability flag.

## Prerequisites and configuration

The existing connected cluster must already have OIDC issuer and workload
identity enabled. Confirm the Site's subscription, resource group,
`properties.aioRelease` and instance name. Use
`parameters.aioInstanceName` for an instance that does not follow the
Site-derived convention.

Enablement can create or configure a user-assigned identity, Key Vault,
federation, role assignments and a Secret Provider Class, and update the
instance's Secret Sync configuration. Review the
[Secret Sync reference](../../../../docs/secret-sync.md) for existing-vault,
permission and object-preservation settings.

Use an identity authorized for the selected resources and role assignments.
Resource creation can incur Azure charges. Planning does not establish
effective authorization or cluster readiness.

## Review and deploy

From the repository root:

```bash
siteops -w workspaces/iot-operations plan manifests/secretsync/manifest.yaml -l name=<site>
siteops -w workspaces/iot-operations deploy manifests/secretsync/manifest.yaml -l name=<site>
```

Replace `<site>` with the configured target. The explicit selector replaces
the default development-fleet selector.

## Enablement and secret materialization

Enablement alone is not proof that a selected Key Vault value materialized in
Kubernetes. Secret population and SecretSync resources have their own
ownership and observation requirements. The
[Secret Sync sample](../../samples/secretsync-sample/README.md) demonstrates
those steps using deliberately replaceable placeholder values.

Keep real values in authorized private configuration. Review Secret Provider
Class object ownership before applying another declaration.

## Removal

Removing a selection does not automatically remove identities, permissions,
vault data or synchronized resources. Follow the reference's removal guidance
for the objects you own. There is no automatic rollback of partial changes.
