# Generated minified launcher. Edit Build-Launcher.ps1 sources, not this file.
[CmdletBinding()]
param(
[Parameter(Mandatory)] [string]$ResourceGroup,
[Parameter(Mandatory)] [string]$Subscription,
[Parameter(Mandatory)] [string]$RunId,
[string]$MachineName = $env:COMPUTERNAME,
[string]$ConfigDir = 'C:\ProgramData\siteops\aksee-upgrade',
[string]$ScheduledTaskName = 'SiteOpsAksEeUpgrade',
[string]$AllowKubernetesMinorUpgrade = 'false',
[string]$TargetKubernetesVersion = '',
[switch]$Force
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ConfirmPreference = 'None'
$ProgressPreference = 'SilentlyContinue'
if ($PSVersionTable.PSEdition -ne 'Desktop') {
throw "Install-AksEeUpgrade.ps1 requires Windows PowerShell 5.1 (Desktop). Detected: $($PSVersionTable.PSEdition) $($PSVersionTable.PSVersion). Re-run with 'powershell.exe -File Install-AksEeUpgrade.ps1 ...' instead of pwsh."
}
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
function Set-StrictAcl {
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
function Set-RunningTag {
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
$EmbeddedWorker = @'
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
$script:StatePath = Join-Path $ConfigDir 'state.json'
$script:ConfigPath = Join-Path $ConfigDir 'config.json'
$script:SnapshotPath = Join-Path $ConfigDir 'snapshot.json'
$script:ProgressPath = Join-Path $ConfigDir 'progress.json'
$script:NodectlPath = Join-Path ${env:ProgramFiles} 'AksEdge\nodectl.exe'
$script:NodeLoginPath = Join-Path $env:ProgramData 'wssdagent\nodelogin.yaml'
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
phase = $Phase
status = $Status
lastUpdated = (Get-Date).ToString('o')
error = $ErrorText
}
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
$mod = Get-Module -ListAvailable -Name AksEdge | Sort-Object Version -Descending | Select-Object -First 1
if ($null -eq $mod) { return $null }
return $mod.Version.ToString()
}
function Assert-MicrosoftSignedFile {
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
if (Get-Command az -ErrorAction SilentlyContinue) {
Write-Log 'az CLI already on PATH'
return
}
$msiUrl = 'https://aka.ms/installazurecliwindowsx64'
$msiPath = Join-Path $ConfigDir 'azure-cli.msi'
$log = Join-Path $ConfigDir 'az-msiexec.log'
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
if ($env:AZURE_CONFIG_DIR -and (Test-Path $env:AZURE_CONFIG_DIR)) {
Remove-Item -Path $env:AZURE_CONFIG_DIR -Recurse -Force -ErrorAction SilentlyContinue
}
if ($a -lt 6) { Start-Sleep -Seconds 30 }
}
throw "Managed-identity authentication failed after 6 attempts: $lastErr. Ensure the Arc machine identity has a role on the resource group (Contributor, or Kubernetes Cluster - Azure Arc Onboarding plus Tag Contributor) and that the Connected Machine Agent is running."
}
function Set-WorkerKubeconfig {
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
param([string[]]$KubectlArgs)
$exe = if ($env:KUBECTL_CLIENT_PATH -and (Test-Path $env:KUBECTL_CLIENT_PATH)) { $env:KUBECTL_CLIENT_PATH } else { 'kubectl' }
return & $exe @KubectlArgs --request-timeout=10s 2>&1
}
function Get-DeployedK8sVersion {
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
$null = Invoke-Kubectl @('get', 'namespace', 'azure-iot-operations')
return $LASTEXITCODE -eq 0
}
function Test-NodesReady {
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
function Test-NodeAgentHealthy {
[OutputType([bool])]
param()
if (-not (Test-Path $script:NodectlPath)) { return $true }
try { & $script:NodectlPath security login --loginpath $script:NodeLoginPath --identity 2>&1 | Out-Null } catch {}
$probe = try { (& $script:NodectlPath compute vm list -o tsv 2>&1) | Out-String } catch { "$_" }
return ($probe -notmatch 'rpc error|tls:|x509|does not exist' -and $probe.Trim().Length -gt 0)
}
function Restore-NodeAgent {
[OutputType([bool])]
param([int]$MaxRestarts = 2)
if (Test-NodeAgentHealthy) { return $true }
for ($i = 1; $i -le $MaxRestarts; $i++) {
Write-Log "Node agent unreachable (nodectl handshake failing). Restarting wssdagent to re-establish the node certificate chain (attempt $i/$MaxRestarts)."
try { Restart-Service wssdagent -Force -ErrorAction Stop } catch { Write-Log "Restart-Service wssdagent failed: $_" }
$deadline = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $deadline -and (Get-Service wssdagent -ErrorAction SilentlyContinue).Status -ne 'Running') { Start-Sleep -Seconds 3 }
Start-Sleep -Seconds 30
if (Test-NodeAgentHealthy) { Write-Log "Node agent healthy after wssdagent restart (attempt $i)."; return $true }
}
Write-Log "Node agent still unreachable after $MaxRestarts wssdagent restart(s)."
return $false
}
function Invoke-ChildAksEeCommand {
param(
[Parameter(Mandatory)] [string]$Label,
[Parameter(Mandatory)] [string]$Script
)
if (Test-Path $script:NodectlPath) { $null = Restore-NodeAgent }
$childScript = $Script
$bytes = [System.Text.Encoding]::Unicode.GetBytes($childScript)
$encoded = [Convert]::ToBase64String($bytes)
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$childLog = Join-Path $ConfigDir ("aksee-{0}-{1}.log" -f $Label, $stamp)
$childErrLog = "$childLog.err"
Write-Log "Running $Label in child PowerShell. stdout=$childLog stderr=$childErrLog"
$proc = Start-Process -FilePath $psExe `
-ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded) `
-PassThru -NoNewWindow `
-RedirectStandardOutput $childLog -RedirectStandardError $childErrLog
try { $null = $proc.Handle } catch {}
$timeoutMs = 60 * 60 * 1000
$exited = $proc.WaitForExit($timeoutMs)
if (-not $exited) {
try { $proc.Kill() } catch {}
throw "$Label child did not exit within 60 minutes and was killed. Full logs at $childLog and $childErrLog."
}
Write-Log "$Label child exited with code $($proc.ExitCode)"
if ($proc.ExitCode -ne 0) {
$tailOut = if (Test-Path $childLog) { (Get-Content $childLog -Tail 40 -ErrorAction SilentlyContinue) -join "`n" } else { '' }
$tailErr = if (Test-Path $childErrLog) { (Get-Content $childErrLog -Tail 40 -ErrorAction SilentlyContinue) -join "`n" } else { '' }
$err = "$Label exited with code $($proc.ExitCode).`nstdout tail:`n$tailOut`nstderr tail:`n$tailErr`nFull logs at $childLog and $childErrLog."
if (("$tailOut`n$tailErr") -match 'bootx64\.efi|trident|/EFI/AZLB') {
throw "TRIDENT-REMEDIATION-REQUIRED: $err"
}
throw $err
}
return $childLog
}
function Test-ArcConnectedChild {
param([string]$Label)
try {
Invoke-ChildAksEeCommand -Label $Label -Script 'Import-Module AksEdge -Force; if (Test-AksEdgeArcConnection) { exit 0 } else { exit 1 }' | Out-Null
return $true
} catch {
return $false
}
}
function Write-UpgradeStateTag {
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
$sub = $config.subscription
$rg = $config.resourceGroup
$name = [string](Get-Prop $config 'machineName' $env:COMPUTERNAME)
if (-not $name) { $name = $env:COMPUTERNAME }
if (-not $sub -or -not $rg -or -not $name) {
Write-Log 'Skipping upgrade-state tag write: missing subscription / resourceGroup / machine name.'
return
}
$runId = [string](Get-Prop $config 'runId' '')
$tags = @("siteops.aksee.upgrade.state=$Value", "siteops.aksee.upgrade.runId=$runId")
if ($AppliedVersion) { $tags += "siteops.aksee.upgrade.appliedVersion=$AppliedVersion" }
if ($FromVersion) { $tags += "siteops.aksee.upgrade.fromVersion=$FromVersion" }
$arcId = "/subscriptions/$sub/resourceGroups/$rg/providers/Microsoft.HybridCompute/machines/$name"
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
param(
[Parameter(Mandatory)] [string]$Label,
[Parameter(Mandatory)] [scriptblock]$Condition,
[int]$RetrySeconds = 15,
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
function Get-K8sMinor {
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
param([string]$A, [string]$B)
$ap = $A -split '\.'; $bp = $B -split '\.'
$md = [int]$ap[0] - [int]$bp[0]
if ($md -ne 0) { return $md }
return [int]$ap[1] - [int]$bp[1]
}
function Set-AcceptUpgrade {
param([bool]$Accept)
$val = if ($Accept) { '$true' } else { '$false' }
$script = "Import-Module AksEdge -Force; `$r = @(Set-AksEdgeUpgrade -AcceptUpgrade $val); if (`$r[-1] -eq 'OK') { exit 0 } else { Write-Output `$r[-1]; exit 1 }"
Invoke-ChildAksEeCommand -Label 'set-accept-upgrade' -Script $script | Out-Null
}
function Get-Progress {
if (-not (Test-Path $script:ProgressPath)) { return $null }
return Get-Content -Raw -Path $script:ProgressPath | ConvertFrom-Json
}
function Set-Progress {
param([Parameter(Mandatory)] [pscustomobject]$Progress)
$tmpPath = "$script:ProgressPath.tmp"
$Progress | ConvertTo-Json | Set-Content -Path $tmpPath -Encoding UTF8
Move-Item -Path $tmpPath -Destination $script:ProgressPath -Force
}
function Invoke-OnlineStage {
param(
[Parameter(Mandatory)] [string]$CurrentMinor,
[bool]$AllowMinor
)
$fail = [pscustomobject]@{ result = 'failed'; expectedMinor = $null }
$none = [pscustomobject]@{ result = 'noUpdate'; expectedMinor = $null }
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
$session = New-Object -ComObject Microsoft.Update.Session
$searcher = $session.CreateUpdateSearcher()
$searcher.ServerSelection = 3 # ssOthers
$searcher.ServiceID = '7971f918-a847-4430-9279-4a52d1efe18d' # Microsoft Update
$found = @($searcher.Search('IsInstalled=0 and IsHidden=0').Updates | Where-Object { $_.Title -match 'AKS Edge' })
} catch { Write-Log "Online stage: Windows Update search failed: $_"; return $fail }
if ($found.Count -eq 0) { return $none }
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
if ($ir.RebootRequired) { Write-Log "Online stage: Windows Update reports reboot required after staging '$($u.Title)'. Not rebooting (staging only)." }
if ($ir.ResultCode -ne 2) { Write-Log "Online stage: install result code $($ir.ResultCode) for '$($u.Title)'"; return $fail }
} catch { Write-Log "Online stage: Windows Update download/install failed: $_"; return $fail }
Write-Log "Online stage: staged '$($u.Title)' (advances to k3s $($sel[0].minor))."
return [pscustomobject]@{ result = 'staged'; expectedMinor = $sel[0].minor }
}
function Invoke-Phase0 {
param($config)
Write-Log 'Phase 0: preflight + pre-upgrade snapshot'
if (-not (Test-IsAdmin)) { throw 'Worker must run as Administrator (or SYSTEM).' }
if (-not (Test-AksEdgeModuleInstalled)) {
throw 'AKS Edge Essentials is not installed on this host. The upgrade worker targets an existing AKS EE cluster.'
}
Install-AzCliIfMissing
Connect-MachineIdentity -config $config
try { Write-UpgradeStateTag -config $config -Value 'running' } catch { Write-Log "WARNING: in-progress tag write failed: $_" }
$kubeconfig = Set-WorkerKubeconfig
$nodeCount = Get-NodeCount
if ($nodeCount -ne 1) {
throw "Expected a single-node AKS EE cluster, found $nodeCount nodes. This worker supports single-node clusters only."
}
$fromK8s = Get-DeployedK8sVersion
$fromHost = Get-AksEeHostVersion
$aio = Test-AioPresent
$arcConnected = Test-ArcConnectedChild -Label 'arc-check-pre'
$snapshot = [pscustomobject]@{
capturedAt = (Get-Date).ToString('o')
fromK8sVersion = $fromK8s
fromHostVersion = $fromHost
nodeCount = $nodeCount
aioPresent = $aio
arcConnected = $arcConnected
kubeconfig = $kubeconfig
}
$snapshot | ConvertTo-Json | Set-Content -Path $script:SnapshotPath -Encoding UTF8
Write-Log "Snapshot: K8s=$fromK8s hostVersion=$fromHost nodes=$nodeCount aio=$aio arcConnected=$arcConnected"
$allowMinor = [bool](Get-Prop $config 'allowKubernetesMinorUpgrade' $false)
$targetRaw = [string](Get-Prop $config 'targetKubernetesVersion' '')
$normalizedTarget = ''
if ($targetRaw) {
$parsed = Get-K8sMinor $targetRaw
if ($null -eq $parsed) {
throw "targetKubernetesVersion '$targetRaw' could not be parsed to a major.minor version. Use a format like '1.33' or 'v1.33.5+k3s1'."
}
$normalizedTarget = $parsed
}
if (-not $allowMinor) { $normalizedTarget = '' }
if ($allowMinor -and $normalizedTarget) {
$currentMinor = if ($fromK8s) { Get-K8sMinor $fromK8s } else { $null }
if ($currentMinor -and (Compare-K8sMinor $normalizedTarget $currentMinor) -lt 0) {
throw "targetKubernetesVersion $normalizedTarget is older than the currently deployed version $currentMinor. Upgrades are forward-only."
}
}
$maxHops = if ($allowMinor) { 6 } else { 1 }
$initProgress = [pscustomobject]@{
hopCount = 0
beforeVersion = ''
expectedMinor = ''
pendingApply = $false
verifyOnly = $false
originalFromVersion = if ($fromK8s) { $fromK8s } else { '' }
target = $normalizedTarget
maxHops = $maxHops
}
Set-Progress $initProgress
Write-Log "Progress initialized: allowMinor=$allowMinor target=$($initProgress.target) maxHops=$maxHops originalFrom=$($initProgress.originalFromVersion)"
if ($allowMinor) {
$muRegistered = $false
try { $muRegistered = [bool]((New-Object -ComObject Microsoft.Update.ServiceManager).Services | Where-Object { $_.ServiceID -eq '7971f918-a847-4430-9279-4a52d1efe18d' }) } catch {}
if (-not $muRegistered) {
throw 'Microsoft Update is not registered on this host. Enable "receive updates for other Microsoft products" so AKS EE upgrade packages are offered by Windows Update.'
}
}
Set-AcceptUpgrade -Accept $allowMinor
Set-State -Phase 1 -Status 'running'
Write-Log 'Phase 0: complete'
}
function Invoke-Phase1 {
param($config)
Write-Log 'Phase 1: stage one hop'
$null = Set-WorkerKubeconfig
$prog = Get-Progress
$target = if ($prog) { [string](Get-Prop $prog 'target' '') } else { '' }
$allowMinor = [bool](Get-Prop $config 'allowKubernetesMinorUpgrade' $false)
if ($prog -and [bool](Get-Prop $prog 'pendingApply' $false)) {
Write-Log 'Phase 1: a staged hop is pending apply (resume). Proceeding to apply.'
Set-State -Phase 2 -Status 'running'
Write-Log 'Phase 1: complete'
return
}
$before = Get-DeployedK8sVersion
$currentMinor = if ($before) { Get-K8sMinor $before } else { $null }
if (-not $currentMinor) { throw 'Phase 1: could not read the current deployed Kubernetes version from the cluster.' }
if ($target -and $currentMinor -eq $target) {
Write-Log "Phase 1: target minor $target already reached (current $before). Verifying then finalizing."
if ($prog) { $prog.verifyOnly = $true; Set-Progress $prog }
Set-State -Phase 3 -Status 'running'
Write-Log 'Phase 1: complete'
return
}
$staged = $null
$attempts = if ($target) { 4 } else { 1 }
for ($a = 1; $a -le $attempts; $a++) {
$staged = Invoke-OnlineStage -CurrentMinor $currentMinor -AllowMinor $allowMinor
if ($staged.result -ne 'noUpdate' -or -not $target) { break }
Write-Log "Phase 1: no AKS EE update offered yet (attempt $a/$attempts, current $currentMinor, target $target). Retrying in 30s."
Start-Sleep -Seconds 30
}
switch ($staged.result) {
'staged' {
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
$prog.pendingApply = $true
Set-Progress $prog
}
Set-State -Phase 2 -Status 'running'
}
'noUpdate' {
if ($target -and $currentMinor -ne $target) {
throw "No AKS EE update offered by Microsoft Update at $before, but target minor $target is not reached. The target may not be published yet, or Microsoft Update access / AcceptUpgrade gating is the cause."
}
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
Invoke-ChildAksEeCommand -Label 'apply-update' `
-Script 'Import-Module AksEdge -Force; $r = @(Start-AksEdgeControlPlaneUpdate -firstControlPlane $true -Force); if ($r[-1] -eq $true) { exit 0 } else { exit 1 }' | Out-Null
Set-State -Phase 3 -Status 'running'
Write-Log 'Phase 2: complete'
}
function Invoke-Phase3 {
param($config)
Write-Log 'Phase 3: verify hop + decide next step'
Connect-MachineIdentity -config $config
$null = Set-WorkerKubeconfig
if (-not (Wait-Until -Label 'cluster nodes Ready' -Condition { Test-NodesReady })) {
throw 'Verification failed: cluster nodes did not return Ready (/readyz or node conditions) within the verification window after the update.'
}
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
if ($prog -and [bool](Get-Prop $prog 'verifyOnly' $false)) {
Write-Log 'Verify-only (no hop applied). Cluster verified healthy. Finalizing.'
Set-State -Phase 99 -Status 'running'
Write-Log 'Phase 3: complete (verify-only)'
return
}
$before = if ($prog) { [string](Get-Prop $prog 'beforeVersion' '') } else { '' }
$expected = if ($prog) { [string](Get-Prop $prog 'expectedMinor' '') } else { '' }
$allowMinor = [bool](Get-Prop $config 'allowKubernetesMinorUpgrade' $false)
$afterMinor = Get-K8sMinor $after
if ($allowMinor -and $expected) {
if ($afterMinor -ne $expected) {
throw "Hop did not reach the staged version: expected k3s minor $expected after the apply, but the cluster reports $after (minor $afterMinor)."
}
} elseif ($allowMinor -and $before) {
$beforeMinor = Get-K8sMinor $before
if ($beforeMinor -and $afterMinor -and (Compare-K8sMinor $afterMinor $beforeMinor) -le 0) {
throw "Hop did not advance: Kubernetes version stayed $after after the apply."
}
}
if ($prog) {
$prog.hopCount = [int](Get-Prop $prog 'hopCount' 0) + 1
$prog.pendingApply = $false
Set-Progress $prog
}
$hopCount = if ($prog) { [int]$prog.hopCount } else { 1 }
$target = if ($prog) { [string](Get-Prop $prog 'target' '') } else { '' }
$maxHops = if ($prog) { [int](Get-Prop $prog 'maxHops' 1) } else { 1 }
Write-Log "Hop $hopCount complete. after=$after target=$target maxHops=$maxHops"
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
Set-State -Phase 99 -Status 'running'
Write-Log 'Phase 3: complete (patch mode, single hop)'
return
}
if ($hopCount -ge $maxHops) {
throw "Maximum hops reached: completed $hopCount of $maxHops allowed, current version is $after but target has not been reached."
}
Set-State -Phase 1 -Status 'running'
Write-Log 'Phase 3: complete. Looping back for next hop.'
}
function Invoke-Phase99 {
param($config)
Write-Log 'Phase 99: finalize'
Connect-MachineIdentity -config $config
$snapshot = if (Test-Path $script:SnapshotPath) { Get-Content -Raw -Path $script:SnapshotPath | ConvertFrom-Json } else { $null }
$prog = Get-Progress
$fromVersion = if ($prog) { [string](Get-Prop $prog 'originalFromVersion' '') } else { '' }
if (-not $fromVersion) { $fromVersion = [string](Get-Prop $snapshot 'fromK8sVersion' '') }
$hopCount = if ($prog) { [int](Get-Prop $prog 'hopCount' 0) } else { 0 }
$appliedVersion = ''
try {
$null = Set-WorkerKubeconfig
$appliedVersion = [string](Get-DeployedK8sVersion)
} catch {
Write-Log "WARNING: could not read deployed version for the tag: $_"
}
if ([bool](Get-Prop $config 'allowKubernetesMinorUpgrade' $false)) {
try {
Set-AcceptUpgrade -Accept $false
Write-Log 'Re-pinned Set-AksEdgeUpgrade -AcceptUpgrade $false after upgrade completion.'
} catch {
Write-Log "WARNING: re-pin Set-AksEdgeUpgrade -AcceptUpgrade false failed: $_. Non-fatal."
}
}
try {
Write-UpgradeStateTag -config $config -Value 'succeeded' -AppliedVersion $appliedVersion -FromVersion $fromVersion
} catch {
Write-Log "WARNING: tag write helper threw: $_. Non-fatal."
}
if ($env:AZURE_CONFIG_DIR -and (Test-Path $env:AZURE_CONFIG_DIR)) {
Remove-Item -Path $env:AZURE_CONFIG_DIR -Recurse -Force -ErrorAction SilentlyContinue
Write-Log "Removed az token cache at $env:AZURE_CONFIG_DIR"
}
Set-State -Phase 99 -Status 'succeeded'
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
if (-not (Test-Path $ConfigDir)) {
New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
}
$env:AZURE_CONFIG_DIR = Join-Path $ConfigDir '.azure'
$logPath = Join-Path $ConfigDir "worker-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
Start-Transcript -Path $logPath -Append | Out-Null
try {
Write-Log "Upgrade worker started. ConfigDir=$ConfigDir Log=$logPath"
$bootState = Get-State
if ($bootState.status -in @('succeeded', 'failed')) {
Write-Log "State is terminal (phase=$($bootState.phase) status=$($bootState.status)). Nothing to resume. Re-deploy the manifest to run again. Exiting."
return
}
while ($true) {
$state = Get-State
$config = Get-Config
$startPhase = $state.phase
Write-Log "Resuming at phase=$startPhase status=$($state.status)"
try {
switch ($state.phase) {
0 { Invoke-Phase0 -config $config }
1 { Invoke-Phase1 -config $config }
2 { Invoke-Phase2 -config $config }
3 { Invoke-Phase3 -config $config }
99 { Invoke-Phase99 -config $config }
default { throw "Unknown phase: $($state.phase)" }
}
} catch {
$errText = $_.ToString()
Write-Log "ERROR in phase ${startPhase}: $errText"
Set-State -Phase $startPhase -Status 'failed' -ErrorText $errText
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
'@
if (-not (Test-IsAdmin)) {
throw 'Install-AksEeUpgrade.ps1 must run as Administrator.'
}
Write-Log "Preparing AKS EE patch update on $MachineName in $ResourceGroup (runId=$RunId)"
if (-not (Test-Path $ConfigDir)) {
New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
Write-Log "Created $ConfigDir"
}
Set-StrictAcl -Path $ConfigDir
$workerPath = Join-Path $ConfigDir 'worker.ps1'
$configPath = Join-Path $ConfigDir 'config.json'
$statePath = Join-Path $ConfigDir 'state.json'
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
resourceGroup = $ResourceGroup
subscription = $Subscription
machineName = $MachineName
runId = $RunId
allowKubernetesMinorUpgrade = ($AllowKubernetesMinorUpgrade -ieq 'true')
targetKubernetesVersion = $TargetKubernetesVersion
scheduledTaskName = $ScheduledTaskName
}
$config | ConvertTo-Json | Set-Content -Path $configPath -Encoding UTF8
Write-Log "Wrote $configPath (auth=managed identity)"
$initialState = [pscustomobject]@{
phase = 0
status = 'running'
lastUpdated = (Get-Date).ToString('o')
error = $null
}
$initialStateTmp = "$statePath.tmp"
$initialState | ConvertTo-Json | Set-Content -Path $initialStateTmp -Encoding UTF8
Move-Item -Path $initialStateTmp -Destination $statePath -Force
Write-Log "Wrote $statePath (phase=0)"
Set-RunningTag -Subscription $Subscription -ResourceGroup $ResourceGroup -MachineName $MachineName -RunId $RunId
$action = New-ScheduledTaskAction `
-Execute 'powershell.exe' `
-Argument "-NoProfile -ExecutionPolicy Bypass -File `"$workerPath`" -ConfigDir `"$ConfigDir`""
$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$onceTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddSeconds(30))
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