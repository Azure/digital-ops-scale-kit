# IoT Operations workspace

This workspace applies the generic Site Ops engine to
[Azure IoT Operations](https://learn.microsoft.com/azure/iot-operations/).
It contains executable manifests, Bicep templates, release pins, site
conventions, reusable resource definitions, and samples.

Installing Site Ops does not install this workspace. The workspace is
currently obtained from a Scale Kit repository checkout and selected with
`-w workspaces/iot-operations`.

## Start with one target

Follow the [one-site quickstart](../../docs/getting-started.md). It prepares
a `sites.local/` overlay for one existing Arc-connected cluster, inspects the
resolved target, validates the manifest, reviews an executable plan, and only
then deploys.

Workspace content runs with the identity supplied to Site Ops. Review the
selected manifest and templates before use. AIO deployments can create role
assignments, update existing resources, and incur Azure charges. A successful
resource deployment does not by itself establish AIO readiness or workload
health.

## Choose the operation

| Goal | Entry point | Read first |
|---|---|---|
| Install AIO on a prepared cluster | `manifests/aio-install.yaml` | [Quickstart](../../docs/getting-started.md) |
| Upgrade an existing AIO installation | `manifests/aio-upgrade.yaml` | [AIO releases](../../docs/aio-releases.md) |
| Apply selected devices, assets, and dataflows | `manifests/aio-resources.yaml` | [Resource catalog](../../docs/resource-catalog.md) |
| Enable Secret Sync on an existing instance | `manifests/secretsync.yaml` | [Secret Sync](../../docs/secret-sync.md) |
| Bootstrap or upgrade an AKS Edge Essentials host | `manifests/aksee-bootstrap.yaml` or `manifests/aksee-upgrade.yaml` | [Host bootstrap](templates/host-bootstrap/README.md) or [host operations](templates/host-ops/README.md) |
| Explore a worked deployment | `samples/<name>/manifest.yaml` | [Sample guide](samples/README.md) |

Use `aio-upgrade.yaml` for in-place AIO version changes. Reapplying
`aio-install.yaml` to an existing instance can overwrite operator-managed
instance settings and child resources.

## Inspect, prepare, and deploy

Run commands from the repository root:

```bash
siteops -w workspaces/iot-operations sites <site> --output yaml
siteops -w workspaces/iot-operations validate manifests/aio-install.yaml -l name=<site>
siteops -w workspaces/iot-operations plan manifests/aio-install.yaml -l name=<site>
siteops -w workspaces/iot-operations deploy manifests/aio-install.yaml -l name=<site>
```

Replace `<site>` before running the commands. `sites` and `validate` read
configuration. `plan` compiles and preflights without Azure or Kubernetes
mutation. `deploy` creates or updates provider resources. See
[plan output](../../docs/plan-output.md) and
[run output](../../docs/run-output.md) for the exact result boundaries.

## Workspace layout

| Directory | Purpose |
|---|---|
| `sites/` | Committed deployment targets and inherited defaults |
| `sites.local/` | Gitignored local overlays |
| `manifests/` | Standalone operations and reusable partials |
| `contracts/` | Composition identities and reference rules |
| `parameters/` | Shared inputs, release pins, chaining, and resource sets |
| `templates/` | Bicep and target-delivered implementation content |
| `samples/` | Deployable examples with their own prerequisites |

Sites can live below nested directories, but basenames must remain unique
within a trusted site directory. Workspace labels and fields under
`properties` are conventions of this content. The Site Ops engine does not
assign AIO meaning to names such as `environment`, `aioRelease`, or
`deployOptions`.

For the engine and workspace ownership boundary, see the
[repository and workspace guide](../../docs/repository-guide.md).
