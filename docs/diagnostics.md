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

## CI result rendering

Copy `scripts/render-siteops-output.py` with updated automation templates.
It uses standard Python, validates the publishable envelope and field bounds,
and encodes logging and Markdown delimiters. It does not import the engine.
It renders `ValidationResult` documents, including records without optional
diagnostic fields. Deployment templates keep their existing rendering.

Malformed details print `Diagnostic details are unavailable.` without replacing
a validated status, counts or operation exit. An engine that rejects the new
flags exits with code 2 and gets a fixed engine/template release mismatch
message. Templates never print captured private stderr or probe by retrying
without the flags.
