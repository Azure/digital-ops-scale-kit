# Site Ops error guide

Read this file when a Site Ops command fails. Each entry starts with the
message Site Ops prints. The
[troubleshooting guide](https://github.com/Azure/digital-ops-scale-kit/blob/main/docs/troubleshooting.md)
has the full explanation for each one.

## Commands and published content

### "is a global option. Put it before the command"

Cause: `-w`, `--project`, `--approved-source`, `--trust-policy`,
`--trusted-root`, `--extra-sites-dir` or `-v` came after the command name.
Nothing was read.

Fix: Move those options before the command, for example
`siteops --project ./factory plan aio-install`.

### "Manifest not found"

Cause: No manifest in the selected release or workspace has that name or
path. Site Ops suggests a close name when there is one.

Fix: Use the suggested name, or list the manifests with
`siteops browse --source "<source>@<release>"`.

### "Direct source use requires an explicit published release"

Cause: `--source` named a source without `@` and a release tag.

Fix: Add the release, for example
`siteops inputs aio-install --source "<source>@<release>"`.

### "This package requires a different Site Ops version"

Cause: The installed Site Ops engine is outside the range the release's
content supports.

Fix: Install the release's own engine with the install-siteops skill, or
choose a release that matches the installed engine.

### "Project directory not found"

Cause: `--project` names a directory that does not exist.

Fix: Check the path with the user. Create the directory only with their
agreement.

### "Noninteractive deployment requires --yes"

Cause: `deploy` cannot ask for confirmation in an agent shell, JSON output or
CI. Nothing was read or deployed.

Fix: Show the plan, get the user's confirmation, then run the same `deploy`
command with `--yes`.

### "Direct content requires explicit Site inputs or --project for configured Sites"

Cause: `plan`, `deploy` or `validate` used `--source` without `--input`,
`--input-file`, `--site-file` or `--project`. Example Sites inside a release
are not deployment targets.

Fix: Add `--input "cluster=<cluster-resource-ID>"`, a complete
`--site-file`, or `--project` with the user's saved Sites.

### "Workspace pin not found"

Cause: The selected project has no `siteops.pin`, and the command named
neither `--source` nor `-w`.

Fix: Add `--source "<source>@<release>"`, or pin a release with
`siteops project pin <project> --source "<source>@<release>"` after the
user agrees.

### "GitHub CLI 2.95 or newer is required"

Cause: Site Ops verifies published content with GitHub CLI, which is missing,
too old or in a location other users can change.

Fix: Install GitHub CLI 2.95 or newer from https://cli.github.com. No GitHub
login is needed.

### "GitHub API rate limit was reached"

Cause: Unauthenticated GitHub API reads are limited per IP address, which is
shared on corporate networks, Cloud Shell and Codespaces.

Fix: Wait until the reported time. Otherwise, with the user's agreement and
only if GitHub CLI is signed in (`gh auth status`), set `GH_TOKEN` in the
same command, for example `GH_TOKEN=$(gh auth token) siteops browse --source "<source>@<release>"`
in bash. In Codespaces, the message suggests `GITHUB_TOKEN` instead. Never
print the token.

## Approved sources

### "The approved source policy has expired"

Cause: The enrollment is older than 30 days.

Fix: With the user's agreement, renew it with `siteops source enroll official`,
using the name the message shows.

### "Approved source '...' is not enrolled"

Cause: No source is enrolled under that name.

Fix: Check `siteops source list`, or enroll the official publisher with
`siteops source enroll official` after the user agrees.

### "requires an approved source"

Cause: A pinned project, `project pin` or a `github:OWNER/REPO` locator was
used without an approved source.

Fix: Follow the message. For a pinned project, it names an approved source
to put before the command, for example
`siteops --approved-source official --project <project> plan aio-install -l name=<site-name>`.
For a locator, it suggests `--source official@<release>` instead. If no
source is enrolled for that publisher, it shows the enrollment command to
run with the user's agreement.

### "does not match the selected workspace source"

Cause: The approved source belongs to a different publisher than the pin or
locator.

Fix: Use the approved source the message names.

### "Choose one approved source"

Cause: The command named a source in both `--source NAME@RELEASE` and
`--approved-source NAME`.

Fix: Keep `--source NAME@RELEASE` for a direct release. Use
`--approved-source` only with a pinned project or `project pin`.

## Azure CLI

### "Azure CLI (`az`) was not found on PATH"

Cause: The plan needs Azure CLI and none is installed. Nothing ran.

Fix: Install Azure CLI from https://aka.ms/installazurecli and open a new
shell.

### "Azure CLI 2.70.0 or newer is required"

Cause: Azure CLI is older than 2.70.0. Nothing was submitted.

Fix: Run `az upgrade` after the user agrees, then retry.

### "The target subscription is not visible to the account signed in to Azure CLI"

Cause: The Site's subscription is not available to the signed in account.
No deployment started.

Fix: Run `az account list`. If the subscription is missing, the user signs
in with an account that can use it.

### "Input '...' read failed"

Cause: Site Ops reads the cluster or instance through Azure CLI before
planning. The code after `inputs.resource.` names the reason. Nothing was
planned or deployed.

Fix, by code:

| Code | Fix |
|---|---|
| `not-logged-in` | The user runs `az login`. |
| `forbidden` | The user needs a role that can read the resource, such as Reader. |
| `not-found` | Check the resource ID with the user. |
| `subscription-missing` | Run `az account list`, or sign in with an account that can use the subscription. |
| `tool-missing` | Install Azure CLI. |
| `timeout` | Check network access to Azure, then retry. |

## Typed inputs

### "contain an unknown input"

Cause: An `--input` name is not declared by the manifest. Site Ops suggests
the closest name.

Fix: Use the suggested name, or list the inputs with
`siteops inputs aio-install --source "<source>@<release>"`, using the
manifest that failed.

### "Missing required input" or "so Site Ops can read the cluster's"

Cause: A required answer is missing. Secret Sync needs a cluster or instance
resource ID so Site Ops can read its OIDC issuer and workload identity.

Fix: Add the `--input` the message names.

### "does not fit input 'siteName'"

Cause: The cluster's name is not a valid Site name, for example because it
contains an underscore or is too long.

Fix: Add `--input siteName=<name>` with lowercase letters, digits and
interior hyphens.

### "already exists. Choose a new file name"

Cause: `--save-site` never replaces an existing file or creates a directory.

Fix: Ask the user for a new file name, or create the directory with their
agreement.

## Sites and selection

### "matched no Sites"

Cause: A `-l` selector has a typo or names a label value no Site has. The
message lists the real values.

Fix: Correct the selector with the user.

### "does not match -l"

Cause: An explicit Site from `--input`, `--input-file` or `--site-file` does
not satisfy a `-l` term.

Fix: Remove the conflicting `-l` term, or use a Site that matches it.

## Deployment

### "AuthorizationFailed"

Cause: The signed in identity lacks a role on the subscription or resource
group.

Fix: The user checks role assignments with their administrator. Do not
change roles yourself.

### A kubectl step fails with "is forbidden"

Cause: The identity can reach the cluster through Azure Arc but lacks
Kubernetes permissions inside it.

Fix: Explain the gap and point the user to the troubleshooting guide's
section on this error. Granting cluster roles is the user's decision.

### An operation reports "unknown"

Cause: Site Ops could not confirm the provider's final result.

Fix: Inspect the named deployment before deciding to deploy again.

### A step failed and later steps are "not-run"

Cause: One operation failed, which stops the remaining operations for that
Site.

Fix: Show the failure, correct the cause, then prepare a new plan through the
deploy-aio or upgrade-aio skill.
