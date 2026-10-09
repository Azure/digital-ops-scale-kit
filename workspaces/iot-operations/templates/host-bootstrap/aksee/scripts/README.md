# AKS Edge Essentials bootstrap scripts

The PowerShell scripts the bootstrap delivers to the Windows VM. The Bicep template at `../template.bicep` inlines the minified launcher via `loadTextContent`. The launcher embeds the worker and the AKS Edge config template as here-strings and registers a Scheduled Task that drives the worker through all phases.

For operator usage, see the [bootstrap guide](../../../../manifests/aksee-bootstrap/README.md). This README covers the build workflow.

## Files in this folder

| File | Role | Edit by hand? |
|---|---|---|
| `worker.ps1` | The phase state machine that runs on the VM. Source. | Yes |
| `launcher-template.ps1` | Launcher source with `__EMBEDDED_*__` sentinels for the worker and the AKS Edge config template. | Yes |
| `aksedge-config.template.json` | AKS EE cluster config template (`AioDeploy` set, cluster only). The worker substitutes `Arc.ClusterName` at runtime from `config.json`. | Yes (cluster sizing, networking) |
| `Build-Launcher.ps1` | Generator. Combines the worker + AKS Edge template into the launcher and emits both full and minified variants. Checks that both parse. | No (run after editing sources) |
| `Install-AksEeBootstrap.ps1` | Generated full launcher. The form an operator invokes directly. | No (regenerated) |
| `Install-AksEeBootstrap.min.ps1` | Generated minified launcher. The Bicep `loadTextContent` references this. | No (regenerated) |
| `config.example.json` | Example to fill in by hand when invoking the worker directly (local testing without the launcher). | Reference only |

## Build workflow

After editing any of `worker.ps1`, `launcher-template.ps1`, or `aksedge-config.template.json`, regenerate both launcher variants:

```powershell
cd workspaces/iot-operations/templates/host-bootstrap/aksee/scripts
.\Build-Launcher.ps1
```

Output:

```
Generated <scripts-dir>\Install-AksEeBootstrap.ps1 (<N> lines, parse OK)
Generated <scripts-dir>\Install-AksEeBootstrap.min.ps1 (<N> lines, <N> bytes, parse OK)
```

The generator checks that both variants parse and exits with a nonzero code when parsing fails or the inline size is exceeded. The minified variant is what the Bicep delivers via Arc Run Command. The full variant is for operators to run directly on the VM. Do not edit the generated files by hand. They are overwritten on every build.

### Size constraints

The Bicep template inlines the minified launcher, so it must stay within the configured Arc
`runCommands` limit on script body size. The generator warns before the limit and fails after it.
`scriptUri` delivery is an alternative when the launcher needs more capacity.

## Direct worker invocation (local testing)

Run the worker directly on a VM without the launcher and Scheduled Task. Useful for iterating on Phase 3 logic without redeploying the launcher.

```powershell
$dir = 'C:\test\bootstrap'
New-Item -ItemType Directory -Path $dir -Force | Out-Null

# Copy the worker and the AKS Edge config template into the test dir
Copy-Item .\worker.ps1                       $dir\
Copy-Item .\aksedge-config.template.json     $dir\
Copy-Item .\config.example.json              $dir\config.json

# Edit $dir\config.json: verify clusterName, resourceGroup, subscription, etc.
notepad $dir\config.json

# Seed initial state
@{ phase = 0; status = 'running'; lastUpdated = (Get-Date).ToString('o') } |
    ConvertTo-Json | Set-Content $dir\state.json

# Run as Administrator
.\worker.ps1 -ConfigDir $dir
```

Phase 3 authenticates with the Arc machine's managed identity (`az login --identity`), so running the full Phase 3 locally requires a host onboarded to Azure Arc whose identity has access on the resource group. Phases 0-2 (preflight, install, cluster deploy) need no Azure auth.

## Phase numbers

Phase numbering is structural to the worker (state machine, anchor points that survive reboots, `state.json` field, log message prefixes, function names like `Invoke-Phase2`). For phase semantics see the [phases reference](../../../../manifests/aksee-bootstrap/README.md#phases-reference).

## Generation conventions

- The worker source cannot contain a here-string opener (`@'` or `@"`). The minifier strips leading whitespace from every kept line, which would corrupt here-string body indentation. `Build-Launcher.ps1` enforces this with a guard.
- The aksedge-config template is minified via `ConvertFrom-Json | ConvertTo-Json -Compress` before embedding.
- A banner at the top of each generated file marks it as generated, not for editing.
