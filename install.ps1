$ErrorActionPreference = "Stop"

$ManifestUrl = $env:AGENTCAT_CONNECTORS_MANIFEST_URL
if ([string]::IsNullOrWhiteSpace($ManifestUrl)) {
  $ManifestUrl = "https://github.com/yong076/agentcat-connectors/releases/latest/download/connector-manifest.json"
}
$InstallDir = $env:AGENTCAT_CONNECTORS_DIR
if ([string]::IsNullOrWhiteSpace($InstallDir)) {
  $InstallDir = Join-Path $HOME ".agentcat\connectors"
}
$BackupRoot = $env:AGENTCAT_CONNECTORS_BACKUP_ROOT
if ([string]::IsNullOrWhiteSpace($BackupRoot)) {
  $BackupRoot = Join-Path $HOME ".agentcat\backups\connector-source"
}

function Resolve-Python {
  $python = Get-Command python -ErrorAction SilentlyContinue
  if ($python) { return @($python.Source) }
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) { return @($py.Source, "-3") }
  throw "Python 3 is required to install Agent Cat Connectors."
}

function Invoke-Python([string[]]$Prefix, [string[]]$Arguments) {
  $command = $Prefix[0]
  $allArguments = @()
  if ($Prefix.Length -gt 1) { $allArguments += $Prefix[1..($Prefix.Length - 1)] }
  $allArguments += $Arguments
  & $command @allArguments
  if ($LASTEXITCODE -ne 0) {
    throw "Agent Cat connector installer failed with exit code $LASTEXITCODE"
  }
}

function Test-IsDevelopmentCheckout {
  if (-not $PSScriptRoot) { return $false }
  if (-not (Test-Path (Join-Path $PSScriptRoot "scripts\install.py"))) { return $false }
  if (-not (Test-Path (Join-Path $PSScriptRoot "bin\agentcat"))) { return $false }
  $source = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
  $target = [System.IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
  return -not $source.Equals($target, [System.StringComparison]::OrdinalIgnoreCase)
}

function Invoke-DevelopmentInstall([string[]]$PythonPrefix) {
  Write-Host "[agentcat] Installing from development checkout $PSScriptRoot"
  Invoke-Python $PythonPrefix @(
    (Join-Path $PSScriptRoot "scripts\install.py"),
    "--repo-dir", $PSScriptRoot,
    "install"
  )
}

function Read-ReleaseManifest([string]$Path) {
  $manifest = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
  $version = [string]$manifest.version
  $archiveUrl = [string]$manifest.archiveUrl
  $sha256 = [string]$manifest.sha256
  $contractVersion = [int]$manifest.contractVersion
  if ($version -notmatch '^\d+\.\d+\.\d+$') { throw "Connector manifest version is invalid." }
  if ($archiveUrl -notmatch '^https://github\.com/yong076/agentcat-connectors/') {
    throw "Connector manifest archive URL is not an approved GitHub URL."
  }
  if ($sha256 -cnotmatch '^[0-9a-f]{64}$') { throw "Connector manifest SHA256 is invalid." }
  if ($contractVersion -lt 1) { throw "Connector manifest contractVersion is invalid." }
  return $manifest
}

function Install-VerifiedRelease([string[]]$PythonPrefix) {
  $tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("agentcat-connectors." + [System.Guid]::NewGuid().ToString("N"))
  $manifestPath = Join-Path $tmpDir "connector-manifest.json"
  $archivePath = Join-Path $tmpDir "agentcat-connectors.zip"
  $extractPath = Join-Path $tmpDir "extracted"
  New-Item -ItemType Directory -Path $tmpDir | Out-Null
  try {
    $directArchiveUrl = $env:AGENTCAT_CONNECTORS_ARCHIVE_URL
    $directSha256 = $env:AGENTCAT_CONNECTORS_SHA256
    $directVersion = $env:AGENTCAT_CONNECTORS_VERSION
    $directContractVersion = $env:AGENTCAT_CONNECTORS_CONTRACT_VERSION
    if (-not [string]::IsNullOrWhiteSpace($directArchiveUrl)) {
      if ([string]::IsNullOrWhiteSpace($directSha256) -or [string]::IsNullOrWhiteSpace($directVersion)) {
        throw "Pinned archive installs require AGENTCAT_CONNECTORS_SHA256 and AGENTCAT_CONNECTORS_VERSION."
      }
      $manifest = [ordered]@{
        version = $directVersion
        contractVersion = if ([string]::IsNullOrWhiteSpace($directContractVersion)) { 2 } else { [int]$directContractVersion }
        archiveUrl = $directArchiveUrl
        sha256 = $directSha256
      }
      [System.IO.File]::WriteAllText(
        $manifestPath,
        ($manifest | ConvertTo-Json),
        [System.Text.UTF8Encoding]::new($false)
      )
    } else {
      Invoke-WebRequest -Uri $ManifestUrl -OutFile $manifestPath -UseBasicParsing
    }

    $release = Read-ReleaseManifest $manifestPath
    Write-Host "[agentcat] Downloading verified connector $($release.version)"
    Invoke-WebRequest -Uri $release.archiveUrl -OutFile $archivePath -UseBasicParsing
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash.ToLowerInvariant()
    if ($actual -cne [string]$release.sha256) {
      throw "Connector archive checksum mismatch. Existing connector was not touched."
    }

    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractPath
    $candidate = Get-ChildItem -LiteralPath $extractPath -Directory | Where-Object {
      (Test-Path (Join-Path $_.FullName "scripts\public_channel_install.py")) -and
      (Test-Path (Join-Path $_.FullName "contracts\connector-v1.json"))
    } | Select-Object -First 1
    if (-not $candidate) { throw "Verified archive does not contain the public channel installer." }

    Invoke-Python $PythonPrefix @(
      (Join-Path $candidate.FullName "scripts\public_channel_install.py"),
      "--archive", $archivePath,
      "--manifest", $manifestPath,
      "--install-dir", $InstallDir,
      "--backup-root", $BackupRoot
    )
  } finally {
    if (Test-Path -LiteralPath $tmpDir) {
      Remove-Item -LiteralPath $tmpDir -Recurse -Force
    }
  }
}

$PythonPrefix = Resolve-Python
if (Test-IsDevelopmentCheckout) {
  Invoke-DevelopmentInstall $PythonPrefix
} else {
  Install-VerifiedRelease $PythonPrefix
}
