# Enable Secret Sync

Enable Secret Sync, backed by workload identity, on an existing AIO instance.
This standalone operation performs enablement directly. The optional
Secret Sync path inside [aio-install](../aio-install/README.md) is controlled
by that manifest's Site capability flag.

## Use an existing instance ID

With an approved source named `official` that you enrolled independently, select the release
from its instructions and supply the existing AIO 2607 or 2608 instance ID:

```text
siteops deploy secretsync --source "official@<release>" --input "instance=<AIO-instance-resource-ID>"
```

The authorized reads resolve the instance's custom location and Arc cluster
within the same subscription and resource group. They verify existing OIDC
and workload identity before deployment. The operation uses the actual
instance name and the API shared by those releases. It does not reinstall
AIO, change extension versions or guess the installed AIO release.
The `2608` content profile supplies the pinned API configuration only.
Deploy displays the prepared plan and asks for confirmation before executing
it. Use `--yes` for unattended execution, or `plan` for a separate preview.

`siteName`, `environment`, `country` and `existingVault` are optional answers.
The default Site name is derived from the associated cluster. Keep an explicit
name when reusing older Site configuration. See
[guided inputs](../../../../docs/guided-inputs.md#enable-secret-sync-on-an-existing-instance)
for answer files and the route with an existing vault.

## Prerequisites and configuration

The existing connected cluster must already have OIDC issuer and workload
identity enabled. For the route with configured Sites, confirm its subscription, resource group,
`properties.aioRelease` and instance name. Use
`parameters.aioInstanceName` for an instance that does not follow the
convention derived from the Site.

Enablement can create or configure a user-assigned identity, Key Vault,
federation, role assignments and a Secret Provider Class, and update the
instance's Secret Sync configuration. Review the
[Secret Sync reference](../../../../docs/secret-sync.md) for settings that
cover an existing vault, permissions and object preservation.

Use an identity authorized for the selected resources and role assignments.
Resource creation can incur Azure charges. Planning does not establish
effective authorization or cluster readiness.

## Review and deploy

For configured Sites, run from the repository root:

```bash
siteops -w workspaces/iot-operations plan manifests/secretsync/manifest.yaml -l name=<site>
siteops -w workspaces/iot-operations deploy manifests/secretsync/manifest.yaml -l name=<site>
```

Replace `<site>` with the configured Site. The explicit selector replaces
the default selector for the development fleet.

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
