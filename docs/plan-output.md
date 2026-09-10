# Deployment plan output

Site Ops can render a deployment plan for a person or emit one structured JSON
document for automation.

See [run-output.md](run-output.md) for what a completed deployment reports.

## Prepare an executable plan

Plain output is the default:

```bash
siteops -w <workspace> plan <manifest>
```

This command runs structural validation, resolves the selected operations,
compiles executable templates, preflights required capabilities, and prints
the canonical plan. It performs no Azure or Kubernetes mutation.

Deployment still submits the source Bicep, which Azure CLI may compile again.
The plan records observed compilation identity, not a guarantee that ARM will
receive those exact compiled bytes. Planning does not establish Azure
authorization, cluster connectivity, or workload health.

Executable preparation may acquire the Bicep compiler or restore modules.
It is not an offline mode. Private module sources need their required
credentials available during preparation.

The engine validates the same loaded inputs for both `plan` and `deploy`,
including direct Python API calls. Structural failures stop preparation
before local tool preflight. Successful template acquisitions remain visible
when a later schema-dependent check blocks an operation.

Use `--describe` for the faster compile-free shape:

```bash
siteops -w <workspace> plan <manifest> --describe
```

The existing `validate --plan` spelling remains a compatibility route to the
describe view. `deploy --dry-run` remains a compatibility route to executable
planning and stops after rendering the plan.

A library manifest without a target set can be checked with `validate`.
Pass a selector to plan that library against specific sites.

## Emit JSON

Choose JSON output:

```bash
siteops -w <workspace> plan <manifest> --output json
```

JSON mode writes exactly one JSON document to stdout. Human guidance and
logging use stderr so a caller can parse stdout directly.

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
| `local-private` | An authorized local terminal or private file | Target, operation, path, condition, composition, and deferred-reference detail |
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

- site names and selectors
- tenant, subscription, resource group, and location
- labels and resource identities
- parameter names and values
- paths and URLs
- conditions and deferred expressions
- provenance and template identity
- raw provider, compiler, and tool errors

It is constructed from an allowlist rather than by redacting the local-private
document.

When redaction is enabled, plain plans render the same allowlisted fields as
the publishable JSON projection. They show status, intent, aggregate activity,
and generic diagnostics rather than manifest names, descriptions, individual
steps, paths, conditions, or target details. Authorized local plain output
retains its detailed view when redaction is disabled.

For CI publication, capture the explicit publishable JSON from stdout.
Progress and diagnostic logs on stderr are a separate stream, not part of the
publication projection. Do not combine the two streams into a plan artifact.

## Parameter values

Structured plan output never serializes parameter values.

The local-private projection can list a parameter name, whether its value is
known or deferred, and the prior-operation outputs it reads. Each descriptor
contains `serialized: false`. The resolved value remains only in the private
in-memory executable plan.

## Invalid plans

An expected validation, targeting, capability, compilation, or composition
failure can produce a typed JSON envelope with `status: invalid` and a nonzero
exit code. The `intent` field distinguishes executable preparation from a
describe request. Publishable diagnostics contain generic categories.
Local-private diagnostics include detail only when the producer supplies a
separate value-free message.

An unexpected internal failure writes no plan document to stdout.
