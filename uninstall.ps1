$ErrorActionPreference = "Stop"

# Use the installed source directly. Uninstall never downloads or replaces a
# connector checkout, and keeps local usage/backups by default.
$InstallDir = $env:AGENTCAT_CONNECTORS_DIR
if ([string]::IsNullOrWhiteSpace($InstallDir)) {
  $InstallDir = Join-Path $HOME ".agentcat\connectors"
}
$Installer = Join-Path $InstallDir "scripts\install.py"
if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) {
  throw "Installed connector source was not found. Set AGENTCAT_CONNECTORS_DIR to its location."
}
$PythonPrefix = $null
$Candidates = @()
if (-not [string]::IsNullOrWhiteSpace($env:AGENTCAT_PYTHON)) {
  $Candidates += ,@($env:AGENTCAT_PYTHON)
}
$Python = Get-Command python -ErrorAction SilentlyContinue
if ($Python) { $Candidates += ,@($Python.Source) }
$Py = Get-Command py -ErrorAction SilentlyContinue
if ($Py) { $Candidates += ,@($Py.Source, "-3") }
foreach ($Candidate in $Candidates) {
  $Command = $Candidate[0]
  $PrefixArguments = @()
  if ($Candidate.Length -gt 1) { $PrefixArguments += $Candidate[1..($Candidate.Length - 1)] }
  try {
    & $Command @PrefixArguments -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" *> $null
    if ($LASTEXITCODE -eq 0) { $PythonPrefix = $Candidate; break }
  } catch {}
}
if (-not $PythonPrefix) { throw "Python 3.9 or newer is required to uninstall the connector." }
$Command = $PythonPrefix[0]
$Arguments = @()
if ($PythonPrefix.Length -gt 1) { $Arguments += $PythonPrefix[1..($PythonPrefix.Length - 1)] }
$Arguments += @($Installer, "--repo-dir", $InstallDir, "uninstall")
& $Command @Arguments
if ($LASTEXITCODE -ne 0) { throw "Connector uninstall failed with exit code $LASTEXITCODE" }
