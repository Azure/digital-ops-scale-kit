<#
.SYNOPSIS
Launcher for the AKS Edge Essentials + AIO bootstrap. Writes the worker
state-machine to disk, registers a Scheduled Task that drives it through
all phases (including the Hyper-V reboot), and returns once the task is
registered. Intended for either direct invocation on a Windows VM or
delivery via Azure Arc run-command.

.DESCRIPTION
The launcher is self-contained. It embeds `worker.ps1` and the AKS EE
config template as here-strings, so the entire bootstrap can be delivered
as a single `Microsoft.HybridCompute/machines/runCommands` script body.

Steps:
  1. Verify admin privileges and tighten ACLs on the config directory.
  2. Write the embedded worker and template to the config directory.
  3. Write `config.json` and the initial `state.json` (phase=0).
  4. Replace stale terminal state with `running`. A completed bootstrap resumes
     at Phase 3 so the worker revalidates Arc connectivity.
  5. Register a Scheduled Task with at-startup + immediate triggers that runs
     `worker.ps1` as NT AUTHORITY\SYSTEM.
  6. Start the task and return `REGISTERED` so the caller sees success.

The Scheduled Task survives reboots (at-startup trigger) so Phase 1's
Hyper-V enablement does not lose state.

.PARAMETER ClusterName
Name of the Arc-connected Kubernetes cluster that AKS EE will register.
Must match the `clusterName` the scalekit site overlay expects.

.PARAMETER ResourceGroup
Resource group that holds the Arc-connected server resource and will
receive the new connectedClusters resource.

.PARAMETER Subscription
Subscription ID.

.PARAMETER MachineName
Name of the Arc-enabled Windows machine resource that receives bootstrap tags.

.PARAMETER RunId
Opaque per-deploy identifier written with bootstrap state tags.

.PARAMETER Location
Azure region for the connectedClusters and custom-location resources.

.PARAMETER CustomLocationsOid
Tenant-wide object ID for the Custom Locations RP service principal.

.PARAMETER AksEdgeMsiUrl
URL of the AKS Edge Essentials MSI to install. Pin a known-good version.

.PARAMETER ConfigDir
Directory holding all worker artifacts. Defaults to
`C:\ProgramData\siteops\aksee-bootstrap`. Override for local testing.

.PARAMETER ScheduledTaskName
Name of the Scheduled Task. Defaults to `SiteOpsAksEeBootstrap`. Set
explicitly only if multiple bootstraps run on the same host.

.EXAMPLE
    # The worker deploys the cluster (AioDeploy, no SP) and Arc-connects it
    # with the Arc machine's managed identity. Grant that identity Contributor
    # on the resource group first.
    .\Install-AksEeBootstrap.ps1 `
        -ClusterName        aksee-cluster1 `
        -ResourceGroup      aksee-rg `
        -Subscription       00000000-0000-0000-0000-000000000000 `
        -MachineName        arc-server1 `
        -RunId              2026-07-23T210000Z `
        -Location           westus3 `
        -CustomLocationsOid 00000000-0000-0000-0000-000000000000 `
        -AksEdgeMsiUrl      https://aka.ms/aks-edge/k3s-msi

.NOTES
Generated from `launcher-template.ps1` + `worker.ps1` +
`aksedge-config.template.json` by `Build-Launcher.ps1`. Regenerate after
editing any of those sources.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$ClusterName,
    [Parameter(Mandatory)] [string]$ResourceGroup,
    [Parameter(Mandatory)] [string]$Subscription,
    [Parameter(Mandatory)] [string]$MachineName,
    [Parameter(Mandatory)] [string]$RunId,
    [Parameter(Mandatory)] [string]$Location,
    [Parameter(Mandatory)] [string]$CustomLocationsOid,
    [Parameter(Mandatory)] [string]$AksEdgeMsiUrl,
    [string]$ConfigDir         = 'C:\ProgramData\siteops\aksee-bootstrap',
    [string]$ScheduledTaskName = 'SiteOpsAksEeBootstrap',
    # Off by default to match the validated AIO baseline. Set 'true' when
    # downstream AIO needs workload-identity-backed secret sync. A string,
    # not a switch: the Arc Run Command delivers parameters as strings, and
    # a [bool]/[switch] would reject the 'false' the runCommand passes.
    [string]$EnableWorkloadIdentity = 'false',
    # Refuse to re-init when state.json shows an in-flight bootstrap.
    # Pass -Force to reset state to phase=0 and re-register the task
    # (destroys progress of any concurrent run).
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ConfirmPreference = 'None'
$ProgressPreference = 'SilentlyContinue'

# The bootstrap targets Windows PowerShell 5.1 ("Desktop"). The worker's
# AksEdge module and the scheduled task both run under powershell.exe, so
# keep the launcher on the same edition and refuse PowerShell 7+ ("Core").
if ($PSVersionTable.PSEdition -ne 'Desktop') {
    throw "Install-AksEeBootstrap.ps1 requires Windows PowerShell 5.1 (Desktop). Detected: $($PSVersionTable.PSEdition) $($PSVersionTable.PSVersion). Re-run with 'powershell.exe -File Install-AksEeBootstrap.ps1 ...' instead of pwsh."
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
    # icacls is native: a non-zero exit does not raise under $ErrorActionPreference=
    # 'Stop', so check $LASTEXITCODE explicitly.
    param([string[]]$IcaclsArgs)
    $out = & icacls @IcaclsArgs 2>&1
    if ($LASTEXITCODE -ne 0) { throw "icacls $($IcaclsArgs -join ' ') failed (exit ${LASTEXITCODE}): $out" }
}

function Set-StrictAcl {
    # Lock the config dir to Administrators + SYSTEM and reclaim ownership. The caller
    # has already rejected a pre-existing reparse point or non-admin-owned directory,
    # so this never recurses and no junction is ever followed. Strip the inherited
    # Users-read grant, which would expose the kubeconfig bearer token and az token
    # cache, grant Administrators + SYSTEM, then set ownership to the Administrators
    # group. The worker Scheduled Task runs as SYSTEM, which retains full access
    # through the explicit SYSTEM grant. Verify the owner by SID.
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
    # Mark this deploy in progress before the runCommand returns. The worker
    # writes the terminal value.
    param([switch]$Required)

    if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
        if ($Required) {
            throw 'Azure CLI is required to refresh completed bootstrap state, but it is not installed.'
        }
        Write-Log 'Skipping in-progress tag write because Azure CLI is not installed.'
        return
    }

    try {
        $env:AZURE_CONFIG_DIR = Join-Path $ConfigDir '.azure'
        foreach ($name in @('IDENTITY_ENDPOINT', 'IMDS_ENDPOINT')) {
            if (-not [Environment]::GetEnvironmentVariable($name)) {
                $machineValue = [Environment]::GetEnvironmentVariable($name, 'Machine')
                if ($machineValue) { Set-Item -Path "Env:$name" -Value $machineValue }
            }
        }

        & az login --identity --only-show-errors 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'az login --identity returned non-zero.' }

        $accountOut = & az account set --subscription $Subscription 2>&1
        if ($LASTEXITCODE -ne 0) { throw "az account set failed: $accountOut" }

        $arcId = "/subscriptions/$Subscription/resourceGroups/$ResourceGroup/providers/Microsoft.HybridCompute/machines/$MachineName"
        $tagOut = & az tag update --resource-id $arcId --operation merge --tags `
            'siteops.bootstrap.state=running' `
            "siteops.bootstrap.runId=$RunId" `
            --only-show-errors 2>&1
        if ($LASTEXITCODE -ne 0) { throw "az tag update failed: $tagOut" }

        Write-Log "Set siteops.bootstrap.state=running on $arcId (runId=$RunId)"
    } catch {
        if ($Required) { throw "Failed to refresh bootstrap state before revalidation: $_" }
        Write-Log "Skipping in-progress tag write. The worker will retry when Azure CLI is available. $_"
    }
}

# ---------------------------------------------------------------------------
# Embedded payloads
# ---------------------------------------------------------------------------

# === BEGIN EMBEDDED WORKER ===
$EmbeddedWorker = @'
__EMBEDDED_WORKER_PS1__
'@
# === END EMBEDDED WORKER ===

# === BEGIN EMBEDDED TEMPLATE ===
$EmbeddedTemplate = @'
__EMBEDDED_AKSEDGE_TEMPLATE__
'@
# === END EMBEDDED TEMPLATE ===

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if (-not (Test-IsAdmin)) {
    throw 'Install-AksEeBootstrap.ps1 must run as Administrator.'
}

Write-Log "Bootstrapping cluster $ClusterName in $ResourceGroup ($Location)"

# Preflight: fail fast on an unreachable or wrong-content MSI URL so we
# do not register a task that will fail Phase 1. A HEAD request avoids a
# full binary download. Validates three things:
#   1. Status 200.
#   2. Content-Type is not text/* (a wrong aka.ms link returns an HTML
#      error page that would otherwise reach msiexec as a bad installer).
#   3. Content-Length is > 50MB (an HTML error page is well below this).
try {
    Write-Log "Pre-checking AKS EE MSI URL $AksEdgeMsiUrl"
    $head = Invoke-WebRequest -Uri $AksEdgeMsiUrl -Method Head -UseBasicParsing -ErrorAction Stop
    if ($head.StatusCode -ne 200) {
        throw "Unexpected status $($head.StatusCode) from MSI URL preflight."
    }
    $ct = $head.Headers['Content-Type']
    if ($ct -match '^text/') {
        throw "MSI URL returned Content-Type '$ct' (expected application/octet-stream or application/x-msi). The URL likely redirects to an error page rather than the installer."
    }
    $cl = [int64]0
    $rawLen = $head.Headers['Content-Length']
    if ($rawLen) { [void][int64]::TryParse(($rawLen | Select-Object -First 1), [ref]$cl) }
    if ($cl -gt 0 -and $cl -lt 50MB) {
        throw "MSI URL returned Content-Length $cl bytes (expected > 50MB). The URL likely redirects to an error page rather than the installer."
    }
} catch {
    throw "AKS EE MSI URL preflight failed for ${AksEdgeMsiUrl}: $_"
}

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

$workerPath   = Join-Path $ConfigDir 'worker.ps1'
$templatePath = Join-Path $ConfigDir 'aksedge-config.template.json'
$configPath   = Join-Path $ConfigDir 'config.json'
$statePath    = Join-Path $ConfigDir 'state.json'

# Re-init guard. Refuse to clobber an in-flight bootstrap without -Force.
# Revalidate a completed bootstrap from Phase 3 so host features and cluster
# creation do not run again.
$initialPhase = 0
if ((Test-Path $statePath) -and -not $Force) {
    $inFlight = $false
    $existingPhase = $null
    $existingStatus = $null
    try {
        $existing = Get-Content -Raw -Path $statePath | ConvertFrom-Json
        if ($existing.PSObject.Properties.Name -contains 'status') {
            $existingStatus = $existing.status
            if ($existing.status -in @('running', 'pending-reboot')) {
                $inFlight = $true
            } elseif ($existing.status -eq 'succeeded') {
                $initialPhase = 3
            }
            if ($existing.PSObject.Properties.Name -contains 'phase') {
                $existingPhase = $existing.phase
            }
        }
    } catch {
        # Unparseable state.json: warn and continue, since re-init repairs it.
        Write-Log "WARNING: existing state.json could not be parsed. Re-initializing. ($_)"
    }
    if ($inFlight) {
        throw "Bootstrap already in flight (state.json shows phase=$existingPhase status=$existingStatus). Pass -Force to reset state and re-register the task, or wait for the existing run to complete."
    }
}

Set-Content -Path $workerPath   -Value $EmbeddedWorker   -Encoding UTF8
Set-Content -Path $templatePath -Value $EmbeddedTemplate -Encoding UTF8
Write-Log "Wrote $workerPath and $templatePath"

Write-Log 'Worker task will run as NT AUTHORITY\SYSTEM (no local account created)'

$config = [pscustomobject]@{
    clusterName            = $ClusterName
    resourceGroup          = $ResourceGroup
    subscription           = $Subscription
    machineName            = $MachineName
    runId                  = $RunId
    location               = $Location
    customLocationsOid     = $CustomLocationsOid
    aksEdgeMsiUrl          = $AksEdgeMsiUrl
    scheduledTaskName      = $ScheduledTaskName
    enableWorkloadIdentity = ($EnableWorkloadIdentity -ieq 'true')
}
$config | ConvertTo-Json | Set-Content -Path $configPath -Encoding UTF8
Write-Log "Wrote $configPath (auth=managed identity, WI=$($EnableWorkloadIdentity -ieq 'true'))"

$initialState = [pscustomobject]@{
    phase       = $initialPhase
    status      = 'running'
    lastUpdated = (Get-Date).ToString('o')
    error       = $null
}
$initialStateTmp = "$statePath.tmp"
$initialState | ConvertTo-Json | Set-Content -Path $initialStateTmp -Encoding UTF8
Move-Item -Path $initialStateTmp -Destination $statePath -Force
Write-Log "Wrote $statePath (phase=$initialPhase)"

Set-RunningTag -Required:($initialPhase -eq 3)

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$workerPath`" -ConfigDir `"$ConfigDir`""

# at-startup trigger handles the Hyper-V reboot resume in Phase 1.
# once-trigger kicks off the initial run without needing a reboot.
$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$onceTrigger    = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddSeconds(30))

# Run as the built-in SYSTEM service account, so there is no credential to
# store for the reboot-surviving task.
$principal = New-ScheduledTaskPrincipal `
    -UserId 'NT AUTHORITY\SYSTEM' `
    -LogonType ServiceAccount `
    -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -MultipleInstances IgnoreNew

# No auto-restart on failure. Non-transient errors (wrong subscription,
# missing RBAC, etc.) would otherwise retry 3 times at 5-min intervals,
# overwriting state.json.error each time and hiding repeated identical
# failures from operators. The worker's idempotency makes manual
# re-invocation via Start-ScheduledTask safe and explicit when an
# operator decides a retry is warranted.

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
