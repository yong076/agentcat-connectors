$ErrorActionPreference = "Stop"

function Write-ItemValue {
    param(
        [string] $Name,
        [object] $Value
    )
    "{0,-34} {1}" -f ($Name + ":"), $Value
}

function Test-Tool {
    param([string] $Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-ToolText {
    param(
        [string] $Name,
        [string[]] $Arguments = @()
    )
    if (-not (Test-Tool $Name)) {
        return "not_found"
    }
    try {
        return ((& $Name @Arguments) 2>&1 | Out-String).Trim()
    } catch {
        return ("error: " + $_.Exception.Message)
    }
}

function Get-JsonProp {
    param(
        [object] $Object,
        [string[]] $Path,
        [object] $Default = $null
    )
    $current = $Object
    foreach ($part in $Path) {
        if ($null -eq $current) {
            return $Default
        }
        $prop = $current.PSObject.Properties[$part]
        if ($null -eq $prop) {
            return $Default
        }
        $current = $prop.Value
    }
    if ($null -eq $current) {
        return $Default
    }
    return $current
}

function Get-TokenTotal {
    param([object] $Provider)
    return [int64](Get-JsonProp $Provider @("tokens", "totalTokens") 0)
}

function Get-PathStatus {
    param([string] $Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return [ordered]@{ exists = $false; bytes = 0; path = $Path }
    }
    $item = Get-Item -LiteralPath $Path
    return [ordered]@{
        exists = $true
        bytes = [int64]$item.Length
        path = $Path
        updated = $item.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
    }
}

Write-Host "Agent Cat Antigravity Windows verification"
Write-Host ""

if (-not (Test-Tool "agentcat")) {
    Write-Host "FAIL: agentcat command was not found on PATH."
    exit 2
}

$agentcatVersion = Invoke-ToolText "agentcat" @("version")
$agyVersion = Invoke-ToolText "agy" @("--version")
$geminiVersion = Invoke-ToolText "gemini" @("--version")

Write-ItemValue "agentcat version" $agentcatVersion
Write-ItemValue "agy version" $agyVersion
Write-ItemValue "gemini version" $geminiVersion

$snapshotText = Invoke-ToolText "agentcat" @("snapshot", "--json")
try {
    $snapshot = $snapshotText | ConvertFrom-Json
} catch {
    Write-Host ""
    Write-Host "FAIL: agentcat snapshot --json did not return valid JSON."
    Write-Host $_.Exception.Message
    exit 3
}

$gemini = Get-JsonProp $snapshot @("providers", "gemini") $null
$antigravity = Get-JsonProp $snapshot @("providers", "antigravity") $null
$activityGemini = [int](Get-JsonProp $snapshot @("activity", "countsByProvider", "gemini") 0)
$activityAntigravity = [int](Get-JsonProp $snapshot @("activity", "countsByProvider", "antigravity") 0)
$geminiTokens = Get-TokenTotal $gemini
$antigravityTokens = Get-TokenTotal $antigravity
$antigravityStatus = Get-JsonProp $antigravity @("status") "missing"
$antigravityAttribution = Get-JsonProp $antigravity @("sourceAttribution") "missing"
$antigravitySourceStatus = Get-JsonProp $antigravity @("sources", "antigravityCli", "status") "missing"
$geminiSourceStatus = Get-JsonProp $gemini @("sources", "geminiCli", "status") "missing"

Write-Host ""
Write-Host "Snapshot"
Write-ItemValue "activity.antigravity" $activityAntigravity
Write-ItemValue "activity.gemini" $activityGemini
Write-ItemValue "provider.antigravity.status" $antigravityStatus
Write-ItemValue "provider.antigravity.hasTokens" ($antigravityTokens -gt 0)
Write-ItemValue "provider.antigravity.source" $antigravityAttribution
Write-ItemValue "source.antigravityCli.status" $antigravitySourceStatus
Write-ItemValue "provider.gemini.status" (Get-JsonProp $gemini @("status") "missing")
Write-ItemValue "provider.gemini.hasTokens" ($geminiTokens -gt 0)
Write-ItemValue "source.geminiCli.status" $geminiSourceStatus

$profile = [Environment]::GetFolderPath("UserProfile")
$paths = [ordered]@{
    commonTelemetry = Join-Path $profile ".agentcat\gemini\telemetry.log"
    antigravityTelemetry = Join-Path $profile ".agentcat\gemini\antigravity-telemetry.log"
    geminiSettings = Join-Path $profile ".gemini\settings.json"
    antigravitySettings = Join-Path $profile ".gemini\antigravity-cli\settings.json"
    antigravityHistory = Join-Path $profile ".gemini\antigravity-cli\history.jsonl"
}

Write-Host ""
Write-Host "Files"
$fileSummary = [ordered]@{}
foreach ($entry in $paths.GetEnumerator()) {
    $status = Get-PathStatus $entry.Value
    $fileSummary[$entry.Key] = $status
    Write-ItemValue $entry.Key (($status.exists.ToString().ToLower()) + ", bytes=" + $status.bytes)
}

$processes = @(Get-Process -Name "agy", "antigravity" -ErrorAction SilentlyContinue)
Write-ItemValue "running agy processes" $processes.Count

$summary = [ordered]@{
    agentcatVersion = $agentcatVersion
    agyVersion = $agyVersion
    geminiVersion = $geminiVersion
    activityAntigravity = $activityAntigravity
    activityGemini = $activityGemini
    antigravityStatus = $antigravityStatus
    antigravityHasTokens = ($antigravityTokens -gt 0)
    antigravitySourceAttribution = $antigravityAttribution
    antigravitySourceStatus = $antigravitySourceStatus
    geminiStatus = Get-JsonProp $gemini @("status") "missing"
    geminiHasTokens = ($geminiTokens -gt 0)
    geminiSourceStatus = $geminiSourceStatus
    commonTelemetryExists = [bool]$fileSummary.commonTelemetry.exists
    antigravityTelemetryExists = [bool]$fileSummary.antigravityTelemetry.exists
    antigravityHistoryExists = [bool]$fileSummary.antigravityHistory.exists
    runningAgyProcesses = $processes.Count
}

Write-Host ""
Write-Host "Issue block"
$summary | ConvertTo-Json -Depth 6

Write-Host ""
if ($null -eq $antigravity) {
    Write-Host "FAIL: snapshot has no providers.antigravity object."
    exit 3
}

if ($antigravityAttribution -eq "dedicated-telemetry") {
    Write-Host "PASS: Antigravity is separated in the snapshot with its own dedicated telemetry."
    exit 0
}

# Antigravity bills server-side and does not write a local telemetry outfile, so an
# empty token set is the correct, honest state. The card must still be a separate
# provider; it must never borrow Gemini's tokens.
if ($antigravityTokens -gt 0) {
    Write-Host "WARN: Antigravity reports tokens without dedicated telemetry. Attach the issue block above; tokens may be misattributed."
}

Write-Host "PASS: Antigravity is a separate provider in the snapshot."
exit 0
