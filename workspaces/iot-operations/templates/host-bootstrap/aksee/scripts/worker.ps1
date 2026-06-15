<#
.SYNOPSIS
State-machine worker that bootstraps AKS Edge Essentials and Arc-connects
the cluster on a Windows VM. Resumes across reboots via a JSON state file.

.DESCRIPTION
Designed to run from a Scheduled Task that fires at startup and on demand.
The worker reads its current phase from `state.json` and cascades through
phases until either a reboot is pending or the bootstrap is complete.

  Phase 0  Pre-flight verification (admin, OS, memory, disk, nested virt).
  Phase 1  Install AKS Edge Essentials MSI, then Install-AksEdgeHostFeatures
           (may reboot when enabling Hyper-V).
  Phase 2  Render the AKS Edge Essentials config (AioDeploy cluster-only,
           no service principal) and create the single-node K3s cluster.
           Arc-connect happens in Phase 3.
  Phase 3  Layer AIO-specific Arc features on top of the cluster:
           install Azure CLI if missing, authenticate with the Arc machine
           managed identity, Arc-connect the cluster, enable custom-locations
           and cluster-connect, and (when workload identity is requested)
           wire the OIDC issuer through the K3s apiserver.
  Phase 99 Cleanup (unregister scheduled task, remove bootstrap user,
           write final state).

Every phase is idempotent so re-runs from any state are safe. Phase 1 writes
the next phase to state.json BEFORE calling Install-AksEdgeHostFeatures so
the at-startup scheduled-task trigger resumes at Phase 2 after the reboot.

.PARAMETER ConfigDir
Directory holding `config.json`, `state.json`, `aksedge-config.template.json`,
and per-invocation log files. Defaults to
`C:\ProgramData\siteops\aksee-bootstrap`. Override for local testing without
the launcher.

.EXAMPLE
    .\worker.ps1
    Read state and cascade through phases until pending-reboot or done.

.EXAMPLE
    .\worker.ps1 -ConfigDir C:\test\bootstrap
    Local-test invocation against a hand-authored config dir.

.NOTES
For local testing without the launcher, hand-author config.json and
state.json in the config dir, and copy aksedge-config.template.json into
the same dir. See README.md.
#>

[CmdletBinding()]
param(
    [string]$ConfigDir = 'C:\ProgramData\siteops\aksee-bootstrap'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Suppress confirmation prompts from cmdlets that ignore -Force or default
# to ShouldProcess. A non-interactive scheduled-task context cannot answer
# prompts, so any prompt would hang the worker.
$ConfirmPreference = 'None'
$ProgressPreference = 'SilentlyContinue'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

$script:StatePath    = Join-Path $ConfigDir 'state.json'
$script:ConfigPath   = Join-Path $ConfigDir 'config.json'
$script:TemplatePath = Join-Path $ConfigDir 'aksedge-config.template.json'

function Write-Log {
    param([string]$Message)
    $ts = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    Write-Host "[$ts] $Message"
}

function Get-State {
    if (-not (Test-Path $script:StatePath)) {
        throw "State file not found at $script:StatePath. The launcher writes the initial state file. For local testing, see README.md."
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
    # Atomic write: serialize to a sibling .tmp file then Move-Item to the
    # final path. Set-Content is not atomic on Windows, so a concurrent
    # reader (operator inspecting state.json, launcher -Force re-init
    # while the worker is mid-iteration) can hit truncated JSON. Move
    # within the same volume is atomic on NTFS.
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

function Test-IsAdmin {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-AksEdgeModuleInstalled {
    return $null -ne (Get-Module -ListAvailable -Name AksEdge)
}

function Test-AksEdgeDeployed {
    # Cluster is up when the kubeconfig exists and kubectl can reach the
    # API server. Try the per-user kubeconfig first, then fall back to the
    # shared copy Phase 2 writes under the config dir (which Phase 99
    # preserves). The fallback lets a -Force re-run detect an existing
    # cluster even though Phase 99 removed the bootstrap user and its
    # profile kubeconfig.
    $kubeconfig = Join-Path $env:USERPROFILE '.kube\config'
    if (-not (Test-Path $kubeconfig)) {
        $sharedKubeconfig = Join-Path $ConfigDir 'kubeconfig'
        if (Test-Path $sharedKubeconfig) {
            $kubeconfig = $sharedKubeconfig
        } else {
            return $false
        }
    }
    if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) { return $false }
    try {
        $null = & kubectl --kubeconfig=$kubeconfig get nodes --request-timeout=5s 2>&1
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Get-Prop {
    # StrictMode-safe property read. Returns $Obj.$Name when present, else
    # $Default. Replaces the repeated PSObject.Properties.Name -contains guard.
    param($Obj, [string]$Name, $Default = $null)
    if ($null -ne $Obj -and $Obj.PSObject.Properties.Name -contains $Name) { return $Obj.$Name }
    return $Default
}

function Assert-MicrosoftSignedFile {
    # Authenticode-verify a downloaded installer before running it. Status
    # 'Valid' means signed, untampered, and chain-trusted. The signer-org pin
    # also rejects a validly-signed non-Microsoft binary (a poisoned redirect).
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
    # Phase 3 needs `az` for connectedk8s connect + enable-features. The
    # Arc-onboarding flow uses `azcmagent`, not `az`, so a freshly-Arc-
    # connected VM may not have `az` on PATH yet. Install the official
    # MSI silently if missing, then refresh PATH so the current process
    # can find `az` without restarting.
    if (Get-Command az -ErrorAction SilentlyContinue) {
        Write-Log 'az CLI already present, skipping install'
        return
    }
    $msiUrl  = 'https://aka.ms/installazurecliwindowsx64'
    $msiPath = Join-Path $ConfigDir 'azurecli-installer.msi'
    $log     = Join-Path $ConfigDir 'az-msiexec.log'
    Write-Log "az CLI not on PATH. Downloading MSI from $msiUrl"
    Invoke-WebRequest -Uri $msiUrl -OutFile $msiPath -UseBasicParsing
    Assert-MicrosoftSignedFile -Path $msiPath
    Write-Log "Installing az CLI MSI via msiexec /quiet, log at $log"
    $proc = Start-Process msiexec.exe -Wait -PassThru -ArgumentList @(
        '/i', $msiPath, '/quiet', '/norestart', '/L*V', $log
    )
    if ($proc.ExitCode -ne 0) {
        throw "az CLI MSI install failed (exit $($proc.ExitCode)). See $log."
    }
    Remove-Item $msiPath -ErrorAction SilentlyContinue
    # Refresh PATH so the new `az` is visible to this process. Same
    # rationale as the AKS Edge module path refresh in Phase 1.
    $env:Path = ([Environment]::GetEnvironmentVariable('Path', 'Machine') +
                 [IO.Path]::PathSeparator +
                 [Environment]::GetEnvironmentVariable('Path', 'User'))
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
        throw "az CLI install reported success but `az` is still not on PATH. Check $log."
    }
    Write-Log "az CLI installed: $((& az version --output tsv --query '\"azure-cli\"' 2>$null) -join ' ')"
}

function Write-BootstrapStateTag {
    # Idempotent tag write on this Arc machine resource. Phase 99 writes
    # 'succeeded'. The per-phase catch writes 'failed-phase-N'. A siteops
    # `type: wait` step polls this tag to gate downstream steps on actual
    # bootstrap completion.
    #
    # Safe to call before az CLI is installed or authenticated. Logs and
    # returns without throwing, and a failed tag write does not fail the
    # bootstrap. Requires `Microsoft.Resources/tags/write` on the Arc
    # machine resource (see README "Bootstrap state tag"). Assumes the
    # resource name equals `$env:COMPUTERNAME`. The constructed ID is
    # logged for manual tagging.
    param(
        [Parameter(Mandatory)] $config,
        [Parameter(Mandatory)] [string]$Value
    )

    if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
        Write-Log "Skipping bootstrap-state tag write: az CLI not installed."
        return
    }

    $sub  = $config.subscription
    $rg   = $config.resourceGroup
    $name = $env:COMPUTERNAME
    if (-not $sub -or -not $rg -or -not $name) {
        Write-Log "Skipping bootstrap-state tag write: missing subscription / resourceGroup / COMPUTERNAME."
        return
    }

    $arcId = "/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.HybridCompute/machines/$name"
    $tagOut = & az tag update --resource-id $arcId --operation merge --tags "siteops.bootstrap.state=$Value" --only-show-errors 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Log "WARNING: tag write failed on $arcId (exit $LASTEXITCODE): $tagOut. See README Prerequisites for the required Microsoft.Resources/tags/write grant."
        return
    }
    Write-Log "Wrote tag siteops.bootstrap.state=$Value on $arcId"
}

function Test-ClusterArcConnected {
    param([string]$ClusterName, [string]$ResourceGroup)
    try {
        $json = & az connectedk8s show --name $ClusterName --resource-group $ResourceGroup --output json 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        $cluster = $json | ConvertFrom-Json
        return $cluster.connectivityStatus -eq 'Connected'
    } catch {
        return $false
    }
}

function Wait-ArcClusterReady {
    # Poll the connectedk8s resource until connectivityStatus is Connected.
    # When -WaitForIssuerUrl is set, also require oidcIssuerProfile.issuerUrl,
    # arcAgentProfile.agentState=Succeeded, and workload identity enabled.
    # The issuer URL can appear before the agents finish rolling out, so
    # gating on all three keeps the apiserver patch from racing the rollout.
    param(
        [string]$ClusterName,
        [string]$ResourceGroup,
        [int]$RetrySeconds = 10,
        [int]$MaxRetries   = 60,
        [switch]$WaitForIssuerUrl
    )
    for ($i = 0; $i -lt $MaxRetries; $i++) {
        $json = & az connectedk8s show --name $ClusterName --resource-group $ResourceGroup --output json 2>$null
        if ($LASTEXITCODE -eq 0) {
            $cluster = $json | ConvertFrom-Json
            $obj = Get-Prop $cluster 'properties' $cluster
            $connStatus = Get-Prop $obj 'connectivityStatus'
            # issuerUrl and agentState are commonly absent in the window between
            # enabling the issuer and Arc finishing provisioning, which is what
            # -WaitForIssuerUrl polls through. Get-Prop reads each StrictMode-safe.
            $issuerUrl  = Get-Prop (Get-Prop $obj 'oidcIssuerProfile') 'issuerUrl'
            $agentState = Get-Prop (Get-Prop $obj 'arcAgentProfile') 'agentState'
            $wi         = Get-Prop (Get-Prop $obj 'securityProfile') 'workloadIdentity'
            $wiEnabled  = [bool](Get-Prop $wi 'enabled')
            if ($WaitForIssuerUrl) {
                Write-Log "Arc cluster status: connectivity=$connStatus agentState=$agentState issuerUrl=$(if ($issuerUrl) { 'present' } else { '(none)' }) wiEnabled=$wiEnabled"
            } else {
                Write-Log "Arc cluster status: connectivity=$connStatus"
            }
            if ($connStatus -eq 'Connected') {
                if (-not $WaitForIssuerUrl) {
                    return
                }
                if ((-not [string]::IsNullOrEmpty($issuerUrl)) -and $agentState -eq 'Succeeded' -and $wiEnabled) {
                    return $issuerUrl
                }
            }
        }
        Start-Sleep -Seconds $RetrySeconds
    }
    throw "Cluster $ClusterName did not reach $(if ($WaitForIssuerUrl) { 'connected + OIDC-issuer-provisioned + workload-identity-enabled' } else { 'connected' }) status within $($MaxRetries * $RetrySeconds)s"
}

function Patch-K3sApiServer {
    # Mirrors Restart-ApiServer (K3s branch) in AksEdgeQuickStartForAio.ps1.
    # K3s reads its config from /var/.eflow/config/k3s/k3s-config.yml.
    # Patch the service-account-issuer line in place, then restart k3s
    # so it picks up the new issuer URL. Reads the file back and asserts
    # the new URL is present, so a silent sed no-op (line missing or
    # different format) fails loudly rather than producing a cluster
    # that looks healthy but lacks the issuer.
    param([string]$IssuerUrl)
    Import-Module AksEdge
    Write-Log "Patching K3s apiserver service-account-issuer to $IssuerUrl"
    Invoke-AksEdgeNodeCommand -command "sudo cat /var/.eflow/config/k3s/k3s-config.yml | tee /home/aksedge-user/k3s-config.yml | tee /home/aksedge-user/k3s-config.yml.working > /dev/null" | Out-Null
    Invoke-AksEdgeNodeCommand -command "sudo sed -i 's|service-account-issuer.*|service-account-issuer=$IssuerUrl|' /home/aksedge-user/k3s-config.yml" | Out-Null
    Invoke-AksEdgeNodeCommand -command "sudo cp /home/aksedge-user/k3s-config.yml /var/.eflow/config/k3s/k3s-config.yml" | Out-Null

    # Verification: grep the patched line out and check the URL is there.
    # A silent sed no-op must fail loudly rather than letting
    # Wait-K3sApiServerReady report success on an unpatched apiserver.
    $verify = Invoke-AksEdgeNodeCommand -command 'sudo grep service-account-issuer /var/.eflow/config/k3s/k3s-config.yml'
    if ($verify -notmatch [regex]::Escape($IssuerUrl)) {
        throw "Patch-K3sApiServer verification failed. Expected `'service-account-issuer=$IssuerUrl`' in k3s-config.yml but observed: $verify"
    }
    Write-Log 'Verified service-account-issuer is patched in k3s-config.yml'

    Write-Log 'Restarting k3s.service to load the patched config'
    Invoke-AksEdgeNodeCommand -command 'sudo systemctl restart k3s.service' | Out-Null
}

function Wait-K3sApiServerReady {
    # Mirrors Wait-ApiServerReady in AksEdgeQuickStartForAio.ps1. Poll
    # /readyz after a k3s restart until "ok" or timeout. Per-call
    # --request-timeout=2s caps each iteration so the total timeout
    # matches wall clock instead of compounding the kubectl client default.
    param([int]$MaxRetries = 120)
    for ($i = 0; $i -lt $MaxRetries; $i++) {
        $ret = & kubectl get --raw='/readyz' --request-timeout=2s 2>$null
        if ($ret -eq 'ok') {
            Write-Log 'K3s apiserver ready'
            return
        }
        Start-Sleep -Seconds 1
    }
    throw 'K3s apiserver did not become ready within 120 seconds'
}

# ---------------------------------------------------------------------------
# Phase implementations
# ---------------------------------------------------------------------------

function Invoke-Phase0 {
    param($config)
    Write-Log 'Phase 0: pre-flight verification'

    if (-not (Test-IsAdmin)) {
        throw 'Worker must run as Administrator. Install-AksEdgeHostFeatures and New-AksEdgeDeployment both require it.'
    }

    $os = Get-CimInstance Win32_OperatingSystem
    Write-Log "OS: $($os.Caption) ($($os.Version))"

    # AKS Edge Essentials supports Windows 10 IoT Enterprise / Enterprise /
    # Pro, Windows 11 Pro / Enterprise / IoT Enterprise, and Windows
    # Server 2022 or 2025. Server 2019 is NOT supported. Fail fast on
    # anything else rather than letting the AKS EE installer hit a
    # confusing error.
    $caption = $os.Caption.ToLower()
    $supported = $false
    if ($caption -like '*server 2022*' -or $caption -like '*server 2025*') {
        $supported = $true
    } elseif ($caption -match 'windows 1[01]') {
        if ($caption -like '*pro*' -or $caption -like '*enterprise*' -or $caption -like '*iot*') {
            $supported = $true
        }
    }
    if (-not $supported) {
        throw "Unsupported OS for AKS Edge Essentials: $($os.Caption). Needs Windows 10/11 Pro/Enterprise/IoT or Windows Server 2022/2025."
    }

    $memGB = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
    Write-Log "Total memory: $memGB GB"
    # Floor is 12 GB: the AKS EE Linux guest requests 10 GB
    # (aksedge-config.template.json MemoryInMB) plus ~2 GB headroom for
    # the Windows host. AIO recommends 16 GB on top of that.
    if ($memGB -lt 12) {
        throw "Host has $memGB GB memory. AKS EE Linux guest requests 10 GB and the Windows host needs ~2 GB headroom (12 GB minimum). AIO additionally recommends 16 GB total."
    }

    $sysDrive = $env:SystemDrive.TrimEnd(':')
    $drive = Get-PSDrive -Name $sysDrive
    $freeGB = [math]::Round($drive.Free / 1GB, 1)
    Write-Log "Free space on ${sysDrive}: $freeGB GB"
    if ($freeGB -lt 30) {
        throw "Only $freeGB GB free on $($env:SystemDrive). AKS EE VM + container images need at least 30 GB."
    }

    $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
    Write-Log "VirtualizationFirmwareEnabled: $($cpu.VirtualizationFirmwareEnabled)"
    if ($cpu.VirtualizationFirmwareEnabled -ne $true) {
        # Some hypervisors expose nested virt without setting this flag.
        # Log a warning and let Phase 1 surface a clearer error if the
        # capability really is absent.
        Write-Log 'WARNING: nested virtualization may not be available. Phase 1 will fail loudly if it is.'
    }

    # Bootstrap the NuGet PSPackageProvider. New-AksEdgeDeployment's
    # internal Arc-connect path calls Get-PackageProvider -Name NuGet
    # without bootstrapping it, so a fresh Windows install fails Phase 2
    # with a misleading error whose real cause is the missing provider.
    # Pre-install here so Phase 2 finds it ready. Also trust PSGallery so
    # any module install the cmdlet triggers does not stop on the
    # untrusted-repository prompt.
    Write-Log 'Bootstrapping NuGet PSPackageProvider for AKS EE Arc-connect path'
    try {
        $nuget = Get-PackageProvider -Name NuGet -ListAvailable -ErrorAction SilentlyContinue |
                 Sort-Object Version -Descending | Select-Object -First 1
        if (-not $nuget -or $nuget.Version -lt [version]'2.8.5.201') {
            Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Force -Scope AllUsers -ErrorAction Stop | Out-Null
            Write-Log 'NuGet PSPackageProvider installed'
        } else {
            Write-Log "NuGet PSPackageProvider already present (version $($nuget.Version))"
        }
        Set-PSRepository -Name PSGallery -InstallationPolicy Trusted -ErrorAction SilentlyContinue
    } catch {
        throw "Failed to bootstrap NuGet PSPackageProvider: $_. New-AksEdgeDeployment Arc-connect path will fail in Phase 2 without it."
    }

    Set-State -Phase 1 -Status 'running'
    Write-Log 'Phase 0: complete'
}

function Invoke-Phase1 {
    param($config)
    Write-Log 'Phase 1: install AKS EE MSI and enable Hyper-V'

    if (Test-AksEdgeModuleInstalled) {
        Write-Log 'AKS Edge module already installed, skipping MSI install'
    } else {
        $msiPath = Join-Path $ConfigDir 'aksedge-installer.msi'
        Write-Log "Downloading MSI from $($config.aksEdgeMsiUrl)"
        Invoke-WebRequest -Uri $config.aksEdgeMsiUrl -OutFile $msiPath -UseBasicParsing

        # Validate the downloaded file BEFORE handing to msiexec. A wrong
        # aka.ms link can redirect to an HTML error page that msiexec
        # rejects with an uninformative error. Catch it here with a clearer
        # message. Two checks:
        #   1. Size > 50MB (an HTML error page is well below this).
        #   2. CFB/CDF magic bytes D0 CF 11 E0 (the file format MSI uses).
        $fileInfo = Get-Item $msiPath
        if ($fileInfo.Length -lt 50MB) {
            throw "Downloaded MSI at $msiPath is only $($fileInfo.Length) bytes (expected > 50MB). The URL '$($config.aksEdgeMsiUrl)' likely returned an error page rather than the installer. Verify the URL serves application/octet-stream and a binary > 50MB."
        }
        $header = [byte[]]::new(8)
        $fs = [System.IO.File]::OpenRead($msiPath)
        try { $null = $fs.Read($header, 0, 8) } finally { $fs.Close() }
        if ($header[0] -ne 0xD0 -or $header[1] -ne 0xCF -or $header[2] -ne 0x11 -or $header[3] -ne 0xE0) {
            $magic = '{0:X2} {1:X2} {2:X2} {3:X2}' -f $header[0],$header[1],$header[2],$header[3]
            throw "Downloaded file at $msiPath is not a valid MSI (magic bytes '$magic', expected 'D0 CF 11 E0'). The URL '$($config.aksEdgeMsiUrl)' likely returned an error page rather than the installer."
        }
        Assert-MicrosoftSignedFile -Path $msiPath

        $msiLog = Join-Path $ConfigDir 'msiexec.log'
        Write-Log "Installing MSI via msiexec /quiet /norestart, log at $msiLog"
        $proc = Start-Process msiexec.exe -Wait -PassThru -ArgumentList @(
            '/i', $msiPath, '/quiet', '/norestart', '/L*V', $msiLog
        )
        if ($proc.ExitCode -ne 0) {
            throw "msiexec exited with code $($proc.ExitCode). See $msiLog for the install log."
        }
        Remove-Item $msiPath -ErrorAction SilentlyContinue
        Write-Log 'MSI install complete'
    }

    # MSI install adds the AksEdge module to the *machine* scope
    # PSModulePath, but the running process inherited PATH/PSModulePath
    # at launch. Refresh from registry so Import-Module can find the
    # newly-installed module without a fresh PowerShell session.
    $env:PSModulePath = ([Environment]::GetEnvironmentVariable('PSModulePath', 'Machine') +
                         [IO.Path]::PathSeparator +
                         [Environment]::GetEnvironmentVariable('PSModulePath', 'User'))
    $env:Path = ([Environment]::GetEnvironmentVariable('Path', 'Machine') +
                 [IO.Path]::PathSeparator +
                 [Environment]::GetEnvironmentVariable('Path', 'User'))

    Import-Module AksEdge

    # Write the next phase BEFORE Install-AksEdgeHostFeatures so the
    # at-startup trigger resumes at Phase 2 after the (possibly inevitable)
    # reboot. If no reboot happens, the cascade in Main falls through to
    # Phase 2 immediately.
    Set-State -Phase 2 -Status 'pending-reboot'

    Write-Log 'Calling Install-AksEdgeHostFeatures -Force'
    $result = Install-AksEdgeHostFeatures -Force
    Write-Log "Install-AksEdgeHostFeatures returned: $result"

    # Treat $false from the cmdlet as a real failure. Coerce to a single
    # boolean by taking the last element, since the cmdlet can emit
    # multiple values into the pipeline.
    $lastResult = @($result) | Select-Object -Last 1
    if ($lastResult -is [bool] -and -not $lastResult) {
        throw "Install-AksEdgeHostFeatures returned `$false. See AksEdge event logs and recent entries under C:\ProgramData\AksEdge for the host-feature install failure."
    }

    # Detect pending reboot via two authoritative signals: the Hyper-V
    # feature install state and the Component Based Servicing key. The
    # cmdlet's own return value is not a reliable reboot signal.
    $hvFeature = Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -ErrorAction SilentlyContinue
    $featureRestartNeeded = $false
    if ($hvFeature -and $null -ne $hvFeature.RestartNeeded) {
        $featureRestartNeeded = [bool]$hvFeature.RestartNeeded
    }
    $cbsPending = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'

    if ($featureRestartNeeded -or $cbsPending) {
        Write-Log "Reboot pending (Hyper-V RestartNeeded=$featureRestartNeeded, CBS RebootPending=$cbsPending). Restarting now."
        Restart-Computer -Force
        Start-Sleep -Seconds 60
        exit 0
    }

    # No reboot needed. Mark phase 2 ready so the cascade picks it up.
    Set-State -Phase 2 -Status 'running'
    Write-Log 'Phase 1: complete (no reboot needed)'
}

function Invoke-Phase2 {
    param($config)
    Write-Log 'Phase 2: deploy single-node K3s cluster'

    # Remove any leftover rendered config from a hard-killed prior run before
    # the early-return path below, so a stale file does not linger.
    $renderedPath = Join-Path $ConfigDir 'aksedge-config.json'
    Remove-Item -Path $renderedPath -Force -ErrorAction SilentlyContinue

    if (Test-AksEdgeDeployed) {
        Write-Log 'AKS EE deployment already present, skipping'
        Set-State -Phase 3 -Status 'running'
        return
    }

    if (-not (Test-Path $script:TemplatePath)) {
        throw "Template not found at $script:TemplatePath. The launcher copies this alongside worker.ps1. For local testing, see README.md."
    }

    Import-Module AksEdge

    try {
        # Render the AKS EE config for an AioDeploy cluster-only deploy. The
        # AioDeploy flag (in the template) makes New-AksEdgeDeployment build
        # the cluster without Arc-connecting, so no service principal is
        # needed here. Only the Arc ClusterName is substituted from runtime
        # parameters. Phase 3 Arc-connects the cluster with the Arc machine
        # managed identity.
        Write-Log "Rendering AKS EE config from $script:TemplatePath"
        $cfg = Get-Content -Raw -Path $script:TemplatePath | ConvertFrom-Json
        $cfg.Arc.ClusterName = $config.clusterName
        $cfg | ConvertTo-Json -Depth 6 | Set-Content -Path $renderedPath -Encoding UTF8
        $cfg = $null

        # Spawn New-AksEdgeDeployment in a fresh child PowerShell process.
        # In-place ErrorActionPreference relaxation does NOT work because the
        # AKS EE module's internal functions reset $ErrorActionPreference='Stop'
        # in their own scope. With strict EAP in effect, the cmdlet's native
        # helper calls have their normal diagnostic stderr converted into
        # terminating errors, which breaks cluster creation. A child process
        # started with default settings (no StrictMode, default EAP=Continue)
        # runs the cmdlet in the environment the module was tested against.
        #
        # Use -EncodedCommand for the child invocation to avoid PowerShell
        # quoting hazards across the process boundary.
        Write-Log "Calling New-AksEdgeDeployment in child PowerShell (this typically takes 10-15 minutes)"
        $childScript = "Import-Module AksEdge; New-AksEdgeDeployment -JsonConfigFilePath '$renderedPath' -Confirm:`$false -Force; exit `$LASTEXITCODE"
        $bytes = [System.Text.Encoding]::Unicode.GetBytes($childScript)
        $encoded = [Convert]::ToBase64String($bytes)

        $psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $childLog    = Join-Path $ConfigDir ("aksee-deploy-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
        $childErrLog = "$childLog.err"
        Write-Log "Child stdout: $childLog"
        Write-Log "Child stderr: $childErrLog"

        $proc = Start-Process -FilePath $psExe `
            -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded) `
            -Wait -PassThru -NoNewWindow `
            -RedirectStandardOutput $childLog -RedirectStandardError $childErrLog

        Write-Log "New-AksEdgeDeployment child exited with code $($proc.ExitCode)"
        if ($proc.ExitCode -ne 0) {
            $tailOut = ''
            $tailErr = ''
            if (Test-Path $childLog)    { $tailOut = (Get-Content $childLog    -Tail 30 -ErrorAction SilentlyContinue) -join "`n" }
            if (Test-Path $childErrLog) { $tailErr = (Get-Content $childErrLog -Tail 30 -ErrorAction SilentlyContinue) -join "`n" }
            throw "New-AksEdgeDeployment exited with code $($proc.ExitCode).`nstdout tail:`n$tailOut`nstderr tail:`n$tailErr`nFull logs at $childLog and $childErrLog."
        }

        # New-AksEdgeDeployment writes the kubeconfig to the invoking user's
        # profile: the dedicated-admin profile, or either WoW64 systemprofile
        # tree when the task runs as SYSTEM. Copy the first hit to the ACL-locked
        # shared path so downstream phases and the operator use one location.
        $kubeCandidates = @(
            (Join-Path $env:USERPROFILE '.kube\config'),
            (Join-Path $env:SystemRoot 'System32\config\systemprofile\.kube\config'),
            (Join-Path $env:SystemRoot 'SysWOW64\config\systemprofile\.kube\config')
        )
        $kubeconfig = $kubeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $kubeconfig) {
            throw "Kubeconfig not found after New-AksEdgeDeployment. Checked: $($kubeCandidates -join '; '). The cluster did not come up, or the kubeconfig landed in an unexpected location."
        }
        $sharedKubeconfig = Join-Path $ConfigDir 'kubeconfig'
        Copy-Item -Path $kubeconfig -Destination $sharedKubeconfig -Force
        Write-Log "Copied kubeconfig from $kubeconfig to $sharedKubeconfig"

        Set-State -Phase 3 -Status 'running'
        Write-Log 'Phase 2: complete (cluster up)'
    } finally {
        # Always remove the rendered config, a per-run artifact.
        if (Test-Path $renderedPath) {
            Write-Log "Removing rendered config at $renderedPath"
            Remove-Item -Path $renderedPath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Invoke-Phase3 {
    param($config)
    Write-Log 'Phase 3: Arc-connect cluster and enable AIO prereqs'

    $cluster = $config.clusterName
    $rg      = $config.resourceGroup
    $sub     = $config.subscription
    $loc     = $config.location
    $oid     = $config.customLocationsOid

    # Workload identity + OIDC issuer are only needed when downstream AIO
    # uses workload-identity-backed secret sync. Default false keeps the
    # riskiest path opt-in. Set enableWorkloadIdentity true in config.json
    # to enable it.
    $enableWi = [bool](Get-Prop $config 'enableWorkloadIdentity')
    Write-Log "Workload identity + OIDC issuer requested: $enableWi"

    Install-AzCliIfMissing

    # Pin the AKS EE kubectl so the connectedk8s extension and any
    # ambient `kubectl` call later in this phase resolve consistently.
    # Hoisted above the connect/skip branch so re-entry on an already-
    # connected cluster keeps the same kubectl path.
    $env:KUBECTL_CLIENT_PATH = "$env:ProgramFiles\AksEdge\kubectl\kubectl.exe"

    # Point all kubectl/az operations at the shared kubeconfig that
    # Phase 2 wrote (or the operator pre-populated for debugging). The
    # az connectedk8s enable-features command reads kubeconfig from
    # $env:KUBECONFIG > $env:USERPROFILE\.kube\config. Using the shared
    # path makes Phase 3 work regardless of which Windows user runs it
    # (the scheduled task user's profile may differ from the user that
    # populated the default location, especially after a Phase 99 cleanup
    # + re-init).
    $sharedKubeconfig = Join-Path $ConfigDir 'kubeconfig'
    if (Test-Path $sharedKubeconfig) {
        $env:KUBECONFIG = $sharedKubeconfig
        Write-Log "Using shared kubeconfig at $sharedKubeconfig"
    } else {
        Write-Log "Shared kubeconfig not found at $sharedKubeconfig. az/kubectl will fall back to `$env:USERPROFILE\.kube\config (typical when Phase 2 wrote it as the current user)."
    }

    Write-Log 'Adding az extension: connectedk8s'
    & az extension add --name connectedk8s --upgrade --only-show-errors | Out-Null

    # Authenticate with the Arc machine's system-assigned managed identity.
    # Phase 2 deploys cluster-only, so this identity is the only one the
    # bootstrap uses for Azure: the Arc-connect below, the AIO feature
    # enablement, and the Phase 99 state-tag write. No secret on disk. It
    # needs Contributor on the resource group (simplest), or the scoped roles
    # Kubernetes Cluster - Azure Arc Onboarding (connect + enable-features)
    # plus Tag Contributor (the tag write) for least privilege.
    Write-Log 'Authenticating with Arc machine managed identity (az login --identity)'
    # The Arc agent publishes the HIMDS endpoints as Machine-scope env vars.
    # A fresh worker process usually inherits them, but refresh from Machine
    # scope defensively so az login --identity can reach HIMDS as SYSTEM.
    foreach ($name in @('IDENTITY_ENDPOINT', 'IMDS_ENDPOINT')) {
        if (-not [Environment]::GetEnvironmentVariable($name)) {
            $machineVal = [Environment]::GetEnvironmentVariable($name, 'Machine')
            if ($machineVal) { Set-Item -Path "Env:$name" -Value $machineVal }
        }
    }
    $loginOut = & az login --identity --only-show-errors 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "az login --identity failed: $loginOut. Ensure the Arc machine identity has Contributor on the resource group."
    }
    # Check exit code so a sub-access failure throws here instead of
    # producing misleading ResourceNotFound errors from subsequent
    # connectedk8s commands fired against the wrong default context.
    $accountSetOut = & az account set --subscription $sub 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "az account set --subscription $sub failed: $accountSetOut. The Arc machine managed identity likely lacks access to subscription $sub."
    }

    if (Test-ClusterArcConnected -ClusterName $cluster -ResourceGroup $rg) {
        Write-Log "Cluster $cluster is already Arc-connected, skipping connect"
    } else {
        # Tag set mirrors AksEdgeQuickStartForAio.ps1 New-ConnectedCluster.
        $aksEdgeVersion = (Get-Module -Name AksEdge).Version.ToString()
        $tags = @('SKU=AKSEdgeEssentials', "AKSEEVersion=$aksEdgeVersion", 'ManagedBy=siteops-bootstrap')

        # --distribution aks_edge_k3s tells Arc the cluster type. WLIF + OIDC
        # issuer are centralized in the `az connectedk8s update` below, not here.
        # Phase 2 is an AioDeploy cluster-only build (never pre-connected), so
        # this is the primary Arc-connect. The check above skips it only on a
        # re-entry where the cluster is already connected.
        $connectArgs = @(
            '-g', $rg,
            '-n', $cluster,
            '-l', $loc,
            '--subscription', $sub,
            '--tags'
        ) + $tags + @(
            '--disable-auto-upgrade',
            '--distribution', 'aks_edge_k3s',
            '--only-show-errors'
        )

        Write-Log "Running az connectedk8s connect $cluster (5-10 minutes)"
        $connectOut = & az connectedk8s connect @connectArgs 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "az connectedk8s connect failed for ${cluster}: $connectOut"
        }
    }

    if ($enableWi) {
        # The cluster is Arc-connected (by the connect above), so enable the
        # issuer + WLIF on that connection. --enable-workload-identity installs
        # the in-cluster webhook, so it needs the routable kubeconfig set above.
        # Idempotent on repeat.
        Write-Log 'Enabling OIDC issuer + workload identity (az connectedk8s update)'
        $wiUpdateOut = & az connectedk8s update -g $rg -n $cluster --enable-oidc-issuer --enable-workload-identity --only-show-errors 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "az connectedk8s update --enable-oidc-issuer failed for ${cluster}: $wiUpdateOut"
        }

        # Wait for Arc to provision the workload-identity agent AND
        # populate oidcIssuerProfile.issuerUrl. Issuer URL is the actual
        # precondition for Patch-K3sApiServer, so wait on that signal
        # (plus agentState + wiEnabled) rather than the looser
        # connectivityStatus.
        Write-Log 'Waiting for Arc to provision the OIDC issuer URL'
        $issuerUrl = Wait-ArcClusterReady -ClusterName $cluster -ResourceGroup $rg -WaitForIssuerUrl
        Write-Log "OIDC issuer URL: $issuerUrl"

        Patch-K3sApiServer -IssuerUrl $issuerUrl
        Wait-K3sApiServerReady

        # Restart the Arc agents so they re-handshake with the patched
        # apiserver. Matches the official AKS EE quickstart. Non-fatal on
        # timeout: a partial rollout is logged, not thrown.
        Write-Log 'Restarting azure-arc deployments to pick up the new issuer'
        & kubectl -n azure-arc rollout restart deployment | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Log 'WARNING: rollout restart (azure-arc) returned non-zero.' }
        & kubectl -n azure-arc rollout status deployment --timeout=300s | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Log 'WARNING: rollout status (azure-arc) did not complete in 300s.' }
    } else {
        Write-Log 'Workload identity not requested. Verifying basic Arc connection only.'
        Wait-ArcClusterReady -ClusterName $cluster -ResourceGroup $rg
    }

    # Custom locations gates AIO extension-based component installs.
    # Always required regardless of WI.
    Write-Log "Enabling custom-locations feature (OID $oid)"
    $featuresOut = & az connectedk8s enable-features `
        --name $cluster `
        --resource-group $rg `
        --features cluster-connect custom-locations `
        --custom-locations-oid $oid `
        --only-show-errors 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "az connectedk8s enable-features failed for ${cluster}: $featuresOut"
    }

    Set-State -Phase 99 -Status 'running'
    Write-Log "Phase 3: complete (cluster Arc-connected, custom-locations enabled$(if ($enableWi) { ', OIDC issuer wired' }))"
}

function Invoke-Phase99 {
    param($config)
    Write-Log 'Phase 99: cleanup'

    # Write the bootstrap-state tag first, while the Phase 3 az login is still
    # valid. The cleanup below removes the az token cache (and the bootstrap
    # user's profile under dedicated-admin), which would strip the managed-
    # identity auth context this tag write depends on.
    try {
        Write-BootstrapStateTag -config $config -Value 'succeeded'
    } catch {
        Write-Log "WARNING: tag write helper threw: $_. Non-fatal."
    }

    $taskName = Get-Prop $config 'scheduledTaskName' 'SiteOpsAksEeBootstrap'
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        Write-Log "Unregistering scheduled task $taskName"
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    } else {
        Write-Log "Scheduled task $taskName not found, nothing to unregister"
    }

    # Remove the bootstrap local admin AND the profile directory it
    # leaves behind. `Remove-LocalUser` only deletes the SAM entry. The
    # profile dir holds the kubeconfig with a long-lived cluster bearer
    # token. Capture the SID first so the profile-by-SID lookup works
    # after the account is gone. Also defensively remove the rendered
    # AKS EE config in case Phase 2's finally block was skipped.
    $bootstrapUser = Get-Prop $config 'localAdminUser' 'siteops-bootstrap'
    $user = Get-LocalUser -Name $bootstrapUser -ErrorAction SilentlyContinue
    $bootstrapSid = $null
    if ($null -ne $user) {
        $bootstrapSid = $user.SID.Value
        Write-Log "Removing local user $bootstrapUser (SID $bootstrapSid)"
        Remove-LocalUser -Name $bootstrapUser
    } else {
        Write-Log "Local user $bootstrapUser not found, nothing to remove"
    }
    if ($bootstrapSid) {
        $profile = Get-CimInstance -ClassName Win32_UserProfile -Filter "SID='$bootstrapSid'" -ErrorAction SilentlyContinue
        if ($null -ne $profile) {
            try {
                Remove-CimInstance -InputObject $profile -ErrorAction Stop
                Write-Log "Removed user profile $($profile.LocalPath)"
            } catch {
                Write-Log "WARNING: failed to remove user profile $($profile.LocalPath): $_. The kubeconfig under .kube\config persists on disk. Remove manually if needed."
            }
        }
    }
    $renderedPath = Join-Path $ConfigDir 'aksedge-config.json'
    if (Test-Path $renderedPath) {
        Remove-Item -Path $renderedPath -Force -ErrorAction SilentlyContinue
        Write-Log 'Removed leftover rendered config'
    }

    # SYSTEM has no user profile to remove, but New-AksEdgeDeployment still
    # wrote a kubeconfig with a bearer token into the systemprofile tree. Purge
    # both WoW64 variants. The retained ConfigDir\kubeconfig is the canonical
    # copy. No-op in dedicated-admin mode.
    foreach ($sysKube in @(
            (Join-Path $env:SystemRoot 'System32\config\systemprofile\.kube'),
            (Join-Path $env:SystemRoot 'SysWOW64\config\systemprofile\.kube'))) {
        if (Test-Path $sysKube) {
            Remove-Item -Path $sysKube -Recurse -Force -ErrorAction SilentlyContinue
            Write-Log "Purged systemprofile kubeconfig at $sysKube"
        }
    }
    # Remove the scoped az token cache. The Phase 99 tag write above was the
    # last az call, so the tokens are done. The retained kubeconfig is separate.
    if ($env:AZURE_CONFIG_DIR -and (Test-Path $env:AZURE_CONFIG_DIR)) {
        Remove-Item -Path $env:AZURE_CONFIG_DIR -Recurse -Force -ErrorAction SilentlyContinue
        Write-Log "Removed az token cache at $env:AZURE_CONFIG_DIR"
    }

    Set-State -Phase 99 -Status 'succeeded'
    Write-Log 'Phase 99: complete. Bootstrap succeeded.'
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
    Write-Log "Worker started. ConfigDir=$ConfigDir Log=$logPath"

    while ($true) {
        $state  = Get-State
        $config = Get-Config
        $startPhase = $state.phase
        Write-Log "Resuming at phase=$startPhase status=$($state.status)"

        try {
            # Phases 0-3 are sequential work. 99 is the terminal cleanup phase.
            # The gap leaves room to insert work phases later without renumbering
            # cleanup or the terminal check below.
            switch ($state.phase) {
                0  { Invoke-Phase0  -config $config }
                1  { Invoke-Phase1  -config $config }
                2  { Invoke-Phase2  -config $config }
                3  { Invoke-Phase3  -config $config }
                99 { Invoke-Phase99 -config $config }
                default { throw "Unknown phase: $($state.phase)" }
            }
        } catch {
            Write-Log "ERROR in phase ${startPhase}: $_"
            Set-State -Phase $startPhase -Status 'failed' -ErrorText $_.ToString()
            try {
                Write-BootstrapStateTag -config $config -Value "failed-phase-$startPhase"
            } catch {
                Write-Log "WARNING: tag write helper threw on failure path: $_. Original phase error re-raised below."
            }
            throw
        }

        $newState = Get-State
        if ($newState.phase -eq 99 -and $newState.status -eq 'succeeded') {
            Write-Log 'Bootstrap complete.'
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
