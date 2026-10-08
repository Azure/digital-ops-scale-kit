# Deployment plan output

Site Ops can render a deployment plan for a person or emit one structured JSON
document for automation.

See [run-output.md](run-output.md) for what a completed deployment reports.
Private plain plans identify each Site's subscription and resource group,
or its subscription scope. Review those destinations before deployment.
Publishable output omits their identities.

## Prepare an executable plan

Plain output is the default:

```bash
siteops -w <workspace> plan <manifest>
```

This command runs structural validation, resolves the selected operations,
compiles executable templates, preflights required capabilities, and prints
the canonical plan. It performs no Azure or Kubernetes mutation.
An explicit resource ID answer authorizes bounded Azure reads of the
declared resource and related resources while resolving the Site. The
planner itself reads no Azure resources, so without such an answer,
planning makes no Azure reads. `validate` never reads them. `inputs`, which
only inspects, requires `--read-resources` to preview or save a Site from an ID.

Ordinary local workspaces submit source Bicep, which Azure CLI may compile
again. Their plans record observed compilation identity, not a guarantee that
ARM will receive those exact compiled bytes.

An internal binding to a materialized package uses the ARM JSON mapped by its
producer instead. Its `local-private` plan records `submission.mode: arm-json`,
`compilationBinding: package-artifact`, the authored `templatePath`, and a
different `effectiveTemplatePath` when Bicep maps to generated JSON. Package
integrity is checked again before execution. This binding is not a saved plan
or a decision about publisher provenance. Plain output identifies this mode as
`Submission: arm-json (package artifact)`.

`plan` and `deploy` work with local content, pinned project packages, or an
explicit published release selected by `--source SOURCE@RELEASE`.
Direct source use resolves the release online and verifies it with
trust that the consumer owns, reusing valid cached bytes when available. It does
not change a project pin. Without a project, provide explicit Site inputs,
not packaged example Sites. With `--source`, global `-w` selects a relative
workspace path inside that release.

Planning does not establish deployment authorization, cluster connectivity,
or workload health. A successful declared resource read proves only that the
selected identity could read that resource when the command ran.

Executable preparation may acquire the Bicep compiler or restore modules.
It is not an offline mode. Private module sources need their required
credentials available during preparation.

The engine validates the same loaded inputs for both `plan` and `deploy`,
including direct Python API calls. Structural failures stop preparation
before local tool preflight. Successful template acquisitions remain visible
when a later check that depends on a schema blocks an operation.

Use `--describe` to see the plan shape faster, without compiling:

```bash
siteops -w <workspace> plan <manifest> --describe
```

Use `plan --describe` for the shape without compiling, or `plan` for
executable preparation without deployment. Bare `validate` remains a
structural check for library manifests that select no Sites. It does not
accept the `--output` and `--projection` options of `plan`. The [migration guide](migrating.md) lists replaced preview
options.

A library manifest that selects no Sites can be checked with `validate`.
Pass a selector to plan that library against specific Sites.

## Read a plain plan

A plain plan marks each authored step for the selected Sites. This example
comes from `siteops -w workspaces/iot-operations plan aio-install --describe`
and is shortened:

```text
  Deployment plan: aio-install
  ----------------------------

  Install Azure IoT Operations on an existing cluster connected to Azure
  Arc: the AIO extensions, instance, custom location, Schema Registry
  and Device Registry namespace. Set `enableSecretSync` to true to also
  enable Secret Sync.

  Preflight: not performed
  Templates and deployment capabilities were not checked.

  Sites (2):
    munich-dev (germanywestcentral)
      Subscription: 00000000-0000-0000-0000-000000000000
      Resource group: rg-iot-munich-dev
    seattle-dev (westus)
      Subscription: 00000000-0000-0000-0000-000000000000
      Resource group: rg-iot-seattle-dev

  Steps (9):
    - 1. global-edge-site (subscription): skipped for all 2 Sites
         templates/edge-site/subscription.bicep
         Reason: Runs at subscription scope. This Site has a resource group.
    + 3. schema-registry (resourceGroup)
         templates/deps/schema-registry.bicep

  Operations: 18 total, 10 to run, 8 skipped
  Execution: up to 3 Sites at once
```

`+` marks a step that runs, `-` a step that is skipped and `x` a step that is
blocked. When several Sites are selected, a step that does not run for every
Site says how many skip it, for example `skipped for 2 of 3 Sites`. A
`Reason:` line says why, such as a `when:` condition that is false for that
Site (`Condition not met: ...`). `Operations:` counts one operation per
selected Site and step, and the redacted plain plan reports the same counts.

The plan shows the first paragraph of the manifest description, and
`siteops browse NAME` shows the authored guidance. When more than one Site is
selected, an `Execution:` line shows how many Sites deploy at once:
`one Site at a time`, `all Sites at once` or `up to N Sites at once`.
A `Selector:` line follows the heading when you pass `-l`.

A plan without `--describe` replaces the `Preflight:` lines with `Status:`,
`Executable:` and `Submission:` lines. Problems found while planning are
listed under `Diagnostics:`, each labeled `Error:` or `Warning:`. When no plan
can be prepared, the output is `Deployment plan is unavailable.` followed by
those labeled lines.

Before the plan, stderr names the manifest by its path in the workspace, for
example `Manifest: manifests/aio-install/manifest.yaml`, and for verified
content names its source in one `Source:` line. Executable preparation prints
`Preparing executable deployment plan...` and then reports elapsed time while
compilation and tool checks continue.

Plain output uses the same ASCII markers as [run output](run-output.md) and no
color. Text from manifests and Site files is shown with control characters
escaped as `\uXXXX`. Prose wraps to the terminal width, up to 100 columns, and
at a fixed width when output is redirected.

## Emit JSON

Choose JSON output:

```bash
siteops -w <workspace> plan <manifest> --output json
```

JSON mode writes exactly one JSON document to stdout. Human guidance and
logging use stderr so a caller can parse stdout directly.
In a private terminal, `deploy` reviews and confirms its own fresh plan.
A prior plan output does not authorize it. A noninteractive, CI or JSON
deploy requires `--yes` before content access.

Every document identifies its contract and projection:

```json
{
  "apiVersion": "siteops/v1alpha1",
  "kind": "DeploymentPlan",
  "projection": "local-private",
  "status": "planned"
}
```

The `siteops/v1alpha1` wire contract is preview. Consumers should reject an
unsupported `apiVersion`, `kind`, or `projection`. Do not hash this preview
JSON and treat it as an exact execution identity.

## Choose a projection

Two projections are available:

| Projection | Intended destination | Detail |
|---|---|---|
| `local-private` | An authorized local terminal or private file | Site, operation, path, condition, composition, and deferred reference detail |
| `publishable` | CI logs, summaries, artifacts, and reports | Aggregate counts and generic typed diagnostics |

Choose one explicitly when needed:

```bash
siteops -w <workspace> plan <manifest> --output json \
  --projection publishable
```

Supported true or false values for `SITEOPS_REDACT_OUTPUT` control redaction
explicitly. Otherwise, `GITHUB_ACTIONS` or `TF_BUILD` enables redaction and
defaults JSON to `publishable`. Site Ops rejects an explicit `local-private`
projection while redaction is enabled.

The publishable projection omits:

- Site names and selectors
- tenant, subscription, resource group, and location
- labels and resource identities
- parameter names and values
- paths and URLs
- conditions and deferred expressions
- provenance and template identity
- raw provider, compiler, and tool errors

It is constructed from an allowlist rather than by redacting the
`local-private` document.

When redaction is enabled, plain plans render the same allowlisted fields as
the publishable JSON projection. They show status, intent, aggregate activity,
and generic diagnostics rather than manifest names, descriptions, individual
steps, paths, conditions, or Site details. Authorized local plain output
retains its detailed view when redaction is disabled.

For CI publication, capture the explicit publishable JSON from stdout.
Progress and diagnostic logs on stderr are a separate stream, not part of the
publication projection. Do not combine the two streams into a plan artifact.

## Parameter values

Structured plan output never serializes parameter values.

The `local-private` projection can list a parameter name, whether its value is
known or deferred, and the outputs of prior operations that it reads. Each descriptor
contains `serialized: false`. The resolved value remains only in the private
executable plan held in memory.

## Invalid plans

An expected validation, targeting, capability, compilation, or composition
failure can produce a typed JSON envelope with `status: invalid` and a nonzero
exit code. The `intent` field distinguishes executable preparation from a
describe request. Publishable diagnostics contain generic categories.
`local-private` diagnostics include detail only when the producer supplies a
separate message that contains no values.

An unexpected internal failure writes no plan document to stdout.
