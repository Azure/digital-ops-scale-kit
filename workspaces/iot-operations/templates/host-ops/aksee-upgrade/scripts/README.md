# aksee-upgrade scripts

Source and generated artifacts for the AKS EE patch-update launcher + worker.
The operator-facing walkthrough (prereqs, run, monitor, verify, Trident
remediation) is in [`../README.md`](../README.md).

| File | Role | Edit? |
|---|---|---|
| `worker.ps1` | The phase state machine that runs on the VM. Source. | Yes |
| `launcher-template.ps1` | Launcher source with the `__EMBEDDED_WORKER_PS1__` sentinel. Writes the worker, registers the SYSTEM task, sets the in-progress tag. | Yes |
| `Build-Launcher.ps1` | Generator. Embeds the worker into the launcher, emits the full and minified variants, and enforces parse and inline-size checks. | No (run after editing sources) |
| `Install-AksEeUpgrade.ps1` | Generated full launcher. Operator-direct invocation form. | No (regenerated) |
| `Install-AksEeUpgrade.min.ps1` | Generated minified launcher. The Bicep `loadTextContent` references this. | No (regenerated) |
| `config.example.json` | Example config for direct worker invocation (debugging only). | Reference |

## Regenerate the launcher

After editing `worker.ps1` or `launcher-template.ps1`, regenerate both launcher
variants with an ABSOLUTE `-ScriptDir` (a relative path mis-resolves the sources
and the parse check falsely errors):

```powershell
powershell -File "<abs>\Build-Launcher.ps1" -ScriptDir "<abs>"
```

The generator parse-checks both variants and exits non-zero on parse or inline-size failure. The
minified launcher is what the Bicep inlines. Move to `scriptUri` delivery when the launcher needs
more capacity.

## Direct worker invocation (local testing)

Phases 0 and 3 authenticate with the Arc machine's managed identity
(`az login --identity`), so running the full flow locally requires an
Arc-onboarded host whose identity has access on the resource group, with an
existing AKS EE cluster. To drive the worker directly:

```powershell
$dir = 'C:\ProgramData\siteops\aksee-upgrade'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Copy-Item .\worker.ps1 $dir\
Copy-Item .\config.example.json $dir\config.json   # edit the values
'{ "phase": 0, "status": "running", "error": null }' | Set-Content $dir\state.json
.\worker.ps1 -ConfigDir $dir
```

## Phase numbers

Phases run 0, 1, 2, 3, then 99. The 0-to-3 range is sequential work and 99 is
the terminal finalize phase. The gap leaves room to insert work phases later
without renumbering the finalize phase or the terminal check.
