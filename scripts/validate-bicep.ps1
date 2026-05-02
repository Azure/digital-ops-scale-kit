#!/usr/bin/env pwsh
# Validate Bicep templates compile without errors.
#
# Usage:
#   ./scripts/validate-bicep.ps1                          # All .bicep files under workspaces/
#   ./scripts/validate-bicep.ps1 path/to/template.bicep   # Specific file(s)
#   ./scripts/validate-bicep.ps1 workspaces/iot-operations/templates/secretsync/*.bicep

param(
    [Parameter(ValueFromRemainingArguments)]
    [string[]]$Files
)

$ErrorActionPreference = 'Continue'
$repoRoot = Split-Path $PSScriptRoot -Parent

# Discover files: use provided paths or find all .bicep files
if ($Files.Count -gt 0) {
    $bicepFiles = @()
    foreach ($pattern in $Files) {
        $resolved = if ([System.IO.Path]::IsPathRooted($pattern)) { $pattern } else { Join-Path $repoRoot $pattern }
        $bicepFiles += Get-Item $resolved -ErrorAction SilentlyContinue
    }
} else {
    $bicepFiles = Get-ChildItem -Path (Join-Path $repoRoot 'workspaces') -Filter '*.bicep' -Recurse
}

if ($bicepFiles.Count -eq 0) {
    Write-Host 'No .bicep files found.' -ForegroundColor Yellow
    exit 0
}

Write-Host "Validating $($bicepFiles.Count) Bicep file(s)..." -ForegroundColor Cyan
Write-Host ''

$failed = @()
$passed = 0

foreach ($file in $bicepFiles) {
    $relPath = [System.IO.Path]::GetRelativePath($repoRoot, $file.FullName)

    # Build to stdout (discarded). Errors go to stderr.
    $output = az bicep build --file $file.FullName --stdout 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  OK    $relPath" -ForegroundColor Green
        $passed++
    } else {
        Write-Host "  FAIL  $relPath" -ForegroundColor Red
        # Show error details indented
        $output | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object {
            Write-Host "        $_" -ForegroundColor Red
        }
        $failed += $relPath
    }
}

Write-Host ''
if ($failed.Count -eq 0) {
    Write-Host "All $passed file(s) compiled successfully." -ForegroundColor Green
    exit 0
} else {
    Write-Host "$($failed.Count) file(s) failed, $passed passed." -ForegroundColor Red
    exit 1
}
