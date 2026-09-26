param(
    [Parameter(Mandatory = $true)][string]$Release,
    [Parameter(Mandatory = $true)][string]$SourceCommit,
    [string]$Repository = 'Azure/digital-ops-scale-kit',
    [string]$SourceRef = 'refs/heads/main',
    [ValidateSet('release.yaml', 'ci.yaml')][string]$Caller = 'release.yaml',
    [string]$EnrollSource,
    [switch]$WithAzureCli,
    [switch]$Replace,
    [switch]$Yes,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
function Fail([string]$Message) { throw "Site Ops installation: $Message" }
function Stage([string]$Message) { Write-Host "Site Ops installation: $Message" }

if ($Release -cnotmatch '^(siteops/)?v[0-9][0-9A-Za-z._-]{0,100}$' -or
    $SourceCommit -cnotmatch '^[0-9a-f]{40}$' -or
    $Repository -cnotmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' -or
    $SourceRef -cnotmatch '^refs/heads/[A-Za-z0-9._/-]+$' -or
    $SourceRef.Contains('..')) {
    Fail 'Select an exact release, full source commit, and approved publisher.'
}
if ($EnrollSource -and $EnrollSource -cnotmatch '^[a-z][a-z0-9-]{0,39}$') {
    Fail 'Choose a lowercase approved source name.'
}
if (-not [Environment]::Is64BitOperatingSystem -or
    [Environment]::OSVersion.Version.Major -lt 10) {
    Fail 'A supported Windows x64 machine is required.'
}
Stage "Release: $Release ($SourceCommit) from $Repository."
Stage 'Missing tools use approved WinGet or your configured Python feed.'
Stage 'GitHub CLI 2.95+, Python with venv, pipx 1.17.2, and shared pip 26.2.1 are needed.'
Stage 'Changing the shared pipx backend may affect other applications. No account is signed in.'
Stage 'pipx may add its application directory to your user PATH.'
$existingPython = Get-Command 'python.exe' -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($existingPython -and $existingPython.Source -notlike '*\WindowsApps\*') {
    $pythonCheck = & $existingPython.Source -c 'import sys;print(sys.version_info[0],sys.version_info[1],sys.maxsize>4294967296)' 2>$null
} else { $pythonCheck = '' }
if ($pythonCheck -match '^3 (10|11|12|13|14) True$') {
    Stage "Keep: supported 64-bit Python ($pythonCheck)."
} else {
    Stage 'Add: supported 64-bit Python through WinGet or a managed channel.'
}
$existingGh = Get-Command 'gh.exe' -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
$ghCheck = if ($existingGh) {
    (Get-Item -LiteralPath $existingGh.Source).VersionInfo.ProductVersion
} else { '' }
if ($ghCheck -match '^2\.([0-9]+)\.' -and [int]$Matches[1] -ge 95) {
    Stage 'Keep: compatible GitHub CLI.'
} else {
    Stage 'Add: GitHub CLI through WinGet or a managed channel.'
}
if (Get-Command 'curl.exe' -CommandType Application -ErrorAction SilentlyContinue) {
    Stage 'Keep: Windows HTTPS downloader.'
} else { Stage 'Requires Windows curl.exe for anonymous HTTPS downloads.' }
if (Get-Command 'pipx.exe' -CommandType Application -ErrorAction SilentlyContinue) {
    Stage 'Check: existing pipx and its shared backend.'
} else { Stage 'Add: pipx 1.17.2 from the configured Python feed.' }
if ($Replace) { Stage 'The selected build will explicitly replace or repair an existing Site Ops installation.' }
if ($WithAzureCli) {
    if (Get-Command 'az.cmd', 'az.exe' -CommandType Application -ErrorAction SilentlyContinue) {
        Stage 'Keep: available Azure CLI.'
    } else { Stage 'Add: Azure CLI through WinGet or a managed channel.' }
}
if ($EnrollSource) {
    if ($env:SITEOPS_REDACT_OUTPUT -eq '1') {
        Stage 'An explicitly selected source will be enrolled after installation.'
    } else {
        Stage "Source $EnrollSource will approve $Repository with a time-limited policy after installation."
    }
}
if ($DryRun) {
    Stage 'Preview only. No tools or content were downloaded.'
    return
}
if (-not $Yes) {
    if ([Console]::IsInputRedirected) {
        Fail 'In automation, pass -Yes after reviewing the changes.'
    }
    if ($EnrollSource -and -not $Yes) {
        if ((Read-Host 'Enroll this publisher as an approved consumer source? [y/N]') -cnotin @('y', 'Y')) {
            Fail 'Source enrollment was not approved.'
        }
    }
    $answer = Read-Host 'Install missing tools and the selected Site Ops build? [y/N]'
    if ($answer -cnotin @('y', 'Y')) { Fail 'Installation was not approved.' }
}

function Native([string]$Name) {
    $tool = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $tool -or $tool.Source -notlike '*.exe') { return $null }
    return $tool.Source
}
function AzureCli {
    $executable = Native 'az.exe'
    if ($executable) { return $executable }
    $launcher = Get-Command 'az.cmd' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($launcher -and $launcher.Source -like '*.cmd') { return $launcher.Source }
    return $null
}
function Require-ApprovedPythonIndex([string]$Python, [ValidateSet('install', 'download')][string]$Command) {
    $check = @'
import ast
import subprocess
import sys
from urllib.parse import urlsplit

try:
    config = subprocess.run(
        [sys.executable, "-m", "pip", "config", "list"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=20,
    )
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit(1)
if config.returncode or len(config.stdout) > 65536:
    raise SystemExit(1)
try:
    lines = config.stdout.decode("utf-8").splitlines()
except UnicodeError:
    raise SystemExit(1)
settings = {}
for line in lines:
    key, separator, raw = line.partition("=")
    if not separator:
        continue
    try:
        value = ast.literal_eval(raw.strip())
    except (SyntaxError, ValueError):
        raise SystemExit(1)
    if not isinstance(value, str):
        raise SystemExit(1)
    settings[key.strip()] = value
if any(value for key, value in settings.items()
       if key.endswith((".extra-index-url", ".find-links", ".trusted-host"))):
    raise SystemExit(1)
index = next((settings[key] for key in
              (":env:.index-url", f"{sys.argv[1]}.index-url", "global.index-url")
              if settings.get(key)), "")
try:
    parsed = urlsplit(index)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SystemExit(1)
except ValueError:
    raise SystemExit(1)
'@
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $check | & $Python - $Command >$null 2>$null
        $checkExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($checkExit -ne 0) {
        Fail 'Configure one approved HTTPS Python index in pip settings or PIP_INDEX_URL, without extra indexes, find-links, or trusted hosts.'
    }
}
function Require-PrivateDataRoot([string]$Path) {
    function Reject([string]$Code) {
        Fail "Configure a private Site Ops data root. $Code Use trusted, non-symlinked directories."
    }
    function Read-DirectoryAcl([string]$Directory, [string]$Code) {
        try {
            $acl = [IO.Directory]::GetAccessControl($Directory)
            return [pscustomobject]@{
                Owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
                Rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
            }
        } catch {
            Reject $Code
        }
    }
    if ($Path -cnotmatch '^[A-Za-z]:\\') {
        Reject 'ROOT_PATH'
    }
    try {
        $fullPath = [IO.Path]::GetFullPath($Path)
    } catch {
        Reject 'ROOT_PATH'
    }
    if ($fullPath -cne $Path) {
        Reject 'ROOT_PATH'
    }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $trusted = @($sid, 'S-1-5-18', 'S-1-5-32-544', 'S-1-3-4')
    # The system volume can be owned by Windows Modules Installer.
    $trustedOwners = $trusted + 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
    $ancestors = [Collections.Generic.List[string]]::new()
    $parent = Split-Path -Parent $Path
    while ($parent) {
        $ancestors.Add($parent)
        $next = Split-Path -Parent $parent
        if (-not $next -or $next -eq $parent) { break }
        $parent = $next
    }
    $ancestors.Reverse()
    foreach ($ancestor in $ancestors) {
        try {
            $node = Get-Item -LiteralPath $ancestor -Force -ErrorAction Stop
        } catch {
            Reject 'ROOT_ANCESTOR_TYPE'
        }
        if (-not $node.PSIsContainer -or
            ($node.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            Reject 'ROOT_ANCESTOR_TYPE'
        }
        $access = Read-DirectoryAcl $ancestor 'ROOT_ANCESTOR_ACL'
        if ($access.Owner -notin $trustedOwners) {
            Reject 'ROOT_ANCESTOR_OWNER'
        }
        foreach ($rule in $access.Rules) {
            if ($rule.AccessControlType -ne 'Allow' -or $rule.IdentityReference.Value -in $trusted -or
                ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)) {
                continue
            }
            # An ancestor must not let another user replace or relabel our private child.
            if ([int]$rule.FileSystemRights -band 0x500D0140) {
                Reject 'ROOT_ANCESTOR_ACL'
            }
        }
    }
    try {
        $exists = Test-Path -LiteralPath $Path -ErrorAction Stop
    } catch {
        Reject 'ROOT_DATA_TYPE'
    }
    if (-not $exists) {
        try {
            New-Item -ItemType Directory -Path $Path -ErrorAction Stop | Out-Null
        } catch {
            Reject 'ROOT_DATA_CREATE'
        }
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & icacls.exe $Path /inheritance:r /grant:r "*${sid}:(OI)(CI)F" *> $null
            $protected = $LASTEXITCODE -eq 0
        } catch {
            Reject 'ROOT_DATA_ACL'
        } finally {
            $ErrorActionPreference = $previousPreference
        }
        if (-not $protected) {
            Reject 'ROOT_DATA_ACL'
        }
    }
    try {
        $node = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    } catch {
        Reject 'ROOT_DATA_TYPE'
    }
    if (-not $node.PSIsContainer -or ($node.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        Reject 'ROOT_DATA_TYPE'
    }
    $access = Read-DirectoryAcl $Path 'ROOT_DATA_ACL'
    if ($access.Owner -cne $sid) {
        Reject 'ROOT_DATA_OWNER'
    }
    foreach ($rule in $access.Rules) {
        if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -notin $trusted) {
            Reject 'ROOT_DATA_ACL'
        }
    }
}
function WinGetPackage([string]$Id, [bool]$UserScope = $true) {
    $winget = Native 'winget.exe'
    if (-not $winget) { Fail "Use an approved software channel to install $Id. WinGet is unavailable." }
    if ($Yes -and -not $UserScope) {
        Fail "Unattended installation of $Id needs a separately approved managed channel."
    }
    $options = @('install', '--id', $Id, '--exact', '--source', 'winget',
                 '--accept-source-agreements', '--accept-package-agreements')
    if ($UserScope) { $options += @('--scope', 'user') }
    if ($Yes) { $options += @('--silent', '--disable-interactivity') }
    & $winget @options
    if ($LASTEXITCODE -ne 0) { Fail "WinGet could not install $Id." }
    $env:PATH = [Environment]::GetEnvironmentVariable('PATH', 'Machine') + ';' +
        [Environment]::GetEnvironmentVariable('PATH', 'User') + ';' + $env:PATH
}
function PythonCommand {
    $candidate = Native 'python.exe'
    if (-not $candidate -or $candidate -like '*\WindowsApps\*') { return $null }
    $version = & $candidate -c 'import sys;print(sys.version_info[0],sys.version_info[1],sys.maxsize>4294967296)' 2>$null
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^3 (10|11|12|13|14) True$') {
        return $null
    }
    return $candidate
}
$data = Join-Path $env:LOCALAPPDATA 'siteops'
Require-PrivateDataRoot $data
$python = PythonCommand
if (-not $python) {
    WinGetPackage 'Python.Python.3.12'
    $python = PythonCommand
    if (-not $python) {
        $candidate = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $candidateVersion = & $candidate -c 'import sys;print(sys.version_info[0],sys.version_info[1],sys.maxsize>4294967296)'
            if ($LASTEXITCODE -eq 0 -and $candidateVersion -match '^3 12 True$') {
                $python = $candidate
            }
        }
    }
    if (-not $python) { Fail 'Python 3.12 was installed but is not available. Open a new shell and retry.' }
}
$curl = Native 'curl.exe'
if (-not $curl) { Fail 'Windows curl.exe is required for anonymous HTTPS downloads.' }
$gh = Native 'gh.exe'
$ghVersion = if ($gh) { & $gh version 2>$null | Select-Object -First 1 } else { '' }
if ($ghVersion -cnotmatch '^gh version 2\.([0-9]+)\.([0-9]+)' -or [int]$Matches[1] -lt 95) {
    WinGetPackage 'GitHub.cli'
    $gh = Native 'gh.exe'
    if (-not $gh) { Fail 'GitHub CLI was installed but is not on PATH. Open a new shell and retry.' }
    $ghVersion = & $gh version 2>$null | Select-Object -First 1
    if ($ghVersion -cnotmatch '^gh version 2\.([0-9]+)\.([0-9]+)' -or [int]$Matches[1] -lt 95) {
        Fail 'GitHub CLI 2.95 or newer in the 2.x line is required.'
    }
}
if ($WithAzureCli -and -not (AzureCli)) {
    WinGetPackage 'Microsoft.AzureCLI' $false
    if (-not (AzureCli)) {
        Fail 'Azure CLI was installed but is not on PATH. Open a new shell and retry.'
    }
}

$pipx = Native 'pipx.exe'
$privatePipx = Join-Path $data 'tools\pipx\Scripts\pipx.exe'
if (Test-Path -LiteralPath $privatePipx -PathType Leaf) { $pipx = $privatePipx }
$pipxVersion = if ($pipx) { & $pipx --version 2>$null } else { '' }
if ($pipxVersion -cne '1.17.2') {
    $tools = Join-Path $data 'tools'
    New-Item -ItemType Directory -Path $tools -Force | Out-Null
    $installed = Join-Path $tools 'pipx'
    if (Test-Path -LiteralPath $installed) {
        if ((Get-Item -LiteralPath $installed).Attributes -band [IO.FileAttributes]::ReparsePoint -or
            -not (Test-Path -LiteralPath (Join-Path $installed 'pyvenv.cfg') -PathType Leaf) -or
            -not (Test-Path -LiteralPath (Join-Path $installed 'Scripts\python.exe') -PathType Leaf)) {
            Fail 'Existing Site Ops pipx tooling differs. Inspect it before repair.'
        }
    } else {
        & $python -m venv $installed
        if ($LASTEXITCODE -ne 0) { Fail 'The user pipx environment could not be created.' }
    }
    $toolPython = Join-Path $installed 'Scripts\python.exe'
    Require-ApprovedPythonIndex $toolPython install
    & $toolPython -m pip install --only-binary=:all: --no-cache-dir 'pipx==1.17.2'
    if ($LASTEXITCODE -ne 0) { Fail 'pipx could not be installed from the configured feed.' }
    $pipx = Join-Path $installed 'Scripts\pipx.exe'
}
if ((& $pipx --version) -cne '1.17.2') { Fail 'pipx 1.17.2 is required.' }

function InvokePipx([string[]]$Arguments, [string]$FailureMessage) {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & $pipx @Arguments 2>$null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0) { Fail $FailureMessage }
    return $output
}

$download = Join-Path $env:TEMP ('siteops-download-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $download | Out-Null
try {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $download /inheritance:r /grant:r "*${sid}:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail 'The download directory could not be protected.' }
    $base = 'https://github.com/' + $Repository + '/releases/download/' +
        [uri]::EscapeDataString($Release) + '/'
    $identity = (@($Repository, $Release, $SourceCommit, $SourceRef, $Caller) -join [char]0) + [char]0
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $cacheId = [BitConverter]::ToString(
            $hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($identity))
        ).Replace('-', '').ToLowerInvariant()
    } finally { $hasher.Dispose() }
    $cache = Join-Path $data ("install-downloads\" + $cacheId)
    $assets = $download
    if (Test-Path -LiteralPath $cache) {
        $node = Get-Item -LiteralPath $cache -Force
        if (-not $node.PSIsContainer -or
            ($node.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            @(Get-ChildItem -LiteralPath $cache -Force).Count -ne 2) {
            Fail 'Retained release bytes have an unsupported path or inventory.'
        }
        $assets = $cache
        Stage 'Rechecking the retained release without downloading its assets.'
    } else {
        foreach ($asset in @('siteops-install.zip', 'siteops-install.zip.attestation.jsonl')) {
            $limit = if ($asset.EndsWith('.attestation.jsonl')) { 2097152 } else { 536870912 }
            Stage "Downloading $asset anonymously."
            & $curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' `
                --tlsv1.2 --max-redirs 3 --max-time 180 --max-filesize $limit `
                --output (Join-Path $download $asset) ($base + $asset)
            if ($LASTEXITCODE -ne 0 -or
                -not (Test-Path -LiteralPath (Join-Path $download $asset) -PathType Leaf) -or
                (Get-Item -LiteralPath (Join-Path $download $asset)).Length -gt $limit) {
                Fail 'A release asset could not be downloaded within its byte limit.'
            }
        }
    }
    $archive = Join-Path $assets 'siteops-install.zip'
    foreach ($asset in @($archive, "$archive.attestation.jsonl")) {
        $item = Get-Item -LiteralPath $asset -Force -ErrorAction SilentlyContinue
        $limit = if ($asset.EndsWith('.attestation.jsonl')) { 2097152 } else { 536870912 }
        if (-not $item -or $item.PSIsContainer -or $item.Length -lt 1 -or $item.Length -gt $limit -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            Fail 'The retained release bytes are unavailable or oversized.'
        }
    }
    $signer = "https://github.com/$Repository/.github/workflows/_siteops-distribution.yaml@$SourceRef"
    $builder = "https://github.com/$Repository/.github/workflows/$Caller@$SourceRef"
    Stage "Checking the bundle's source, signer, caller, and runner."
    $lines = [Collections.Generic.List[string]]::new()
    $bytes = 0
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $gh attestation verify $archive --bundle "$archive.attestation.jsonl" `
            --repo $Repository --cert-identity $signer --source-ref $SourceRef `
            --source-digest $SourceCommit --signer-digest $SourceCommit `
            --cert-oidc-issuer https://token.actions.githubusercontent.com `
            --predicate-type https://slsa.dev/provenance/v1 --hostname github.com `
            --digest-alg sha256 --format json 2>$null | ForEach-Object {
                $bytes += [Text.Encoding]::UTF8.GetByteCount($_) + 1
                if ($bytes -gt 8388608) { Fail 'Verification evidence exceeds its byte limit.' }
                $lines.Add($_)
            }
        $verifyExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($verifyExit -ne 0 -or $lines.Count -eq 0) {
        Fail 'The bundle provenance could not be verified.'
    }
    $results = @((($lines -join "`n") | ConvertFrom-Json))
    if ($results.Count -lt 1 -or $results.Count -gt 128) {
        Fail 'Verification evidence has an unsupported result count.'
    }
    $expected = @{
        subjectAlternativeName = $signer
        issuer = 'https://token.actions.githubusercontent.com'
        sourceRepositoryURI = "https://github.com/$Repository"
        sourceRepositoryDigest = $SourceCommit
        sourceRepositoryRef = $SourceRef
        buildSignerDigest = $SourceCommit
        buildConfigURI = $builder
        buildConfigDigest = $SourceCommit
        runnerEnvironment = 'self-hosted'
    }
    foreach ($result in $results) {
        $verification = $result.verificationResult
        $certificate = $verification.signature.certificate
        if ($verification -isnot [pscustomobject] -or
            $certificate -isnot [pscustomobject] -or
            $verification.mediaType -isnot [string] -or
            $verification.mediaType -cne
            'application/vnd.dev.sigstore.verificationresult+json;version=0.1') {
            Fail 'The verified observation format is unsupported.'
        }
        foreach ($key in $expected.Keys) {
            $value = $certificate.PSObject.Properties[$key].Value
            if ($value -isnot [string] -or $value -cne $expected[$key]) {
                Fail 'The verified certificate does not match the selected release.'
            }
        }
    }
    $bundleId = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    $root = Join-Path $data 'bundles'
    $listing = InvokePipx @('list', '--output', 'json') `
        'The current pipx installation could not be inspected.'
    if (-not $listing) {
        Fail 'The current pipx installation could not be inspected.'
    }
    $installation = ($listing -join "`n") | ConvertFrom-Json
    if ($null -eq $installation.venvs) {
        Fail 'The current pipx installation could not be inspected.'
    }
    $mainPackage = $installation.venvs.siteops.metadata.main_package
    $recorded = if ($mainPackage) {
        if ($mainPackage.lock_file.__Path__) { $mainPackage.lock_file.__Path__ } else { 'unlocked' }
    } else { $null }
    $bundle = Join-Path $root $bundleId
    if ($recorded -and $recorded -ine (Join-Path $bundle 'pylock.toml') -and -not $Replace) {
        Fail 'Another Site Ops build is installed. Select -Replace after reviewing the native pipx transition.'
    }
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    $repeat = $false
    if (Test-Path -LiteralPath $bundle) {
        $node = Get-Item -LiteralPath $bundle -Force
        if (-not $node.PSIsContainer -or
            ($node.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            -not (Test-Path -LiteralPath (Join-Path $bundle 'bundle.json') -PathType Leaf) -or
            -not (Test-Path -LiteralPath (Join-Path $bundle 'pylock.toml') -PathType Leaf)) {
            Fail 'The retained bundle is incomplete. Inspect it before repair.'
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [IO.Compression.ZipFile]::OpenRead($archive)
        try {
            $members = @($zip.Entries | Where-Object { $_.FullName -ceq 'bundle.json' })
            if ($members.Count -ne 1 -or $members[0].Length -gt 1048576) {
                Fail 'The authenticated bundle manifest inventory is invalid.'
            }
            $stream = $members[0].Open()
            $reader = [IO.StreamReader]::new($stream, [Text.Encoding]::UTF8)
            try { $expectedManifest = $reader.ReadToEnd() } finally { $reader.Dispose() }
            $manifestPath = Join-Path $bundle 'bundle.json'
            if ((Get-Item -LiteralPath $manifestPath).Length -gt 1048576 -or
                [IO.File]::ReadAllText($manifestPath, [Text.Encoding]::UTF8) -cne $expectedManifest) {
                Fail 'The retained bundle manifest differs from the authenticated archive.'
            }
        }
        finally { $zip.Dispose() }
        $check = Join-Path $download 'bundle-compare'
        Expand-Archive -LiteralPath $archive -DestinationPath $check
        $dirs = @(Get-ChildItem -LiteralPath $bundle -Directory -Force)
        if ($dirs.Count -ne 1 -or $dirs[0].Name -cne 'wheels' -or
            ($dirs[0].Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            @(Get-ChildItem -LiteralPath $dirs[0].FullName -Directory -Force).Count -ne 0) {
            Fail 'The retained bundle directory differs from the authenticated archive.'
        }
        $expectedFiles = @(Get-ChildItem -LiteralPath $check -Recurse -File -Force)
        $existingFiles = @(Get-ChildItem -LiteralPath $bundle -Recurse -File -Force)
        if ($expectedFiles.Count -ne $existingFiles.Count) {
            Fail 'The retained bundle contents differ from the authenticated archive.'
        }
        foreach ($file in $existingFiles) {
            $relative = $file.FullName.Substring($bundle.Length + 1)
            $expectedFile = Join-Path $check $relative
            if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
                -not (Test-Path -LiteralPath $expectedFile -PathType Leaf) -or
                (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -cne
                    (Get-FileHash -LiteralPath $expectedFile -Algorithm SHA256).Hash) {
                Fail 'The retained bundle contents differ from the authenticated archive.'
            }
        }
        if ($recorded -ieq (Join-Path $bundle 'pylock.toml') -and -not $Replace) {
            $backendVersion = InvokePipx @('runpip', 'siteops', '--version') `
                'The installed pipx backend could not be inspected.'
            if ($backendVersion -cnotmatch '^pip 26\.2\.1 ') {
                Fail 'The installed pipx backend differs from the verified lock reader.'
            }
            $repeat = $true
        }
    } else {
        New-Item -ItemType Directory -Path $bundle | Out-Null
        & icacls.exe $bundle /inheritance:r /grant:r "*${sid}:(OI)(CI)F" | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail 'The bundle directory could not be protected.' }
        Expand-Archive -LiteralPath $archive -DestinationPath $bundle
    }
    $manifest = Get-Content -LiteralPath (Join-Path $bundle 'bundle.json') -Raw | ConvertFrom-Json
    if ($manifest.apiVersion -cne 'siteops.install/v1' -or
        $manifest.source.repository -cne $Repository -or
        $manifest.source.commit -cne $SourceCommit -or
        $manifest.source.ref -cne $SourceRef) {
        Fail 'The verified bundle describes another source.'
    }
    $version = $manifest.package.version
    if ($version -isnot [string] -or $version -cnotmatch '^[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}$') {
        Fail 'The verified bundle has an unsupported version.'
    }
    if (-not $repeat) {
        $wheelhouse = Join-Path $download 'backend'
        New-Item -ItemType Directory -Path $wheelhouse | Out-Null
        $backendTools = Join-Path $download 'backend-tools'
        & $python -m venv $backendTools
        if ($LASTEXITCODE -ne 0) { Fail 'Python venv is unavailable.' }
        $backendPython = Join-Path $backendTools 'Scripts\python.exe'
        Require-ApprovedPythonIndex $backendPython download
        & $backendPython -m pip download 'pip==26.2.1' `
            --no-deps --only-binary=:all: --dest $wheelhouse
        if ($LASTEXITCODE -ne 0) { Fail 'The approved pip backend is unavailable.' }
        $wheels = @(Get-ChildItem -LiteralPath $wheelhouse -Filter 'pip-26.2.1-*.whl' -File)
        if ($wheels.Count -ne 1 -or
            (Get-FileHash -LiteralPath $wheels[0].FullName -Algorithm SHA256).Hash.ToLowerInvariant() -cne
                '71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e') {
            Fail 'The pip backend does not match its reviewed hash.'
        }
        $wheelUri = ([UriBuilder]::new('file', '', -1, $wheelhouse)).Uri.AbsoluteUri
        $env:PIPX_DEFAULT_PYTHON = $python
        $null = InvokePipx @(
            'upgrade-shared', '--pip-args',
            "--no-index --only-binary=:all: --no-cache-dir --force-reinstall --find-links=$wheelUri"
        ) 'The pipx shared backend could not be provisioned.'
        $install = @('install', 'siteops', '--lock', (Join-Path $bundle 'pylock.toml'),
                     '--backend', 'pip', '--fetch-python', 'never', '--skip-maintenance',
                     '--app', 'siteops', '--pip-args',
                     '--isolated --require-hashes --no-index --only-binary=:all: --no-cache-dir')
        if ($Replace -and $recorded) { $install += '--force' }
        $null = InvokePipx $install 'Site Ops could not be installed from the verified lock.'
        $backendVersion = InvokePipx @('runpip', 'siteops', '--version') `
            'The installed pipx backend could not be inspected.'
        if ($backendVersion -cnotmatch '^pip 26\.2\.1 ') {
            Fail 'The installed pipx backend differs from the verified lock reader.'
        }
        $null = InvokePipx @('ensurepath') 'pipx could not update the user command path.'
    }
    $binDir = InvokePipx @('environment', '--value', 'PIPX_BIN_DIR') `
        'The pipx command directory could not be resolved.'
    if ($binDir -isnot [string] -or -not [IO.Path]::IsPathRooted($binDir)) {
        Fail 'The pipx command directory could not be resolved.'
    }
    $expectedCommand = Join-Path $binDir 'siteops.exe'
    if (-not (Test-Path -LiteralPath $expectedCommand -PathType Leaf)) {
        Fail 'pipx did not expose the selected siteops command.'
    }
    $env:PATH = $binDir + ';' + $env:PATH
    $siteops = Native 'siteops.exe'
    if (-not $siteops -or $siteops -ine $expectedCommand -or
        (& $siteops --version) -cne "siteops $version") {
        Fail 'The exposed siteops command does not match the selected build.'
    }
    Stage "Command directory: $binDir. Add it to your current PATH or open a new shell."
    if ($EnrollSource) {
        $lines = [Collections.Generic.List[string]]::new()
        $bytes = 0
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & $gh attestation trusted-root 2>$null | ForEach-Object {
                $bytes += [Text.Encoding]::UTF8.GetByteCount($_) + 1
                if ($bytes -gt 2097152) { Fail 'The trusted-root snapshot exceeds its byte limit.' }
                $lines.Add($_)
            }
            $rootExit = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousPreference
        }
        if ($rootExit -ne 0 -or $lines.Count -eq 0) {
            Fail 'The GitHub trusted-root snapshot could not be obtained.'
        }
        $rootFile = Join-Path $download 'trusted-root.jsonl'
        $utf8 = [Text.UTF8Encoding]::new($false)
        [IO.File]::WriteAllText($rootFile, ($lines -join "`n") + "`n", $utf8)
        $rootDigest = (Get-FileHash -LiteralPath $rootFile -Algorithm SHA256).Hash.ToLowerInvariant()
        $policyFile = Join-Path $download 'source-policy.json'
        $policy = @{
            apiVersion = 'siteops/v1alpha1'
            kind = 'ArtifactVerificationPolicy'
            id = 'approved-source'
            version = 1
            validUntil = [DateTimeOffset]::UtcNow.AddDays(30).ToString(
                'yyyy-MM-ddTHH:mm:ss.ffffffzzz', [Globalization.CultureInfo]::InvariantCulture
            )
            trustedRootSha256 = $rootDigest
            provider = @{
                kind = 'github-attestation/v1'
                repository = $Repository
                sourceRef = $SourceRef
                signerWorkflow = '.github/workflows/_workspace-distribution.yaml'
                builderWorkflow = ".github/workflows/$Caller"
                runnerEnvironment = 'self-hosted'
            }
        }
        [IO.File]::WriteAllText($policyFile, ($policy | ConvertTo-Json -Depth 5) + "`n", $utf8)
        & $siteops --trust-policy $policyFile --trusted-root $rootFile `
            source enroll $EnrollSource --source "github:$Repository"
        if ($LASTEXITCODE -ne 0) { Fail 'The approved source could not be enrolled.' }
    }
    if ($assets -eq $download) {
        $cacheRoot = Join-Path $data 'install-downloads'
        New-Item -ItemType Directory -Path $cacheRoot -Force | Out-Null
        New-Item -ItemType Directory -Path $cache | Out-Null
        & icacls.exe $cache /inheritance:r /grant:r "*${sid}:(OI)(CI)F" | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail 'The retained release location could not be protected.' }
        foreach ($name in @('siteops-install.zip', 'siteops-install.zip.attestation.jsonl')) {
            $source = Join-Path $download $name
            $target = Join-Path $cache $name
            Copy-Item -LiteralPath $source -Destination $target -ErrorAction Stop
            if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -cne
                (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash) {
                Fail 'The retained release bytes differ from the authenticated download.'
            }
        }
    }
    if ($EnrollSource) {
        if ($env:SITEOPS_REDACT_OUTPUT -eq '1') {
            Stage "Installed siteops $version with an approved source. Authenticate to Azure separately."
        } else {
            Stage "Installed siteops $version with approved source $EnrollSource. Authenticate to Azure separately."
        }
    } else {
        Stage "Installed siteops $version. Authenticate to Azure and approve a workspace source separately."
    }
}
finally {
    if (Test-Path -LiteralPath $download) {
        Remove-Item -LiteralPath $download -Recurse -Force
    }
}
