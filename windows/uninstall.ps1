# uninstall.ps1 - removes Odoo Edu Manager and the Linux system it lives in.
# Everything inside that Linux goes with it: instances, databases, files.
#
#   -Yes     do not ask (for scripts and agents; a person is asked to type YES)
#   -Pause   keep the window open at the end, so the result can be read (the Start menu
#            entry and Settings > Apps use it; uninstall.cmd has its own pause)
param([switch]$Yes, [switch]$Pause)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib.ps1')

function Wait-ForClose([bool]$Wait) {
    if ($Wait) { [void](Read-Host 'Press Enter to close') }
}

$installDir = Join-Path $env:LOCALAPPDATA 'OdooEduManager'
$config = Read-InstallConfig -InstallDir $installDir
$distro = 'OdooEdu'
if ($null -ne $config) { $distro = $config.distro }

Write-Host ''
Write-Host 'This removes Odoo Edu Manager and the Linux system it runs in:' -ForegroundColor Yellow
Write-Host '  - every Odoo instance, with its database and files'
Write-Host '  - the downloaded Ubuntu image and the installer files'
Write-Host '  - its Start menu entries, its desktop icon and its entry in Settings > Apps'
Write-Host 'It cannot be undone. Your other WSL distributions and Windows files are not touched.'
Write-Host 'WSL itself stays switched on.'
Write-Host ''
if (-not $Yes) {
    $answer = Read-Host 'Type YES (in capitals) to remove it'
    if ($answer -cne 'YES') {
        Write-Host 'Nothing was changed.'
        Wait-ForClose $Pause
        exit 0
    }
}

if ((Get-WslDistros) -contains $distro) {
    $r = Invoke-Wsl @('--unregister', $distro) -Capture
    if ($r.ExitCode -ne 0) { Write-Warn ('Removing the Linux system failed: ' + $r.Text) }
    else { Write-Good ('Removed "{0}".' -f $distro) }
}
foreach ($folder in @((Get-StartMenuFolder), (Get-DesktopFolder))) {
    foreach ($name in @('Odoo Edu Manager.lnk', 'Stop Odoo Edu Manager.lnk', 'Uninstall Odoo Edu Manager.lnk')) {
        $path = Join-Path $folder $name
        if (Test-Path $path) { Remove-Item -Force $path }
    }
}
Write-Good 'Removed the Start menu entries.'
if (Unregister-Uninstaller) { Write-Good 'Removed the entry in Settings > Apps.' }

# WSL makes an empty Start menu folder named after the Linux when it is imported; take it away if
# it is still there and empty (a folder with anything in it is not ours to remove)
$distroFolder = Join-Path (Get-StartMenuFolder) $distro
if ((Test-Path $distroFolder) -and (@(Get-ChildItem -Force -Path $distroFolder).Count -eq 0)) {
    Remove-Item -Force -Path $distroFolder
}

if (Test-Path $installDir) {
    # this script lives in that folder, so it is removed a moment after it ends; the helper must not
    # start inside the folder it is deleting, or Windows refuses to remove it
    $command = 'ping -n 3 127.0.0.1 >nul & rmdir /s /q "{0}"' -f $installDir
    Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', $command) -WindowStyle Hidden -WorkingDirectory ([System.IO.Path]::GetTempPath())
}
Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
Wait-ForClose $Pause
