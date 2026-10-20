# install.ps1 - the Odoo Edu Manager installer for Windows.
# Normally started by double-clicking install.cmd; see windows\README.md.
param(
    [switch]$NoShortcuts,
    [string]$ImagePath = ''    # an Ubuntu 24.04 .wsl/.tar.gz you already have, instead of downloading one
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here 'lib.ps1')

$installDir = Join-Path $env:LOCALAPPDATA 'OdooEduManager'
New-Item -ItemType Directory -Force -Path $installDir | Out-Null
$logging = $false
try {
    Start-Transcript -Path (Join-Path $installDir 'install.log') -Append | Out-Null
    $logging = $true
}
catch { $logging = $false }   # no log file is no reason not to install

$code = 1
try {
    # files from a downloaded ZIP carry a "came from the internet" mark that makes Windows ask before running them
    Get-ChildItem -Path (Split-Path -Parent $here) -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
    $result = @(Invoke-OdooEduInstall -SourceDir (Split-Path -Parent $here) -ScriptsDir $here -InstallDir $installDir -ImagePath $ImagePath -NoShortcuts:$NoShortcuts)
    $code = [int]$result[-1]
}
catch {
    Write-Host ''
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host ''
    Write-Host ('The full log is here: ' + (Join-Path $installDir 'install.log'))
    Write-Host 'If you ask for help, send that file and the output of diagnose.cmd.'
    $code = 1
}
if ($logging) {
    try { Stop-Transcript | Out-Null } catch { $logging = $false }
}
exit $code
