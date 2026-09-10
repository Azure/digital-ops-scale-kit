# Deployment run output

Site Ops reports what a deployment did as clearly as
[plan output](plan-output.md) reports what it would do. A run can be rendered
for a person or emitted as one structured JSON document for automation.

## Run a deployment

Plain output is the default:

```bash
siteops -w <workspace> deploy <manifest>
```

The command prepares an executable plan, runs it, and prints a final summary
with one row per site. Progress lines and logs go to stderr while the run is in
flight. The final summary is the record of what happened, not a prediction.

`deploy --dry-run` remains a compatibility route to executable planning. It
prints a plan and executes nothing.

## Read the summary

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

  Next: inspect affected resources, correct the error, and review a new
  plan before deploying again.
```

Rows size themselves to the sites in the run rather than to a fixed table. A
name longer than the column keeps its own line, so a target identity is never
truncated. Markers are plain ASCII (`+` succeeded, `x` failed, `-` did not
run, `?` unconfirmed) and no color or cursor control is emitted, so the
summary reads the same in a terminal, a redirected file, and a CI log.

The `Next:` line appears only when there is something to do. Inspect affected
resources before deciding to deploy again. Unconfirmed work takes priority
in this guidance even when another operation failed. A new deployment runs
a fresh plan, not just unfinished operations from this run.

Progress lines during the run use the same markers and the `[site]` prefix,
and they go to stderr so stdout stays parseable.

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

A site carries the same set of values, aggregated from its operations. A run
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
siteops -w <workspace> deploy <manifest> --output json --projection publishable
```

For execution results and expected preparation failures, JSON mode writes
one JSON document to stdout. Progress, diagnostics, and logging use stderr,
so a caller can parse stdout directly. Argument errors, such as a missing
manifest, are reported on stderr without a result document.

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
    "operations": {"counts": {"skipped": 1, "succeeded": 3}, "total": 4},
    "sites": {"counts": {"succeeded": 2}, "total": 2}
  }
}
```

The counts above are abbreviated. Each `counts` object carries every status
value, including the zero entries. `engine.version` identifies the installed
Site Ops build.

The `siteops/v1alpha1` wire contract is preview. Consumers should reject an
unsupported `apiVersion`, `kind`, or `projection`, and should treat
`summary.interrupted` as the only reason an otherwise successful run reports
exit code 130.

Each run generates a fresh identifier. It appears in local-private JSON for
correlating one execution and is not published, so public automation should
not depend on it.

## Choose a projection

Two projections are available, matching the plan surface:

| Projection | Intended destination | Detail |
|---|---|---|
| `local-private` | An authorized local terminal or private file | Site and step identities, per operation status, timing, deployment names, typed reasons |
| `publishable` | CI logs, summaries, artifacts, and reports | Aggregate counts, run status, exit code, generic typed diagnostics |

```bash
siteops -w <workspace> deploy <manifest> --output json \
  --projection local-private
```

`SITEOPS_REDACT_OUTPUT` controls redaction explicitly. Otherwise
`GITHUB_ACTIONS` or `TF_BUILD` enables it and defaults JSON to `publishable`.
Site Ops rejects an explicit `local-private` projection while redaction is
enabled.

The publishable projection is built from an allowlist rather than by redacting
the local document. It omits site names, step names, deployment names, paths,
provider outputs, and raw error text. Diagnostic codes and summaries are fixed
category text, so a diagnostic written by a producer is never published
verbatim. An unrecognized category publishes a generic entry.

The local-private projection carries identities and typed reasons. It never
serializes operation outputs or raw provider errors. A reason or diagnostic
adds detail only when its producer supplied a separate value free message.

Redacted plain output renders the same allowlisted fields as the publishable
JSON. Local plain output keeps the detailed view, including private reason
text and unconfirmed deployment names.

## Interrupt a run

Ctrl-C records a stop request and prints what to expect.

During preparation, the request lets preparation finish, including any
remaining template compilations. If preparation succeeds, no deployment
operation starts. If preparation fails, the command reports that failure.

During execution, the request stops new work and wakes polling loops and
backoff sleeps. A call already running in a child process is not interrupted.
It returns on its own or reaches its existing timeout first. The current
bounds are 60 seconds for one deployment state read, 5 minutes for a
deployment submission, and 10 minutes for a kubectl operation. These are
per-call bounds, not a time limit for the whole command.

Pressing Ctrl-C again repeats that expectation. It does not force an exit,
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
If a target fails unexpectedly during execution, the result retains earlier
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
deployment outcome.

Set `SITEOPS_TEMP_DIR` to an absolute path to select a different parent, for
example a runner local disk. An unset variable uses the platform default. A
value that is empty or relative is an error rather than a silent fallback.

## Publish from CI

Capture the explicit publishable JSON from stdout and keep stderr as a separate
private stream. The shipped GitHub Actions and Azure Pipelines templates do
this, validate the envelope before publishing anything, and preserve the
process exit code. See [ci-cd-setup.md](ci-cd-setup.md).
