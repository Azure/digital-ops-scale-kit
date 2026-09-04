# Deployment plan output

Site Ops can render a deployment plan for a person or emit one structured JSON
document for automation.

## Show the plain plan

Plain output is the default:

```bash
siteops -w <workspace> validate <manifest> --plan
```

The plan shows selected sites, manifest steps, resource composition, and
aggregate operation counts. It does not deploy resources.

`deploy --dry-run` prepares the executable plan once and prints its plain
view:

```bash
siteops -w <workspace> deploy <manifest> --dry-run
```

Add `-v` to also print the exact command each step would run.

## Emit JSON

Request JSON together with `--plan`:

```bash
siteops -w <workspace> validate <manifest> --plan --output json
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
siteops -w <workspace> validate <manifest> --plan --output json \
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

## Parameter values

Structured plan output never serializes parameter values.

The local-private projection can list a parameter name, whether its value is
known or deferred, and the prior-operation outputs it reads. Each descriptor
contains `serialized: false`. The resolved value remains only in the private
in-memory executable plan.

## Invalid plans

An expected validation, targeting, or composition failure can produce a typed
JSON envelope with `status: invalid` and a nonzero exit code. Publishable
diagnostics contain generic categories. Local-private diagnostics include
detail only when the producer supplies a separate value-free message.

An unexpected internal failure writes no plan document to stdout.
