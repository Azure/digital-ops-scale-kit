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
  3. Under -RunAsDedicatedAdmin, create (or refresh) a local admin user with
     an on-box generated password. The default (SYSTEM) needs no account.
  4. Write `config.json` and the initial `state.json` (phase=0).
  5. Register a Scheduled Task with at-startup + immediate triggers that runs
     `worker.ps1` as SYSTEM (or the local admin under -RunAsDedicatedAdmin).
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

.PARAMETER LocalAdminUser
Local user the Scheduled Task runs as. Defaults to `siteops-bootstrap`.
The launcher creates the user (or resets its password) and adds it to
the local Administrators group.

.EXAMPLE
    # The worker deploys the cluster (AioDeploy, no SP) and Arc-connects it
    # with the Arc machine's managed identity. Grant that identity Contributor
    # on the resource group first.
    .\Install-AksEeBootstrap.ps1 `
        -ClusterName        aksee-cluster1 `
        -ResourceGroup      aksee-rg `
        -Subscription       00000000-0000-0000-0000-000000000000 `
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
    [Parameter(Mandatory)] [string]$Location,
    [Parameter(Mandatory)] [string]$CustomLocationsOid,
    [Parameter(Mandatory)] [string]$AksEdgeMsiUrl,
    [string]$ConfigDir         = 'C:\ProgramData\siteops\aksee-bootstrap',
    [string]$ScheduledTaskName = 'SiteOpsAksEeBootstrap',
    [string]$LocalAdminUser    = 'siteops-bootstrap',
    # Off by default to match the validated AIO baseline. Set 'true' when
    # downstream AIO needs workload-identity-backed secret sync. A string,
    # not a switch: the Arc Run Command delivers parameters as strings, and
    # a [bool]/[switch] would reject the 'false' the runCommand passes.
    [string]$EnableWorkloadIdentity = 'false',
    # Refuse to re-init when state.json shows an in-flight bootstrap.
    # Pass -Force to reset state to phase=0 and re-register the task
    # (destroys progress of any concurrent run).
    [switch]$Force,
    # The worker Scheduled Task runs as NT AUTHORITY\SYSTEM by default, so no
    # local account or password is created. Set 'true' to instead create the
    # $LocalAdminUser account with an on-box generated password and run the task
    # as it (a hardened-environment fallback). A string, not a switch, so the
    # Arc Run Command can deliver it (matching EnableWorkloadIdentity).
    [string]$RunAsDedicatedAdmin = 'false'
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

function New-RandomPassword {
    # Strong 24-char password (upper, lower, digit, symbol) for the local
    # admin user, drawn from a cryptographic RNG (GetBytes, for PS 5.1
    # compatibility) with rejection sampling to avoid modulo bias. The
    # credential grants local admin and can outlive the bootstrap on an
    # early failure, so it must be unpredictable.
    $upper = 'ABCDEFGHJKLMNPQRSTUVWXYZ'
    $lower = 'abcdefghijkmnpqrstuvwxyz'
    $digit = '23456789'
    $symbol = '!@#$%^&*()-_=+'
    $all = $upper + $lower + $digit + $symbol
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        # Uniform 0..max-1 for any alphabet under 256. Single-byte rejection
        # sampling: reject the top partial block so the result is unbiased.
        $idx = {
            param([int]$max)
            $b = New-Object 'byte[]' 1; $cap = 256 - (256 % $max)
            do { $rng.GetBytes($b) } while ($b[0] -ge $cap)
            $b[0] % $max
        }
        $chars = @($upper[(& $idx $upper.Length)], $lower[(& $idx $lower.Length)], $digit[(& $idx $digit.Length)], $symbol[(& $idx $symbol.Length)])
        for ($i = 0; $i -lt 20; $i++) { $chars += $all[(& $idx $all.Length)] }
        # Cryptographic Fisher-Yates shuffle so the required classes are not
        # pinned to the first positions.
        for ($i = $chars.Count - 1; $i -gt 0; $i--) { $j = & $idx ($i + 1); $t = $chars[$i]; $chars[$i] = $chars[$j]; $chars[$j] = $t }
        return -join $chars
    } finally {
        $rng.Dispose()
    }
}

function Set-StrictAcl {
    # Lock the config dir to Administrators + SYSTEM only. Removes the
    # inherited Users-read grant from ProgramData so non-admin local users
    # cannot read the cluster kubeconfig (a bearer token) or the az token cache.
    #
    # icacls is a native binary, so a non-zero exit does NOT raise under
    # $ErrorActionPreference='Stop'. Check $LASTEXITCODE explicitly after
    # each call. A silent /inheritance:r failure here would leave the
    # default ProgramData ACL in place (BUILTIN\Users: ReadAndExecute +
    # Write), and the subsequent /grant would only ADD entries without
    # stripping the inherited Users grant, breaking the at-rest protection
    # the README describes.
    param([string]$Path)
    $inheritOut = & icacls $Path /inheritance:r 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "icacls /inheritance:r failed on ${Path} with exit ${LASTEXITCODE}: $inheritOut"
    }
    $grantOut = & icacls $Path /grant 'Administrators:(OI)(CI)F' 'SYSTEM:(OI)(CI)F' 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "icacls /grant failed on ${Path} with exit ${LASTEXITCODE}: $grantOut"
    }
    Write-Log "Locked ACLs on $Path to Administrators + SYSTEM"
}

function Set-LocalAdminUser {
    # Create or refresh the local admin user the Scheduled Task runs as.
    # Returns the plaintext password (needed for Register-ScheduledTask).
    param([string]$Username, [string]$Password)
    $secure = ConvertTo-SecureString $Password -AsPlainText -Force
    $user = Get-LocalUser -Name $Username -ErrorAction SilentlyContinue
    if ($null -eq $user) {
        Write-Log "Creating local user $Username"
        New-LocalUser -Name $Username -Password $secure -AccountNeverExpires -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
    } else {
        Write-Log "Resetting password on existing local user $Username"
        Set-LocalUser -Name $Username -Password $secure
    }
    $group = Get-LocalGroupMember -Group 'Administrators' -Member $Username -ErrorAction SilentlyContinue
    if ($null -eq $group) {
        Write-Log "Adding $Username to local Administrators group"
        Add-LocalGroupMember -Group 'Administrators' -Member $Username
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

if (-not (Test-Path $ConfigDir)) {
    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
    Write-Log "Created $ConfigDir"
}
Set-StrictAcl -Path $ConfigDir

$workerPath   = Join-Path $ConfigDir 'worker.ps1'
$templatePath = Join-Path $ConfigDir 'aksedge-config.template.json'
$configPath   = Join-Path $ConfigDir 'config.json'
$statePath    = Join-Path $ConfigDir 'state.json'

# Re-init guard. Inspect any existing state.json before resetting state and
# re-registering the task. A bootstrap that is in flight must not be clobbered,
# and one that already succeeded must not be re-run blindly. -Force overrides
# both, destroying any prior state for a deliberate re-bootstrap.
if ((Test-Path $statePath) -and -not $Force) {
    $inFlight = $false
    $alreadyDone = $false
    $existingPhase = $null
    $existingStatus = $null
    try {
        $existing = Get-Content -Raw -Path $statePath | ConvertFrom-Json
        if ($existing.PSObject.Properties.Name -contains 'status') {
            $existingStatus = $existing.status
            if ($existing.status -in @('running', 'pending-reboot')) {
                $inFlight = $true
            } elseif ($existing.status -eq 'succeeded') {
                $alreadyDone = $true
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
    if ($alreadyDone) {
        # Idempotent re-apply. The cluster is already bootstrapped and the
        # state tag already reads succeeded, so the composition wait step
        # passes without any work here. Leave state, the task, and the user
        # untouched. -Force re-bootstraps from scratch.
        Write-Log "Bootstrap already completed (state.json shows phase=$existingPhase status=succeeded). Nothing to do. Pass -Force to re-bootstrap from scratch."
        Write-Output 'ALREADY-BOOTSTRAPPED'
        return
    }
}

Set-Content -Path $workerPath   -Value $EmbeddedWorker   -Encoding UTF8
Set-Content -Path $templatePath -Value $EmbeddedTemplate -Encoding UTF8
Write-Log "Wrote $workerPath and $templatePath"

$runAsSystem = ($RunAsDedicatedAdmin -ine 'true')
$adminPassword = $null
if (-not $runAsSystem) {
    $adminPassword = New-RandomPassword
    Set-LocalAdminUser -Username $LocalAdminUser -Password $adminPassword
    Write-Log "Worker task will run as local admin $LocalAdminUser"
} else {
    Write-Log 'Worker task will run as NT AUTHORITY\SYSTEM (no local account created)'
}

$config = [pscustomobject]@{
    clusterName            = $ClusterName
    resourceGroup          = $ResourceGroup
    subscription           = $Subscription
    location               = $Location
    customLocationsOid     = $CustomLocationsOid
    aksEdgeMsiUrl          = $AksEdgeMsiUrl
    scheduledTaskName      = $ScheduledTaskName
    localAdminUser         = $LocalAdminUser
    runAsSystem            = $runAsSystem
    enableWorkloadIdentity = ($EnableWorkloadIdentity -ieq 'true')
}
$config | ConvertTo-Json | Set-Content -Path $configPath -Encoding UTF8
Write-Log "Wrote $configPath (auth=managed identity, WI=$($EnableWorkloadIdentity -ieq 'true'))"

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

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$workerPath`" -ConfigDir `"$ConfigDir`""

# at-startup trigger handles the Hyper-V reboot resume in Phase 1.
# once-trigger kicks off the initial run without needing a reboot.
$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$onceTrigger    = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddSeconds(30))

# Default: run as the built-in SYSTEM service account (no credential to store).
# -RunAsDedicatedAdmin instead runs as the created local admin (password logon).
if ($runAsSystem) {
    $principal = New-ScheduledTaskPrincipal `
        -UserId 'NT AUTHORITY\SYSTEM' `
        -LogonType ServiceAccount `
        -RunLevel Highest
} else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:COMPUTERNAME\$LocalAdminUser" `
        -LogonType Password `
        -RunLevel Highest
}

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

if ($runAsSystem) {
    Register-ScheduledTask `
        -TaskName $ScheduledTaskName `
        -InputObject $task `
        -Force | Out-Null
} else {
    Register-ScheduledTask `
        -TaskName $ScheduledTaskName `
        -InputObject $task `
        -User "$env:COMPUTERNAME\$LocalAdminUser" `
        -Password $adminPassword `
        -Force | Out-Null
}
Write-Log "Registered Scheduled Task $ScheduledTaskName"

Start-ScheduledTask -TaskName $ScheduledTaskName
Write-Log "Started $ScheduledTaskName"

Write-Output 'REGISTERED'
