# IoT Operations workspace

This workspace applies the generic Site Ops engine to
[Azure IoT Operations](https://learn.microsoft.com/azure/iot-operations/).
It contains executable manifests, Bicep templates, AIO release pins, Site
conventions, reusable resource definitions, and samples.

Installing Site Ops does not include this workspace. Use it in one of these
ways:

- Deploy a published release directly with `--source SOURCE@RELEASE`.
- Pin a release in an operator project for repeatable use.
- Run from a local checkout with `-w workspaces/iot-operations`.

The published routes need no clone.

## Start with one Site

For a package from an approved source and one explicit Site, follow
[guided inputs](../../docs/guided-inputs.md). With the source enrolled
independently and an authorized Azure CLI identity, supply the existing
Arc cluster ID and the release selected by its instructions:

```text
siteops deploy aio-install --source "official@<release>" --input "cluster=<Arc-cluster-resource-ID>"
```

Deploy prepares and displays a plan, then asks for confirmation in an
interactive terminal.
A project pin and separate plan are optional. Add
`--input enableSecretSync=true` only after confirming the cluster's OIDC
and workload identity prerequisites.

For a local checkout with a reusable Site, follow
[Deploy AIO from a local checkout](../../docs/getting-started.md).
It prepares a `sites.local/` overlay for one existing cluster connected to
Azure Arc, inspects the Site, validates the manifest, reviews an executable plan,
and only then deploys. Both use the same manifest and executor.

Workspace content runs with the identity supplied to Site Ops. Review the
selected manifest and templates before use. AIO deployments can create role
assignments, update existing resources, and incur Azure charges. A successful
resource deployment does not by itself establish AIO readiness or workload
health.

## Choose the operation

The following commands browse a local checkout without loading Site values:

```bash
siteops -w workspaces/iot-operations browse
siteops -w workspaces/iot-operations browse aio-install
siteops -w workspaces/iot-operations browse --tag mqtt
```

See [browsing](../../docs/browse-content.md) for filtering, private JSON and the
deployment journey for configured Sites. Each manifest owns its operator guide
and optional descriptive `entry.yaml`. These are not package verification or
evidence of deployment readiness.

| Goal | Manifest | Read first |
|---|---|---|
| Install AIO on a prepared cluster | `manifests/aio-install/manifest.yaml` | [Install guide](manifests/aio-install/README.md) |
| Upgrade an existing AIO installation | `manifests/aio-upgrade/manifest.yaml` | [Upgrade guide](manifests/aio-upgrade/README.md) |
| Apply selected devices, assets, and dataflows | `manifests/aio-resources/manifest.yaml` | [Resource set guide](manifests/aio-resources/README.md) |
| Enable Secret Sync on an existing instance | `manifests/secretsync/manifest.yaml` | [Enablement guide](manifests/secretsync/README.md) |
| Bootstrap or upgrade an AKS Edge Essentials host | `manifests/aksee-bootstrap/manifest.yaml` or `manifests/aksee-upgrade/manifest.yaml` | [Host bootstrap](manifests/aksee-bootstrap/README.md) or [host upgrade](manifests/aksee-upgrade/README.md) |
| Explore a worked deployment | `samples/<name>/manifest.yaml` | [Sample guide](samples/README.md) |

Use `aio-upgrade` to change the AIO version in place. Reapplying
`aio-install` to an existing instance can overwrite instance settings and
child resources that operators manage.

## Inspect, prepare, and deploy

For configured Sites in a local checkout, run from the repository root:

```bash
siteops -w workspaces/iot-operations sites <site> --output yaml
siteops -w workspaces/iot-operations validate manifests/aio-install/manifest.yaml -l name=<site>
siteops -w workspaces/iot-operations plan manifests/aio-install/manifest.yaml -l name=<site>
siteops -w workspaces/iot-operations deploy manifests/aio-install/manifest.yaml -l name=<site>
```

Replace `<site>` before running the commands. `sites` and `validate` read
configuration. `plan` compiles and preflights without Azure or Kubernetes
mutation. `deploy` creates or updates provider resources. See
[plan output](../../docs/plan-output.md) and
[run output](../../docs/run-output.md) for the exact result boundaries.

## Workspace layout

| Directory | Purpose |
|---|---|
| [`sites/`](sites/README.md) | Committed Sites and inherited defaults |
| `sites.local/` | Gitignored local overlays |
| [`manifests/`](manifests/README.md) | Core manifest directories and shared `_partials/` |
| [`contracts/`](contracts/README.md) | Composition identities and reference rules |
| [`parameters/`](parameters/README.md) | Shared defaults, AIO release pins and step wiring |
| [`resource-sets/`](resource-sets/README.md) | Reusable workload definitions that Sites select |
| [`templates/`](templates/README.md) | Bicep and implementation content delivered to hosts |
| [`samples/`](samples/README.md) | Deployable examples with their own prerequisites |

Sites can live below nested directories, but basenames must remain unique
within a trusted Site directory. Workspace labels and fields under
`properties` are conventions of this content. The Site Ops engine does not
assign AIO meaning to names such as `environment`, `aioRelease`, or
`deployOptions`.

The generated `siteops-index.json` presents published descriptions to remote
browsers. `siteops-index.inputs.json` holds separate source freshness
bindings. Follow [index publication](../../docs/remote-content.md#publish-descriptions-from-a-workspace)
when updating those artifacts. Neither file is an execution package.

For the engine and workspace ownership boundary, see the
[repository and workspace guide](../../docs/repository-guide.md).
