# Agent Cat - Gemini / Antigravity Windows diagnostic
# READ-ONLY. Prints NO tokens or secrets - only file existence, which keys are
# present, paths, and the connector's own (non-secret) error strings.
#
# How to run (PowerShell):
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\diagnose-gemini-windows.ps1
# or paste the whole file into a PowerShell window.
#
# Then send the full output back. It tells us which of three things blocks the
# Gemini/Antigravity quota: (a) auth-type detection, (b) oauth creds location,
# (c) OAuth client-credential (oauth2.js) discovery.

$ErrorActionPreference = 'SilentlyContinue'
function Row($k, $v) { "{0,-32} {1}" -f $k, $v }

Write-Output "==== Agent Cat Gemini/Antigravity Windows diagnostic ===="
Row "time"        (Get-Date -Format o)
Row "os"          ([System.Environment]::OSVersion.VersionString)
Row "userprofile" $env:USERPROFILE

# ---------- connector live state (this is the key signal) ----------
Write-Output "`n-- connector --"
Row "agentcat version" ((& agentcat version 2>$null) -join ' ')
try {
    $snap = Invoke-RestMethod -Uri "http://127.0.0.1:8765/v1/snapshot" -TimeoutSec 6
    $g = $snap.providers.gemini
    Row "gemini.status" $g.status
    Row "gemini.source" $g.source
    Row "gemini process count" (@($snap.processes | Where-Object { $_.kind -eq 'gemini' }).Count)
    Write-Output "gemini.limits (quota numbers + error strings, no secrets):"
    ($g.limits | ConvertTo-Json -Depth 5)
} catch {
    Row "snapshot" "ERROR: $($_.Exception.Message)  (is agentcatd running on 8765?)"
}

# ---------- (a) auth type ----------
Write-Output "`n-- (a) read_gemini_auth_type inputs --"
$settings = Join-Path $env:USERPROFILE ".gemini\settings.json"
Row ".gemini\settings.json" (Test-Path $settings)
if (Test-Path $settings) {
    try {
        $s = Get-Content $settings -Raw | ConvertFrom-Json
        Row "  security.auth.selectedType" $s.security.auth.selectedType   # what the connector reads now
        Row "  selectedAuthType (legacy)"  $s.selectedAuthType             # old flat schema (connector does NOT read this)
        Row "  top-level keys" (($s.PSObject.Properties.Name) -join ',')
    } catch { Row "  parse" "ERROR" }
}
Row "env GOOGLE_GENAI_USE_GCA"     $env:GOOGLE_GENAI_USE_GCA
Row "env GEMINI_API_KEY set"       ([bool]$env:GEMINI_API_KEY)
Row "env GOOGLE_GENAI_USE_VERTEXAI" $env:GOOGLE_GENAI_USE_VERTEXAI

# ---------- (b) oauth creds ----------
Write-Output "`n-- (b) oauth creds (presence + key names only, NO values) --"
$credCandidates = @(
    (Join-Path $env:USERPROFILE ".gemini\oauth_creds.json"),
    (Join-Path $env:USERPROFILE ".gemini\antigravity-cli\oauth_creds.json"),
    (Join-Path $env:USERPROFILE ".antigravity\oauth_creds.json"),
    (Join-Path $env:APPDATA      "antigravity\oauth_creds.json"),
    (Join-Path $env:LOCALAPPDATA "antigravity\oauth_creds.json")
)
foreach ($c in $credCandidates) {
    if (Test-Path $c) {
        try {
            $j = Get-Content $c -Raw | ConvertFrom-Json
            $keys = ($j.PSObject.Properties.Name) -join ','
            Row "FOUND" "$c"
            Row "  keys" $keys
            Row "  has refresh_token" ([bool]$j.refresh_token)
            Row "  has client_id/secret" ("$([bool]$j.client_id)/$([bool]$j.client_secret)")
        } catch { Row "FOUND (parse error)" $c }
    } else {
        Row "absent" $c
    }
}

# ---------- .gemini directory layout ----------
Write-Output "`n-- .gemini directory entries --"
$gdir = Join-Path $env:USERPROFILE ".gemini"
if (Test-Path $gdir) { (Get-ChildItem $gdir -Force | Select-Object -ExpandProperty Name) -join ', ' }

# ---------- (c) gemini cli + oauth2.js discovery ----------
Write-Output "`n-- (c) gemini CLI + oauth2.js --"
$gcmd = (Get-Command gemini -ErrorAction SilentlyContinue).Source
$acmd = (Get-Command agy    -ErrorAction SilentlyContinue).Source
Row "which gemini" $gcmd
Row "which agy"    $acmd
$npmPrefix = (& npm config get prefix 2>$null)
Row "npm prefix"  $npmPrefix

$searchRoots = @(
    $npmPrefix,
    (Join-Path $env:APPDATA "npm"),
    (Join-Path $env:APPDATA "npm\node_modules"),
    $(if ($acmd)  { Split-Path (Split-Path $acmd  -Parent) -Parent }),
    $(if ($gcmd)  { Split-Path (Split-Path $gcmd  -Parent) -Parent }),
    (Join-Path $env:LOCALAPPDATA "Programs\Antigravity"),
    (Join-Path $env:LOCALAPPDATA "Antigravity")
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique

Write-Output "searching these roots for code_assist\oauth2.js:"
$searchRoots | ForEach-Object { "  $_" }
$found = $false
foreach ($r in $searchRoots) {
    Get-ChildItem -Path $r -Recurse -Filter "oauth2.js" -Depth 12 -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match 'code_assist' } |
        ForEach-Object { $found = $true; "  FOUND oauth2.js: $($_.FullName)" }
}
if (-not $found) { Write-Output "  (no code_assist\oauth2.js found - this is likely break-point (c))" }

# ---------- running processes ----------
Write-Output "`n-- processes (agy/gemini/antigravity/node) --"
Get-Process | Where-Object { $_.ProcessName -match 'agy|gemini|antigravity|node' } |
    Select-Object Id, ProcessName | Format-Table -AutoSize | Out-String

Write-Output "==== end of diagnostic (no tokens/secrets printed) ===="
