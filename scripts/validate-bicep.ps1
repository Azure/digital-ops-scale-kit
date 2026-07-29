#!/usr/bin/env pwsh
# Validate Bicep templates compile without errors or warnings.
#
# `az bicep build` exits 0 on warnings, so a warning-only defect such as BCP081
# (unknown resource type or API version) compiles clean and is not caught until a
# live deployment. Warnings are therefore treated as failures here.
#
# Usage:
#   ./scripts/validate-bicep.ps1                          # All .bicep files under workspaces/
#   ./scripts/validate-bicep.ps1 path/to/template.bicep   # Specific file(s)
#   ./scripts/validate-bicep.ps1 workspaces/iot-operations/templates/secretsync/*.bicep
#   ./scripts/validate-bicep.ps1 -AllowWarnings           # Report warnings without failing

param(
    [switch]$AllowWarnings,
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
$warned = @()
$passed = 0

foreach ($file in $bicepFiles) {
    $relPath = [System.IO.Path]::GetRelativePath($repoRoot, $file.FullName)

    # Build to a discarded outfile rather than stdout, so the captured stream holds
    # only diagnostics. Compiled ARM JSON legitimately contains words like "error"
    # (broker log levels), which would otherwise be misread as a diagnostic.
    $outFile = [System.IO.Path]::GetTempFileName()
    $output = az bicep build --file $file.FullName --outfile $outFile 2>&1
    Remove-Item $outFile -Force -ErrorAction SilentlyContinue

    $diagnostics = @($output | Where-Object { "$_".Trim() })
    $warnings = @($diagnostics | Where-Object { $_ -match '\bWarning\b' })

    if ($LASTEXITCODE -ne 0) {
        Write-Host "  FAIL  $relPath" -ForegroundColor Red
        $diagnostics | ForEach-Object { Write-Host "        $_" -ForegroundColor Red }
        $failed += $relPath
    } elseif ($warnings.Count -gt 0 -and -not $AllowWarnings) {
        Write-Host "  WARN  $relPath" -ForegroundColor Red
        $warnings | ForEach-Object { Write-Host "        $_" -ForegroundColor Red }
        $warned += $relPath
    } elseif ($warnings.Count -gt 0) {
        Write-Host "  WARN  $relPath" -ForegroundColor Yellow
        $warnings | ForEach-Object { Write-Host "        $_" -ForegroundColor Yellow }
        $passed++
    } else {
        Write-Host "  OK    $relPath" -ForegroundColor Green
        $passed++
    }
}

Write-Host ''
if ($failed.Count -eq 0 -and $warned.Count -eq 0) {
    Write-Host "All $passed file(s) compiled successfully." -ForegroundColor Green
    exit 0
} else {
    if ($failed.Count -gt 0) {
        Write-Host "$($failed.Count) file(s) failed to compile." -ForegroundColor Red
    }
    if ($warned.Count -gt 0) {
        Write-Host "$($warned.Count) file(s) compiled with warnings. Fix them, or re-run with -AllowWarnings." -ForegroundColor Red
    }
    Write-Host "$passed file(s) passed." -ForegroundColor Red
    exit 1
}
