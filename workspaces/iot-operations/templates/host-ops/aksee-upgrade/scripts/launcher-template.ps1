<#
.SYNOPSIS
Launcher for the AKS Edge Essentials patch-update worker. Writes the worker
state-machine to disk, registers a Scheduled Task that drives it, sets the
in-progress completion tag, and returns once the task is registered. Intended
for either direct invocation on a Windows VM or delivery via Azure Arc
run-command.

.DESCRIPTION
The launcher is self-contained. It embeds `worker.ps1` as a here-string, so the
whole upgrade can be delivered as a single
`Microsoft.HybridCompute/machines/runCommands` script body.

Steps:
  1. Verify admin privileges and tighten ACLs on the config directory.
  2. Write the embedded worker to the config directory.
  3. Write `config.json` and the initial `state.json` (phase=0).
  4. Best-effort: set the Arc machine tag `siteops.aksee.upgrade.state=running`
     synchronously (via the machine managed identity) so a `type: wait` step
     never observes a stale `succeeded` from a previous run before the new
     worker has started.
  5. Register a Scheduled Task with at-startup + immediate triggers that runs
     `worker.ps1` as NT AUTHORITY\SYSTEM.
  6. Start the task and return `REGISTERED` so the caller sees success.

Re-running against an already-upgraded host is safe: the launcher resets state
and the worker no-ops in Phase 1 when no newer update is available. Only an
in-flight run (status running / pending-reboot) is refused without -Force.

.PARAMETER ResourceGroup
Resource group that holds the Arc-connected server and the connected cluster.

.PARAMETER Subscription
Subscription ID.

.PARAMETER RunId
Opaque per-deploy identifier written into the completion tag so the wait step
and operators can correlate a tag with a specific deploy.

.PARAMETER ConfigDir
Directory holding all worker artifacts. Defaults to
`C:\ProgramData\siteops\aksee-upgrade`. Override for local testing.

.PARAMETER ScheduledTaskName
Name of the Scheduled Task. Defaults to `SiteOpsAksEeUpgrade`.

.EXAMPLE
    # Patch-update an AKS EE cluster. The worker authenticates as the Arc
    # machine's managed identity for verification and the completion tag.
    .\Install-AksEeUpgrade.ps1 `
        -ResourceGroup aksee-rg `
        -Subscription  00000000-0000-0000-0000-000000000000 `
        -RunId         2026-06-15T180000Z

.NOTES
Generated from `launcher-template.ps1` + `worker.ps1` by `Build-Launcher.ps1`.
Regenerate after editing either source.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$ResourceGroup,
    [Parameter(Mandatory)] [string]$Subscription,
    [Parameter(Mandatory)] [string]$RunId,
    # The Arc machine resource name the wait step targets for the completion tag.
    # Defaults to the hostname. The Bicep passes the actual machine resource name.
    [string]$MachineName       = $env:COMPUTERNAME,
    [string]$ConfigDir         = 'C:\ProgramData\siteops\aksee-upgrade',
    [string]$ScheduledTaskName = 'SiteOpsAksEeUpgrade',
    # A string, not a switch, so the Arc Run Command can deliver it. When false
    # (default), the worker applies patch updates only. When true, the worker
    # performs sequential minor-version hops with AcceptUpgrade scoped to the run.
    [string]$AllowKubernetesMinorUpgrade = 'false',
    # Optional target Kubernetes version for minor-mode upgrades (e.g. '1.33').
    # The worker stops hopping once the deployed minor matches this value.
    # Empty string means no explicit target (upgrade to the latest available).
    [string]$TargetKubernetesVersion = '',
    # Refuse to re-init when state.json shows an in-flight upgrade. Pass -Force
    # to reset state to phase=0 and re-register the task.
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ConfirmPreference = 'None'
$ProgressPreference = 'SilentlyContinue'

# The worker's AksEdge module and the scheduled task both run under
# powershell.exe, so keep the launcher on the same edition and refuse
# PowerShell 7+ ("Core").
if ($PSVersionTable.PSEdition -ne 'Desktop') {
    throw "Install-AksEeUpgrade.ps1 requires Windows PowerShell 5.1 (Desktop). Detected: $($PSVersionTable.PSEdition) $($PSVersionTable.PSVersion). Re-run with 'powershell.exe -File Install-AksEeUpgrade.ps1 ...' instead of pwsh."
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Write-Log {
    param([string]$Message)
    $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    Write-Host "[$ts] [launcher] $Message"
}

function Test-IsAdmin {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-IcaclsOrThrow {
    # icacls is native: a non-zero exit does not raise under the strict settings, so
    # check $LASTEXITCODE explicitly.
    param([string[]]$IcaclsArgs)
    $out = & icacls @IcaclsArgs 2>&1
    if ($LASTEXITCODE -ne 0) { throw "icacls $($IcaclsArgs -join ' ') failed (exit ${LASTEXITCODE}): $out" }
}

function Set-StrictAcl {
    # Lock the config dir to Administrators + SYSTEM and reclaim ownership. The caller
    # has already rejected a pre-existing reparse point or non-admin-owned directory,
    # so this never recurses and no junction is ever followed. Strip the inherited
    # Users-read grant exposes the kubeconfig and az token cache. Grant
    # Administrators + SYSTEM, then set ownership to the Administrators group.
    # Verify the owner by SID.
    param([string]$Path)
    Invoke-IcaclsOrThrow @($Path, '/setowner', 'Administrators')
    Invoke-IcaclsOrThrow @($Path, '/inheritance:r')
    Invoke-IcaclsOrThrow @($Path, '/grant', 'Administrators:(OI)(CI)F', 'SYSTEM:(OI)(CI)F')
    # Verify by SID (locale-independent): Administrators S-1-5-32-544, SYSTEM S-1-5-18.
    $ownerSid = ((Get-Acl -Path $Path).GetOwner([System.Security.Principal.SecurityIdentifier])).Value
    if ($ownerSid -notin @('S-1-5-32-544', 'S-1-5-18')) {
        throw "Refusing to use ${Path}: owner SID '$ownerSid' is not Administrators or SYSTEM after hardening."
    }
    Write-Log "Locked ACLs and reclaimed ownership on $Path"
}

function Set-RunningTag {
    # Best-effort: mark the Arc machine tag in-progress synchronously, before
    # the runCommand returns, so a downstream wait step never sees a stale
    # `succeeded` from a previous run. Runs in the runCommand context (SYSTEM),
    # which can reach HIMDS for the machine identity. Skips silently if az is
    # absent or the login fails, in which case the worker sets the tag instead.
    param([string]$Subscription, [string]$ResourceGroup, [string]$MachineName, [string]$RunId)
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
        Write-Log 'Skipping in-progress tag write: az CLI not installed (the worker will set it).'
        return
    }
    try {
        $env:AZURE_CONFIG_DIR = Join-Path $ConfigDir '.azure'
        foreach ($name in @('IDENTITY_ENDPOINT', 'IMDS_ENDPOINT')) {
            if (-not [Environment]::GetEnvironmentVariable($name)) {
                $machineVal = [Environment]::GetEnvironmentVariable($name, 'Machine')
                if ($machineVal) { Set-Item -Path "Env:$name" -Value $machineVal }
            }
        }
        & az login --identity --only-show-errors 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Log 'In-progress tag write skipped: az login --identity failed (the worker will retry).'; return }
        $arcId = "/subscriptions/$Subscription/resourceGroups/$ResourceGroup/providers/Microsoft.HybridCompute/machines/$MachineName"
        & az tag update --resource-id $arcId --operation merge --tags "siteops.aksee.upgrade.state=running" "siteops.aksee.upgrade.runId=$RunId" --only-show-errors 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Log "Set siteops.aksee.upgrade.state=running on $arcId (runId=$RunId)" }
        else { Write-Log 'In-progress tag write returned non-zero (the worker will retry).' }
    } catch {
        Write-Log "In-progress tag write skipped due to error: $_ (the worker will retry)."
    }
}

# ---------------------------------------------------------------------------
# Embedded payload
# ---------------------------------------------------------------------------

# === BEGIN EMBEDDED WORKER ===
$EmbeddedWorker = @'
__EMBEDDED_WORKER_PS1__
'@
# === END EMBEDDED WORKER ===

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if (-not (Test-IsAdmin)) {
    throw 'Install-AksEeUpgrade.ps1 must run as Administrator.'
}

Write-Log "Preparing AKS EE patch update on $MachineName in $ResourceGroup (runId=$RunId)"

if (Test-Path $ConfigDir) {
    # A pre-existing config dir under the world-writable ProgramData may have been
    # planted by a non-admin. Reject a reparse point (a junction would redirect the
    # later ownership and ACL operations to an arbitrary target) or a directory owned
    # by a non-admin (its owner keeps implicit WRITE_DAC to tamper with the worker). A
    # legitimate resume dir is owned by Administrators or SYSTEM and passes. No
    # recursion is used here or in Set-StrictAcl, so a junction is never followed.
    $existing = Get-Item -LiteralPath $ConfigDir -Force
    if ($existing.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        throw "Refusing to use ${ConfigDir}: it is a reparse point (possible pre-creation by a non-admin)."
    }
    $existingOwner = ((Get-Acl -LiteralPath $ConfigDir).GetOwner([System.Security.Principal.SecurityIdentifier])).Value
    if ($existingOwner -notin @('S-1-5-32-544', 'S-1-5-18')) {
        throw "Refusing to use ${ConfigDir}: pre-existing owner SID '$existingOwner' is not Administrators or SYSTEM (possible pre-creation by a non-admin)."
    }
} else {
    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
    Write-Log "Created $ConfigDir"
}
Set-StrictAcl -Path $ConfigDir

$workerPath = Join-Path $ConfigDir 'worker.ps1'
$configPath = Join-Path $ConfigDir 'config.json'
$statePath  = Join-Path $ConfigDir 'state.json'

# Re-init guard. Refuse to clobber an in-flight upgrade (status running /
# pending-reboot) without -Force. A previous `succeeded` or `failed` is reset
# and re-run, since re-applying the upgrade manifest should re-check for newer
# patches (the worker no-ops when nothing newer is available).
if ((Test-Path $statePath) -and -not $Force) {
    $existingPhase = $null
    $existingStatus = $null
    try {
        $existing = Get-Content -Raw -Path $statePath | ConvertFrom-Json
        if ($existing.PSObject.Properties.Name -contains 'status') {
            $existingStatus = $existing.status
            if ($existing.PSObject.Properties.Name -contains 'phase') { $existingPhase = $existing.phase }
        }
    } catch {
        Write-Log "WARNING: existing state.json could not be parsed. Re-initializing. ($_)"
    }
    if ($existingStatus -in @('running', 'pending-reboot')) {
        throw "Upgrade already in flight (state.json shows phase=$existingPhase status=$existingStatus). Pass -Force to reset state and re-register the task, or wait for the existing run to complete."
    }
}

Set-Content -Path $workerPath -Value $EmbeddedWorker -Encoding UTF8
Write-Log "Wrote $workerPath"

Write-Log 'Worker task will run as NT AUTHORITY\SYSTEM'

$config = [pscustomobject]@{
    resourceGroup               = $ResourceGroup
    subscription                = $Subscription
    machineName                 = $MachineName
    runId                       = $RunId
    allowKubernetesMinorUpgrade = ($AllowKubernetesMinorUpgrade -ieq 'true')
    targetKubernetesVersion     = $TargetKubernetesVersion
    scheduledTaskName           = $ScheduledTaskName
}
$config | ConvertTo-Json | Set-Content -Path $configPath -Encoding UTF8
Write-Log "Wrote $configPath (auth=managed identity)"

$initialState = [pscustomobject]@{
    phase       = 0
    status      = 'running'
    lastUpdated = (Get-Date).ToString('o')
    error       = $null
}
$initialStateTmp = "$statePath.tmp"
$initialState | ConvertTo-Json | Set-Content -Path $initialStateTmp -Encoding UTF8
Move-Item -Path $initialStateTmp -Destination $statePath -Force
Write-Log "Wrote $statePath (phase=0)"

# Close the stale-tag race before the runCommand returns.
Set-RunningTag -Subscription $Subscription -ResourceGroup $ResourceGroup -MachineName $MachineName -RunId $RunId

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$workerPath`" -ConfigDir `"$ConfigDir`""

# at-startup trigger is a safety net for an unrelated host reboot. The upgrade
# itself only reboots the inner node VM. The once-trigger kicks off the run.
$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$onceTrigger    = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddSeconds(30))

$principal = New-ScheduledTaskPrincipal `
    -UserId 'NT AUTHORITY\SYSTEM' `
    -LogonType ServiceAccount `
    -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
    -MultipleInstances IgnoreNew

$task = New-ScheduledTask `
    -Action $action `
    -Trigger @($startupTrigger, $onceTrigger) `
    -Principal $principal `
    -Settings $settings

Register-ScheduledTask `
    -TaskName $ScheduledTaskName `
    -InputObject $task `
    -Force | Out-Null
Write-Log "Registered Scheduled Task $ScheduledTaskName"

Start-ScheduledTask -TaskName $ScheduledTaskName
Write-Log "Started $ScheduledTaskName"

Write-Output 'REGISTERED'
