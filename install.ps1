$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/yong076/agentcat-connectors.git"
$ArchiveUrl = "https://github.com/yong076/agentcat-connectors/archive/refs/heads/main.zip"
$InstallDir = $env:AGENTCAT_CONNECTORS_DIR
if ([string]::IsNullOrWhiteSpace($InstallDir)) {
  $InstallDir = Join-Path $HOME ".agentcat\connectors"
}

function Install-FromArchive {
  $tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("agentcat-connectors." + [System.Guid]::NewGuid().ToString("N"))
  $archive = Join-Path $tmpDir "agentcat-connectors.zip"
  New-Item -ItemType Directory -Path $tmpDir | Out-Null
  try {
    Invoke-WebRequest -Uri $ArchiveUrl -OutFile $archive -UseBasicParsing
    Expand-Archive -Path $archive -DestinationPath $tmpDir -Force
    $extracted = Get-ChildItem -Path $tmpDir -Directory -Filter "agentcat-connectors-*" | Select-Object -First 1
    if ($null -eq $extracted -or !(Test-Path (Join-Path $extracted.FullName "scripts\install.py")) -or !(Test-Path (Join-Path $extracted.FullName "bin\agentcat"))) {
      throw "Downloaded archive did not contain Agent Cat connector files."
    }
    if (Test-Path $InstallDir) {
      Remove-Item -LiteralPath $InstallDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $InstallDir) -Force | Out-Null
    Move-Item -LiteralPath $extracted.FullName -Destination $InstallDir
  } finally {
    if (Test-Path $tmpDir) {
      Remove-Item -LiteralPath $tmpDir -Recurse -Force
    }
  }
}

function Resolve-RepoDir {
  if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "scripts\install.py")) -and (Test-Path (Join-Path $PSScriptRoot "bin\agentcat"))) {
    return $PSScriptRoot
  }

  if (Test-Path (Join-Path $InstallDir ".git")) {
    try {
      git -C $InstallDir pull --ff-only | Out-Host
    } catch {
      Write-Host "[agentcat] GitHub update failed; using existing local connector copy."
    }
    return $InstallDir
  }

  if ((Test-Path (Join-Path $InstallDir "scripts\install.py")) -and (Test-Path (Join-Path $InstallDir "bin\agentcat"))) {
    try {
      Install-FromArchive
    } catch {
      Write-Host "[agentcat] GitHub archive update failed; using existing local connector copy."
    }
    return $InstallDir
  }

  New-Item -ItemType Directory -Path (Split-Path -Parent $InstallDir) -Force | Out-Null
  try {
    git clone --depth 1 --filter=blob:none $RepoUrl $InstallDir | Out-Host
  } catch {
    Write-Host "[agentcat] git clone failed; retrying with GitHub archive download."
    Install-FromArchive
  }
  return $InstallDir
}

function Invoke-PythonInstall($RepoDir) {
  $python = Get-Command python -ErrorAction SilentlyContinue
  if ($python) {
    & $python.Source (Join-Path $RepoDir "scripts\install.py") --repo-dir $RepoDir install
    return
  }
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) {
    & $py.Source -3 (Join-Path $RepoDir "scripts\install.py") --repo-dir $RepoDir install
    return
  }
  throw "Python 3 is required to install Agent Cat Connectors."
}

$RepoDir = Resolve-RepoDir
Invoke-PythonInstall $RepoDir
