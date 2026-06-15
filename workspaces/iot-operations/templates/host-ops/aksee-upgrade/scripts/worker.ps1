<#
.SYNOPSIS
Phase-driven worker that applies an in-place AKS Edge Essentials patch update on
a single-node cluster and verifies the result. Runs on the VM, driven by a
Scheduled Task the launcher registers.

.DESCRIPTION
AKS Edge Essentials has no cloud-driven upgrade channel, so the only way to
drive an upgrade remotely is to invoke the on-box PowerShell cmdlets. This
worker wraps that sequence with idempotency, a pre-upgrade snapshot, a mandatory
verification gate, and a completion tag a siteops `type: wait` step polls.

Scope: patch updates within the current Kubernetes minor version on a
single-node cluster (`Set-AksEdgeUpgrade -AcceptUpgrade $false`).

  Phase 0  Preflight + snapshot. Verify admin, AKS EE installed, single-node
           topology. Install Azure CLI if missing (signature-verified), log in
           as the Arc machine managed identity, set the shared kubeconfig and
           pin the AKS EE kubectl, detect AIO presence, and capture the
           pre-upgrade snapshot (deployed Kubernetes version, host AKS EE
           version, node count, Arc + AIO state).
  Phase 1  Stage. `Set-AksEdgeUpgrade -AcceptUpgrade $false` then
           `Start-AksEdgeUpdate -Force` in a child process. If nothing newer is
           staged, record a no-op and skip the apply.
  Phase 2  Apply. `Import-Module AksEdge -Force` then
           `Start-AksEdgeControlPlaneUpdate -firstControlPlane $true -Force` in a
           child process. The inner Linux node VM reboots, and the cmdlet waits.
  Phase 3  Verify. Re-read the deployed Kubernetes version, `/readyz`, nodes
           Ready, and `Test-AksEdgeArcConnection`. Detect the known Trident/EFI
           finalize failure and surface it as needs-remediation.
  Phase 99 Cleanup. Write the completion tag (`siteops.aksee.upgrade.state`
           plus `appliedVersion`, `fromVersion`, `runId`) and remove the az
           token cache.

The host does not reboot during an AKS EE upgrade (Hyper-V is already enabled
and only the inner node VM restarts), so the worker normally runs straight
through. The at-startup Scheduled Task trigger is kept as a safety net for an
unrelated host reboot, which the phase state machine resumes from.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$ConfigDir
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ConfirmPreference = 'None'
$ProgressPreference = 'SilentlyContinue'

if ($PSVersionTable.PSEdition -ne 'Desktop') {
    throw "worker.ps1 requires Windows PowerShell 5.1 (Desktop). Detected: $($PSVersionTable.PSEdition) $($PSVersionTable.PSVersion). The AksEdge module runs under powershell.exe."
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

$script:StatePath    = Join-Path $ConfigDir 'state.json'
$script:ConfigPath   = Join-Path $ConfigDir 'config.json'
$script:SnapshotPath = Join-Path $ConfigDir 'snapshot.json'

function Write-Log {
    param([string]$Message)
    $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    Write-Host "[$ts] $Message"
}

function Get-State {
    if (-not (Test-Path $script:StatePath)) {
        throw "State file not found at $script:StatePath. The launcher writes the initial state file."
    }
    return Get-Content -Raw -Path $script:StatePath | ConvertFrom-Json
}

function Set-State {
    param(
        [Parameter(Mandatory)] [int]$Phase,
        [Parameter(Mandatory)] [ValidateSet('running', 'pending-reboot', 'succeeded', 'failed')] [string]$Status,
        [string]$ErrorText
    )
    $state = [pscustomobject]@{
        phase       = $Phase
        status      = $Status
        lastUpdated = (Get-Date).ToString('o')
        error       = $ErrorText
    }
    # Atomic write: serialize to a sibling .tmp then Move-Item (atomic on NTFS),
    # so a concurrent reader never sees truncated JSON.
    $tmpPath = "$script:StatePath.tmp"
    $state | ConvertTo-Json | Set-Content -Path $tmpPath -Encoding UTF8
    Move-Item -Path $tmpPath -Destination $script:StatePath -Force
}

function Get-Config {
    if (-not (Test-Path $script:ConfigPath)) {
        throw "Config file not found at $script:ConfigPath. The launcher writes the config from caller-supplied parameters."
    }
    return Get-Content -Raw -Path $script:ConfigPath | ConvertFrom-Json
}

function Get-Prop {
    # StrictMode-safe property read. Returns $Obj.$Name when present, else $Default.
    param($Obj, [string]$Name, $Default = $null)
    if ($null -ne $Obj -and $Obj.PSObject.Properties.Name -contains $Name) { return $Obj.$Name }
    return $Default
}

function Test-IsAdmin {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-AksEdgeModuleInstalled {
    return $null -ne (Get-Module -ListAvailable -Name AksEdge)
}

function Get-AksEeHostVersion {
    # Installed host AKS EE package/module version. A staging clue only, NOT the
    # source of truth for whether the cluster is upgraded (the deployed node
    # Kubernetes version is). Returns a string or $null.
    $mod = Get-Module -ListAvailable -Name AksEdge | Sort-Object Version -Descending | Select-Object -First 1
    if ($null -eq $mod) { return $null }
    return $mod.Version.ToString()
}

function Assert-MicrosoftSignedFile {
    # Authenticode-verify a downloaded installer before running it.
    param([string]$Path)
    $sig = Get-AuthenticodeSignature -FilePath $Path
    if ($sig.Status -ne 'Valid') {
        throw "Authenticode check failed for ${Path}: status=$($sig.Status) ($($sig.StatusMessage))."
    }
    if ($sig.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
        throw "Unexpected signer for ${Path}: $($sig.SignerCertificate.Subject). Expected O=Microsoft Corporation."
    }
}

function Install-AzCliIfMissing {
    # The verify gate and the tag write need az. A bootstrapped host already has
    # it, but install (signature-verified) if missing so the worker is
    # self-contained against an arbitrary Arc host.
    if (Get-Command az -ErrorAction SilentlyContinue) {
        Write-Log 'az CLI already on PATH'
        return
    }
    $msiUrl  = 'https://aka.ms/installazurecliwindowsx64'
    $msiPath = Join-Path $ConfigDir 'azure-cli.msi'
    $log     = Join-Path $ConfigDir 'az-msiexec.log'
    Write-Log "az CLI not on PATH. Downloading MSI from $msiUrl"
    Invoke-WebRequest -Uri $msiUrl -OutFile $msiPath -UseBasicParsing
    Assert-MicrosoftSignedFile -Path $msiPath
    Write-Log "Installing az CLI MSI via msiexec /quiet, log at $log"
    $proc = Start-Process msiexec.exe -Wait -PassThru -ArgumentList @(
        '/i', $msiPath, '/quiet', '/norestart', '/L*V', $log
    )
    if ($proc.ExitCode -ne 0) {
        throw "az CLI MSI install failed with exit $($proc.ExitCode). See $log."
    }
    $azDir = Join-Path ${env:ProgramFiles} 'Microsoft SDKs\Azure\CLI2\wbin'
    if (Test-Path $azDir) { $env:PATH = "$azDir;$env:PATH" }
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
        throw "az CLI still not on PATH after install. Checked $azDir."
    }
    Remove-Item -Path $msiPath -Force -ErrorAction SilentlyContinue
}

function Connect-MachineIdentity {
    # Authenticate as the Arc machine's system-assigned managed identity. HIMDS
    # only releases the token to a local administrator, which the SYSTEM task
    # satisfies. Refresh the HIMDS endpoints from Machine scope defensively in
    # case a fresh worker process did not inherit them.
    param($config)
    foreach ($name in @('IDENTITY_ENDPOINT', 'IMDS_ENDPOINT')) {
        if (-not [Environment]::GetEnvironmentVariable($name)) {
            $machineVal = [Environment]::GetEnvironmentVariable($name, 'Machine')
            if ($machineVal) { Set-Item -Path "Env:$name" -Value $machineVal }
        }
    }
    Write-Log 'Authenticating with Arc machine managed identity (az login --identity)'
    $loginOut = & az login --identity --only-show-errors 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "az login --identity failed: $loginOut. Ensure the Arc machine identity has a role on the resource group (Contributor, or Kubernetes Cluster - Azure Arc Onboarding plus Tag Contributor)."
    }
    $sub = $config.subscription
    $accountSetOut = & az account set --subscription $sub 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "az account set --subscription $sub failed: $accountSetOut. The Arc machine managed identity likely lacks access to subscription $sub."
    }
}

function Set-WorkerKubeconfig {
    # Point kubectl at the cluster kubeconfig and pin the AKS EE kubectl so the
    # worker does not depend on a particular Windows user's profile. The
    # bootstrap copies the cluster kubeconfig into its own ConfigDir and PURGES
    # the systemprofile copy in its Phase 99, so on a SYSTEM-deployed host that
    # retained copy at `aksee-bootstrap\kubeconfig` is the only kubeconfig left.
    # Check it (the standard bootstrap ConfigDir, and the sibling of this
    # ConfigDir under the same parent) before the systemprofile fallbacks, which
    # only exist on a host whose bootstrap has not finalized.
    $kubeCandidates = @(
        (Join-Path $ConfigDir 'kubeconfig'),
        (Join-Path (Split-Path $ConfigDir -Parent) 'aksee-bootstrap\kubeconfig'),
        'C:\ProgramData\siteops\aksee-bootstrap\kubeconfig',
        (Join-Path $env:USERPROFILE '.kube\config'),
        (Join-Path $env:SystemRoot 'System32\config\systemprofile\.kube\config'),
        (Join-Path $env:SystemRoot 'SysWOW64\config\systemprofile\.kube\config')
    )
    $kubeconfig = $kubeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $kubeconfig) {
        throw "No kubeconfig found. Checked: $($kubeCandidates -join '; '). Is the AKS EE cluster deployed on this host?"
    }
    $env:KUBECONFIG = $kubeconfig
    $akseeKubectl = Join-Path ${env:ProgramFiles} 'AksEdge\kubectl\kubectl.exe'
    if (Test-Path $akseeKubectl) { $env:KUBECTL_CLIENT_PATH = $akseeKubectl }
    Write-Log "Using kubeconfig $kubeconfig"
    return $kubeconfig
}

function Invoke-Kubectl {
    # Prefer the AKS EE kubectl, fall back to kubectl on PATH. Returns stdout.
    # Check $LASTEXITCODE at the call site.
    param([string[]]$KubectlArgs)
    $exe = if ($env:KUBECTL_CLIENT_PATH -and (Test-Path $env:KUBECTL_CLIENT_PATH)) { $env:KUBECTL_CLIENT_PATH } else { 'kubectl' }
    return & $exe @KubectlArgs 2>&1
}

function Get-DeployedK8sVersion {
    # Source-of-truth version: the Kubernetes version reported by the control
    # plane node, NOT the host AKS EE module version. Returns e.g. 'v1.30.6+k3s1'
    # or $null when unreadable.
    $json = Invoke-Kubectl @('get', 'nodes', '-o', 'json')
    if ($LASTEXITCODE -ne 0) { return $null }
    try {
        $obj = ($json -join "`n") | ConvertFrom-Json
        $node = $obj.items | Select-Object -First 1
        return $node.status.nodeInfo.kubeletVersion
    } catch {
        return $null
    }
}

function Get-NodeCount {
    $json = Invoke-Kubectl @('get', 'nodes', '-o', 'json')
    if ($LASTEXITCODE -ne 0) { return 0 }
    try {
        $obj = ($json -join "`n") | ConvertFrom-Json
        return @($obj.items).Count
    } catch {
        return 0
    }
}

function Test-AioPresent {
    # Detect actual AIO presence (not config intent): the azure-iot-operations
    # namespace exists on the cluster. Informational only. The worker does not
    # verify AIO health.
    $null = Invoke-Kubectl @('get', 'namespace', 'azure-iot-operations')
    return $LASTEXITCODE -eq 0
}

function Test-NodesReady {
    # All nodes report Ready. /readyz on the apiserver plus node conditions.
    $ready = Invoke-Kubectl @('get', '--raw=/readyz')
    if ($LASTEXITCODE -ne 0 -or (($ready -join '') -notmatch 'ok')) { return $false }
    $json = Invoke-Kubectl @('get', 'nodes', '-o', 'json')
    if ($LASTEXITCODE -ne 0) { return $false }
    try {
        $obj = ($json -join "`n") | ConvertFrom-Json
        foreach ($n in $obj.items) {
            $readyCond = $n.status.conditions | Where-Object { $_.type -eq 'Ready' } | Select-Object -First 1
            if ($null -eq $readyCond -or $readyCond.status -ne 'True') { return $false }
        }
        return $true
    } catch {
        return $false
    }
}

function Invoke-ChildAksEeCommand {
    # Run an AksEdge cmdlet in a fresh child powershell.exe. The AKS EE module
    # resets $ErrorActionPreference='Stop' in its own scope, turning the native
    # helpers' diagnostic stderr into terminating errors under the worker's
    # strict settings. A child process with default settings runs the cmdlet in
    # the environment the module was tested against. Returns the child log path.
    # Throws on non-zero exit with a tail of stdout and stderr.
    param(
        [Parameter(Mandatory)] [string]$Label,
        [Parameter(Mandatory)] [string]$Script
    )
    $childScript = "$Script; exit `$LASTEXITCODE"
    $bytes   = [System.Text.Encoding]::Unicode.GetBytes($childScript)
    $encoded = [Convert]::ToBase64String($bytes)
    $psExe   = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $stamp   = Get-Date -Format 'yyyyMMdd-HHmmss'
    $childLog    = Join-Path $ConfigDir ("aksee-{0}-{1}.log" -f $Label, $stamp)
    $childErrLog = "$childLog.err"
    Write-Log "Running $Label in child PowerShell. stdout=$childLog stderr=$childErrLog"
    $proc = Start-Process -FilePath $psExe `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded) `
        -Wait -PassThru -NoNewWindow `
        -RedirectStandardOutput $childLog -RedirectStandardError $childErrLog
    Write-Log "$Label child exited with code $($proc.ExitCode)"
    if ($proc.ExitCode -ne 0) {
        $tailOut = if (Test-Path $childLog)    { (Get-Content $childLog    -Tail 40 -ErrorAction SilentlyContinue) -join "`n" } else { '' }
        $tailErr = if (Test-Path $childErrLog) { (Get-Content $childErrLog -Tail 40 -ErrorAction SilentlyContinue) -join "`n" } else { '' }
        $err = "$Label exited with code $($proc.ExitCode).`nstdout tail:`n$tailOut`nstderr tail:`n$tailErr`nFull logs at $childLog and $childErrLog."
        # Surface the known Trident/EFI finalize failure as a distinct, operator-
        # actionable signal rather than a generic phase failure.
        if (("$tailOut`n$tailErr") -match 'bootx64\.efi|trident|/EFI/AZLB') {
            throw "TRIDENT-REMEDIATION-REQUIRED: $err"
        }
        throw $err
    }
    return $childLog
}

function Invoke-ChildCheck {
    # Run a boolean-check script in a fresh child powershell.exe (EAP-safe, like
    # the update cmdlets) and return its exit code WITHOUT throwing. The script
    # is responsible for `exit 0` (true) / `exit 1` (false). Use for checks where
    # a non-zero result is an expected negative, not an error. Note that AksEdge
    # cmdlets like Test-AksEdgeArcConnection return a value rather than setting
    # $LASTEXITCODE, so the script must translate the return into an exit code.
    param(
        [Parameter(Mandatory)] [string]$Label,
        [Parameter(Mandatory)] [string]$Script
    )
    $bytes   = [System.Text.Encoding]::Unicode.GetBytes($Script)
    $encoded = [Convert]::ToBase64String($bytes)
    $psExe   = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $proc = Start-Process -FilePath $psExe `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded) `
        -Wait -PassThru -NoNewWindow
    Write-Log "$Label child exited with code $($proc.ExitCode)"
    return $proc.ExitCode
}

function Test-ArcConnectedChild {
    # Test-AksEdgeArcConnection returns a boolean (and may emit diagnostic stderr
    # under the worker's strict settings), so run it in a child process and map
    # the return to an exit code.
    param([string]$Label)
    $exit = Invoke-ChildCheck -Label $Label -Script 'Import-Module AksEdge -Force; if (Test-AksEdgeArcConnection) { exit 0 } else { exit 1 }'
    return ($exit -eq 0)
}

function Write-UpgradeStateTag {
    # Generation-aware tag write on this Arc machine resource. Phase 99 writes
    # 'succeeded' with the applied/from versions and the run id. The per-phase
    # catch writes 'failed-phase-N' (or 'failed-needs-remediation' for the
    # Trident case). A siteops `type: wait` step polls siteops.aksee.upgrade.state
    # to gate downstream steps. Safe to call before az is authenticated: logs
    # and returns without throwing. Requires Microsoft.Resources/tags/write on
    # the Arc machine resource. Assumes the resource name equals COMPUTERNAME.
    param(
        [Parameter(Mandatory)] $config,
        [Parameter(Mandatory)] [string]$Value,
        [string]$AppliedVersion,
        [string]$FromVersion
    )
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
        Write-Log 'Skipping upgrade-state tag write: az CLI not installed.'
        return
    }
    $sub  = $config.subscription
    $rg   = $config.resourceGroup
    $name = $env:COMPUTERNAME
    if (-not $sub -or -not $rg -or -not $name) {
        Write-Log 'Skipping upgrade-state tag write: missing subscription / resourceGroup / COMPUTERNAME.'
        return
    }
    $runId = [string](Get-Prop $config 'runId' '')
    $tags = @("siteops.aksee.upgrade.state=$Value", "siteops.aksee.upgrade.runId=$runId")
    if ($AppliedVersion) { $tags += "siteops.aksee.upgrade.appliedVersion=$AppliedVersion" }
    if ($FromVersion)    { $tags += "siteops.aksee.upgrade.fromVersion=$FromVersion" }
    $arcId = "/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.HybridCompute/machines/$name"
    $tagOut = & az tag update --resource-id $arcId --operation merge --tags @tags --only-show-errors 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Log "WARNING: tag write failed on $arcId (exit $LASTEXITCODE): $tagOut. See README Prerequisites for the required Microsoft.Resources/tags/write grant."
        return
    }
    Write-Log "Wrote tag siteops.aksee.upgrade.state=$Value (runId=$runId appliedVersion=$AppliedVersion) on $arcId"
}

function Wait-Until {
    # Poll a boolean scriptblock until it returns true or the attempt budget is
    # spent. The node VM restarts during apply and Arc transiently disconnects
    # while it comes back, so the post-update verify checks need to tolerate a
    # settling window rather than fail on the first transient negative. Mirrors
    # the bootstrap's Wait-ArcClusterReady budget (about 10 minutes).
    param(
        [Parameter(Mandatory)] [string]$Label,
        [Parameter(Mandatory)] [scriptblock]$Condition,
        [int]$RetrySeconds = 15,
        [int]$MaxRetries   = 40
    )
    for ($i = 1; $i -le $MaxRetries; $i++) {
        if (& $Condition) {
            Write-Log "$Label satisfied (attempt $i/$MaxRetries)."
            return $true
        }
        if ($i -lt $MaxRetries) {
            Write-Log "$Label not satisfied yet (attempt $i/$MaxRetries). Retrying in ${RetrySeconds}s."
            Start-Sleep -Seconds $RetrySeconds
        }
    }
    Write-Log "$Label not satisfied within $($MaxRetries * $RetrySeconds)s."
    return $false
}

# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

function Invoke-Phase0 {
    param($config)
    Write-Log 'Phase 0: preflight + pre-upgrade snapshot'

    if (-not (Test-IsAdmin)) { throw 'Worker must run as Administrator (or SYSTEM).' }
    if (-not (Test-AksEdgeModuleInstalled)) {
        throw 'AKS Edge Essentials is not installed on this host. The upgrade worker targets an existing AKS EE cluster.'
    }

    Install-AzCliIfMissing
    Connect-MachineIdentity -config $config

    # Mark in-progress so a stale tag from a previous run cannot pass the wait
    # gate before this run finishes.
    try { Write-UpgradeStateTag -config $config -Value 'running' } catch { Write-Log "WARNING: in-progress tag write failed: $_" }

    $kubeconfig = Set-WorkerKubeconfig

    $nodeCount = Get-NodeCount
    if ($nodeCount -ne 1) {
        throw "Expected a single-node AKS EE cluster, found $nodeCount nodes. This worker supports single-node clusters only."
    }

    $fromK8s  = Get-DeployedK8sVersion
    $fromHost = Get-AksEeHostVersion
    $aio      = Test-AioPresent
    $arcConnected = Test-ArcConnectedChild -Label 'arc-check-pre'

    $snapshot = [pscustomobject]@{
        capturedAt      = (Get-Date).ToString('o')
        fromK8sVersion  = $fromK8s
        fromHostVersion = $fromHost
        nodeCount       = $nodeCount
        aioPresent      = $aio
        arcConnected    = $arcConnected
        kubeconfig      = $kubeconfig
    }
    $snapshot | ConvertTo-Json | Set-Content -Path $script:SnapshotPath -Encoding UTF8
    Write-Log "Snapshot: K8s=$fromK8s hostVersion=$fromHost nodes=$nodeCount aio=$aio arcConnected=$arcConnected"

    Set-State -Phase 1 -Status 'running'
    Write-Log 'Phase 0: complete'
}

function Invoke-Phase1 {
    param($config)
    Write-Log 'Phase 1: stage AKS EE patch update'

    $hostBefore = Get-AksEeHostVersion

    # AcceptUpgrade $false keeps the Kubernetes minor version fixed (patch-only).
    $allowMinor = [bool](Get-Prop $config 'allowKubernetesMinorUpgrade' $false)
    if ($allowMinor) {
        throw 'allowKubernetesMinorUpgrade is true. This worker applies patch updates only and does not support minor-version upgrades.'
    }
    Invoke-ChildAksEeCommand -Label 'set-upgrade'   -Script 'Import-Module AksEdge -Force; Set-AksEdgeUpgrade -AcceptUpgrade $false' | Out-Null
    $stageLog = Invoke-ChildAksEeCommand -Label 'stage-update' -Script 'Import-Module AksEdge -Force; Start-AksEdgeUpdate -Force'

    $hostAfter = Get-AksEeHostVersion
    $stageText = if (Test-Path $stageLog) { (Get-Content $stageLog -Raw -ErrorAction SilentlyContinue) } else { '' }
    # Detect whether a newer package was staged: a bumped host version, or an
    # output that does not read as already up to date. Used to skip the apply
    # when there is nothing to do.
    $stagedSomething = ($hostBefore -ne $hostAfter) -or ($stageText -notmatch '(?i)up.to.date|no update|already.*latest|nothing to')

    if (-not $stagedSomething) {
        Write-Log "Phase 1: no newer AKS EE update available (host version $hostBefore unchanged). Skipping apply."
        Set-State -Phase 3 -Status 'running'
    } else {
        Write-Log "Phase 1: update staged (host version $hostBefore -> $hostAfter). Proceeding to apply."
        Set-State -Phase 2 -Status 'running'
    }
    Write-Log 'Phase 1: complete'
}

function Invoke-Phase2 {
    param($config)
    Write-Log 'Phase 2: apply control-plane update (single-node)'

    # Module reload is required between stage and apply. The inner node VM
    # reboots during this call, and the cmdlet waits for it to come back.
    Invoke-ChildAksEeCommand -Label 'apply-update' `
        -Script 'Import-Module AksEdge -Force; Start-AksEdgeControlPlaneUpdate -firstControlPlane $true -Force' | Out-Null

    Set-State -Phase 3 -Status 'running'
    Write-Log 'Phase 2: complete'
}

function Invoke-Phase3 {
    param($config)
    Write-Log 'Phase 3: verify upgrade'

    # The kubeconfig and az login from Phase 0 may belong to a prior worker
    # invocation (e.g. host reboot resume), so re-establish both defensively.
    Connect-MachineIdentity -config $config
    $null = Set-WorkerKubeconfig

    # The node VM restarts during apply, so poll for the cluster to settle rather
    # than fail on the first transient negative.
    if (-not (Wait-Until -Label 'cluster nodes Ready' -Condition { Test-NodesReady })) {
        throw 'Verification failed: cluster nodes did not return Ready (/readyz or node conditions) within the verification window after the update.'
    }

    $deployed = Get-DeployedK8sVersion
    Write-Log "Deployed Kubernetes version after update: $deployed"

    # Arc transiently disconnects while the node VM restarts, so poll the Arc
    # connection through the reconnect window before declaring a regression.
    if (-not (Wait-Until -Label 'Arc connection' -Condition { Test-ArcConnectedChild -Label 'arc-check-post' })) {
        throw 'Verification failed: Test-AksEdgeArcConnection did not report the cluster Arc-connected within the verification window after the update.'
    }
    Write-Log 'Arc connection verified after update'

    Set-State -Phase 99 -Status 'running'
    Write-Log 'Phase 3: complete (verification passed)'
}

function Invoke-Phase99 {
    param($config)
    Write-Log 'Phase 99: finalize'

    $snapshot = if (Test-Path $script:SnapshotPath) { Get-Content -Raw -Path $script:SnapshotPath | ConvertFrom-Json } else { $null }
    $fromVersion = [string](Get-Prop $snapshot 'fromK8sVersion' '')
    $appliedVersion = ''
    try {
        $null = Set-WorkerKubeconfig
        $appliedVersion = [string](Get-DeployedK8sVersion)
    } catch {
        Write-Log "WARNING: could not read deployed version for the tag: $_"
    }

    # Write the success tag first, while the managed-identity login is still
    # valid, then clean up.
    try {
        Write-UpgradeStateTag -config $config -Value 'succeeded' -AppliedVersion $appliedVersion -FromVersion $fromVersion
    } catch {
        Write-Log "WARNING: tag write helper threw: $_. Non-fatal."
    }

    # Remove the scoped az token cache. The tag write above was the last az call.
    if ($env:AZURE_CONFIG_DIR -and (Test-Path $env:AZURE_CONFIG_DIR)) {
        Remove-Item -Path $env:AZURE_CONFIG_DIR -Recurse -Force -ErrorAction SilentlyContinue
        Write-Log "Removed az token cache at $env:AZURE_CONFIG_DIR"
    }

    Set-State -Phase 99 -Status 'succeeded'
    Write-Log "Phase 99: complete. Upgrade finished. fromVersion=$fromVersion appliedVersion=$appliedVersion"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if (-not (Test-Path $ConfigDir)) {
    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
}

# Scope the az config and token cache into the ACL-locked ConfigDir. Under
# SYSTEM the default ~/.azure lands in the shared systemprofile, readable by any
# SYSTEM-context process. Phase 99 removes this on success.
$env:AZURE_CONFIG_DIR = Join-Path $ConfigDir '.azure'

$logPath = Join-Path $ConfigDir "worker-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
Start-Transcript -Path $logPath -Append | Out-Null
try {
    Write-Log "Upgrade worker started. ConfigDir=$ConfigDir Log=$logPath"

    while ($true) {
        $state  = Get-State
        $config = Get-Config
        $startPhase = $state.phase
        Write-Log "Resuming at phase=$startPhase status=$($state.status)"

        try {
            # Phases 0-3 are sequential work. 99 is the terminal finalize phase.
            switch ($state.phase) {
                0  { Invoke-Phase0  -config $config }
                1  { Invoke-Phase1  -config $config }
                2  { Invoke-Phase2  -config $config }
                3  { Invoke-Phase3  -config $config }
                99 { Invoke-Phase99 -config $config }
                default { throw "Unknown phase: $($state.phase)" }
            }
        } catch {
            $errText = $_.ToString()
            Write-Log "ERROR in phase ${startPhase}: $errText"
            Set-State -Phase $startPhase -Status 'failed' -ErrorText $errText
            # The known Trident/EFI finalize failure needs a human at the box, so
            # surface it as a distinct tag value the wait step can route on.
            $tagValue = if ($errText -match 'TRIDENT-REMEDIATION-REQUIRED') { 'failed-needs-remediation' } else { "failed-phase-$startPhase" }
            try {
                Write-UpgradeStateTag -config $config -Value $tagValue
            } catch {
                Write-Log "WARNING: tag write helper threw on failure path: $_. Original phase error re-raised below."
            }
            throw
        }

        $newState = Get-State
        if ($newState.phase -eq 99 -and $newState.status -eq 'succeeded') {
            Write-Log 'Upgrade complete.'
            break
        }
        if ($newState.status -eq 'pending-reboot') {
            Write-Log 'Pending reboot. Worker exits.'
            break
        }
        if ($newState.phase -eq $startPhase) {
            Write-Log "Phase $startPhase did not advance. Stopping cascade to avoid infinite loop."
            break
        }
    }
} finally {
    Stop-Transcript | Out-Null
}
