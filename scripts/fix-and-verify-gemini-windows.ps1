# Agent Cat - one-shot Gemini/Antigravity Windows fix + verify
#
# Installs the fixed connector from the windows-gemini-antigravity branch
# (this replaces the installed connector and restarts the AgentCatD daemon via
# the official installer), then checks whether the Gemini/Antigravity quota now
# shows up. PASS = fixed. If it still fails, it auto-runs the read-only
# diagnostic so the whole story is in one paste (no second round needed).
#
# Run in PowerShell (no admin needed):
#   irm -UseBasicParsing https://raw.githubusercontent.com/yong076/agentcat-connectors/windows-gemini-antigravity/scripts/fix-and-verify-gemini-windows.ps1 | iex
#
# Prints NO tokens or secrets.

$ErrorActionPreference = 'Stop'
$ref = 'windows-gemini-antigravity'
Write-Output '==== Agent Cat: install fix + verify Gemini/Antigravity ===='

# --- 1) download the branch ---
$zipUrl  = "https://github.com/yong076/agentcat-connectors/archive/refs/heads/$ref.zip"
$tmp     = Join-Path $env:TEMP ('agentcat-fix-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
$zipPath = Join-Path $tmp 'src.zip'
try {
    Write-Output "downloading $ref ..."
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
    Expand-Archive -Path $zipPath -DestinationPath $tmp -Force
    $src = Get-ChildItem -Path $tmp -Directory -Filter 'agentcat-connectors-*' | Select-Object -First 1
    if (-not $src) { throw 'archive extract failed' }

    # --- 2) official install (replaces files + restarts the AgentCatD task) ---
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $py) { $py = (Get-Command py -ErrorAction SilentlyContinue).Source }
    if (-not $py) { throw 'Python 3 is required (python or py not found on PATH)' }
    Write-Output 'installing fixed connector + restarting agentcatd ...'
    & $py (Join-Path $src.FullName 'scripts\install.py') --repo-dir $src.FullName install
} finally {
    if (Test-Path $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}

# --- 3) wait for the daemon ---
Write-Output 'waiting for agentcatd on 127.0.0.1:8765 ...'
$snap = $null
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 1
    try { $snap = Invoke-RestMethod 'http://127.0.0.1:8765/v1/snapshot' -TimeoutSec 3; break } catch { $snap = $null }
}
if (-not $snap) {
    Write-Output 'FAIL: agentcatd did not come up on 8765. Try toggling Agent Cat (quit + reopen), then re-run.'
    return
}

# --- 4) verify gemini/antigravity quota ---
$g   = $snap.providers.gemini
$lim = $g.limits
$hasQuota = $false
if ($lim -and $lim.status -eq 'ok') {
    if ($null -ne $lim.weeklyUsedPercent -or $lim.quotas -or $null -ne $lim.shortUsedPercent) { $hasQuota = $true }
}
Write-Output ''
Write-Output ("gemini.status        = " + $g.status)
Write-Output ("gemini.limits.status = " + $lim.status)

if ($hasQuota) {
    Write-Output ''
    Write-Output 'PASS - Gemini/Antigravity quota is now available. You can close the issue.'
    Write-Output ($lim | ConvertTo-Json -Depth 5)
} else {
    Write-Output ''
    Write-Output 'STILL NO QUOTA - running the read-only diagnostic so we can pinpoint it.'
    Write-Output 'Please paste this entire output back (no tokens/secrets are printed):'
    Write-Output ''
    try {
        Invoke-Expression ((Invoke-WebRequest -UseBasicParsing "https://raw.githubusercontent.com/yong076/agentcat-connectors/$ref/scripts/diagnose-gemini-windows.ps1").Content)
    } catch {
        Write-Output ("diagnostic fetch failed: " + $_.Exception.Message)
    }
}
Write-Output '==== done ===='
