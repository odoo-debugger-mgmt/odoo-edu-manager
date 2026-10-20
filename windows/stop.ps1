# stop.ps1 - stops Odoo Edu Manager and every Odoo instance it runs.
# This is what the "Stop Odoo Edu Manager" Start menu entry runs.
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib.ps1')

$config = Read-InstallConfig -InstallDir (Join-Path $env:LOCALAPPDATA 'OdooEduManager')
if ($null -eq $config) {
    Write-Host 'Odoo Edu Manager is not installed.'
    Start-Sleep -Seconds 3
    exit 0
}
Write-Host 'Stopping Odoo Edu Manager and every Odoo instance it runs ...'
Invoke-Wsl @('--terminate', $config.distro) -Capture | Out-Null
Write-Host 'Stopped. Your instances and their data are kept - open "Odoo Edu Manager" and press Start on the ones you need.'
Start-Sleep -Seconds 3
