<#
.SYNOPSIS
    SiteScope -> UMD push forwarder (PowerShell 5.1, Windows Server 2019).

.DESCRIPTION
    Tails the SiteScope OM-integration log, REDACTS embedded credentials on this
    box, and POSTs redacted lines to the UMD ingest endpoint in batches. The UMD
    parses/normalizes and derives hosts + alerts.

    SAFETY (this runs on a critical production server):
      * READ-ONLY on the log. Opened with FileShare.ReadWrite, so the SiteScope
        service and the Netcool probe keep writing normally — we never lock it.
      * Never writes, deletes, rotates, or truncates the log or any existing
        file. The ONLY things this script writes are under -state_dir
        (checkpoint + its own capped log).
      * Never touches the Netcool probe or the SiteScope service.
      * Reads only the NEW bytes since the last checkpoint (light on CPU/IO).
      * Fails safe: on any send failure the checkpoint is NOT advanced, so
        nothing is lost; it simply retries next cycle. event_id makes re-sends
        idempotent (no duplicates in the UMD).
      * The forwarder's own log NEVER contains raw log lines or unredacted URLs.

.PARAMETER ConfigPath
    Path to config.json (defaults to config.json next to this script).

.PARAMETER DryRun
    Parse + redact + report counts and sends NOTHING. Does not advance the
    checkpoint. Use this first to validate safely on the box.

.PARAMETER Loop
    Run continuously, sleeping -IntervalSeconds between cycles. Default is a
    single cycle (for Task Scheduler every 60s).

.PARAMETER IntervalSeconds
    Sleep between cycles in -Loop mode (default 60).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\sitescope_forwarder.ps1 -DryRun
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\sitescope_forwarder.ps1
#>
[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'config.json'),
    [switch]$DryRun,
    [switch]$Loop,
    [int]$IntervalSeconds = 60
)

$ErrorActionPreference = 'Stop'

# --- Redaction (whitelist-safe; identical to the UMD's canonical rules) ------
function Protect-Line([string]$s) {
    if ([string]::IsNullOrEmpty($s)) { return $s }
    $s = [regex]::Replace($s, '(?i)([?&;]\s*[A-Za-z0-9_.\-]*(?:user|pass|pwd|token|account|login)[A-Za-z0-9_.\-]*=)(?!<REDACTED>)([^&\s\t;]*)', '${1}<REDACTED>')
    $s = [regex]::Replace($s, '(?i)\b(sisuser|sispass|password|account|login|token)=(?!<REDACTED>)([^&\s\t;]*)', '${1}=<REDACTED>')
    $s = [regex]::Replace($s, '(?i)(://)[^/@\s:]+:[^/@\s]+@', '${1}<REDACTED>@')
    return $s
}

# --- Small helpers -----------------------------------------------------------
function Get-Sha256Hex([byte[]]$bytes, [int]$count) {
    if ($count -le 0) { return "" }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return -join ($sha.ComputeHash($bytes, 0, $count) | ForEach-Object { $_.ToString('x2') }) }
    finally { $sha.Dispose() }
}

function Get-NtfsFileId([string]$path) {
    # Stable per-physical-file id (survives rename, changes on new file). Best
    # rotation signal. Non-fatal if fsutil is unavailable.
    try {
        $out = & fsutil file queryfileid "$path" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return (($out -split ' ')[-1]).Trim() }
    } catch {}
    return $null
}

function Get-Token($cfg) {
    if ($cfg.token_file -and (Test-Path -LiteralPath $cfg.token_file)) {
        $sec = Get-Content -LiteralPath $cfg.token_file -Raw | ConvertTo-SecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
        try { return [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    }
    foreach ($scope in @('Process', 'User', 'Machine')) {
        $t = [Environment]::GetEnvironmentVariable($cfg.token_env_var, $scope)
        if (-not [string]::IsNullOrEmpty($t)) { return $t }
    }
    return $null
}

# --- Logging (own capped log; NEVER contains raw log lines) ------------------
$script:LogPath = $null
function Write-FwdLog([string]$level, [string]$msg) {
    $line = ('{0} {1,-5} {2}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'), $level, $msg)
    try {
        if ($script:LogPath) {
            # Rotate own log at ~2 MB (keep one .1 backup).
            if ((Test-Path $script:LogPath) -and ((Get-Item $script:LogPath).Length -gt 2MB)) {
                Move-Item -Force $script:LogPath ($script:LogPath + '.1')
            }
            Add-Content -LiteralPath $script:LogPath -Value $line
        }
    } catch {}
    Write-Host $line
}

# --- JSON body (manual build so a 1-element array never collapses) -----------
function New-IngestBody([string]$instance, [bool]$heartbeat, [string[]]$lines) {
    $arr = @()
    foreach ($l in $lines) { $arr += ($l | ConvertTo-Json) }  # ConvertTo-Json escapes safely
    $inst = ($instance | ConvertTo-Json)
    $hb = $heartbeat.ToString().ToLower()
    return '{"source_instance":' + $inst + ',"heartbeat":' + $hb + ',"lines":[' + ($arr -join ',') + ']}'
}

function Send-Batch($cfg, [string]$token, [string]$body) {
    $headers = @{ Authorization = "Bearer $token" }
    for ($attempt = 0; $attempt -le [int]$cfg.max_retries; $attempt++) {
        try {
            return Invoke-RestMethod -Uri $cfg.umd_url -Method Post -ContentType 'application/json' `
                -Body $body -Headers $headers -TimeoutSec ([int]$cfg.http_timeout_sec)
        } catch {
            if ($attempt -eq [int]$cfg.max_retries) { throw }
            $backoff = [Math]::Min(60, [Math]::Pow(2, $attempt)) + ((Get-Random -Minimum 0 -Maximum 1000) / 1000.0)
            Write-FwdLog 'WARN' ("send failed (attempt {0}/{1}): {2}; retry in {3:N1}s" -f ($attempt + 1), ([int]$cfg.max_retries + 1), $_.Exception.Message, $backoff)
            Start-Sleep -Seconds $backoff
        }
    }
}

# --- Read new complete lines since the checkpoint ---------------------------
function Read-NewLines($cfg, $state) {
    $log = $cfg.log_path
    $result = [ordered]@{ lines = @(); new_offset = 0; size = 0; head = ''; file_id = $null; rotated = $false }

    $fs = [System.IO.FileStream]::new($log, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    try {
        $size = $fs.Length
        $headBuf = New-Object byte[] 256
        $headRead = $fs.Read($headBuf, 0, [int][Math]::Min(256, $size))
        $head = Get-Sha256Hex $headBuf $headRead
        $fileId = Get-NtfsFileId $log

        $result.size = $size
        $result.head = $head
        $result.file_id = $fileId

        # Decide start offset: rotation / truncation detection.
        $startOffset = 0
        if ($state) {
            $rotated = $false
            if ($state.file_id -and $fileId -and ($fileId -ne $state.file_id)) { $rotated = $true }
            elseif ($size -lt [long]$state.offset) { $rotated = $true }
            elseif ($state.head -and $head -and ($head -ne $state.head) -and ([long]$state.offset -gt 0)) { $rotated = $true }
            if ($rotated) {
                $result.rotated = $true
                Write-FwdLog 'INFO' 'rotation/truncation detected — reading current file from start'
                $startOffset = 0
            } else {
                $startOffset = [long]$state.offset
            }
        }

        if ($size -gt $startOffset) {
            $toRead = [int][Math]::Min(($size - $startOffset), [int]$cfg.max_bytes_per_read)
            $fs.Seek($startOffset, [System.IO.SeekOrigin]::Begin) | Out-Null
            $buf = New-Object byte[] $toRead
            $read = $fs.Read($buf, 0, $toRead)
            $text = [System.Text.Encoding]::UTF8.GetString($buf, 0, $read)
            $lastNl = $text.LastIndexOf("`n")
            if ($lastNl -ge 0) {
                $complete = $text.Substring(0, $lastNl + 1)
                $consumed = [System.Text.Encoding]::UTF8.GetByteCount($complete)
                $result.new_offset = $startOffset + $consumed
                $result.lines = @($complete -split "`r`n" | Where-Object { $_.Trim() -ne '' })
            } else {
                # No complete line yet (partial write) — leave offset untouched.
                $result.new_offset = $startOffset
            }
        } else {
            $result.new_offset = $startOffset
        }
    } finally {
        $fs.Close()
    }
    return $result
}

# --- One cycle ---------------------------------------------------------------
function Invoke-Cycle($cfg, [string]$token, [string]$statePath) {
    $state = $null
    if (Test-Path -LiteralPath $statePath) {
        try { $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json } catch { $state = $null }
    }

    $r = Read-NewLines $cfg $state
    $rawLines = $r.lines

    # Redact BEFORE anything leaves the box.
    $redacted = New-Object System.Collections.Generic.List[string]
    $redCount = 0
    foreach ($ln in $rawLines) {
        $rl = Protect-Line $ln
        if ($rl -ne $ln) { $redCount++ }
        $redacted.Add($rl)
    }

    if ($DryRun) {
        Write-FwdLog 'INFO' ("DRY-RUN: {0} new line(s), {1} redacted; nothing sent, checkpoint unchanged" -f $redacted.Count, $redCount)
        if ($redacted.Count -gt 0) {
            Write-FwdLog 'INFO' ('DRY-RUN sample (redacted): ' + ($redacted[0].Substring(0, [Math]::Min(180, $redacted[0].Length))))
        }
        return
    }

    # Send in batches. If ANY batch fails permanently, do NOT advance checkpoint.
    $sent = 0
    if ($redacted.Count -eq 0) {
        # Heartbeat so the UMD marks the forwarder alive.
        [void](Send-Batch $cfg $token (New-IngestBody $cfg.source_instance $true @()))
        Write-FwdLog 'INFO' 'heartbeat sent (0 new lines)'
    } else {
        $batch = [int]$cfg.batch_size
        for ($i = 0; $i -lt $redacted.Count; $i += $batch) {
            $end = [Math]::Min($i + $batch, $redacted.Count)
            $chunk = $redacted.GetRange($i, $end - $i).ToArray()
            $resp = Send-Batch $cfg $token (New-IngestBody $cfg.source_instance $false $chunk)
            $sent += $chunk.Count
        }
        Write-FwdLog 'INFO' ("sent {0} line(s) in {1} redaction(s)" -f $sent, $redCount)
    }

    # Advance checkpoint ONLY after everything above succeeded.
    $newState = [ordered]@{
        offset  = $r.new_offset
        size    = $r.size
        head    = $r.head
        file_id = $r.file_id
        updated = (Get-Date -Format 'o')
    }
    $tmp = $statePath + '.tmp'
    ($newState | ConvertTo-Json -Compress) | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -Force -LiteralPath $tmp -Destination $statePath
}

# --- Main --------------------------------------------------------------------
$cfg = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json

if (-not (Test-Path -LiteralPath $cfg.state_dir)) {
    New-Item -ItemType Directory -Path $cfg.state_dir | Out-Null
}
$script:LogPath = Join-Path $cfg.state_dir 'forwarder.log'
$statePath = Join-Path $cfg.state_dir '.state.json'

[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12
if (-not $cfg.verify_tls) {
    [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
}

$token = $null
if (-not $DryRun) {
    $token = Get-Token $cfg
    if ([string]::IsNullOrEmpty($token)) {
        Write-FwdLog 'ERROR' ("no token found (env '{0}' or token_file). Not sending." -f $cfg.token_env_var)
        exit 2
    }
}

try {
    if ($Loop) {
        Write-FwdLog 'INFO' ("forwarder started (loop, every {0}s)" -f $IntervalSeconds)
        while ($true) {
            try { Invoke-Cycle $cfg $token $statePath }
            catch { Write-FwdLog 'ERROR' ("cycle failed (checkpoint NOT advanced): {0}" -f $_.Exception.Message) }
            Start-Sleep -Seconds $IntervalSeconds
        }
    } else {
        Invoke-Cycle $cfg $token $statePath
    }
} catch {
    Write-FwdLog 'ERROR' ("fatal (checkpoint NOT advanced): {0}" -f $_.Exception.Message)
    exit 1
}
