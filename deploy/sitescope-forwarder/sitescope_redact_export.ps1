<#
.SYNOPSIS
    Read-only redacted EXPORT of the SiteScope OM-integration log.

.DESCRIPTION
    Runs ON THE SITESCOPE SERVER. Reads the OM-integration log READ-ONLY,
    REDACTS embedded credentials on this box, and writes a clean tab-delimited
    .tsv you can safely copy to another machine (e.g. the laptop running UMD)
    and feed to tools/sitescope_local_ingest.py.

    Use this when the UMD is NOT reachable from the SiteScope server (localhost
    demo). For a live push, use sitescope_forwarder.ps1 instead.

    SAFETY (by design):
      * READ-ONLY on the log. Opened with FileShare.ReadWrite, so the SiteScope
        service and the Netcool probe keep writing normally. This script NEVER
        locks, writes, deletes, rotates, or truncates the log or any existing
        file.
      * Redaction happens on THIS box, before the file is written. The output
        .tsv never contains raw credentials or unredacted URLs.
      * The ONLY file it writes is the -OutFile you choose.

.PARAMETER LogPath
    Path to the SiteScope OM-integration log.

.PARAMETER OutFile
    Where to write the redacted .tsv (default: .\sitescope_redacted.tsv).

.PARAMETER TailLines
    Export only the last N lines (0 = whole file). Default 5000.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\sitescope_redact_export.ps1 `
      -LogPath 'D:\SiteScope\logs\HPSiteScopeOperationsManagerIntegration.log' `
      -OutFile 'D:\Scripts\umd\sitescope_redacted.tsv' -TailLines 5000
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$LogPath,
    [string]$OutFile = '.\sitescope_redacted.tsv',
    [int]$TailLines = 5000
)

$ErrorActionPreference = 'Stop'

# --- Redaction (whitelist-safe; identical to the forwarder / UMD rules) ------
function Protect-Line([string]$s) {
    if ([string]::IsNullOrEmpty($s)) { return $s }
    $s = [regex]::Replace($s, '(?i)([?&;]\s*[A-Za-z0-9_.\-]*(?:user|pass|pwd|token|account|login)[A-Za-z0-9_.\-]*=)(?!<REDACTED>)([^&\s\t;]*)', '${1}<REDACTED>')
    $s = [regex]::Replace($s, '(?i)\b(sisuser|sispass|password|account|login|token)=(?!<REDACTED>)([^&\s\t;]*)', '${1}=<REDACTED>')
    $s = [regex]::Replace($s, '(?i)(://)[^/@\s:]+:[^/@\s]+@', '${1}<REDACTED>@')
    return $s
}

if (-not (Test-Path -LiteralPath $LogPath)) {
    throw "Log not found: $LogPath"
}

# Read the whole file READ-ONLY without ever locking it against the writers.
$fs = [System.IO.FileStream]::new(
    $LogPath,
    [System.IO.FileMode]::Open,
    [System.IO.FileAccess]::Read,
    [System.IO.FileShare]::ReadWrite)
try {
    $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::ASCII)
    try { $raw = $sr.ReadToEnd() } finally { $sr.Dispose() }
} finally {
    $fs.Dispose()
}

# Split on CRLF/LF, drop blank lines.
$lines = $raw -split "`r`n|`n" | Where-Object { $_.Trim().Length -gt 0 }

if ($TailLines -gt 0 -and $lines.Count -gt $TailLines) {
    $lines = $lines[($lines.Count - $TailLines)..($lines.Count - 1)]
}

# Redact every line BEFORE writing anything to disk.
$redCount = 0
$out = New-Object System.Collections.Generic.List[string]
foreach ($ln in $lines) {
    $r = Protect-Line $ln
    if ($r -ne $ln) { $redCount++ }
    $out.Add($r)
}

# Write the redacted export (ASCII, no BOM) — the ONLY file this script writes.
$dir = Split-Path -Parent $OutFile
if ($dir -and -not (Test-Path -LiteralPath $dir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
}
[System.IO.File]::WriteAllLines(
    $OutFile,
    $out.ToArray(),
    (New-Object System.Text.ASCIIEncoding))

Write-Host ("Exported {0} redacted line(s) to {1}" -f $out.Count, $OutFile)
Write-Host ("Lines with credentials redacted: {0}" -f $redCount)
if ($out.Count -gt 0) {
    $sample = $out[0]
    if ($sample.Length -gt 200) { $sample = $sample.Substring(0, 200) }
    Write-Host ("Sample (redacted): " + $sample)
}
Write-Host ""
Write-Host "Copy this .tsv to the UMD machine, then run:"
Write-Host "  python tools\sitescope_local_ingest.py --file <copied.tsv> --token `$env:SITESCOPE_INGEST_TOKEN --instance SiteScope-141"
