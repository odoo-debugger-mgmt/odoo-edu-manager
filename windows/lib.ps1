# lib.ps1 - shared code for the Odoo Edu Manager Windows scripts.
#
# ASCII only, and written for Windows PowerShell 5.1 - the one every Windows 10
# and 11 has. Everything that touches the system goes through a small function
# (Invoke-Wsl, Invoke-Curl, ...) so tests/install/windows-scripts.ps1 can replace
# it with a fake and run the whole installer on any machine.

# ---- messages ---------------------------------------------------------------

function Write-Step([string]$Text) { Write-Host ''; Write-Host ('==> ' + $Text) -ForegroundColor Cyan }
function Write-Info([string]$Text) { Write-Host ('    ' + $Text) }
function Write-Warn([string]$Text) { Write-Host ('  ! ' + $Text) -ForegroundColor Yellow }
function Write-Good([string]$Text) { Write-Host ('    ' + $Text) -ForegroundColor Green }

# ---- the outside world (replaced by fakes in the tests) ---------------------

function Get-WindowsBuild { return [int](Get-CimInstance -ClassName Win32_OperatingSystem).BuildNumber }

function Get-CpuArchitecture {
    if ($env:PROCESSOR_ARCHITEW6432) { return $env:PROCESSOR_ARCHITEW6432 }
    return $env:PROCESSOR_ARCHITECTURE
}

# Runs wsl.exe. With -Capture the output comes back as text (WSL used to print
# UTF-16, which shows up as NUL characters between the letters: removed here);
# without it the output goes straight to the console, where a person can read it.
function Invoke-Wsl {
    param([string[]]$Arguments, [switch]$Capture)
    $env:WSL_UTF8 = '1'
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Capture) {
            $raw = & wsl.exe @Arguments 2>&1
            $code = $LASTEXITCODE
            $text = (@($raw) | ForEach-Object { '' + $_ }) -join "`n"
            $text = ($text -replace "`0", '').Trim()
            return [pscustomobject]@{ ExitCode = $code; Text = $text }
        }
        & wsl.exe @Arguments | Out-Host
        return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Text = '' }
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

function Invoke-Curl {
    param([string[]]$Arguments)
    $out = & curl.exe @Arguments
    $code = $LASTEXITCODE
    if ($code -ne 0) { throw ('curl.exe failed with exit code {0} while running: curl {1}' -f $code, ($Arguments -join ' ')) }
    if ($null -eq $out) { return @() }
    return @($out)
}

function Invoke-Robocopy {
    param([string[]]$Arguments)
    & robocopy.exe @Arguments | Out-Null
    if ($LASTEXITCODE -ge 8) { throw ('robocopy failed with exit code {0}' -f $LASTEXITCODE) }
}

function Get-Sha256([string]$Path) { return (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLower() }

function New-Shortcut {
    param([string]$Path, [string]$Target, [string]$Arguments, [string]$WorkingDirectory, [string]$Icon, [string]$Description)
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($Path)
    $link.TargetPath = $Target
    $link.Arguments = $Arguments
    $link.WorkingDirectory = $WorkingDirectory
    $link.Description = $Description
    if ($Icon) { $link.IconLocation = $Icon }
    $link.Save()
}

# ---- checks -----------------------------------------------------------------

function Test-Prerequisites {
    $build = Get-WindowsBuild
    if ($build -lt 19041) {
        throw ('This needs Windows 10 version 2004 (build 19041) or newer, or Windows 11. This computer is on build {0}.' -f $build)
    }
    $arch = Get-CpuArchitecture
    if ($arch -ne 'AMD64') {
        throw ('This installer supports 64-bit Intel/AMD computers. This one reports "{0}".' -f $arch)
    }
}

# Is Windows' hypervisor running? $true / $false, or $null when it cannot be read.
function Get-HypervisorPresent {
    try { return [bool](Get-CimInstance -ClassName Win32_ComputerSystem).HypervisorPresent }
    catch { return $null }
}

# What is wrong with WSL, if anything: Ready, NeedsComponent (Windows' hypervisor is not
# available to it - what a fresh PC looks like) or NotInstalled.
#
# wsl.exe's exit code is NOT a signal: `wsl --status` and `wsl --list` exit with 255 on a
# perfectly healthy WSL that has no Linux installed yet. And its wording follows the
# Windows display language. So Windows' own answer about the hypervisor comes first, and
# wsl.exe's English message is only a second opinion.
function Get-WslState {
    $r = Invoke-Wsl @('--status') -Capture
    if ($r.Text -match 'Virtual Machine Platform|virtualization') { return 'NeedsComponent' }
    if ($r.Text -match 'not installed|not enabled|not recognized') { return 'NotInstalled' }
    if ((Get-HypervisorPresent) -eq $false) { return 'NeedsComponent' }
    if ($r.Text) { return 'Ready' }
    return 'NotInstalled'
}

# Is Windows in the middle of installing something that only a restart completes?
# True / False, or $null when it cannot be read. Readable without administrator rights.
function Test-RebootPending {
    $base = 'HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing'
    $codes = @()
    foreach ($name in @('RebootPending', 'PackagesPending', 'SessionsPending')) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & reg.exe query ($base + '\' + $name) 2>&1 | Out-Null
            $codes += $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previous
        }
    }
    if ($codes -contains 0) { return $true }
    if ($codes -contains 1) { return $false }
    return $null
}

# A moment this installer started waiting for a restart (when it switched the Windows
# component on, or first saw Windows waiting for one), so that restarts since then can
# be counted: a restart that did not finish the job must not be asked for for ever.
function Read-RestartMarker {
    param([string]$InstallDir)
    $path = Join-Path $InstallDir 'wsl-enable.json'
    if (-not (Test-Path $path)) { return $null }
    try { return [datetime](Get-Content -Path $path -Raw | ConvertFrom-Json).since }
    catch { return $null }
}

function Write-RestartMarker {
    param([string]$InstallDir)
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $marker = [pscustomobject]@{ since = (Get-Date).ToString('o') }
    Set-Content -Path (Join-Path $InstallDir 'wsl-enable.json') -Value ($marker | ConvertTo-Json) -Encoding ASCII
}

# How many times Windows has started since a moment (the event log records "event log
# service started" once per boot), or $null when that cannot be read.
function Get-RestartsSince {
    param([datetime]$Since)
    try {
        return @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = 6005; StartTime = $Since } -ErrorAction Stop).Count
    }
    catch {
        if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') { return 0 }   # "none" is reported as an error
        return $null
    }
}

# What Windows says about virtualization, all of it readable without administrator
# rights. A value that cannot be read is $null - "unknown", never "no".
function Get-VirtualizationFacts {
    $facts = [pscustomobject]@{ HypervisorPresent = $null; PlatformEnabled = $null; RebootPending = $null; FirmwareEnabled = $null }
    $facts.HypervisorPresent = Get-HypervisorPresent
    try {
        $feature = @(Get-CimInstance -ClassName Win32_OptionalFeature -Filter "Name = 'VirtualMachinePlatform'")
        if ($feature.Count -gt 0) { $facts.PlatformEnabled = ([int]$feature[0].InstallState -eq 1) }
    }
    catch { $facts.PlatformEnabled = $null }
    try { $facts.FirmwareEnabled = [bool]@(Get-CimInstance -ClassName Win32_Processor)[0].VirtualizationFirmwareEnabled }
    catch { $facts.FirmwareEnabled = $null }
    $facts.RebootPending = Test-RebootPending
    return $facts
}

# What to do about a WSL that cannot start (state is not Ready):
#   RestartFirst    Windows is already waiting for a restart from something else (an update):
#                   switching more on top of that can wedge it, so restart first
#   Enable          turn the Windows component on (asks for administrator rights)
#   RestartAgain    it is on, but Windows has not finished: asking again would change nothing
#   StuckServicing  it is on, and Windows has restarted twice without finishing: a problem in Windows
#   FirmwareOff     virtualization is off in the BIOS/UEFI
#   HypervisorOff   it is on, nothing is pending, yet Windows' hypervisor is not running
function Get-WslPlan {
    param([string]$State, $Facts, $RestartsSince = $null)
    if (($State -eq 'NeedsComponent') -and ($Facts.PlatformEnabled -eq $true)) {
        if ($Facts.FirmwareEnabled -eq $false) { return 'FirmwareOff' }
        if ($Facts.RebootPending -ne $false) {
            if (($null -ne $RestartsSince) -and ($RestartsSince -ge 2)) { return 'StuckServicing' }
            return 'RestartAgain'
        }
        return 'HypervisorOff'
    }
    # The component is not on yet. A restart that Windows is already waiting for belongs to
    # something else: have it done first. If one restart did not clear it, the flag is stale
    # and is not a reason to wait any longer.
    if (($Facts.RebootPending -eq $true) -and (($null -eq $RestartsSince) -or ($RestartsSince -lt 1))) { return 'RestartFirst' }
    return 'Enable'
}

function Get-WslDistros {
    $r = Invoke-Wsl @('--list', '--quiet') -Capture
    if ($r.ExitCode -ne 0) { return @() }
    return @($r.Text -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' })
}

# Turns an error from wsl.exe into something a person can act on.
function Get-WslProblemText([string]$Text, [string]$Headline) {
    $hint = ''
    if ($Text -match '0x80370102|0x80070003|virtualization|Virtual Machine Platform') {
        $hint = 'WSL2 cannot use virtualization. Switch on "Virtual Machine Platform" (run this installer again after a restart) and check that virtualization is enabled in the computer''s BIOS/UEFI settings.'
    }
    elseif ($Text -match '0x80070070|not enough space') {
        $hint = 'There is not enough free disk space - the Linux needs about 3 GB, and Odoo images need several more.'
    }
    return ($Headline + '.' + "`n" + $Text + "`n" + $hint).Trim()
}

# ---- turning WSL on (needs administrator rights) ----------------------------

function Enable-WslComponent {
    param([string]$InstallDir)
    $script = Join-Path $InstallDir 'enable-wsl.ps1'
    $log = Join-Path $InstallDir 'enable-wsl.log'
    if (Test-Path $log) { Remove-Item -Force $log }
    $lines = @(
        '$env:WSL_UTF8 = ''1''',
        ('& wsl.exe --install --no-distribution *> "{0}"' -f $log),
        'exit $LASTEXITCODE'
    )
    Set-Content -Path $script -Value $lines -Encoding ASCII
    $argLine = '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $script
    try {
        $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $argLine -Verb RunAs -Wait -PassThru
    }
    catch {
        throw 'Windows asked for permission to turn WSL on and it was not given. Run install.cmd again and answer Yes to the question.'
    }
    $output = ''
    if (Test-Path $log) { $output = ((Get-Content -Path $log -Raw) -replace "`0", '').Trim() }
    return [pscustomobject]@{ ExitCode = $process.ExitCode; Output = $output }
}

# ---- the Ubuntu image -------------------------------------------------------

# The newest Ubuntu 24.04 WSL image on Ubuntu's own release server, with its
# published SHA-256. Found in the release list rather than hard-coded, so a new
# point release does not break the installer.
function Get-UbuntuImage {
    $base = 'https://releases.ubuntu.com/noble'
    $sums = @(Invoke-Curl @('-sSL', '--fail', '--max-time', '60', ($base + '/SHA256SUMS')))
    $best = $null
    foreach ($line in $sums) {
        if ($line -match '^([0-9a-fA-F]{64}) \*?(ubuntu-24\.04\.(\d+)-wsl-amd64\.wsl)\s*$') {
            $minor = [int]$Matches[3]
            if (($null -eq $best) -or ($minor -gt $best.Minor)) {
                $best = [pscustomobject]@{
                    Minor  = $minor
                    Sha256 = $Matches[1].ToLower()
                    Name   = $Matches[2]
                    Url    = $base + '/' + $Matches[2]
                }
            }
        }
    }
    if ($null -eq $best) { throw 'Could not find the Ubuntu 24.04 WSL image in Ubuntu''s release list. Check the internet connection and try again.' }
    return $best
}

# Downloads it (resuming an interrupted download) and checks it against the
# published checksum. A .wsl file is a gzip tar archive; it is saved as .tar.gz
# because that is what wsl --import is documented to take.
function Save-UbuntuImage {
    param($Image, [string]$CacheDir)
    New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
    $target = Join-Path $CacheDir ($Image.Name -replace '\.wsl$', '.tar.gz')
    if ((Test-Path $target) -and ((Get-Sha256 $target) -eq $Image.Sha256)) {
        Write-Info 'The Ubuntu image is already downloaded - using it.'
        return $target
    }
    $part = $target + '.part'
    Write-Info ('Downloading {0} (about 390 MB) ...' -f $Image.Name)
    Invoke-Curl @('-L', '--fail', '--retry', '3', '--retry-delay', '3', '-C', '-', '-o', $part, $Image.Url) | Out-Null
    if ((Get-Sha256 $part) -ne $Image.Sha256) {
        Remove-Item -Force $part
        throw 'The downloaded Ubuntu image does not match the checksum Ubuntu publishes, so it was thrown away. Run the installer again.'
    }
    Move-Item -Force -Path $part -Destination $target
    return $target
}

function Import-OdooEduDistro {
    param([string]$Name, [string]$InstallDir, [string]$ImageFile)
    $folder = Join-Path $InstallDir 'distro'
    if (Test-Path (Join-Path $folder 'ext4.vhdx')) { Remove-Item -Recurse -Force $folder }   # left over from a failed import
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    Invoke-Wsl @('--set-default-version', '2') -Capture | Out-Null
    Write-Info 'Creating the Linux system (a minute or two) ...'
    $r = Invoke-Wsl @('--import', $Name, $folder, $ImageFile, '--version', '2') -Capture
    if (($r.ExitCode -ne 0) -and ($r.Text -match 'Invalid command line|Usage|--version')) {
        $r = Invoke-Wsl @('--import', $Name, $folder, $ImageFile) -Capture   # an older WSL without --version
    }
    if ($r.ExitCode -ne 0) { throw (Get-WslProblemText $r.Text 'Creating the Linux system failed') }
}

# ---- the app inside it ------------------------------------------------------

function ConvertTo-LfFile([string]$Path) {
    $text = [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($Path)).TrimStart([char]0xFEFF)
    if ($text.Contains("`r")) {
        $text = $text -replace "`r`n", "`n" -replace "`r", "`n"
    }
    [System.IO.File]::WriteAllText($Path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

# A clean copy of the app for the Linux side to install from, with Linux line
# endings on every script (bash cannot run a script saved with Windows ones).
function Copy-AppStage {
    param([string]$Source, [string]$Stage, [string]$InstallDir)
    if (-not $Stage.StartsWith($InstallDir)) { throw 'internal error: the staging folder is outside the install folder' }
    New-Item -ItemType Directory -Force -Path $Stage | Out-Null
    Invoke-Robocopy @($Source, $Stage, '/MIR', '/XD', '.git', 'venv', 'data', 'build', 'dist', '__pycache__', '.pytest_cache', '*.egg-info', '/XF', '*.pyc', '/NFL', '/NDL', '/NJH', '/NJS', '/NP')
    foreach ($file in @(Get-ChildItem -Path $Stage -Recurse -Filter '*.sh')) { ConvertTo-LfFile $file.FullName }
}

# The settings WSL reads when it starts the Linux (default user, systemd, ...). A change only
# applies after the Linux is restarted - and a restart stops every Odoo instance that is
# running - so the installer compares this before and after provisioning and restarts only
# when it changed.
function Get-WslConfText {
    param([string]$Distro)
    $r = Invoke-Wsl @('-d', $Distro, '-u', 'root', '--exec', 'cat', '/etc/wsl.conf') -Capture
    return [string]$r.Text
}

function Invoke-Provision {
    param([string]$Distro, [string]$Stage, [string]$User)
    $where = Invoke-Wsl @('-d', $Distro, '--exec', 'wslpath', '-a', $Stage) -Capture
    if (($where.ExitCode -ne 0) -or (-not $where.Text)) { throw ('Could not find the staging folder from inside Linux: ' + $where.Text) }
    $stageLinux = $where.Text.Trim()
    $r = Invoke-Wsl @('-d', $Distro, '-u', 'root', '--exec', 'bash', ($stageLinux + '/windows/provision.sh'), $stageLinux, $User)
    if ($r.ExitCode -ne 0) { throw ('Setting up Linux and the app failed (exit code {0}). The messages above say where.' -f $r.ExitCode) }
}

function Wait-ForDocker {
    param([string]$Distro, [string]$User, [int]$Seconds = 90)
    for ($i = 0; $i -lt $Seconds; $i++) {
        $r = Invoke-Wsl @('-d', $Distro, '-u', $User, '--exec', 'docker', 'info') -Capture
        if ($r.ExitCode -eq 0) { return $true }
        if ($i -eq 8) {
            Invoke-Wsl @('-d', $Distro, '-u', 'root', '--exec', 'sh', '-c', 'systemctl start docker 2>/dev/null || service docker start') -Capture | Out-Null
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

# WSL shuts a distribution down a few seconds after its last window closes, which
# would stop every Odoo instance the moment the manager's window is closed. One
# hidden "sleep" keeps it up until "Stop Odoo Edu Manager" ends it.
function Start-KeepAlive {
    param([string]$Distro)
    $existing = @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'wsl.exe'" |
        Where-Object { ($_.CommandLine -match 'sleep') -and ($_.CommandLine -match [regex]::Escape($Distro)) })
    if ($existing.Count -eq 0) {
        Start-Process -FilePath 'wsl.exe' -ArgumentList @('-d', $Distro, '-u', 'root', '--exec', 'sleep', 'infinity') -WindowStyle Hidden | Out-Null
    }
}

# ---- what the other scripts read --------------------------------------------

function Write-InstallConfig {
    param([string]$InstallDir, [string]$Distro, [string]$User, [int]$Port = 5001)
    $config = [pscustomobject]@{ distro = $Distro; user = $User; port = $Port }
    Set-Content -Path (Join-Path $InstallDir 'config.json') -Value ($config | ConvertTo-Json) -Encoding ASCII
}

function Read-InstallConfig {
    param([string]$InstallDir)
    $path = Join-Path $InstallDir 'config.json'
    if (-not (Test-Path $path)) { return $null }
    return (Get-Content -Path $path -Raw | ConvertFrom-Json)
}

# The folders for this user's Start menu and desktop (they may be redirected, e.g.
# to OneDrive, which is why Windows is asked rather than the path being built).
# The two environment variables let the tests point them at a temporary folder, so
# that a test run can never touch real shortcuts.
function Get-StartMenuFolder {
    if ($env:ODOO_EDU_MANAGER_START_MENU_DIR) { return $env:ODOO_EDU_MANAGER_START_MENU_DIR }
    $path = [Environment]::GetFolderPath('Programs')
    if (-not $path) { $path = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs' }
    return $path
}

function Get-DesktopFolder {
    if ($env:ODOO_EDU_MANAGER_DESKTOP_DIR) { return $env:ODOO_EDU_MANAGER_DESKTOP_DIR }
    $path = [Environment]::GetFolderPath('Desktop')
    if (-not $path) { $path = Join-Path $env:USERPROFILE 'Desktop' }
    return $path
}

# ---- the entry in Settings > Apps ------------------------------------------------------------
# Windows lists a per-user program from a key under HKEY_CURRENT_USER, so this needs no
# administrator rights and is only this user's. The key is imported from a small .reg file
# rather than written with reg add: the uninstall command contains quotes, which Windows
# PowerShell 5.1 does not pass on to a native program correctly.
function Get-UninstallKeyName { return 'Software\Microsoft\Windows\CurrentVersion\Uninstall\OdooEduManager' }

function Get-UninstallEntryValues {
    param([string]$InstallDir)
    return @{
        DisplayName     = 'Odoo Edu Manager'
        UninstallString = ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}" -Pause' -f (Join-Path $InstallDir 'uninstall.ps1'))
        InstallLocation = $InstallDir
        DisplayIcon     = (Join-Path $env:SystemRoot 'System32\wsl.exe') + ',0'
        NoModify        = 1
        NoRepair        = 1
    }
}

# the text of a value in a .reg file: backslashes and quotes are escaped
function ConvertTo-RegText([string]$Text) { return (($Text -replace '\\', '\\') -replace '"', '\"') }

function Invoke-Reg {
    param([string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'   # a native program's error text must not become a terminating error
    try {
        $raw = & reg.exe @Arguments 2>&1
        $code = $LASTEXITCODE
        return [pscustomobject]@{ ExitCode = $code; Text = ((@($raw) | ForEach-Object { '' + $_ }) -join "`n").Trim() }
    }
    finally {
        $ErrorActionPreference = $previous
    }
}

function Register-Uninstaller {
    param([string]$InstallDir)
    $values = Get-UninstallEntryValues -InstallDir $InstallDir
    $lines = @('Windows Registry Editor Version 5.00', '', ('[HKEY_CURRENT_USER\' + (Get-UninstallKeyName) + ']'))
    foreach ($name in @($values.Keys | Sort-Object)) {
        $value = $values[$name]
        if ($value -is [int]) { $lines += ('"{0}"=dword:{1:x8}' -f $name, $value) }
        else { $lines += ('"{0}"="{1}"' -f $name, (ConvertTo-RegText ([string]$value))) }
    }
    $file = Join-Path ([System.IO.Path]::GetTempPath()) ('oem-uninstall-' + [guid]::NewGuid().ToString('N') + '.reg')
    try {
        Set-Content -Path $file -Value $lines -Encoding Unicode
        $r = Invoke-Reg @('import', $file)
        if ($r.ExitCode -ne 0) { throw ('reg.exe could not add the entry: ' + $r.Text) }
    }
    finally {
        Remove-Item -Force -Path $file -ErrorAction SilentlyContinue
    }
}

function Unregister-Uninstaller {
    $r = Invoke-Reg @('delete', ('HKCU\' + (Get-UninstallKeyName)), '/f')
    return ($r.ExitCode -eq 0)
}

function Test-Uninstaller {
    $r = Invoke-Reg @('query', ('HKCU\' + (Get-UninstallKeyName)))
    return ($r.ExitCode -eq 0)
}

function Install-Shortcuts {
    param([string]$InstallDir, [bool]$OnDesktop = $true)
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $programs = Get-StartMenuFolder
    $desktop = Get-DesktopFolder
    $icon = (Join-Path $env:SystemRoot 'System32\wsl.exe') + ',0'
    $items = @(
        @{ Name = 'Odoo Edu Manager'; Script = 'start.ps1'; Desktop = $OnDesktop; Extra = ''; Start = ''; Note = 'Start Odoo Edu Manager and open it in the browser' },
        @{ Name = 'Stop Odoo Edu Manager'; Script = 'stop.ps1'; Desktop = $false; Extra = ''; Start = ''; Note = 'Stop Odoo Edu Manager and every Odoo instance it runs' },
        # it deletes the install folder, so it must not start inside it; -Pause keeps the window open to read the result
        @{ Name = 'Uninstall Odoo Edu Manager'; Script = 'uninstall.ps1'; Desktop = $false; Extra = ' -Pause'; Start = $env:USERPROFILE; Note = 'Remove Odoo Edu Manager and every Odoo instance it runs' }
    )
    foreach ($item in $items) {
        $argLine = ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $InstallDir $item.Script)) + $item.Extra
        $startIn = $InstallDir
        if ($item.Start) { $startIn = $item.Start }
        New-Shortcut -Path (Join-Path $programs ($item.Name + '.lnk')) -Target $powershell -Arguments $argLine -WorkingDirectory $startIn -Icon $icon -Description $item.Note
        if ($item.Desktop) {
            New-Shortcut -Path (Join-Path $desktop ($item.Name + '.lnk')) -Target $powershell -Arguments $argLine -WorkingDirectory $startIn -Icon $icon -Description $item.Note
        }
    }
}

# ---- the whole install ------------------------------------------------------

# Returns 0 when everything is done, 10 when Windows has to restart first.
function Invoke-OdooEduInstall {
    param(
        [Parameter(Mandatory = $true)][string]$SourceDir,
        [Parameter(Mandatory = $true)][string]$ScriptsDir,
        [Parameter(Mandatory = $true)][string]$InstallDir,
        [string]$Distro = 'OdooEdu',
        [string]$User = 'edu',
        [string]$ImagePath = '',
        [switch]$NoShortcuts
    )

    Write-Step 'Step 1 of 5 - checking this computer'
    Test-Prerequisites
    if (-not (Test-Path (Join-Path $SourceDir 'install.sh'))) {
        throw ('Cannot find install.sh in {0}. Run install.cmd from the folder you downloaded, with all the other files still next to it.' -f $SourceDir)
    }
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Write-Good 'Windows and the processor are fine.'

    Write-Step 'Step 2 of 5 - Windows Subsystem for Linux (WSL)'
    $state = Get-WslState
    if ($state -ne 'Ready') {
        $facts = Get-VirtualizationFacts
        $restarts = $null
        if (($facts.PlatformEnabled -eq $true) -or ($facts.RebootPending -eq $true)) {
            $since = Read-RestartMarker -InstallDir $InstallDir
            if ($null -eq $since) {
                Write-RestartMarker -InstallDir $InstallDir   # first time this is seen: count from now
                $since = Get-Date
            }
            $restarts = Get-RestartsSince -Since $since
        }
        $plan = Get-WslPlan -State $state -Facts $facts -RestartsSince $restarts
        if ($plan -eq 'RestartFirst') {
            Write-Warn 'Windows is already waiting for a restart (usually to finish installing updates).'
            Write-Host ''
            Write-Host 'RESTART WINDOWS NOW (Start > Power > Restart) and let it finish updating,' -ForegroundColor Yellow
            Write-Host 'then double-click install.cmd again. Switching WSL on while Windows is in the middle of'
            Write-Host 'an update can leave it stuck, so nothing has been changed.'
            return 10
        }
        if ($plan -eq 'StuckServicing') {
            throw ('Windows has restarted twice since it was asked to finish a change and still says it has not, so another restart will not help. ' +
                'This is a problem inside Windows, not with this installer. In this order: (1) Settings > Windows Update: check for updates, install everything, restart; ' +
                '(2) if that does not clear it, open Command Prompt as administrator and run  DISM /Online /Cleanup-Image /RestoreHealth  then  sfc /scannow  and restart; ' +
                '(3) then check the boot setting:  bcdedit /enum {current}  - if it shows hypervisorlaunchtype Off, run  bcdedit /set hypervisorlaunchtype auto  and restart. ' +
                'Then run install.cmd again. If it still fails, run diagnose.cmd and send its output.')
        }
        if ($plan -eq 'RestartAgain') {
            Write-Warn 'Windows switched WSL''s component on earlier, but it has not finished: a restart is still pending.'
            Write-Host ''
            Write-Host 'RESTART WINDOWS NOW (Start > Power > Restart), then double-click install.cmd again.' -ForegroundColor Yellow
            Write-Host 'If you restarted once already, restart one more time: Windows sometimes needs two restarts'
            Write-Host 'to finish switching a component on. Nothing else has been installed yet.'
            return 10
        }
        if ($plan -eq 'FirmwareOff') {
            throw 'Virtualization is switched off in this computer''s firmware. Turn on "Intel Virtualization Technology" / "VT-x" (or "AMD SVM Mode") in the BIOS/UEFI setup, then run install.cmd again.'
        }
        if ($plan -eq 'HypervisorOff') {
            throw ('The WSL component is on and Windows is not waiting for a restart, but its hypervisor is not running. ' +
                'Usual causes: it is switched off in the boot settings - open Command Prompt as administrator, run  bcdedit /set hypervisorlaunchtype auto  and restart; ' +
                'other virtualization software that needs it off; or a company policy. If that does not help, run diagnose.cmd and send its output.')
        }
        Write-Info ('WSL is not ready on this computer yet ({0}). Turning it on needs administrator rights:' -f $state)
        Write-Info 'Windows will ask for permission - please answer Yes.'
        $result = Enable-WslComponent -InstallDir $InstallDir
        if ($result.Output) { Write-Host $result.Output }
        if (($result.ExitCode -ne 0) -and ($result.ExitCode -ne 3010)) {
            throw (Get-WslProblemText $result.Output ('Turning WSL on failed (exit code {0})' -f $result.ExitCode))
        }
        Write-RestartMarker -InstallDir $InstallDir
        if ((Get-WslState) -ne 'Ready') {
            Write-Host ''
            Write-Good 'WSL is switched on.'
            Write-Host ''
            Write-Host 'RESTART WINDOWS NOW (Start > Power > Restart), then double-click install.cmd again.' -ForegroundColor Yellow
            Write-Host 'Nothing else has been installed yet - the second run does the rest.'
            Write-Host 'If it then says a restart is still pending, restart once more: Windows sometimes needs two.'
            return 10
        }
    }
    Write-Good 'WSL is ready.'

    Write-Step 'Step 3 of 5 - the Linux that runs everything'
    $distroIsNew = $true
    if ((Get-WslDistros) -contains $Distro) {
        $distroIsNew = $false
        Write-Good ('"{0}" is already installed - keeping it and everything in it.' -f $Distro)
    }
    else {
        $imageFile = $ImagePath
        if (-not $imageFile) {
            $image = Get-UbuntuImage
            $imageFile = Save-UbuntuImage -Image $image -CacheDir (Join-Path $InstallDir 'cache')
        }
        Import-OdooEduDistro -Name $Distro -InstallDir $InstallDir -ImageFile $imageFile
        Write-Good ('Created "{0}".' -f $Distro)
    }

    Write-Step 'Step 4 of 5 - Docker and Odoo Edu Manager (the long step)'
    $stage = Join-Path $InstallDir 'stage'
    Copy-AppStage -Source $SourceDir -Stage $stage -InstallDir $InstallDir
    $confBefore = $null
    if (-not $distroIsNew) { $confBefore = Get-WslConfText -Distro $Distro }
    Invoke-Provision -Distro $Distro -Stage $stage -User $User
    # A new Linux, or one whose WSL settings just changed, must be restarted for them to apply.
    # An update must not: that would stop the Odoo instances people have running.
    $restart = $distroIsNew -or ($confBefore -cne (Get-WslConfText -Distro $Distro))
    if ($restart) {
        Invoke-Wsl @('--terminate', $Distro) -Capture | Out-Null   # so the new settings apply
        Write-Info 'Starting Linux again to check that Docker comes up ...'
    }
    else {
        Write-Info 'Checking that Docker is running ...'
    }
    if (Wait-ForDocker -Distro $Distro -User $User) {
        Write-Good 'Docker is running.'
    }
    else {
        Write-Warn 'Docker did not answer within 90 seconds. The Start menu entry tries again; if it keeps failing, run diagnose.ps1 and send its output.'
    }

    Write-Step 'Step 5 of 5 - Start menu entry'
    foreach ($name in @('lib.ps1', 'start.ps1', 'stop.ps1', 'uninstall.ps1', 'diagnose.ps1', 'verify.ps1')) {
        Copy-Item -Force -Path (Join-Path $ScriptsDir $name) -Destination (Join-Path $InstallDir $name)
    }
    Write-InstallConfig -InstallDir $InstallDir -Distro $Distro -User $User
    if ($NoShortcuts) {
        Write-Info 'Shortcuts skipped.'
    }
    else {
        Install-Shortcuts -InstallDir $InstallDir
        Write-Good 'Added "Odoo Edu Manager", "Stop Odoo Edu Manager" and "Uninstall Odoo Edu Manager" to the Start menu, and a desktop icon.'
        try {
            Register-Uninstaller -InstallDir $InstallDir
            Write-Good 'It is also listed in Settings > Apps, where it can be uninstalled.'
        }
        catch {
            Write-Warn ('It could not be listed in Settings > Apps (' + $_.Exception.Message + '). "Uninstall Odoo Edu Manager" in the Start menu still works.')
        }
    }

    Write-Host ''
    Write-Host 'All done.' -ForegroundColor Green
    Write-Host ''
    Write-Host '  Open "Odoo Edu Manager" from the Start menu. It starts everything and opens http://localhost:5001 in your browser.'
    Write-Host '  Keep its window open while you work. Closing it stops the app, but your Odoo instances keep running;'
    Write-Host '  "Stop Odoo Edu Manager" stops everything.'
    if (-not $NoShortcuts) {
        Write-Host '  To remove it later: "Uninstall Odoo Edu Manager" in the Start menu, or Settings > Apps.'
    }
    if (-not $distroIsNew) {
        Write-Host '  This was an update: if Odoo Edu Manager is open, close its window and open it again to use the new version.'
    }
    Write-Host ('  Logs and settings: ' + $InstallDir)
    return 0
}
