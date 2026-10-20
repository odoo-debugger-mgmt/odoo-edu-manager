# start.ps1 - starts Odoo Edu Manager and opens it in the browser.
# This is what the "Odoo Edu Manager" Start menu entry runs.
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib.ps1')

$installDir = Join-Path $env:LOCALAPPDATA 'OdooEduManager'
$config = Read-InstallConfig -InstallDir $installDir
if ($null -eq $config) {
    Write-Host 'Odoo Edu Manager is not installed yet - run install.cmd first.' -ForegroundColor Red
    Read-Host 'Press Enter to close'
    exit 1
}
$distro = $config.distro
$user = $config.user
$url = 'http://localhost:{0}' -f [int]$config.port

function Test-AppUp {
    try { return ((Invoke-WebRequest -UseBasicParsing -Uri ($url + '/help') -TimeoutSec 2).StatusCode -eq 200) }
    catch { return $false }
}

Write-Host 'Odoo Edu Manager' -ForegroundColor Cyan
if (-not ((Get-WslDistros) -contains $distro)) {
    Write-Host ('The Linux system "{0}" is missing - run install.cmd again.' -f $distro) -ForegroundColor Red
    Read-Host 'Press Enter to close'
    exit 1
}

Write-Host 'Starting Linux and Docker ...'
Start-KeepAlive -Distro $distro
if (-not (Wait-ForDocker -Distro $distro -User $user -Seconds 90)) {
    Write-Warn 'Docker is not answering yet. The page will explain what to do.'
}

if (Test-AppUp) {
    Start-Process $url
    Write-Host ('Odoo Edu Manager is already running - opened {0} in your browser.' -f $url)
    Start-Sleep -Seconds 3
    exit 0
}

# open the browser as soon as the app answers
$null = Start-Job -ScriptBlock {
    for ($i = 0; $i -lt 120; $i++) {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri ($using:url + '/help') -TimeoutSec 2 | Out-Null
            Start-Process $using:url
            return
        }
        catch { Start-Sleep -Seconds 1 }
    }
}

Write-Host ''
Write-Host ('Odoo Edu Manager is starting at {0}' -f $url) -ForegroundColor Green
Write-Host 'Keep this window open while you work. Closing it stops the app, but your Odoo instances keep running.'
Write-Host 'Use "Stop Odoo Edu Manager" from the Start menu to stop everything.'
Write-Host ''
& wsl.exe -d $distro -u $user --exec bash -lc 'exec ~/.local/bin/odoo-edu-manager'
Write-Host ''
Write-Host 'The app has stopped.'
Read-Host 'Press Enter to close'
