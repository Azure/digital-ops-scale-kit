# Site Ops diagnostics

Use a diagnostic code to identify the next action without publishing Site values,
paths or provider messages. Validate the same Manifest locally for file and line
details:

```bash
siteops -w <workspace> validate <manifest>
```

In CI, request `--output json --projection publishable`. Validation emits a
`ValidationResult`. Planning and deployment retain their own document kinds and
published codes. Each diagnostic's summary, guidance and documentation link comes
from the engine registry, not an exception message.

Diagnostic codes are permanent anchors in this main branch index. A retired
code keeps its section. A remedy that depends on the engine version names the
version it applies to.

Identical public diagnostics are grouped for display with an occurrence count.
JSON retains each record. An occurrence count is not a deployment operation count.
Private output retains the context for each occurrence.
Both JSON projections use registry text and omit exception messages. Authorized
local plain output retains detailed errors.

## content.duplicate-key

Merge repeated YAML or JSON mapping entries, or rename one. The public diagnostic
does not name the key. See [duplicate keys](migrating.md#every-file-siteops-reads).

## content.unknown-field

Use a supported field in its correct container. A Site's operator metadata belongs
in `labels` or `properties`. That advice does not apply to Manifest fields.
Unknown authored names are not published.
See [Site fields](migrating.md#site-files).

## content.site-selector-misplaced

Neither `selector` nor `siteSelector` belongs in a Site. Move targeting to
`selector` on the Manifest, or pass `-l` to the command.
`siteSelector` was renamed on Manifests in v1.0.0b4 and remains an accepted
deprecated Manifest alias. Renaming a Site field alone does not repair it.

## content.invalid-type

Use the type required by the configuration schema. Quote labels and other fields
that must be text. See [Site fields](migrating.md#site-files).

## content.required-value-empty

Give the required field a value. Remove an empty Site scalar when its value
should be inherited. An explicit empty scalar overrides the inherited value.

## content.required-field-missing

Supply the required field, or inherit it where supported.
See [preparation checks](migrating.md#preparation-checks).

## content.field-placement-invalid

Move the field to its supported container. Use one Site shape and keep `inherits`
at the top level. Composition metadata and governed resource definitions belong
in Manifest parameter sources.

## content.legacy-resource-selection

Use an ordered list of resource set names. Omit a selection for none, or use `[]`
to clear an inherited selection.
See [resource sets](migrating.md#resource-sets).

## content.condition-invalid

Use a supported `when` expression. Omit `when` or use `null` for an unconditional
operation. See [empty conditions](migrating.md#empty-conditions).

## content.path-invalid

Keep dynamic paths within the workspace. Use HTTPS where remote inputs are
permitted. See [parameter file selection](migrating.md#parameter-file-selection).

## content.file-missing

Restore the required file or correct its reference.
See [preparation checks](migrating.md#preparation-checks).

## content.syntax-invalid

Correct the document syntax and encoding. Local validation reports the parser
detail. Public output never includes a source excerpt.

## content.file-unreadable

Check local file access and encoding, then validate again.

## content.schema-unsupported

Use the supported document `apiVersion` and `kind`. A `SiteTemplate` is an
inheritance parent, not a directly deployable Site.

## content.other

Inspect the detailed error locally, correct the content, and validate again.
This is the safe fallback for an expected content rejection without a more
specific registered cause. It does not turn an unexpected programming error into
a successful validation.

## Warnings and deprecations

A warning names something to correct that does not stop the command. Codes
that start with `deprecation.` name a spelling that still works and has a
replacement. Other warning codes start with `warning.`. A result with warnings
keeps its status and exit code. See [warnings in results](run-output.md#read-warnings).

## deprecation.manifest-site-selector

Rename `siteSelector` to `selector` on the Manifest. Both spellings select the
same Sites. A Manifest that declares both is rejected. Run `validate` locally
to see the Manifest file in the command log on stderr.

## warning.manifest-site-repeated

Remove the repeated name from the Manifest's `sites` list. Site Ops deploys each
Site once. Run `validate` locally to see the Manifest and Site names in the
command log on stderr.

## warning.cleanup-incomplete

Site Ops could not remove some of its temporary or staging files. The command
outcome is unchanged. The leftovers are usually in the Site Ops temporary
directory, which is `SITEOPS_TEMP_DIR` or the system default as
[temporary files](run-output.md#temporary-files) describes, or in the
`staging` folder of the [Site Ops cache root](cache.md). A local run names
each leftover on stderr by its folder and name, such as
`.../staging/transfer-<id>`, without the rest of the path. When a removal
failed, the reason follows, such as `Access is denied`. The GitHub
Actions and Azure Pipelines templates keep stderr private, so their
summaries show the guidance that names these folders instead.

When output redaction is on, the log names a folder only when Site Ops
created that folder itself, such as the cache's `staging` folder or an engine
scratch directory. Other leftovers appear by their own names, such as
`.../.siteops-cache-<id>` next to the configured cache directory or
`.../siteops-run-<id>` in the temporary directory. The log withholds the
location of a file that Site Ops was writing under a name you chose, such as
a `siteops inputs` output file or an approved source. Look in the folder that
the command was writing to. The reason stays in the log either way. For the
folder that redaction withholds, run the same command locally with
`SITEOPS_REDACT_OUTPUT=0` and keep that output private. Remove leftovers
when no Site Ops command is running, because temporary deployment files can
hold resolved parameter values.

## Existing preparation diagnostics

Existing `plan.*`, `run.*`, `site.invalid`, `inputs.invalid`,
`inputs.resource.*` and lookup codes retain their meanings and spellings.
Their text also comes from the registry. Use `siteops inputs` for typed input
requirements, `siteops browse` for Manifest selection, and
[plan output](plan-output.md) for executable preparation.

## plan.diagnostic
## run.diagnostic
## plan.composition-invalid
## plan.operation-preparation-failed
## plan.parameter-selection-invalid
## plan.validation-failed
## run.validation-failed
## run.preparation-invalid

Inspect the detailed preparation error locally. Correct the content and validate
again.

## plan.target-set-incomplete
## run.targeting-incomplete
## plan.targeting-empty
## run.targeting-empty
## plan.targeting-required
## run.targeting-required
## plan.subscription-target-missing
## plan.targeting.conflict

Correct rejected Sites or selection requirements. An incomplete inventory is not
a usable partial plan.

## plan.dependency-blocked
## run.dependency-unavailable

A required prior operation is unavailable. Review its outcome and the reference.

## plan.compilation-failed
## run.compilation-failed

Inspect the local compiler detail and correct the template or module source.

## plan.capability-unavailable
## run.capability-unavailable

Install the tools needed by the selected steps, then plan again.

## site.invalid
## inputs.invalid

Check the Site structure or use `siteops inputs` for the typed input contract.

## lookup.missing
## lookup.incomplete
## lookup.ambiguous

Use `siteops browse` to inspect Manifest choices. Choose an explicit path if name
selection is ambiguous or the inventory is incomplete.

## run.interrupted
## run.progress-reporting-failed

Interruption is distinct from provider failure. Progress reporting errors do not
change observed operation outcomes. See [run output](run-output.md).

## composition.invalid
## operation-preparation.invalid
## operation.dependency-blocked
## parameter-selection.invalid
## validation.failed
## plan.targeting.empty
## plan.targeting.required
## subscription-target.missing
## run-interrupted
## progress-reporting-failed

Local structured results retain these existing code spellings. Their summaries
and guidance come from the same registry as the publishable preparation codes.

## capability.arm-control-plane.missing
## capability.bicep-compiler.missing
## capability.kubectl.missing
## capability.arc-proxy.missing
## compilation.failed
## compilation.input-changed
## compilation.module-unavailable
## compilation.output-invalid
## compilation.source-unreadable
## compilation.timeout
## compilation.tool-missing
## compilation.tool-unavailable

Install required local tools or correct the compiler inputs. These existing
local codes retain their spelling and fixed recovery guidance.

## inputs.resource.invalid-id
## inputs.resource.invalid-type
## inputs.resource.type-mismatch
## inputs.resource.invalid-api-version
## inputs.resource.invalid-observation
## inputs.resource.unsupported-fact
## inputs.resource.unsupported-reference
## inputs.resource.unsupported-provider
## inputs.resource.tool-missing
## inputs.resource.timeout
## inputs.resource.response-limit
## inputs.resource.not-found
## inputs.resource.forbidden
## inputs.resource.not-logged-in
## inputs.resource.subscription-missing
## inputs.resource.cancelled
## inputs.resource.failed
## inputs.resource.subscription-mismatch
## inputs.resource.resource-group-mismatch
## inputs.resource.conflict
## inputs.resource.requirement-unmet
## inputs.resource.requirement-unverified
## inputs.resource.nothing-to-read
## inputs.resource.read-required
## inputs.resource.provider-unavailable

Check the declared input and required read access. Resource input admission is
not a deployment outcome. Validation does not read Azure resources.

## provider.subscription-not-visible

The target subscription is not visible to the current Azure account.
Check the subscriptions visible to the current account and use an identity with
access to the target subscription. `SubscriptionNotFound` is published only
when it comes from a supported structured Azure error.

## provider.authorization-failed

The current identity is not authorized for the requested operation.
Check the identity and its access at the required Azure scope. For unknown
work, confirm the provider state before another deployment.
The admitted structured Azure code is `AuthorizationFailed`.

## provider.template-invalid

The deployment template or its parameters were rejected.
Correct the template or required parameter inputs, then review a new plan.
Inspect the full provider detail locally or in the Azure activity log.
The admitted structured Azure code is `InvalidTemplate`.

## provider.tool-unavailable

A required local tool is unavailable. Install or repair the tools required by
the selected steps, then plan again.

## provider.timeout

The local execution or observation deadline was reached.
Confirm the provider state before another deployment. A local deadline does
not establish whether cloud work completed.

## provider.other

The operation failed or its completion could not be established.
Inspect the detailed error locally or in the Azure activity log. Confirm
unknown work before another deployment. Unrecognized or conflicting causes
use this category without an Azure code, including `QuotaExceeded`.

Provider groups count logical Site Ops operations, not provider attempts,
Sites or nested ARM operations. Failed and unknown groups stay distinct.
Their counts reconcile with the existing operation counters.

## CI result rendering

Copy `scripts/render-siteops-output.py` with updated automation templates.
It uses standard Python, validates the publishable envelope and field bounds,
and encodes logging and Markdown delimiters. It does not import the engine.
It renders `ValidationResult`, `DeploymentPlan` and `DeploymentRun` documents.
Older supported documents can omit optional diagnostic fields and provider
groups. The adapter validates counts and preserves the actual operation exit.

Malformed details print `Diagnostic details are unavailable.` without replacing
a validated status, counts or operation exit. An engine that rejects the new
flags exits with code 2 and gets a fixed engine/template release mismatch
message. Templates never print captured private stderr or probe by retrying
without the flags.
