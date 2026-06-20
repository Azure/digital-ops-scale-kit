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

Two modes are supported, controlled by `allowKubernetesMinorUpgrade` in config:
- Patch mode (false, default): one hop. `AcceptUpgrade` stays false. No
  Kubernetes minor version change.
- Minor mode (true): sequential multi-hop loop (Phase 1 -> 2 -> 3 -> 1 ...),
  each hop advancing one Kubernetes minor version. `AcceptUpgrade` is set true
  for this run only and re-pinned false on success. A failed run leaves it set so
  the staged update-cache survives for a re-deploy to resume. An optional
  `targetKubernetesVersion` config field stops the loop when the target minor is
  reached. Hop progress is tracked in `progress.json`.

  Phase 0  Preflight + snapshot. Verify admin, AKS EE installed, single-node
           topology. Install Azure CLI if missing (signature-verified), log in
           as the Arc machine managed identity, set the shared kubeconfig and
           pin the AKS EE kubectl, detect AIO presence, and capture the
           pre-upgrade snapshot (deployed Kubernetes version, host AKS EE
           version, node count, Arc + AIO state). Validate the target version
           if set. Initialize `progress.json`. Set `AcceptUpgrade` for the run.
  Phase 1  Stage one hop. Check whether the target minor is already met. If not,
           stage the next AKS EE update from Microsoft Update via `Invoke-OnlineStage`
           (a Windows Update scan, download, and install that self-extracts into
           the update-cache), then install the cached MSI with `Start-AksEdgeUpdate
           -Force`. Persist hop progress and proceed to Phase 2. When Microsoft
           Update offers nothing, finalize.
  Phase 2  Apply. `Import-Module AksEdge -Force` then
           `Start-AksEdgeControlPlaneUpdate -firstControlPlane $true -Force` in a
           child process. The inner Linux node VM reboots, and the cmdlet waits.
  Phase 3  Verify hop + decide. Re-read the deployed Kubernetes version,
           `/readyz`, nodes Ready, and `Test-AksEdgeArcConnection`. Increment hop
           count. Decide: target reached -> Phase 99, patch mode -> Phase 99,
           max hops exceeded -> throw, else loop back to Phase 1.
  Phase 99 Cleanup. Re-pin `AcceptUpgrade $false` (best-effort). Write the
           completion tag (`siteops.aksee.upgrade.state` plus `appliedVersion`,
           `fromVersion`, `hopCount`, `runId`) and remove the az token cache.

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
$script:ProgressPath = Join-Path $ConfigDir 'progress.json'

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
    # satisfies. Refresh the HIMDS endpoints from Machine scope in case a fresh
    # worker process did not inherit them. Retry the login: right after a host
    # reboot (the at-startup resume path) HIMDS can be slow to come up, and a
    # partial token cache from an interrupted run can make az fail, so reset the
    # scoped cache between attempts. The az calls are wrapped because az can
    # surface a transient failure as a terminating error under the strict settings.
    param($config)
    foreach ($name in @('IDENTITY_ENDPOINT', 'IMDS_ENDPOINT')) {
        if (-not [Environment]::GetEnvironmentVariable($name)) {
            $machineVal = [Environment]::GetEnvironmentVariable($name, 'Machine')
            if ($machineVal) { Set-Item -Path "Env:$name" -Value $machineVal }
        }
    }
    $sub = $config.subscription
    $lastErr = ''
    for ($a = 1; $a -le 6; $a++) {
        Write-Log "Authenticating with Arc machine managed identity (az login --identity), attempt $a/6"
        try {
            $loginOut = & az login --identity --only-show-errors 2>&1
            if ($LASTEXITCODE -eq 0) {
                $setOut = & az account set --subscription $sub 2>&1
                if ($LASTEXITCODE -eq 0) { Write-Log 'Managed-identity login established.'; return }
                $lastErr = "az account set failed: $setOut"
            } else {
                $lastErr = "az login --identity failed: $loginOut"
            }
        } catch {
            $lastErr = "az invocation error: $_"
        }
        Write-Log "Managed-identity auth attempt $a/6 failed: $lastErr"
        # Reset the scoped token cache so a partial or corrupt state from an
        # interrupted run does not poison the retry, then let HIMDS settle.
        if ($env:AZURE_CONFIG_DIR -and (Test-Path $env:AZURE_CONFIG_DIR)) {
            Remove-Item -Path $env:AZURE_CONFIG_DIR -Recurse -Force -ErrorAction SilentlyContinue
        }
        if ($a -lt 6) { Start-Sleep -Seconds 30 }
    }
    throw "Managed-identity authentication failed after 6 attempts: $lastErr. Ensure the Arc machine identity has a role on the resource group (Contributor, or Kubernetes Cluster - Azure Arc Onboarding plus Tag Contributor) and that the Connected Machine Agent is running."
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
    # Cap each call so a hung apiserver cannot stall a Wait-Until attempt past its
    # wall-clock budget. The flag is global, so it is safe on every verb.
    return & $exe @KubectlArgs --request-timeout=10s 2>&1
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
    # All nodes report Ready. /readyz must return exactly 'ok', every node
    # condition Ready=True, and there must be at least one node (an empty list is
    # not Ready).
    $ready = Invoke-Kubectl @('get', '--raw=/readyz')
    if ($LASTEXITCODE -ne 0 -or (($ready -join '').Trim() -ne 'ok')) { return $false }
    $json = Invoke-Kubectl @('get', 'nodes', '-o', 'json')
    if ($LASTEXITCODE -ne 0) { return $false }
    try {
        $obj = ($json -join "`n") | ConvertFrom-Json
        $nodes = @($obj.items)
        if ($nodes.Count -lt 1) { return $false }
        foreach ($n in $nodes) {
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
    # The caller's script owns its exit code, translating the AKS EE cmdlet's
    # boolean (or string) return into 0 for success or non-zero for failure. These
    # cmdlets do not set $LASTEXITCODE, so we must not rely on it here.
    $childScript = $Script
    $bytes   = [System.Text.Encoding]::Unicode.GetBytes($childScript)
    $encoded = [Convert]::ToBase64String($bytes)
    $psExe   = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $stamp   = Get-Date -Format 'yyyyMMdd-HHmmss'
    $childLog    = Join-Path $ConfigDir ("aksee-{0}-{1}.log" -f $Label, $stamp)
    $childErrLog = "$childLog.err"
    Write-Log "Running $Label in child PowerShell. stdout=$childLog stderr=$childErrLog"
    $proc = Start-Process -FilePath $psExe `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded) `
        -PassThru -NoNewWindow `
        -RedirectStandardOutput $childLog -RedirectStandardError $childErrLog
    # Cache the native handle so $proc.ExitCode is still readable after WaitForExit.
    # Without -Wait, Start-Process -PassThru releases the handle for a fast-exiting
    # child, and ExitCode then reads $null, which would misclassify a clean exit 0
    # as a failure. Guarded because accessing Handle can throw if the child already
    # exited.
    try { $null = $proc.Handle } catch {}
    $timeoutMs = 60 * 60 * 1000
    $exited = $proc.WaitForExit($timeoutMs)
    if (-not $exited) {
        try { $proc.Kill() } catch {}
        throw "$Label child did not exit within 60 minutes and was killed. Full logs at $childLog and $childErrLog."
    }
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

function Test-ArcConnectedChild {
    # Test-AksEdgeArcConnection returns a boolean (and may emit diagnostic stderr
    # under the worker's strict settings), so run it in a child process. The child
    # maps the boolean to an exit code, and Invoke-ChildAksEeCommand throws on a
    # non-zero exit, so a not-connected result (exit 1) surfaces as a caught
    # exception that we translate back to $false.
    param([string]$Label)
    try {
        Invoke-ChildAksEeCommand -Label $Label -Script 'Import-Module AksEdge -Force; if (Test-AksEdgeArcConnection) { exit 0 } else { exit 1 }' | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Write-UpgradeStateTag {
    # Write the upgrade-state tag on this Arc machine resource. Phase 99 writes
    # 'succeeded' with the applied/from versions and the run id. The per-phase
    # catch writes 'failed-phase-N' (or 'failed-needs-remediation' for the
    # Trident case). A siteops `type: wait` step polls siteops.aksee.upgrade.state
    # to gate downstream steps. Safe to call before az is authenticated: logs
    # and returns without throwing. Requires Microsoft.Resources/tags/write on
    # the Arc machine resource, which is named by config.machineName.
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
    # The Arc machine resource name, which the wait step also targets. Falls back
    # to the hostname only when the launcher did not pass a machine name.
    $name = [string](Get-Prop $config 'machineName' $env:COMPUTERNAME)
    if (-not $name) { $name = $env:COMPUTERNAME }
    if (-not $sub -or -not $rg -or -not $name) {
        Write-Log 'Skipping upgrade-state tag write: missing subscription / resourceGroup / machine name.'
        return
    }
    $runId = [string](Get-Prop $config 'runId' '')
    $tags = @("siteops.aksee.upgrade.state=$Value", "siteops.aksee.upgrade.runId=$runId")
    if ($AppliedVersion) { $tags += "siteops.aksee.upgrade.appliedVersion=$AppliedVersion" }
    if ($FromVersion)    { $tags += "siteops.aksee.upgrade.fromVersion=$FromVersion" }
    $arcId = "/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.HybridCompute/machines/$name"
    # Retry transient tag-write failures. The wait step gates on this tag, so a
    # terminal write that never lands would hang the deploy until its timeout.
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $tagOut = & az tag update --resource-id $arcId --operation merge --tags @tags --only-show-errors 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Log "Wrote tag siteops.aksee.upgrade.state=$Value (runId=$runId appliedVersion=$AppliedVersion) on $arcId"
            return
        }
        Write-Log "WARNING: tag write attempt $attempt/3 failed on $arcId (exit $LASTEXITCODE): $tagOut"
        if ($attempt -lt 3) { Start-Sleep -Seconds 5 }
    }
    Write-Log "WARNING: tag write did not succeed after 3 attempts on $arcId. See README Prerequisites for the required Microsoft.Resources/tags/write grant."
}

function Wait-Until {
    # Poll a boolean scriptblock until it returns true or a wall-clock deadline
    # passes. The node VM restarts during apply and Arc transiently disconnects
    # while it comes back, so the post-update verify checks need to tolerate a
    # settling window rather than fail on the first transient negative. The
    # deadline is wall-clock (not an attempt count) so a slow condition eval
    # cannot push the total past the intended budget. Mirrors the bootstrap's
    # Wait-ArcClusterReady budget (about 10 minutes).
    param(
        [Parameter(Mandatory)] [string]$Label,
        [Parameter(Mandatory)] [scriptblock]$Condition,
        [int]$RetrySeconds   = 15,
        [int]$TimeoutSeconds = 600
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $attempt = 0
    while ($true) {
        $attempt++
        if (& $Condition) {
            Write-Log "$Label satisfied (attempt $attempt)."
            return $true
        }
        if ((Get-Date) -ge $deadline) {
            Write-Log "$Label not satisfied within ${TimeoutSeconds}s ($attempt attempts)."
            return $false
        }
        Write-Log "$Label not satisfied yet (attempt $attempt). Retrying in ${RetrySeconds}s."
        Start-Sleep -Seconds $RetrySeconds
    }
}

# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------

function Get-K8sMinor {
    # Parse a kubelet version string or a target version string to 'MAJOR.MINOR'.
    # Accepts formats like 'v1.31.6+k3s1', '1.32', 'v1.33.5+k3s1'. Strips the
    # leading 'v', drops the patch and build suffix, and returns the first two
    # dot-segments. Returns $null if the input is empty or unparseable.
    param([string]$Version)
    if (-not $Version) { return $null }
    $v = $Version.TrimStart('v') -replace '\+.*$', ''
    $parts = $v -split '\.'
    if ($parts.Count -lt 2) { return $null }
    $major = $parts[0]; $minor = $parts[1]
    if ($major -notmatch '^\d+$' -or $minor -notmatch '^\d+$') { return $null }
    return "$major.$minor"
}

function Compare-K8sMinor {
    # Compare two 'MAJOR.MINOR' strings numerically.
    # Returns a negative int when A < B, 0 when equal, positive when A > B.
    param([string]$A, [string]$B)
    $ap = $A -split '\.'; $bp = $B -split '\.'
    $md = [int]$ap[0] - [int]$bp[0]
    if ($md -ne 0) { return $md }
    return [int]$ap[1] - [int]$bp[1]
}

function Set-AcceptUpgrade {
    # Set `Set-AksEdgeUpgrade -AcceptUpgrade` in a child process. Minor mode passes
    # $true to allow the next Kubernetes minor version hop. The gate is re-pinned to
    # $false in Phase 99 on success. A failed run leaves it $true so the staged
    # update-cache survives for a re-deploy to resume (the cmdlet wipes the cache
    # whenever the flag value changes).
    param([bool]$Accept)
    $val = if ($Accept) { '$true' } else { '$false' }
    # Set-AksEdgeUpgrade returns the string 'OK' on success and never sets
    # $LASTEXITCODE. Capture the last pipeline value and translate it to an exit
    # code the child wrapper can classify.
    $script = "Import-Module AksEdge -Force; `$r = @(Set-AksEdgeUpgrade -AcceptUpgrade $val); if (`$r[-1] -eq 'OK') { exit 0 } else { Write-Output `$r[-1]; exit 1 }"
    Invoke-ChildAksEeCommand -Label 'set-accept-upgrade' -Script $script | Out-Null
}

function Get-Progress {
    # Read the hop-progress file. Returns $null when the file does not exist yet.
    if (-not (Test-Path $script:ProgressPath)) { return $null }
    return Get-Content -Raw -Path $script:ProgressPath | ConvertFrom-Json
}

function Set-Progress {
    # Atomically persist the hop-progress object to progress.json.
    param([Parameter(Mandatory)] [pscustomobject]$Progress)
    $tmpPath = "$script:ProgressPath.tmp"
    $Progress | ConvertTo-Json | Set-Content -Path $tmpPath -Encoding UTF8
    Move-Item -Path $tmpPath -Destination $script:ProgressPath -Force
}

function Invoke-OnlineStage {
    # Stage the next AKS EE hop into the local update-cache through Microsoft
    # Update. The AksEdge module never downloads. `Start-AksEdgeUpdate` installs
    # whatever Windows Update has already placed in update-cache, so this drives a
    # Windows Update scan for the single applicable AKS EE update and downloads and
    # installs it. Installing runs the AKS EE update handler, which self-extracts
    # the package into update-cache. The caller then runs `Start-AksEdgeUpdate` to
    # install the staged MSI. This runs in the worker session, not a child process.
    # The Windows Update COM calls raise terminating exceptions we catch, so the
    # worker's strict error settings do not interfere.
    #
    # Returns a PSCustomObject:
    #   result       : 'staged' | 'noUpdate' | 'failed'
    #   expectedMinor: the k3s MAJOR.MINOR the staged update advances to ('staged' only)
    param(
        [Parameter(Mandatory)] [string]$CurrentMinor,
        [bool]$AllowMinor
    )
    $fail = [pscustomobject]@{ result = 'failed';   expectedMinor = $null }
    $none = [pscustomobject]@{ result = 'noUpdate'; expectedMinor = $null }
    # Reuse an already-staged update. A prior run that staged the package but failed
    # at the MSI install leaves it in update-cache, and Windows Update would now
    # report it installed and offer nothing on a re-search. The MSI name carries the
    # k3s minor, so detect and reuse it directly.
    $cacheBase = @('AksEdge', 'AKS-Edge') | ForEach-Object { Join-Path ${env:ProgramFiles} "$_\update-cache" } | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($cacheBase) {
        $msi = Get-ChildItem $cacheBase -Filter '*.msi' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($msi -and $msi.Name -match 'k3s-?(\d+)\.(\d+)') {
            $m = "$($matches[1]).$($matches[2])"
            Write-Log "Online stage: reusing staged package '$($msi.Name)' (k3s $m). Skipping the Windows Update search."
            return [pscustomobject]@{ result = 'staged'; expectedMinor = $m }
        }
    }
    if ((Get-Service wuauserv -ErrorAction SilentlyContinue).Status -ne 'Running') {
        try { Start-Service wuauserv } catch { Write-Log "Online stage: could not start wuauserv: $_"; return $fail }
    }
    try {
        $session  = New-Object -ComObject Microsoft.Update.Session
        $searcher = $session.CreateUpdateSearcher()
        $searcher.ServerSelection = 3                                # ssOthers
        $searcher.ServiceID = '7971f918-a847-4430-9279-4a52d1efe18d' # Microsoft Update
        $found = @($searcher.Search('IsInstalled=0 and IsHidden=0').Updates | Where-Object { $_.Title -match 'AKS Edge' })
    } catch { Write-Log "Online stage: Windows Update search failed: $_"; return $fail }
    if ($found.Count -eq 0) { return $none }

    # Map each offered AKS EE update to the k3s MAJOR.MINOR in its title, e.g.
    # 'AKS Edge Essentials k3s-1.32.9 (Version 1.12.268.0)' -> '1.32'. In minor
    # mode pick the lowest minor strictly above the current one (the next
    # sequential hop, never an overshoot). In patch mode pick a same-minor
    # servicing build. Updates with no parseable k3s minor (for example a k8s
    # build on a k3s node) are ignored.
    $cands = @(foreach ($u in $found) {
        if ($u.Title -match 'k3s-(\d+)\.(\d+)') { [pscustomobject]@{ update = $u; minor = "$($matches[1]).$($matches[2])" } }
    })
    if ($cands.Count -eq 0) {
        Write-Log "Online stage: offered AKS updates carry no parseable k3s version: $($found.Title -join '; ')"
        return $fail
    }
    if ($AllowMinor) {
        $sel = @($cands | Where-Object { (Compare-K8sMinor $_.minor $CurrentMinor) -gt 0 } | Sort-Object { [int]($_.minor.Split('.')[1]) })
    } else {
        $sel = @($cands | Where-Object { $_.minor -eq $CurrentMinor })
    }
    if ($sel.Count -eq 0) { return $none }
    $u = $sel[0].update

    try {
        if (-not $u.EulaAccepted) { $u.AcceptEula() }
        $coll = New-Object -ComObject Microsoft.Update.UpdateColl
        [void]$coll.Add($u)
        if (-not $u.IsDownloaded) {
            $dl = $session.CreateUpdateDownloader(); $dl.Updates = $coll
            if ($dl.Download().ResultCode -ne 2) { Write-Log "Online stage: download failed for '$($u.Title)'"; return $fail }
        }
        $inst = $session.CreateUpdateInstaller(); $inst.Updates = $coll
        $ir = $inst.Install()
        # The self-extract should not require a reboot. If Windows Update claims it
        # does, do not reboot the host. That would disrupt the run, and the cache
        # is already staged.
        if ($ir.RebootRequired) { Write-Log "Online stage: Windows Update reports reboot required after staging '$($u.Title)'. Not rebooting (staging only)." }
        if ($ir.ResultCode -ne 2) { Write-Log "Online stage: install result code $($ir.ResultCode) for '$($u.Title)'"; return $fail }
    } catch { Write-Log "Online stage: Windows Update download/install failed: $_"; return $fail }
    Write-Log "Online stage: staged '$($u.Title)' (advances to k3s $($sel[0].minor))."
    return [pscustomobject]@{ result = 'staged'; expectedMinor = $sel[0].minor }
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

    $fromK8s      = Get-DeployedK8sVersion
    $fromHost     = Get-AksEeHostVersion
    $aio          = Test-AioPresent
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

    $allowMinor = [bool](Get-Prop $config 'allowKubernetesMinorUpgrade' $false)
    $targetRaw  = [string](Get-Prop $config 'targetKubernetesVersion' '')
    $normalizedTarget = ''
    if ($targetRaw) {
        $parsed = Get-K8sMinor $targetRaw
        if ($null -eq $parsed) {
            throw "targetKubernetesVersion '$targetRaw' could not be parsed to a major.minor version. Use a format like '1.33' or 'v1.33.5+k3s1'."
        }
        $normalizedTarget = $parsed
    }

    # Patch mode ignores any configured target. It applies same-version servicing
    # only and never crosses a Kubernetes version.
    if (-not $allowMinor) { $normalizedTarget = '' }

    if ($allowMinor -and $normalizedTarget) {
        $currentMinor = if ($fromK8s) { Get-K8sMinor $fromK8s } else { $null }
        if ($currentMinor -and (Compare-K8sMinor $normalizedTarget $currentMinor) -lt 0) {
            throw "targetKubernetesVersion $normalizedTarget is older than the currently deployed version $currentMinor. Upgrades are forward-only."
        }
    }

    $maxHops = if ($allowMinor) { 6 } else { 1 }
    $initProgress = [pscustomobject]@{
        hopCount            = 0
        beforeVersion       = ''
        expectedMinor       = ''
        pendingApply        = $false
        verifyOnly          = $false
        originalFromVersion = if ($fromK8s) { $fromK8s } else { '' }
        target              = $normalizedTarget
        maxHops             = $maxHops
    }
    Set-Progress $initProgress
    Write-Log "Progress initialized: allowMinor=$allowMinor target=$($initProgress.target) maxHops=$maxHops originalFrom=$($initProgress.originalFromVersion)"

    # The online upgrade stages each hop from Microsoft Update. Verify the
    # Microsoft Update opt-in is registered here (Invoke-OnlineStage starts the
    # Windows Update service when it stages) so a missing opt-in fails with a clear
    # message rather than as a silent no-update during staging.
    if ($allowMinor) {
        $muRegistered = $false
        try { $muRegistered = [bool]((New-Object -ComObject Microsoft.Update.ServiceManager).Services | Where-Object { $_.ServiceID -eq '7971f918-a847-4430-9279-4a52d1efe18d' }) } catch {}
        if (-not $muRegistered) {
            throw 'Microsoft Update is not registered on this host. Enable "receive updates for other Microsoft products" so AKS EE upgrade packages are offered by Windows Update.'
        }
    }

    # Gate minor upgrades: set AcceptUpgrade only for this run's scope. Patch mode
    # passes $false (a no-op that confirms the pin). A failed run leaves the gate
    # as set so the staged cache survives for a re-deploy to resume.
    Set-AcceptUpgrade -Accept $allowMinor

    Set-State -Phase 1 -Status 'running'
    Write-Log 'Phase 0: complete'
}

function Invoke-Phase1 {
    param($config)
    Write-Log 'Phase 1: stage one hop'

    $null = Set-WorkerKubeconfig
    $prog       = Get-Progress
    $target     = if ($prog) { [string](Get-Prop $prog 'target' '') } else { '' }
    $allowMinor = [bool](Get-Prop $config 'allowKubernetesMinorUpgrade' $false)

    # Resume safety: a hop already staged through Start-AksEdgeUpdate (pendingApply
    # set) only needs the apply. Do not re-stage, which would skip the apply.
    if ($prog -and [bool](Get-Prop $prog 'pendingApply' $false)) {
        Write-Log 'Phase 1: a staged hop is pending apply (resume). Proceeding to apply.'
        Set-State -Phase 2 -Status 'running'
        Write-Log 'Phase 1: complete'
        return
    }

    $before       = Get-DeployedK8sVersion
    $currentMinor = if ($before) { Get-K8sMinor $before } else { $null }
    if (-not $currentMinor) { throw 'Phase 1: could not read the current deployed Kubernetes version from the cluster.' }

    # Already at the target minor: verify health, then finalize. No hop applied.
    if ($target -and $currentMinor -eq $target) {
        Write-Log "Phase 1: target minor $target already reached (current $before). Verifying then finalizing."
        if ($prog) { $prog.verifyOnly = $true; Set-Progress $prog }
        Set-State -Phase 3 -Status 'running'
        Write-Log 'Phase 1: complete'
        return
    }

    # Stage the next hop from Microsoft Update. Retry a no-update result while we
    # are still below target: Windows Update applicability can lag for a short
    # window right after a hop is applied.
    $staged   = $null
    $attempts = if ($target) { 4 } else { 1 }
    for ($a = 1; $a -le $attempts; $a++) {
        $staged = Invoke-OnlineStage -CurrentMinor $currentMinor -AllowMinor $allowMinor
        if ($staged.result -ne 'noUpdate' -or -not $target) { break }
        Write-Log "Phase 1: no AKS EE update offered yet (attempt $a/$attempts, current $currentMinor, target $target). Retrying in 30s."
        Start-Sleep -Seconds 30
    }

    switch ($staged.result) {
        'staged' {
            # Install the staged MSI from update-cache. The node-management agent can
            # be briefly unreachable right after staging (a transient nodectl or
            # wssdagent error), so retry across a short settling window. The cache
            # persists and the cmdlet resets updateState on failure, so a retry is safe.
            $ok = $false
            for ($a = 1; $a -le 4 -and -not $ok; $a++) {
                try {
                    Invoke-ChildAksEeCommand -Label "install-msi-$a" `
                        -Script 'Import-Module AksEdge -Force; $r = @(Start-AksEdgeUpdate -Force); if ($r[-1] -eq $true) { exit 0 } else { exit 1 }' | Out-Null
                    $ok = $true
                } catch {
                    Write-Log "Phase 1: MSI install attempt $a/4 failed: $_"
                    if ($a -lt 4) { Write-Log 'Phase 1: waiting 30s for the node agent to settle before retry.'; Start-Sleep -Seconds 30 }
                }
            }
            if (-not $ok) { throw 'Phase 1: Start-AksEdgeUpdate failed to install the staged MSI after retries. See the aksee-install-msi-*.log child logs.' }
            Write-Log "Phase 1: MSI installed from cache. Persisting hop progress (before=$before expected=$($staged.expectedMinor))."
            if ($prog) {
                $prog.beforeVersion = if ($before) { $before } else { '' }
                $prog.expectedMinor = [string]$staged.expectedMinor
                $prog.pendingApply  = $true
                Set-Progress $prog
            }
            Set-State -Phase 2 -Status 'running'
        }
        'noUpdate' {
            if ($target -and $currentMinor -ne $target) {
                throw "No AKS EE update offered by Microsoft Update at $before, but target minor $target is not reached. The target may not be published yet, or Microsoft Update access / AcceptUpgrade gating is the cause."
            }
            # Verify health before finalizing even though no hop was applied.
            Write-Log "Phase 1: no newer AKS EE update available (current $before). Verifying then finalizing."
            if ($prog) { $prog.verifyOnly = $true; Set-Progress $prog }
            Set-State -Phase 3 -Status 'running'
        }
        default {
            throw 'Online staging failed. See the worker log for the Windows Update error detail.'
        }
    }
    Write-Log 'Phase 1: complete'
}

function Invoke-Phase2 {
    param($config)
    Write-Log 'Phase 2: apply control-plane update (single-node)'

    # Module reload is required between stage and apply. The inner node VM reboots
    # during this call, and the cmdlet waits for it to come back. The cmdlet returns
    # a boolean and never sets $LASTEXITCODE, so the child translates it.
    Invoke-ChildAksEeCommand -Label 'apply-update' `
        -Script 'Import-Module AksEdge -Force; $r = @(Start-AksEdgeControlPlaneUpdate -firstControlPlane $true -Force); if ($r[-1] -eq $true) { exit 0 } else { exit 1 }' | Out-Null

    Set-State -Phase 3 -Status 'running'
    Write-Log 'Phase 2: complete'
}

function Invoke-Phase3 {
    param($config)
    Write-Log 'Phase 3: verify hop + decide next step'

    # The kubeconfig and az login from Phase 0 may belong to a prior worker
    # invocation (e.g. host reboot resume), so re-establish both defensively.
    Connect-MachineIdentity -config $config
    $null = Set-WorkerKubeconfig

    # The node VM restarts during apply, so poll for the cluster to settle rather
    # than fail on the first transient negative.
    if (-not (Wait-Until -Label 'cluster nodes Ready' -Condition { Test-NodesReady })) {
        throw 'Verification failed: cluster nodes did not return Ready (/readyz or node conditions) within the verification window after the update.'
    }

    # Arc transiently disconnects while the node VM restarts, so poll the Arc
    # connection through the reconnect window before declaring a regression.
    if (-not (Wait-Until -Label 'Arc connection' -Condition { Test-ArcConnectedChild -Label 'arc-check-post' })) {
        throw 'Verification failed: Test-AksEdgeArcConnection did not report the cluster Arc-connected within the verification window after the update.'
    }
    Write-Log 'Arc connection verified after update'

    $after = Get-DeployedK8sVersion
    if (-not $after) {
        throw 'Verification failed: could not read deployed Kubernetes version after apply. The cluster may not be healthy.'
    }
    Write-Log "Deployed Kubernetes version after hop: $after"

    $prog = Get-Progress

    # Verify-only path: Phase 1 routed a no-op here (already at target, or no
    # update available) so cluster health is still confirmed before finalizing,
    # without counting a hop or asserting a version advance.
    if ($prog -and [bool](Get-Prop $prog 'verifyOnly' $false)) {
        Write-Log 'Verify-only (no hop applied). Cluster verified healthy. Finalizing.'
        Set-State -Phase 99 -Status 'running'
        Write-Log 'Phase 3: complete (verify-only)'
        return
    }

    $before     = if ($prog) { [string](Get-Prop $prog 'beforeVersion' '') } else { '' }
    $expected   = if ($prog) { [string](Get-Prop $prog 'expectedMinor' '') } else { '' }
    $allowMinor = [bool](Get-Prop $config 'allowKubernetesMinorUpgrade' $false)
    $afterMinor = Get-K8sMinor $after

    # Each applied hop must land on the version Microsoft Update staged. The
    # expected minor comes from the offered update's title, so a stalled or failed
    # apply (Kubernetes version unchanged) is caught here rather than passing as a
    # phantom hop. A same-minor servicing build legitimately keeps the minor, in
    # which case expected equals the prior minor and this check still holds.
    if ($allowMinor -and $expected) {
        if ($afterMinor -ne $expected) {
            throw "Hop did not reach the staged version: expected k3s minor $expected after the apply, but the cluster reports $after (minor $afterMinor)."
        }
    } elseif ($allowMinor -and $before) {
        # Fallback when no expected minor was recorded (legacy progress): require the
        # Kubernetes version to have moved forward.
        $beforeMinor = Get-K8sMinor $before
        if ($beforeMinor -and $afterMinor -and (Compare-K8sMinor $afterMinor $beforeMinor) -le 0) {
            throw "Hop did not advance: Kubernetes version stayed $after after the apply."
        }
    }

    # Increment hop count and clear the in-flight flag.
    if ($prog) {
        $prog.hopCount     = [int](Get-Prop $prog 'hopCount' 0) + 1
        $prog.pendingApply = $false
        Set-Progress $prog
    }

    $hopCount = if ($prog) { [int]$prog.hopCount } else { 1 }
    $target   = if ($prog) { [string](Get-Prop $prog 'target' '') } else { '' }
    $maxHops  = if ($prog) { [int](Get-Prop $prog 'maxHops' 1) } else { 1 }
    Write-Log "Hop $hopCount complete. after=$after target=$target maxHops=$maxHops"

    # Decide next step based on target, mode, and hop budget.
    if ($target -and $afterMinor) {
        $cmp = Compare-K8sMinor $afterMinor $target
        if ($cmp -eq 0) {
            Write-Log "Target minor $target reached (after=$after). Moving to finalize."
            Set-State -Phase 99 -Status 'running'
            Write-Log 'Phase 3: complete'
            return
        }
        if ($cmp -gt 0) {
            throw "Overshoot: deployed version $after (minor $afterMinor) exceeds target $target. Manual review required."
        }
    }

    if (-not $allowMinor) {
        # Patch mode: one hop is the full upgrade.
        Set-State -Phase 99 -Status 'running'
        Write-Log 'Phase 3: complete (patch mode, single hop)'
        return
    }

    if ($hopCount -ge $maxHops) {
        throw "Maximum hops reached: completed $hopCount of $maxHops allowed, current version is $after but target has not been reached."
    }

    # More hops needed. Loop back to stage the next minor version.
    Set-State -Phase 1 -Status 'running'
    Write-Log 'Phase 3: complete. Looping back for next hop.'
}

function Invoke-Phase99 {
    param($config)
    Write-Log 'Phase 99: finalize'

    # Re-establish the managed-identity login defensively. On a host-reboot resume
    # this phase can run in a fresh process whose Phase 3 token is gone, and the
    # terminal tag write below must succeed for the wait step to release.
    Connect-MachineIdentity -config $config

    $snapshot    = if (Test-Path $script:SnapshotPath) { Get-Content -Raw -Path $script:SnapshotPath | ConvertFrom-Json } else { $null }
    $prog        = Get-Progress
    $fromVersion = if ($prog) { [string](Get-Prop $prog 'originalFromVersion' '') } else { '' }
    if (-not $fromVersion) { $fromVersion = [string](Get-Prop $snapshot 'fromK8sVersion' '') }
    $hopCount    = if ($prog) { [int](Get-Prop $prog 'hopCount' 0) } else { 0 }

    $appliedVersion = ''
    try {
        $null = Set-WorkerKubeconfig
        $appliedVersion = [string](Get-DeployedK8sVersion)
    } catch {
        Write-Log "WARNING: could not read deployed version for the tag: $_"
    }

    # Re-pin AcceptUpgrade to false before cleaning up. Best-effort: a failure
    # here does not block the success tag or the cleanup.
    if ([bool](Get-Prop $config 'allowKubernetesMinorUpgrade' $false)) {
        try {
            Set-AcceptUpgrade -Accept $false
            Write-Log 'Re-pinned Set-AksEdgeUpgrade -AcceptUpgrade $false after upgrade completion.'
        } catch {
            Write-Log "WARNING: re-pin Set-AksEdgeUpgrade -AcceptUpgrade false failed: $_. Non-fatal."
        }
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

    # Unregister the Scheduled Task so the at-startup trigger does not re-run this
    # worker on a later host reboot. A failed run intentionally leaves the task in
    # place for diagnostics. The main-loop terminal-state guard stops it from
    # re-running there.
    $taskName = [string](Get-Prop $config 'scheduledTaskName' '')
    if ($taskName) {
        try {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
            Write-Log "Unregistered Scheduled Task $taskName"
        } catch {
            Write-Log "WARNING: could not unregister Scheduled Task ${taskName}: $_. Non-fatal."
        }
    }

    Write-Log "Phase 99: complete. Upgrade finished. fromVersion=$fromVersion appliedVersion=$appliedVersion hopCount=$hopCount"
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

    # Terminal-state guard. The at-startup trigger re-runs this worker on every
    # host reboot. If the previous run already reached a terminal state, do not
    # re-dispatch: a 'failed' state must not silently retry, and 'succeeded' must
    # not re-run Phase 99. A deliberate re-deploy resets state to phase 0.
    $bootState = Get-State
    if ($bootState.status -in @('succeeded', 'failed')) {
        Write-Log "State is terminal (phase=$($bootState.phase) status=$($bootState.status)). Nothing to resume. Re-deploy the manifest to run again. Exiting."
        return
    }

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
            # Leave AcceptUpgrade as-is on failure. Re-pinning it to $false here
            # would wipe the staged update-cache (the cmdlet clears the cache when
            # the flag value changes), making an online retry unrecoverable because
            # Windows Update no longer offers a hop it considers installed. A
            # re-deploy resumes from the preserved state, and Phase 99 re-pins on
            # success.
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
