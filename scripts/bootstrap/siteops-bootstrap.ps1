param(
    [string]$Release,
    [string]$ContentRelease,
    [Parameter(Mandatory = $true)][string]$SourceCommit,
    [string]$Repository = 'Azure/digital-ops-scale-kit',
    [string]$SourceRef = 'refs/heads/main',
    [ValidateSet('release.yaml', 'ci.yaml')][string]$Caller = 'release.yaml',
    [string]$EnrollSource,
    [switch]$Replace,
    [switch]$Yes,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -ErrorAction Stop
function Fail([string]$Message) { throw "Site Ops installation failed: $Message" }
function Stage([string]$Message) { Write-Host "Site Ops installation: $Message" }
# Windows PowerShell reads ACLs through FileSystemInfo; PowerShell 7 uses the .NET extension class.
function Read-NodeAcl([string]$Path, [switch]$Directory) {
    $node = if ($Directory) { [IO.DirectoryInfo]::new($Path) } else { [IO.FileInfo]::new($Path) }
    if ($PSVersionTable.PSEdition -eq 'Core') { return [IO.FileSystemAclExtensions]::GetAccessControl($node) }
    return $node.GetAccessControl()
}

function Get-SelectionKey([string]$SelectedRelease, [string]$Commit, [string]$Ref, [string]$Workflow) {
    $identity = (@($Repository, $SelectedRelease, $Commit, $Ref, $Workflow) -join [char]0) + [char]0
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString(
            $hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($identity))
        ).Replace('-', '').ToLowerInvariant()
    } finally { $hasher.Dispose() }
}
function Verify-ReleaseAsset([string]$Subject, [string]$Commit, [string]$Ref, [string]$Workflow, [string]$SignerWorkflow) {
    $signer = "https://github.com/$Repository/.github/workflows/$SignerWorkflow@$Ref"
    $builder = "https://github.com/$Repository/.github/workflows/$Workflow@$Ref"
    $lines = [Collections.Generic.List[string]]::new()
    $bytes = 0
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $gh attestation verify $Subject --bundle "$Subject.attestation.jsonl" `
            --repo $Repository --cert-identity $signer --source-ref $Ref `
            --source-digest $Commit --signer-digest $Commit `
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
        Fail 'The selected asset provenance could not be verified.'
    }
    $results = @((($lines -join "`n") | ConvertFrom-Json))
    if ($results.Count -lt 1 -or $results.Count -gt 128) {
        Fail 'Verification evidence has an unsupported result count.'
    }
    $expected = @{
        subjectAlternativeName = $signer
        issuer = 'https://token.actions.githubusercontent.com'
        sourceRepositoryURI = "https://github.com/$Repository"
        sourceRepositoryDigest = $Commit
        sourceRepositoryRef = $Ref
        buildSignerDigest = $Commit
        buildConfigURI = $builder
        buildConfigDigest = $Commit
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
}
function Read-EngineReference([string]$Path, [string]$SelectedRelease, [string]$Commit, [string]$Ref, [string]$Workflow) {
    $info = Get-Item -LiteralPath $Path -Force
    if ($info.Length -lt 1 -or $info.Length -gt 16384 -or $info.PSIsContainer -or
        ($info.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        Fail 'The engine reference is unavailable or oversized.'
    }
    try {
        $raw = [Text.UTF8Encoding]::new($false, $true).GetString([IO.File]::ReadAllBytes($Path))
        $record = $raw | ConvertFrom-Json
    } catch [ArgumentException] {
        Fail 'The signed engine reference is invalid.'
    }
    $engine = $record.engine
    if ($record -isnot [pscustomobject] -or $engine -isnot [pscustomobject] -or
        $record.apiVersion -cne 'siteops.release.engine/v1' -or $record.kind -cne 'EngineReference' -or
        $record.release -cne $SelectedRelease -or $record.revision -cne $Commit -or
        $record.preview -isnot [bool] -or $record.preview -ne ($Workflow -ceq 'ci.yaml') -or
        $engine.release -isnot [string] -or $engine.release -cnotmatch '^(siteops/)?v[0-9][0-9A-Za-z._-]{0,100}$' -or
        $engine.revision -isnot [string] -or $engine.revision -cnotmatch '^[0-9a-f]{40}$' -or
        $engine.version -isnot [string] -or $engine.version -cnotmatch '^[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}$') {
        Fail 'The signed engine reference differs from the selected content release.'
    }
    if ($engine.release -ceq $SelectedRelease) {
        if ($engine.revision -cne $Commit) { Fail 'The combined engine has a different source revision.' }
        $engineRef = $Ref
        $engineCaller = $Workflow
    } else {
        if ($engine.release -cne ('siteops/v' + $engine.version)) {
            Fail 'The engine reference does not select an exact engine release.'
        }
        $engineRef = 'refs/heads/main'
        $engineCaller = 'release.yaml'
    }
    foreach ($key in @('bundle', 'proof')) {
        $asset = $engine.$key
        $name = if ($key -eq 'bundle') { 'siteops-install.zip' } else { 'siteops-install.zip.attestation.jsonl' }
        $limit = if ($key -eq 'bundle') { 134217728 } else { 2097152 }
        if ($asset -isnot [pscustomobject] -or $asset.name -cne $name -or
            ($asset.size -isnot [int] -and $asset.size -isnot [long]) -or
            $asset.size -lt 1 -or $asset.size -gt $limit -or
            $asset.sha256 -isnot [string] -or $asset.sha256 -cnotmatch '^[0-9a-f]{64}$') {
            Fail 'The engine reference contains invalid asset identities.'
        }
    }
    $canonical = [ordered]@{
        apiVersion = 'siteops.release.engine/v1'
        engine = [ordered]@{
            bundle = [ordered]@{ name = $engine.bundle.name; sha256 = $engine.bundle.sha256; size = $engine.bundle.size }
            proof = [ordered]@{ name = $engine.proof.name; sha256 = $engine.proof.sha256; size = $engine.proof.size }
            release = $engine.release
            revision = $engine.revision
            version = $engine.version
        }
        kind = 'EngineReference'
        preview = $record.preview
        release = $SelectedRelease
        revision = $Commit
    } | ConvertTo-Json -Compress -Depth 5
    if ($raw -cne ($canonical + "`n")) { Fail 'The engine reference has unsupported or ambiguous fields.' }
    return [pscustomobject]@{
        Release = $engine.release; Commit = $engine.revision; SourceRef = $engineRef
        Caller = $engineCaller; Version = $engine.version
        BundleSize = $engine.bundle.size; BundleSha = $engine.bundle.sha256
        ProofSize = $engine.proof.size; ProofSha = $engine.proof.sha256
    }
}

function Get-InstallerHelper([string]$Archive, [string]$Directory, [string]$ExpectedVersion = '') {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        if ($ExpectedVersion) {
            $metadata = @($zip.Entries | Where-Object { $_.FullName -ieq 'bundle.json' })
            if ($metadata.Count -ne 1 -or $metadata[0].FullName -cne 'bundle.json' -or
                $metadata[0].Length -lt 1 -or $metadata[0].Length -gt 1048576) {
                Fail 'The bundle has no valid version metadata.'
            }
            $reader = [IO.StreamReader]::new($metadata[0].Open())
            try { $manifest = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
            if ($manifest.package.version -isnot [string] -or $manifest.package.version -cne $ExpectedVersion) {
                Fail 'The bundle version differs from the signed content selection.'
            }
        }
        $entries = @($zip.Entries | Where-Object { $_.FullName -ieq 'siteops-install.py' })
        if ($entries.Count -ne 1 -or $entries[0].FullName -cne 'siteops-install.py' -or
            $entries[0].Length -lt 1 -or $entries[0].Length -gt 1048576 -or
            (($entries[0].ExternalAttributes -shr 16) -band 0xF000) -notin @(0, 0x8000)) {
            Fail 'The authenticated release has no valid installer helper.'
        }
        $helper = Join-Path $Directory 'siteops-install.py'
        $incoming = $entries[0].Open()
        try {
            $output = [IO.File]::Open($helper, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
            try {
                $buffer = New-Object byte[] 65536
                $total = 0
                while (($read = $incoming.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $total += $read
                    if ($total -gt 1048576) { Fail 'The installer helper exceeds its byte limit.' }
                    $output.Write($buffer, 0, $read)
                }
                if ($total -ne $entries[0].Length) { Fail 'The installer helper is incomplete.' }
            } finally { $output.Dispose() }
        } finally { $incoming.Dispose() }
        return $helper
    } finally { $zip.Dispose() }
}
function Check-Payload([string[]]$Arguments) {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & $python -I -S -B $installerHelper @Arguments 2>$null
        $codeExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($codeExit -eq 2) { Fail 'Another Site Ops selection is installed. Use -Replace after review.' }
    if ($codeExit -eq 3) { Fail 'The exposed command belongs to another installation. Remove it with its original manager.' }
    if ($codeExit -eq 4) { Fail 'The tool has unrecognized Python startup files. Inspect it before using uv tool uninstall siteops.' }
    if ($codeExit -ne 0) { Fail 'The bundle or installed payload failed validation.' }
    $text = $output -join "`n"
    if ([Text.Encoding]::UTF8.GetByteCount($text) -gt 4096) {
        Fail 'The installer helper returned oversized results.'
    }
    $result = $text | ConvertFrom-Json
    if ($result.version -isnot [string] -or
        $result.version -cnotmatch '^[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}$' -or
        $result.wheel -isnot [string] -or $result.wheel -cnotmatch '^wheels/[^/]+\.whl$') {
        Fail 'The installer helper returned unsupported results.'
    }
    return $result
}

function Native([string]$Name) {
    $tool = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $tool -or $tool.Source -notlike '*.exe') { return $null }
    return $tool.Source
}
function Require-PrivateDataRoot([string]$Path, [switch]$Managed) {
    function Reject([string]$Code) {
        Fail "Configure a private Site Ops data root. $Code Use trusted, non-symlinked directories."
    }
    function Read-DirectoryAcl([string]$Directory, [string]$Code) {
        try {
            $acl = Read-NodeAcl $Directory -Directory
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
        $phase = 'ROOT_DATA_OWNER'
        $owned = $false
        $protected = $false
        try {
            $ErrorActionPreference = 'Continue'
            & icacls.exe $Path /setowner "*$sid" *> $null
            $owned = $LASTEXITCODE -eq 0
            if ($owned) {
                $phase = 'ROOT_DATA_ACL'
                & icacls.exe $Path /inheritance:r /grant:r "*${sid}:(OI)(CI)F" *> $null
                $protected = $LASTEXITCODE -eq 0
            }
        } catch {
            Reject $phase
        } finally {
            $ErrorActionPreference = $previousPreference
        }
        if (-not $owned) {
            Reject 'ROOT_DATA_OWNER'
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
    if (($Managed -and $access.Owner -notin $trustedOwners) -or
        (-not $Managed -and $access.Owner -cne $sid)) {
        Reject 'ROOT_DATA_OWNER'
    }
    foreach ($rule in $access.Rules) {
        if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -notin $trusted -and
            (-not $Managed -or ([int]$rule.FileSystemRights -band 0x500D0156))) {
            Reject 'ROOT_DATA_ACL'
        }
    }
}
function Ensure-UvStorage([string]$Path) {
    if ($Path -cnotmatch '^[A-Za-z]:\\' -or [IO.Path]::GetFullPath($Path) -cne $Path) {
        Fail 'Select an absolute canonical uv storage directory.'
    }
    $missing = [Collections.Generic.List[string]]::new()
    $current = $Path
    while (-not (Test-Path -LiteralPath $current)) {
        $missing.Add($current)
        $parent = Split-Path -Parent $current
        if (-not $parent -or $parent -eq $current) { Fail 'The uv storage ancestry is unavailable.' }
        $current = $parent
    }
    $missing.Reverse()
    foreach ($directory in $missing) { Require-PrivateDataRoot $directory -Managed }
    Require-PrivateDataRoot $Path -Managed
}
function Invoke-Uv([string]$Executable, [string[]]$Arguments) {
    if ($env:UV_INSECURE_HOST -or $env:UV_PYTHON_DOWNLOADS_JSON_URL -or
        ($env:UV_INSECURE_NO_ZIP_VALIDATION -and
         $env:UV_INSECURE_NO_ZIP_VALIDATION -notin @('0', 'false'))) {
        Fail 'Remove insecure uv settings and custom runtime catalogs before verified installation.'
    }
    if ($env:UV_PYTHON_INSTALL_MIRROR) {
        $mirror = $null
        if (-not [uri]::TryCreate($env:UV_PYTHON_INSTALL_MIRROR, [UriKind]::Absolute, [ref]$mirror) -or
            $mirror.Scheme -cne 'https' -or -not $mirror.Host) {
            Fail 'Select an approved HTTPS Python runtime mirror.'
        }
    }
    $retained = @(
        'UV_TOOL_DIR', 'UV_TOOL_BIN_DIR', 'UV_PYTHON_INSTALL_DIR', 'UV_PYTHON_INSTALL_MIRROR'
    )
    $saved = @{}
    foreach ($item in Get-ChildItem Env:) {
        if ($item.Name -like 'UV_*' -or $item.Name -like 'PYTHON*' -or
            $item.Name -in @('VIRTUAL_ENV', 'CONDA_PREFIX')) {
            $saved[$item.Name] = $item.Value
        }
    }
    $previousPreference = $ErrorActionPreference
    try {
        foreach ($name in $saved.Keys) {
            if ($name -notin $retained) { [Environment]::SetEnvironmentVariable($name, $null) }
        }
        if ($script:UvCacheDir) { $env:UV_CACHE_DIR = $script:UvCacheDir }
        $ErrorActionPreference = 'Continue'
        $global:LASTEXITCODE = 1
        $lines = [Collections.Generic.List[string]]::new()
        $bytes = 0
        & $Executable @Arguments --no-config --system-certs --no-progress 2>$null |
            ForEach-Object {
                $bytes += [Text.Encoding]::UTF8.GetByteCount($_) + 1
                if ($bytes -gt 1048576) { Fail 'The uv observation exceeds its byte limit.' }
                $lines.Add($_)
            }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
        foreach ($name in $saved.Keys) {
            [Environment]::SetEnvironmentVariable($name, $saved[$name])
        }
        if (-not $saved.ContainsKey('UV_CACHE_DIR')) {
            [Environment]::SetEnvironmentVariable('UV_CACHE_DIR', $null)
        }
    }
    if ($code -ne 0) { Fail 'The selected uv operation failed. Inspect the approved tool and runtime source.' }
    return ($lines -join "`n")
}
function Require-PrivateExecutablePath([string]$Path, [string]$PrivateRoot, [switch]$Optional) {
    function Reject([string]$Code) {
        $message = "Choose a private Windows tool location. $Code Use trusted directories and the selected executable."
        if ($Optional) { throw [Security.SecurityException]::new($message) }
        Fail $message
    }
    if ($Path -cnotmatch '^[A-Za-z]:\\' -or $PrivateRoot -cnotmatch '^[A-Za-z]:\\') {
        Reject 'TOOL_PATH'
    }
    try {
        $fullPath = [IO.Path]::GetFullPath($Path)
        $fullRoot = [IO.Path]::GetFullPath($PrivateRoot)
    } catch {
        Reject 'TOOL_PATH'
    }
    if ($fullPath -cne $Path -or $fullRoot -cne $PrivateRoot) {
        Reject 'TOOL_PATH'
    }
    $root = $PrivateRoot.TrimEnd('\')
    # A root equal to the file applies the ancestor rules to every directory, as for an installed tool.
    if ($Path -ine $root -and -not $Path.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        Reject 'TOOL_PATH'
    }
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    # Windows Modules Installer owns and writes the protected program directories.
    $trusted = @($sid, 'S-1-5-18', 'S-1-5-32-544', 'S-1-3-4',
                 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
    $nodes = [Collections.Generic.List[string]]::new()
    $parent = Split-Path -Parent $Path
    while ($parent) {
        $nodes.Add($parent)
        $next = Split-Path -Parent $parent
        if (-not $next -or $next -eq $parent) { break }
        $parent = $next
    }
    $nodes.Reverse()
    $nodes.Add($Path)
    foreach ($nodePath in $nodes) {
        $isFile = $nodePath -ceq $Path
        try {
            $node = Get-Item -LiteralPath $nodePath -Force -ErrorAction Stop
        } catch {
            Reject 'TOOL_TYPE'
        }
        if (($isFile -and $node.PSIsContainer) -or
            (-not $isFile -and -not $node.PSIsContainer)) {
            Reject 'TOOL_TYPE'
        }
        if ($node.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            Reject 'TOOL_TYPE'
        }
        try {
            $acl = Read-NodeAcl $nodePath -Directory:(-not $isFile)
            $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
            $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
        } catch {
            Reject 'TOOL_ACL'
        }
        if ($owner -notin $trusted) {
            Reject 'TOOL_OWNER'
        }
        $privateNode = $isFile -or $nodePath -ieq $root -or
            $nodePath.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)
        # Within the selected tool root, create-only rights can plant files loaded by an executable.
        $unsafeRights = if ($privateNode) { 0x500D0156 } else { 0x500D0140 }
        foreach ($rule in $rules) {
            if ($rule.AccessControlType -ne 'Allow' -or $rule.IdentityReference.Value -in $trusted -or
                ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)) {
                continue
            }
            if ([int]$rule.FileSystemRights -band $unsafeRights) {
                Reject 'TOOL_ACL'
            }
        }
    }
}
function Require-PrivateRuntimeTree([string]$Root, [string]$Storage, [string]$Interpreter) {
    $null = Require-PrivateExecutablePath $Interpreter $Storage
    Require-PrivateDataRoot $Root -Managed
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $trusted = @($sid, 'S-1-5-18', 'S-1-5-32-544', 'S-1-3-4')
    $trustedOwners = $trusted + 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
    $pending = [Collections.Generic.Queue[object]]::new()
    $pending.Enqueue([pscustomobject]@{ Path = $Root; Depth = 0 })
    $count = 0
    while ($pending.Count) {
        $directory = $pending.Dequeue()
        try {
            foreach ($path in [IO.Directory]::EnumerateFileSystemEntries($directory.Path)) {
                $count++
                if ($count -gt 16384 -or $directory.Depth -ge 32) {
                    Fail 'The selected Python runtime exceeds its safe traversal limits.'
                }
                try {
                    $attributes = [IO.File]::GetAttributes($path)
                    if ($attributes -band [IO.FileAttributes]::ReparsePoint) {
                        Fail 'RUNTIME_TYPE: A selected Python runtime contains a redirected entry.'
                    }
                    $isDirectory = ($attributes -band [IO.FileAttributes]::Directory) -ne 0
                    $acl = Read-NodeAcl $path -Directory:$isDirectory
                    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
                    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
                } catch [UnauthorizedAccessException] {
                    Fail 'RUNTIME_ACL: A selected Python runtime cannot be inspected.'
                } catch [IO.IOException] {
                    Fail 'RUNTIME_TYPE: A selected Python runtime cannot be inspected.'
                }
                if ($owner -notin $trustedOwners) {
                    Fail 'RUNTIME_OWNER: A selected Python runtime has an untrusted owner.'
                }
                foreach ($rule in $rules) {
                    if ($rule.AccessControlType -ne 'Allow' -or $rule.IdentityReference.Value -in $trusted -or
                        ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)) {
                        continue
                    }
                    if ([int]$rule.FileSystemRights -band 0x500D0156) {
                        Fail 'RUNTIME_ACL: A selected Python runtime is writable by another user.'
                    }
                }
                if ($isDirectory) {
                    $pending.Enqueue([pscustomobject]@{ Path = $path; Depth = $directory.Depth + 1 })
                }
            }
        } catch [UnauthorizedAccessException] {
            Fail 'RUNTIME_ACL: A selected Python runtime cannot be enumerated.'
        } catch [IO.IOException] {
            Fail 'RUNTIME_TYPE: A selected Python runtime cannot be enumerated.'
        }
    }
}
function Assert-UvArchive([string]$Archive, [string]$Executable = '') {
    if ((Get-Item -LiteralPath $Archive -Force).Length -ne 18039150 -or
        (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant() -cne
            '95f9bc30fbb3574d276e28ac4a6de932d25153645853d13da8c21eec3bc88d06') {
        Fail 'The native uv archive differs from the selected release.'
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        $expected = @{ 'uv.exe' = 42547504; 'uvx.exe' = 348976; 'uvw.exe' = 348976 }
        $members = @($zip.Entries)
        if ($members.Count -ne 3 -or
            @($members | Where-Object {
                -not $expected.ContainsKey($_.FullName) -or
                $_.Length -ne $expected[$_.FullName] -or
                (($($_.ExternalAttributes) -shr 16) -band 0xF000) -notin @(0, 0x8000)
            }).Count -ne 0 -or
            @($members | Select-Object -ExpandProperty FullName -Unique).Count -ne 3) {
            Fail 'The native uv archive has an unexpected inventory.'
        }
        if ($Executable) {
            $inputStream = ($members | Where-Object { $_.FullName -ceq 'uv.exe' })[0].Open()
            try {
                $output = [IO.File]::Open($Executable, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
                try {
                    $buffer = New-Object byte[] 65536
                    $size = 0
                    while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                        $size += $read
                        if ($size -gt 42547504) { Fail 'The native uv executable exceeds its byte limit.' }
                        $output.Write($buffer, 0, $read)
                    }
                    if ($size -ne 42547504) { Fail 'The native uv executable is incomplete.' }
                } finally { $output.Dispose() }
            } finally { $inputStream.Dispose() }
        }
    } finally { $zip.Dispose() }
}
function Assert-PinnedUv([string]$Executable, [string]$Root) {
    Require-PrivateExecutablePath $Executable $Root
    if ((Get-Item -LiteralPath $Executable -Force).Length -ne 42547504 -or
        (Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash.ToLowerInvariant() -cne
            'a0d2742d49564a32488753b02e76276e7b5ef1b1ea8cf30bcbf06ee28f60cd73') {
        Fail 'The retained native uv executable differs from the selected release.'
    }
}
function Select-Uv([string]$Data, [string]$Downloads, [string]$Curl) {
    $available = Native 'uv.exe'
    if ($available) {
        $admitted = $false
        try {
            Require-PrivateExecutablePath $available (Split-Path -Parent $available) -Optional
            $admitted = $true
        } catch [Security.SecurityException] {
            Stage 'Keep: the other uv installation unchanged. Use a pinned Site Ops tooling copy.'
        }
        if ($admitted) {
            if ((Get-Item -LiteralPath $available -Force).Length -eq 42547504 -and
                (Get-FileHash -LiteralPath $available -Algorithm SHA256).Hash.ToLowerInvariant() -ceq
                    'a0d2742d49564a32488753b02e76276e7b5ef1b1ea8cf30bcbf06ee28f60cd73') {
                Stage 'Keep: the selected qualified uv installation.'
                return $available
            }
            Stage 'Keep: the other uv installation unchanged. Use a pinned Site Ops tooling copy.'
        }
    }
    if (-not $available) {
        $commandHome = if ($env:UV_TOOL_BIN_DIR) {
            $env:UV_TOOL_BIN_DIR
        } else {
            Join-Path $env:USERPROFILE '.local\bin'
        }
        Ensure-UvStorage $commandHome
        $maintenance = Join-Path $commandHome 'uv.exe'
        if (Test-Path -LiteralPath $maintenance) {
            Assert-PinnedUv $maintenance $commandHome
            Stage 'Keep: the selected qualified uv maintenance command.'
            return $maintenance
        }
    }
    $parent = Join-Path $Data 'tools\uv'
    Ensure-UvStorage $parent
    $cache = Join-Path $parent '0.12.20'
    $archive = Join-Path $cache 'uv-windows.zip'
    $uv = Join-Path $cache 'uv.exe'
    if (Test-Path -LiteralPath $cache) {
        Require-PrivateDataRoot $cache
        if (@(Get-ChildItem -LiteralPath $cache -Force).Count -ne 2) {
            Fail 'The native uv tooling cache has an unexpected inventory. Inspect it before repair.'
        }
        Require-PrivateExecutablePath $archive $cache
        Assert-UvArchive $archive
        Assert-PinnedUv $uv $cache
    } else {
        $incoming = Join-Path $Downloads 'uv-windows.zip'
        & $Curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' `
            --tlsv1.2 --max-redirs 3 --max-time 180 --max-filesize 18039150 `
            --output $incoming 'https://github.com/astral-sh/uv/releases/download/0.12.20/uv-x86_64-pc-windows-msvc.zip'
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $incoming -PathType Leaf)) {
            Fail 'The native uv archive could not be downloaded.'
        }
        Assert-UvArchive $incoming (Join-Path $Downloads 'uv.exe')
        Assert-PinnedUv (Join-Path $Downloads 'uv.exe') $Downloads
        Require-PrivateDataRoot $cache
        Move-Item -LiteralPath $incoming -Destination $archive -ErrorAction Stop
        Move-Item -LiteralPath (Join-Path $Downloads 'uv.exe') -Destination $uv -ErrorAction Stop
        Require-PrivateExecutablePath $archive $cache
        Assert-UvArchive $archive
        Assert-PinnedUv $uv $cache
    }
    if (-not $available) {
        try {
            [IO.File]::Copy($uv, $maintenance, $false)
        } catch [IO.IOException] {
            Fail 'An unrelated uv command occupies the native maintenance location. Inspect it before installation.'
        }
        Assert-PinnedUv $maintenance $commandHome
        Stage "uv maintenance directory: $commandHome. Add it to your PATH if needed."
    }
    return $uv
}
function Get-ManagedPythonCandidates([string]$Directory) {
    Require-PrivateDataRoot $Directory -Managed
    $count = 0
    try {
        foreach ($path in [IO.Directory]::EnumerateFileSystemEntries($Directory)) {
            $count++
            if ($count -gt 1024) {
                Fail 'The uv-managed Python directory exceeds its safe inventory limit.'
            }
            $name = [IO.Path]::GetFileName($path)
            if ($name -cnotmatch '^cpython-(3\.(?:10|11|12|13|14)\.[0-9]+)-windows-x86_64-none$') {
                continue
            }
            $version = $Matches[1]
            $python = Join-Path $path 'python.exe'
            [pscustomobject]@{ Version = $version; Python = $python }
        }
    } catch [UnauthorizedAccessException] {
        Fail 'The uv-managed Python inventory cannot be inspected.'
    } catch [IO.IOException] {
        Fail 'The uv-managed Python inventory cannot be inspected.'
    }
}
function Select-ManagedPython([string]$Uv, [string]$Directory, [string]$Tools) {
    $siteopsHome = Join-Path $Tools 'siteops'
    $existingVersion = ''
    $existingHome = ''
    if (Test-Path -LiteralPath $siteopsHome) {
        Require-PrivateDataRoot $siteopsHome -Managed
        $configuration = Join-Path $siteopsHome 'pyvenv.cfg'
        Require-PrivateExecutablePath $configuration $Tools
        if ((Get-Item -LiteralPath $configuration).Length -gt 65536) {
            Fail 'The current Site Ops runtime configuration exceeds its byte limit.'
        }
        $text = [IO.File]::ReadAllText($configuration)
        $versionMatch = [regex]::Match($text, '(?m)^version_info\s*=\s*(3\.(?:10|11|12|13|14)\.[0-9]+)\s*$')
        $homeMatch = [regex]::Match($text, '(?m)^home\s*=\s*(.+?)\s*$')
        if (-not $versionMatch.Success -or -not $homeMatch.Success) {
            Fail 'The current Site Ops runtime configuration needs inspection.'
        }
        $existingVersion = $versionMatch.Groups[1].Value
        $existingHome = $homeMatch.Groups[1].Value
    }
    $records = @(Get-ManagedPythonCandidates $Directory)
    $selected = $null
    if ($existingVersion) {
        $selected = @($records | Where-Object {
            $_.Version -ceq $existingVersion -and
            (Split-Path -Parent $_.Python) -ieq $existingHome
        } | Select-Object -First 1)
        if ($selected.Count -ne 1) {
            Fail 'The existing Site Ops runtime is not an available uv-managed Python. Inspect it or run uv tool uninstall siteops after review, then retry.'
        } else { $selected = $selected[0] }
    }
    if (-not $selected -and $records.Count) {
        $selected = $records | Sort-Object `
            @{ Expression = { if ($_.Version -ceq '3.11.16') { 0 } else { 1 } } }, `
            @{ Expression = { [version]$_.Version }; Descending = $true } |
            Select-Object -First 1
    }
    if (-not $selected) {
        $concrete = Join-Path $Directory 'cpython-3.11.16-windows-x86_64-none'
        if (Test-Path -LiteralPath $concrete) {
            Fail 'The uv-managed Python installation is incomplete. Inspect it before repair.'
        }
        Stage 'Provisioning uv-managed CPython 3.11.16 without command aliases or registry changes.'
        $null = Invoke-Uv $Uv @('python', 'install', '3.11.16', '--no-bin', '--no-registry')
        $matches = @(Get-ManagedPythonCandidates $Directory | Where-Object {
            $_.Version -ceq '3.11.16' -and $_.Python -ieq (Join-Path $concrete 'python.exe')
        })
        if ($matches.Count -ne 1) { Fail 'The selected uv-managed Python is unavailable.' }
        $selected = $matches[0]
    }
    $python = $selected.Python
    Require-PrivateDataRoot (Split-Path -Parent $python) -Managed
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        Fail 'The uv-managed Python installation is incomplete. Inspect it before repair.'
    }
    Require-PrivateRuntimeTree (Split-Path -Parent $python) $Directory $python
    $parts = $selected.Version.Split('.')
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $identity = & $python -I -S -B -c 'import sys;print(sys.implementation.name,sys.version_info[0],sys.version_info[1],sys.version_info[2],sys.maxsize>4294967296,sys._base_executable)' 2>$null
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($code -ne 0 -or $identity -cne
        "cpython $($parts[0]) $($parts[1]) $($parts[2]) True $python") {
        Fail 'The selected concrete uv-managed Python differs from the installed runtime.'
    }
    return $python
}
function Select-GitHubCli() {
    $path = Native 'gh.exe'
    $version = ''
    if ($path) {
        try {
            # The verifier runs only after its file and every parent pass admission.
            Require-PrivateExecutablePath $path $path -Optional
        } catch [Security.SecurityException] {
            Fail 'The GitHub CLI executable must be owned by an administrator or the current user and protected from other users.'
        }
        $version = & $path version 2>$null | Select-Object -First 1
    }
    if ($version -cnotmatch '^gh version 2\.([0-9]+)\.[0-9]+' -or [int]$Matches[1] -lt 95) {
        Fail 'GitHub CLI 2.95 or newer is required. Install it from https://cli.github.com, then retry.'
    }
    return $path
}

if ($ContentRelease) {
    if ($Release -or $ContentRelease -cnotmatch '^v[0-9][0-9A-Za-z._-]{0,100}$') {
        Fail 'Choose either -Release for an engine or -ContentRelease for its signed engine selection.'
    }
    $Release = $ContentRelease
}
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
$curl = Native 'curl.exe'
if (-not $curl) { Fail 'Windows curl.exe is required for anonymous HTTPS downloads.' }
$gh = Select-GitHubCli
Stage "Release: $Release ($SourceCommit) from $Repository."
Stage 'Uses the installed GitHub CLI, checksum-pinned uv 0.12.20, and uv-managed Python.'
Stage 'Existing uv and Python installations are not upgraded. No account is signed in.'
if (Get-Command 'uv.exe' -CommandType Application -ErrorAction SilentlyContinue) {
    Stage 'Check: the existing uv executable. Preserve it if another version is installed.'
} else {
    Stage 'Add: pinned native uv in protected tooling storage and expose it for maintenance.'
}
Stage 'Check: ordinary uv tool, command, and managed Python storage.'
if ($Replace) { Stage 'The selected build will explicitly replace or repair an existing Site Ops installation.' }
if (-not (Get-Command 'az.cmd', 'az.exe' -CommandType Application -ErrorAction SilentlyContinue)) {
    Stage 'Azure CLI was not found. Install it before deploying: https://aka.ms/installazurecli'
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

$data = Join-Path $env:LOCALAPPDATA 'siteops'
Require-PrivateDataRoot $data
$staging = Join-Path $data 'install-staging'
Require-PrivateDataRoot $staging
foreach ($entry in Get-ChildItem -LiteralPath $staging -Force) {
    if ($entry.LastWriteTimeUtc -lt [DateTime]::UtcNow.AddHours(-24)) {
        # .NET deletes a link itself and never recurses through it.
        try {
            if ($entry.PSIsContainer) { [IO.Directory]::Delete($entry.FullName, $true) } else { [IO.File]::Delete($entry.FullName) }
        } catch [IO.IOException], [UnauthorizedAccessException] { }
    }
}
# Staging shares the cache volume, so retained downloads move into place without copying.
$download = Join-Path $staging ([guid]::NewGuid().ToString('N'))
Require-PrivateDataRoot $download
$callerTemp = @($env:TEMP, $env:TMP, $env:TMPDIR)
try {
    $temporary = Join-Path $download 'temp'
    Require-PrivateDataRoot $temporary
    $env:TEMP = $env:TMP = $env:TMPDIR = $temporary
    $engineRelease = $Release
    $engineCommit = $SourceCommit
    $engineRef = $SourceRef
    $engineCaller = $Caller
    $engineVersion = ''
    $referenceDownload = $null
    if ($ContentRelease) {
        $referenceId = Get-SelectionKey $Release $SourceCommit $SourceRef $Caller
        $referenceRoot = Join-Path $data 'engine-references'
        Require-PrivateDataRoot $referenceRoot
        $referenceCache = Join-Path $referenceRoot $referenceId
        $referenceAssets = $referenceCache
        if (Test-Path -LiteralPath $referenceCache) {
            Require-PrivateDataRoot $referenceCache
            if (@(Get-ChildItem -LiteralPath $referenceCache -Force).Count -ne 2) {
                Fail 'Retained engine reference bytes are incomplete. Inspect them before retrying.'
            }
            Stage "Rechecking the retained content release's engine selection."
        } else {
            $referenceDownload = Join-Path $download 'reference'
            Require-PrivateDataRoot $referenceDownload
            $referenceAssets = $referenceDownload
            foreach ($asset in @('siteops-engine.json', 'siteops-engine.json.attestation.jsonl')) {
                $limit = if ($asset.EndsWith('.attestation.jsonl')) { 2097152 } else { 16384 }
                & $curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' `
                    --tlsv1.2 --max-redirs 3 --max-time 180 --max-filesize $limit `
                    --output (Join-Path $referenceAssets $asset) `
                    ("https://github.com/$Repository/releases/download/$Release/$asset")
                if ($LASTEXITCODE -ne 0) {
                    Fail "The content release's signed engine reference is unavailable. Use that release's explicit engine installation instructions."
                }
            }
        }
        foreach ($asset in @('siteops-engine.json', 'siteops-engine.json.attestation.jsonl')) {
            $path = Join-Path $referenceAssets $asset
            Require-PrivateExecutablePath $path $referenceAssets
            $length = (Get-Item -LiteralPath $path).Length
            $limit = if ($asset.EndsWith('.attestation.jsonl')) { 2097152 } else { 16384 }
            if ($length -lt 1 -or $length -gt $limit) { Fail 'The engine reference or proof is empty or oversized.' }
        }
        Stage "Checking the content release's engine reference."
        $referencePath = Join-Path $referenceAssets 'siteops-engine.json'
        Verify-ReleaseAsset $referencePath $SourceCommit $SourceRef $Caller '_release-candidate.yaml'
        $selectedEngine = Read-EngineReference $referencePath $Release $SourceCommit $SourceRef $Caller
        $engineRelease = $selectedEngine.Release
        $engineCommit = $selectedEngine.Commit
        $engineRef = $selectedEngine.SourceRef
        $engineCaller = $selectedEngine.Caller
        $engineVersion = $selectedEngine.Version
        Stage "Selected engine: $engineVersion from $engineRelease ($engineCommit)."
    }
    $base = 'https://github.com/' + $Repository + '/releases/download/' +
        [uri]::EscapeDataString($engineRelease) + '/'
    $cacheId = Get-SelectionKey $engineRelease $engineCommit $engineRef $engineCaller
    $cache = Join-Path $data ("install-downloads\" + $cacheId)
    $cacheRoot = Join-Path $data 'install-downloads'
    Require-PrivateDataRoot $cacheRoot
    $assets = $download
    if (Test-Path -LiteralPath $cache) {
        Require-PrivateDataRoot $cache
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
        Require-PrivateExecutablePath $asset $assets
    }
    if ($ContentRelease -and (
        (Get-Item -LiteralPath $archive).Length -ne $selectedEngine.BundleSize -or
        (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -cne $selectedEngine.BundleSha -or
        (Get-Item -LiteralPath "$archive.attestation.jsonl").Length -ne $selectedEngine.ProofSize -or
        (Get-FileHash -LiteralPath "$archive.attestation.jsonl" -Algorithm SHA256).Hash.ToLowerInvariant() -cne $selectedEngine.ProofSha)) {
        Fail 'The engine bundle or proof differs from the signed content selection.'
    }
    Stage "Checking the bundle's source, signer, caller, and runner."
    Verify-ReleaseAsset $archive $engineCommit $engineRef $engineCaller '_siteops-distribution.yaml'
    $bundleId = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    $installerHelper = Get-InstallerHelper $archive $download $engineVersion
    Require-PrivateExecutablePath $installerHelper $download
    $root = Join-Path $data 'bundles'
    Require-PrivateDataRoot $root
    $bundle = Join-Path $root $bundleId
    # Create the retained bundle as the user. An elevated helper would otherwise assign it to Administrators.
    Require-PrivateDataRoot $bundle
    $script:UvCacheDir = Join-Path $download 'uv-cache'
    Ensure-UvStorage $script:UvCacheDir
    foreach ($name in @('UV_TOOL_DIR', 'UV_TOOL_BIN_DIR', 'UV_PYTHON_INSTALL_DIR')) {
        $override = [Environment]::GetEnvironmentVariable($name)
        if ($override) { Ensure-UvStorage $override }
    }
    $uv = Select-Uv $data $download $curl
    $toolDirectory = Invoke-Uv $uv @('tool', 'dir')
    $commandDirectory = Invoke-Uv $uv @('tool', 'dir', '--bin')
    $pythonDirectory = Invoke-Uv $uv @('python', 'dir')
    Ensure-UvStorage $toolDirectory
    Ensure-UvStorage $commandDirectory
    Ensure-UvStorage $pythonDirectory
    $expectedCommand = Join-Path $commandDirectory 'siteops.exe'
    $toolHome = Join-Path $toolDirectory 'siteops'
    if (Test-Path -LiteralPath $expectedCommand) {
        Require-PrivateExecutablePath $expectedCommand $commandDirectory
        if (-not (Test-Path -LiteralPath $toolHome)) {
            Fail 'The exposed command belongs to another installation. Remove it with its original manager.'
        }
    }
    $python = Select-ManagedPython $uv $pythonDirectory $toolDirectory
    if (Test-Path -LiteralPath $toolHome) {
        Require-PrivateRuntimeTree $toolHome $toolDirectory (Join-Path $toolHome 'Scripts\python.exe')
    }
    $mode = if ($Replace) { 'replace' } else { 'install' }
    $installed = Check-Payload @($mode, $archive, $bundle, $Repository, $engineCommit,
        $engineRef, $uv, $toolDirectory, $commandDirectory)
    $version = $installed.version
    if ($engineVersion -and $version -cne $engineVersion) {
        Fail 'The installed version differs from the signed content selection.'
    }
    Require-PrivateDataRoot $bundle
    $expectedTarget = Join-Path $toolHome 'Scripts\siteops.exe'
    Require-PrivateExecutablePath $expectedTarget $toolDirectory
    Require-PrivateExecutablePath $expectedCommand $commandDirectory
    $env:PATH = $commandDirectory + ';' + $env:PATH
    $siteops = Native 'siteops.exe'
    if (-not $siteops -or $siteops -ine $expectedCommand) {
        Fail 'The exposed siteops command does not match the selected build.'
    }
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $observed = & $siteops --version 2>$null
        $versionExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($versionExit -ne 0 -or $observed -cne "siteops $version") {
        Fail 'The exposed siteops command does not match the selected build.'
    }
    Stage "Command directory: $commandDirectory. Add it to your current PATH or open a new shell."
    Stage 'For native removal, run uv tool uninstall siteops.'
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
        if (Test-Path -LiteralPath $cache) {
            Fail 'The retained release location changed during installation. Inspect it before retrying.'
        }
        Require-PrivateDataRoot $cache
        foreach ($name in @('siteops-install.zip', 'siteops-install.zip.attestation.jsonl')) {
            try {
                [IO.File]::Move((Join-Path $download $name), (Join-Path $cache $name))
            } catch [IO.IOException] {
                Fail 'The retained release bytes could not be moved without replacing existing files.'
            }
        }
    }
    if ($referenceDownload) {
        if (Test-Path -LiteralPath $referenceCache) {
            Fail 'The engine reference cache changed during installation. Inspect it before retrying.'
        }
        Require-PrivateDataRoot $referenceCache
        foreach ($name in @('siteops-engine.json', 'siteops-engine.json.attestation.jsonl')) {
            try {
                [IO.File]::Move((Join-Path $referenceDownload $name), (Join-Path $referenceCache $name))
            } catch [IO.IOException] {
                Fail 'The verified engine reference could not be retained without replacing existing files.'
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
    $env:TEMP, $env:TMP, $env:TMPDIR = $callerTemp
    if (Test-Path -LiteralPath $download) {
        Remove-Item -LiteralPath $download -Recurse -Force
    }
}
