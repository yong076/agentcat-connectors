# Agent Cat - one-shot Antigravity Windows fix + verify
#
# Installs the latest connector from main, then verifies whether Antigravity is
# separated from Gemini in the local snapshot.
#
# Run in PowerShell (no admin needed):
#   irm -UseBasicParsing https://raw.githubusercontent.com/yong076/agentcat-connectors/main/scripts/fix-and-verify-antigravity-windows.ps1 | iex
#
# Prints no prompt contents, API keys, or telemetry event bodies.

$ErrorActionPreference = "Stop"
$ref = "main"

Write-Output "==== Agent Cat: install fix + verify Antigravity ===="

$zipUrl = "https://github.com/yong076/agentcat-connectors/archive/refs/heads/$ref.zip"
$tmp = Join-Path $env:TEMP ("agentcat-antigravity-fix-" + [Guid]::NewGuid().ToString("N"))
$zipPath = Join-Path $tmp "src.zip"

New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    Write-Output "downloading $ref ..."
    Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
    Expand-Archive -Path $zipPath -DestinationPath $tmp -Force
    $src = Get-ChildItem -Path $tmp -Directory -Filter "agentcat-connectors-*" | Select-Object -First 1
    if (-not $src) {
        throw "archive extract failed"
    }

    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $py) {
        $py = (Get-Command py -ErrorAction SilentlyContinue).Source
    }
    if (-not $py) {
        throw "Python 3 is required (python or py not found on PATH)"
    }

    Write-Output "installing connector + restarting agentcatd ..."
    & $py (Join-Path $src.FullName "scripts\install.py") --repo-dir $src.FullName install

    Write-Output ""
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $src.FullName "scripts\verify-antigravity-windows.ps1")
} finally {
    if (Test-Path -LiteralPath $tmp) {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Output "==== done ===="
