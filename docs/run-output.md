# Deployment run output

Site Ops reports what a deployment did as clearly as
[plan output](plan-output.md) reports what it would do. A run can be rendered
for a person or emitted as one structured JSON document for automation.

## Run a deployment

Plain output is the default:

```bash
siteops -w <workspace> deploy <manifest>
```

In a private interactive terminal, the command prepares one executable plan,
displays its Sites and operations, asks for confirmation, then executes
that same plan and prints a final summary with one row per Site. A declined
review submits no operations. `--yes` explicitly confirms unattended, CI or
JSON execution without printing the full private plan or bypassing
validation, source trust or Site prerequisites. Without `--yes`, a
noninteractive invocation exits with argparse usage error 2 before content
or Azure access. For preparation without execution, use `plan`.

Progress lines and logs go to stderr while the run is in flight. For a direct
published source, fixed messages name release resolution, missing content
download, verification and plan preparation phases. While preparation compiles
templates and checks local tools, a line reports elapsed time after 10 seconds
and then every 30 seconds. These lines do not expose a percentage or turn
stderr into publishable output. Waiting operations report elapsed time at most
once per minute, not invented completion progress.
The final summary is the record of what happened, not a prediction.

## Read the summary

A successful run ends like this:

```text
  Deployment summary
  ------------------

  + seattle-dev  succeeded  2/2 ops  44.5s
  + munich-dev   succeeded  2/2 ops  41.0s

  Result: all deployment operations succeeded in 46.1s
  Sites: 2 total, 2 succeeded
  Operations: 4 total, 4 succeeded
```

A successful result means every deployment operation the plan selected to run
completed for its Site. For AIO, [check the result](guided-inputs.md#check-the-result)
on the cluster before adding a workload.

A run that does not succeed names the incomplete work and the next step:

```text
  Deployment summary
  ------------------

  + seattle-dev  succeeded  2/2 ops  44.5s
  x munich-prod  failed     1/3 ops (1 failed, 1 not run)  32.0s

  Result: failed in 76.5s
  Sites: 2 total, 1 succeeded, 1 failed
  Operations: 5 total, 3 succeeded, 1 failed, 1 not run

  Incomplete
  ----------
  x munich-prod
      BadRequest: the dataflow endpoint template was rejected

  Failure diagnostics
  -------------------
  x failed: other, 1 operation
    Inspect the detailed error locally or in the Azure activity log.
    Confirm unknown work before another deployment.

  Next: inspect affected resources, correct the error, and review a new
  plan before deploying again.
```

Rows size themselves to the Sites in the run rather than to a fixed table. A
name longer than the column keeps its own line, so a Site name is never
truncated. Markers are plain ASCII (`+` succeeded, `x` failed, `-` did not
run, `?` unconfirmed), the same set that [plan output](plan-output.md) uses,
and no color or cursor control is emitted. Prose wraps to the terminal width,
up to 100 columns, and at a fixed width in a redirected file or CI log. Site,
step and error text is shown with control characters escaped as `\uXXXX`.

The `Next:` line appears only when there is something to do. Inspect affected
resources before deciding to deploy again. Unconfirmed work takes priority
in this guidance even when another operation failed. A new deployment runs
a fresh plan, not just unfinished operations from this run.

Progress lines during the run go to stderr so stdout stays parseable. A line
about one Site starts with the Site name in brackets and uses the same
markers. A manifest with steps at subscription scope runs in two phases, and
each phase starts with its own line. This example is shortened:

```text
  [Phase 1] Steps at subscription scope: 1 Site
[contoso-global] starting
[contoso-global] > global-edge-site (deployment)...
[contoso-global] + global-edge-site
[contoso-global] + succeeded in 21.3s

  [Phase 2] Steps in resource groups: 2 Sites

  [Parallel] Deploying to 2 Sites (2 concurrent)
[munich-dev] starting
[munich-dev] > schema-registry (deployment)...
[munich-dev] + schema-registry
[munich-dev] + succeeded in 41.0s
```

A manifest without steps at subscription scope starts with
`[Execution] Prepared Sites: N Sites` instead. The `[Parallel]` line
appears when more than one Site deploys at once. Redacted output shows
`<site>` in place of each Site name.

## Read the outcome

Every prepared operation is accounted for, including work that never started.

| Status | Meaning |
|---|---|
| `succeeded` | The operation ran and the provider reported success |
| `failed` | The operation was attempted and failed, locally or in Azure |
| `skipped` | A condition or scope excluded the operation, so nothing was attempted |
| `not-run` | The operation was prepared but never started, such as after an earlier failure |
| `cancelled` | The operation was still queued when the run was asked to stop |
| `unknown` | The operation may have completed, but its final state was not confirmed |

A Site carries the same set of values, aggregated from its operations. A run
adds `invalid`, which means preparation failed and nothing was executed.

`unknown` means an operation's final effect or observation could not be
confirmed. This includes ambiguous submission, lost observation, and a
kubectl apply that may have applied only some resources. For ARM deployments,
the local summary names the deployment so its state can be checked in Azure.
An observed failure can also leave partial resource changes. Neither failure
nor interruption implies rollback.

Exit codes follow the outcome:

| Exit code | Condition |
|---|---|
| `0` | The run succeeded or every operation was skipped |
| `1` | Any operation failed, was left incomplete, was unconfirmed, or preparation was invalid |
| `130` | The run was interrupted, whatever the observed operations achieved |

A run where every operation was skipped is a legitimate success. The summary
says so explicitly rather than implying that work was performed.

## Emit JSON

Choose a compact JSON result suitable for publication:

```bash
siteops -w <workspace> deploy <manifest> --yes --output json --projection publishable
```

For execution results and expected preparation failures, JSON mode writes
one JSON document to stdout. Progress, diagnostics, and logging use stderr,
so a caller can parse stdout directly. Usage errors, such as omitting the
Manifest argument, are reported on stderr without a result document.
An expected lookup or content error after the command's content context is
admitted produces an invalid result instead.

```json
{
  "apiVersion": "siteops/v1alpha1",
  "diagnostics": [],
  "engine": {"name": "siteops", "version": "1.0.0b1"},
  "exitCode": 0,
  "kind": "DeploymentRun",
  "projection": "publishable",
  "status": "succeeded",
  "summary": {
    "interrupted": false,
    "operations": {
      "counts": {"succeeded": 3, "failed": 0, "skipped": 1, "not-run": 0, "cancelled": 0, "unknown": 0},
      "total": 4,
      "failureDiagnostics": []
    },
    "sites": {
      "counts": {"succeeded": 2, "failed": 0, "skipped": 0, "not-run": 0, "cancelled": 0, "unknown": 0},
      "total": 2
    }
  }
}
```

Each `counts` object carries every status, including zero entries.
`engine.version` identifies the installed Site Ops build.

The `siteops/v1alpha1` wire contract is preview. Consumers should reject an
unsupported `apiVersion`, `kind`, or `projection`, and should treat
`summary.interrupted` as the only reason an otherwise successful run reports
exit code 130.

Each run generates a fresh identifier. It appears in `local-private` JSON for
correlating one execution and is not published, so public automation should
not depend on it.

## Read failure diagnostics

Each failed or unknown logical operation has one failure category. The category
explains the outcome but does not change its status, retries or exit code.

| Category | Meaning | Admitted structured Azure code |
|---|---|---|
| `subscription-not-visible` | The account cannot see the subscription | `SubscriptionNotFound` |
| `authorization-failed` | The identity lacks access for the operation | `AuthorizationFailed` |
| `template-invalid` | The template or its parameters were rejected | `InvalidTemplate` |
| `tool-unavailable` | A required local tool is unavailable | None |
| `timeout` | Local execution or observation reached its deadline | None |
| `other` | The cause is unrecognized or completion cannot be established | None |

Azure codes are admitted only from supported structured error fields, with
exact case. Text alone can explain a category but cannot supply an Azure code.
`QuotaExceeded` remains `other` without a code. A generic `DeploymentFailed`
wrapper is not published. Repeated matching ARM causes reduce to one
category and code. Mixed, unrecognized or malformed causes use `other`.
See the [diagnostic index](diagnostics.md) for fixed guidance.

Both projections add `summary.operations.failureDiagnostics` beside `counts`
and `total`, never inside the counters. Groups are keyed by status, category
and optional Azure code, sorted with failed before unknown. Each failed or
unknown operation contributes once. Provider attempts, Sites and nested ARM
resource operations are not the unit of counting.

For example, this operations summary records one failed and one unknown
operation:

```json
{
  "counts": {"succeeded": 0, "failed": 1, "skipped": 0, "not-run": 0, "cancelled": 0, "unknown": 1},
  "total": 2,
  "failureDiagnostics": [
    {
      "status": "failed",
      "category": "subscription-not-visible",
      "azureCode": "SubscriptionNotFound",
      "count": 1,
      "summary": "The target subscription is not visible to the current Azure account.",
      "guidance": "Check the subscriptions visible to the current account and use an identity with access to the target subscription.",
      "documentation": "https://github.com/Azure/digital-ops-scale-kit/blob/main/docs/diagnostics.md#providersubscription-not-visible"
    },
    {
      "status": "unknown",
      "category": "timeout",
      "count": 1,
      "summary": "The local execution or observation deadline was reached.",
      "guidance": "Confirm the provider state before another deployment. A local deadline does not establish whether cloud work completed.",
      "documentation": "https://github.com/Azure/digital-ops-scale-kit/blob/main/docs/diagnostics.md#providertimeout"
    }
  ]
}
```

Group counts sum to `counts.failed` and `counts.unknown` separately.
No such work produces an empty array. Skipped, cancelled, not-run and succeeded
operations have no failure category. Invalid preparation has result diagnostics,
not a counted provider rejection.

Content diagnostics remain one JSON record per occurrence. Human output groups
identical public records as `N occurrences`, not `N operations`.
`summary.diagnosticCount`, when present, still counts serialized records.
Each private context remains attached to its own record.

## Read warnings

A warning reports something to correct that does not stop the command, such
as a Manifest that still uses `siteSelector`. Warnings print on stderr while
the command runs, with local detail such as the file path.

With `--output json`, `validate`, `plan` and `deploy` also add each registered
warning to the result's `diagnostics` array, after the result's own
diagnostics:

```json
{
  "code": "deprecation.manifest-site-selector",
  "severity": "warning",
  "summary": "A Manifest uses the deprecated siteSelector field.",
  "guidance": "Rename siteSelector to selector on the Manifest. Both spellings select the same Sites.\nRun `siteops -w <workspace> validate <manifest>` locally to see the Manifest file on stderr.",
  "documentation": "https://github.com/Azure/digital-ops-scale-kit/blob/main/docs/diagnostics.md#deprecationmanifest-site-selector",
  "subject": "manifest",
  "occurrences": 2
}
```

- Codes that start with `deprecation.` name a spelling that still works and
  has a replacement. Other warning codes start with `warning.`. The
  [diagnostic index](diagnostics.md#warnings-and-deprecations) lists each one.
- A warning reported more than once appears as one record. `occurrences`
  counts the reports and is present only when there was more than one.
- Records keep the order in which each warning was first reported.
- `summary.diagnosticCount`, when present, includes warning records.
- Summary, guidance and documentation come from the engine registry, so both
  projections carry the same warning records without Site values or paths.
- A result with warnings keeps its `status`, `exitCode` and process exit code.
  A valid validation, a planned result and a successful run stay successful.

Plain output keeps warnings on stderr only. The GitHub Actions and Azure
Pipelines templates show warning records as warning annotations and summary
lines with their occurrence count, and the task still succeeds.

A warning reaches JSON only through a result document. When a command stops
before it writes one, such as on a usage error, a refused projection or a
failure to admit content, its warnings stay on stderr. A warning reported after
the document is written, such as a cleanup failure as the command exits, also
stays on stderr. Other commands, including `browse`, `sites` and `source`,
report warnings on stderr only, even when they write JSON.

## Choose a projection

Two projections are available, matching the plan surface:

| Projection | Intended destination | Detail |
|---|---|---|
| `local-private` | An authorized local terminal or private file | Site and step identities, per operation status, timing, deployment names, typed reasons and failure metadata |
| `publishable` | CI logs, summaries, artifacts, and reports | Aggregate counts, run status, exit code, typed diagnostics and failure groups |

```bash
siteops -w <workspace> deploy <manifest> --yes --output json \
  --projection local-private
```

`SITEOPS_REDACT_OUTPUT` controls redaction explicitly. Otherwise
`GITHUB_ACTIONS` or `TF_BUILD` enables it and defaults JSON to `publishable`.
Site Ops rejects an explicit `local-private` projection while redaction is
enabled.

The publishable projection is built from an allowlist rather than by redacting
the local document. It omits Site names, step names, deployment names, paths,
provider outputs, and raw error text. Diagnostic summary, guidance and
documentation come from the engine registry, not a producer's message.
Documentation links always point to the fixed main branch
[diagnostic index](diagnostics.md). An unrecognized provider cause uses `other`.

The `local-private` projection carries identities, typed reasons and an
operation's failure category with optional admitted code and registry text.
Neither JSON projection serializes raw provider messages, outputs or parameter
values. Authorized local plain output retains detailed errors.

Redacted plain output renders the same allowlisted fields as the publishable
JSON. Local plain output keeps the detailed view, including private reason
text and unconfirmed deployment names.

## Interrupt a run

During preparation or interactive review, Ctrl+C cancels before deployment
submission and exits `130`. Local tool cleanup may delay the return.
Declining the confirmation also exits `130`, without submitting operations
or producing a completed deployment result.

During execution, Ctrl+C records a stop request, prints what to expect,
stops new work and wakes polling loops and
backoff sleeps. A call already running in a child process is not interrupted.
It returns on its own or reaches its existing timeout first. The current
bounds are 60 seconds for one deployment state read, 5 minutes for a
deployment submission, and 10 minutes for a kubectl operation. These bounds
apply to each call, not to the whole command.

Pressing Ctrl+C again repeats that expectation. It does not force an exit,
because workers still hold temporary files and outcomes already observed would
be lost.

Stopping locally does not cancel a deployment Azure already accepted.
Work whose outcome was not observed is reported as
`unknown`, with the deployment name in local output.

An interrupted execution prints its final result and exits `130`, with
`summary.interrupted` set to `true`.

## Preparation failures

An expected validation, targeting, capability, compilation, or composition
failure ends the run before execution. In JSON mode this produces a
`DeploymentRun` document with `status: invalid`, any operation that was
already prepared recorded as `not-run`, and a nonzero exit code. Plain mode
prints the same failure as the equivalent `plan` command.

An unexpected error outside execution produces no synthetic result document.
If a Site fails unexpectedly during execution, the result retains earlier
observations, identifies the incomplete work, and exits nonzero.

## Temporary files

A run writes resolved parameter files and other transient inputs outside the
workspace, under the operating system temporary directory. Directories and
files are created with owner only permissions on POSIX. On Windows, protection
follows the selected parent's inherited ACLs. Choose a parent whose access is
restricted to the intended account. Workspace content is never used as
scratch space.

Site Ops attempts to remove parameter files when they are no longer needed
and the remaining deployment scratch after workers finish. A removal failure
is logged as a warning and may leave files behind, without replacing the
deployment outcome. JSON results record it as
[`warning.cleanup-incomplete`](diagnostics.md#warningcleanup-incomplete).

Set `SITEOPS_TEMP_DIR` to an absolute path to select a different parent, for
example a runner local disk. An unset variable uses the platform default. A
value that is empty or relative is an error rather than a silent fallback.

## Publish from CI

Capture the explicit publishable JSON from stdout and keep stderr as a separate
private stream. The shipped GitHub Actions and Azure Pipelines templates do
this, validate the envelope before publishing anything, and preserve the
process exit code. See [ci-cd-setup.md](ci-cd-setup.md).

## Validate a Manifest

Check syntax, files and static references without compiling templates or
reading Azure resources:

```bash
siteops -w <workspace> validate <manifest> --output json --projection publishable
```

Validation writes one `ValidationResult` to stdout. Plain output remains the
default. A reusable Manifest without requested targeting is valid and reports
`targetResolution: not-requested`:

```json
{
  "apiVersion": "siteops/v1alpha1",
  "kind": "ValidationResult",
  "projection": "publishable",
  "engine": {"name": "siteops", "version": "1.0.0b1"},
  "status": "valid",
  "exitCode": 0,
  "summary": {"diagnosticCount": 0, "targetResolution": "not-requested"},
  "diagnostics": []
}
```

Completed validation exits `0` for valid content and `1` for invalid content.
Warnings alone do not invalidate it. `summary.diagnosticCount` counts records
of all severities. Requested targeting reports `complete` or `incomplete`.
Expected lookup and content failures after content admission produce an invalid
document with diagnostic codes and registry guidance.

JSON defaults to `local-private` for a private destination and `publishable`
when redaction is enabled. Explicit `publishable` is also allowed locally.
`local-private` adds recorded source context but no raw errors or source
excerpts. Privacy and option refusals exit `1` before content access, with
stderr only. `--projection` requires `--output json`, and private JSON is
refused while redaction is enabled.

Usage errors exit `2` without a result. Failures before content admission,
unexpected internal errors and interruption do not fabricate a completed
`ValidationResult`. Interruption exits `130`. JSON stdout is separate from
stderr logging and plain error guidance.
