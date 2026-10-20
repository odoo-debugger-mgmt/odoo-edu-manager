# Runs the real windows\*.ps1 against a pretend Windows.
#
# Everything at the boundary with the operating system is a fake that records what it was
# asked and answers from a scripted "world": wsl.exe, curl.exe, robocopy.exe, the UAC
# prompt, the Start menu shortcut object. Everything above that line - install.ps1,
# lib.ps1, start.ps1, ... - is the real code, so this catches typos, wrong arguments, bad
# ordering and mishandled failures on any machine with PowerShell 7 (the test container),
# without touching a real Windows. What it cannot show is how a real Windows behaves:
# that is what running the installer on one is for.
#
#   ./tests/install/run-windows-scripts.sh
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$global:LASTEXITCODE = 0

$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$windows = Join-Path $repo 'windows'
$work = Join-Path ([System.IO.Path]::GetTempPath()) ('oem-windows-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $work | Out-Null
function Remove-TestFolder {
    # only ever the folder this run made, and only if it is where it should be
    if (($work -like '*oem-windows-test-*') -and (Test-Path $work)) { Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue }
}
trap { Remove-TestFolder; break }   # a crash must not leave the temporary folder behind

# Always redirected, so a test run can never touch the real one:
$env:LOCALAPPDATA = Join-Path $work 'LocalAppData'
# On Windows these exist and must stay as they are (the crypto provider behind Get-FileHash, for one, finds
# its DLL through %SystemRoot%). They are only filled in where there is no Windows: the Linux test container.
function Set-EnvIfMissing([string]$Name, [string]$Value) {
    if (-not [Environment]::GetEnvironmentVariable($Name, 'Process')) { [Environment]::SetEnvironmentVariable($Name, $Value, 'Process') }
}
Set-EnvIfMissing 'SystemRoot' (Join-Path $work 'Windows')
Set-EnvIfMissing 'APPDATA' (Join-Path $work 'Roaming')
Set-EnvIfMissing 'USERPROFILE' (Join-Path $work 'Profile')
$env:PROCESSOR_ARCHITECTURE = 'AMD64'
# the real Start menu and desktop must never be touched by a test run, whatever machine it is on
$env:ODOO_EDU_MANAGER_START_MENU_DIR = Join-Path $work 'StartMenu'
$env:ODOO_EDU_MANAGER_DESKTOP_DIR = Join-Path $work 'Desktop'
$installDir = Join-Path $env:LOCALAPPDATA 'OdooEduManager'

$global:Pass = 0
$global:Fail = 0
function Check([string]$What, $Condition) {
    if ($Condition) { Write-Host "  ok    $What"; $global:Pass++ }
    else { Write-Host "  FAIL  $What" -ForegroundColor Red; $global:Fail++ }
}
# true when the block throws and the message matches the pattern
function Throws([scriptblock]$Block, [string]$Pattern) {
    try { & $Block | Out-Null; return $false }
    catch { return ([string]$_.Exception.Message -match $Pattern) }
}
function global:Note([string]$Text) { [void]$global:Calls.Add($Text) }
function Index-Of([string]$Pattern) {
    for ($i = 0; $i -lt $global:Calls.Count; $i++) { if ($global:Calls[$i] -like $Pattern) { return $i } }
    return -1
}
function Called([string]$Pattern) { return ((Index-Of $Pattern) -ge 0) }
function Count-Of([string]$Pattern) { return @($global:Calls | Where-Object { $_ -like $Pattern }).Count }

# ---- the pretend Windows -----------------------------------------------------

# The real "wsl --status" text from a laptop where WSL is installed but cannot start yet.
$laptopStatus = @'
WSL version: 3.0.3.0
Kernel version: 6.18.54.1-1
Default Version: 2
WSL1 is not supported with your current machine configuration.
Please enable the "Windows Subsystem for Linux" optional component to use WSL1.
WSL2 is unable to start since virtualization is not enabled on this machine.
Please ensure the "Virtual Machine Platform" optional component is enabled and virtualization is turned on in your computer's firmware settings.

Enable "Virtual Machine Platform" by running: wsl.exe --install --no-distribution
Windows Subsystem for Linux has no installed distributions.
'@

# The fake curl writes this text as "the image"; the release list below publishes its real hash, so
# the real Get-FileHash and the real checksum comparison are what get tested.
$probe = Join-Path $work 'probe.txt'
Set-Content -Path $probe -Value 'pretend image'
$hashNewest = (Get-FileHash -Algorithm SHA256 -Path $probe).Hash.ToLower()
$hashOlder = 'bb415d824822c4b878125729af451a5d18fb13d1cf5cbed9a7393ad64ac6039e'
$sums = @"
c74833a55e525b1e99e1541509c566bb3e32bdb53bf27ea3347174364a57f47c *ubuntu-24.04.3-wsl-amd64.wsl
9b2f7730dc68227dd04a9f3e5eab86ad85caf556b8606ad94f1f29ff5c4fd3f5 *ubuntu-24.04.4-wsl-amd64.wsl
$hashOlder *ubuntu-24.04.5-wsl-amd64.wsl
aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa *ubuntu-24.04.5-live-server-amd64.iso
$hashNewest *ubuntu-24.04.10-wsl-amd64.wsl
"@

function Reset-World {
    $global:Calls = New-Object System.Collections.ArrayList
    $global:Links = New-Object System.Collections.ArrayList
    $global:World = @{
        HypervisorPresent = $null; PlatformEnabled = $false; RebootPending = $false; FirmwareEnabled = $true; FactsUnreadable = $false; RestartsSince = 0; RestartsNoMatchError = $false; EventsUnreadable = $false
        State = 'Ready'; StateAfterEnable = 'Ready'; EnableExit = 0; EnableOutput = 'The requested operation is successful.'; UacDeclined = $false
        Distros = @(); ImportExit = 0; ImportText = ''; ImportRejectsVersion = $false
        ProvisionExit = 0; DockerUp = $true
        ProvisionChangesConf = $false; WslConf = "[boot]`nsystemd = true"
        DistroState = 'Running'; DistroVersion = 2; Systemd = 'running'; Groups = 'edu sudo docker'; AppBinary = $true; AppHasSetup = $true
        Containers = @{}; Instances = @(); Http = @{}
        Registry = @{}; RegImportExit = 0
        Build = 26200
        Sums = $sums
        AppUp = $false; KeepAlive = $false; Answer = ''
        WslRaw = $null
    }
    if (-not $env:LOCALAPPDATA.StartsWith($work)) { throw 'refusing to clean a folder outside the test folder' }
    if (Test-Path $env:LOCALAPPDATA) { Remove-Item -Recurse -Force $env:LOCALAPPDATA }
    New-Item -ItemType Directory -Force -Path $env:LOCALAPPDATA | Out-Null
    $global:ElevatedScript = ''
}

function global:wsl.exe {
    $line = $args -join ' '
    Note "wsl $line"
    $w = $global:World
    $global:LASTEXITCODE = 0
    if ($null -ne $w.WslRaw) { $global:LASTEXITCODE = $w.WslRaw.Exit; return $w.WslRaw.Text }
    switch -Regex ($line) {
        '^--status$' {
            if ($w.State -eq 'Ready') {
                if ($w.Distros.Count -eq 0) { $global:LASTEXITCODE = 255 }   # the real one does this when no Linux is installed
                return "Default Version: 2`nWSL1 is not supported with your current machine configuration.`nPlease enable the `"Windows Subsystem for Linux`" optional component to use WSL1."
            }
            if ($w.State -eq 'NeedsComponent') { return $laptopStatus }
            $global:LASTEXITCODE = 1
            return "The Windows Subsystem for Linux is not installed. You can install by running 'wsl.exe --install'."
        }
        '^--version$' { return 'WSL version: 3.0.3.0' }
        '^--list --verbose$' {
            $rows = @('  NAME      STATE           VERSION')
            foreach ($d in @($w.Distros)) { $rows += ('* {0}    {1}         {2}' -f $d, $w.DistroState, $w.DistroVersion) }
            return $rows
        }
        '^--list --quiet$' {
            if ($w.Distros.Count -eq 0) { $global:LASTEXITCODE = 1; return 'Windows Subsystem for Linux has no installed distributions.' }
            return $w.Distros
        }
        '^--set-default-version' { return }
        '^--import ' {
            if ($w.ImportRejectsVersion -and ($line -match '--version')) { $global:LASTEXITCODE = 1; return 'Invalid command line argument: --version' }
            if ($w.ImportExit -ne 0) { $global:LASTEXITCODE = $w.ImportExit; return $w.ImportText }
            $w.Distros = @($w.Distros) + $args[1]
            return 'Import in progress, this may take a few minutes.'
        }
        '--exec wslpath -a ' { return '/mnt/c/fake/stage' }
        'provision\.sh' {
            $global:LASTEXITCODE = $w.ProvisionExit
            if ($w.ProvisionChangesConf) { $w.WslConf = $w.WslConf + "`n[user]`ndefault = edu" }
            return 'provisioning output'
        }
        '--exec cat /etc/wsl\.conf' { return $w.WslConf }
        '^--terminate ' { return }
        '--exec docker info --format' {
            if (-not $w.DockerUp) { $global:LASTEXITCODE = 1; return 'Cannot connect to the Docker daemon' }
            return '27.0 on Ubuntu 24.04'
        }
        '--exec systemctl is-system-running' { return $w.Systemd }
        '--exec id -nG' { return $w.Groups }
        '--exec bash -lc test -x' {
            $out = @()
            if ($w.AppBinary) { $out += 'binary=yes'; $out += 'odoo-edu-manager v0.1.0' } else { $out += 'binary=no' }
            if ($w.AppHasSetup) { $out += 'setup=present' } else { $out += 'setup=missing' }
            return $out
        }
        '--exec bash -lc free -m' { return @('Mem:           3815        1674        1920', '/dev/sdd       1007G  9.3G  947G   1% /') }
        '--exec docker inspect --format' {
            $name = $args[-1]
            if ($w.Containers.ContainsKey($name)) { return $w.Containers[$name] }
            $global:LASTEXITCODE = 1
            return ('Error: No such object: ' + $name)
        }
        '--exec docker ps -a --filter' { return $w.Instances }
        '--exec docker info' { if (-not $w.DockerUp) { $global:LASTEXITCODE = 1 }; return }
        '^--unregister ' { $gone = $args[1]; $w.Distros = @($w.Distros | Where-Object { $_ -ne $gone }); return }
        'uptime' { return 'up 1 min' }
        default { return }
    }
}

function global:curl.exe {
    $line = $args -join ' '
    Note "curl $line"
    $global:LASTEXITCODE = 0
    if ([Array]::IndexOf($args, '-w') -ge 0) {   # an HTTP status check: the world says what each address answers
        $url = '' + $args[-1]
        if ($global:World.Http.ContainsKey($url)) { return $global:World.Http[$url] }
        return '000'
    }
    if ($line -match 'SHA256SUMS$') { return ($global:World.Sums -split "`n") }
    $o = [Array]::IndexOf($args, '-o')
    if ($o -ge 0) { Set-Content -Path $args[$o + 1] -Value 'pretend image'; return }
    $global:LASTEXITCODE = 22
}

function global:robocopy.exe {
    Note 'robocopy'
    foreach ($relative in @('install.sh', 'pyproject.toml', 'windows/provision.sh')) {
        $from = Join-Path $args[0] $relative
        if (Test-Path $from) {
            $to = Join-Path $args[1] $relative
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $to) | Out-Null
            Copy-Item -Force $from $to
        }
    }
    $global:LASTEXITCODE = 1   # robocopy answers 1 when it copied something: that is success
}

function global:Get-CimInstance {
    param($ClassName, $Filter)
    $w = $global:World
    if ($w.FactsUnreadable -and ($ClassName -in @('Win32_ComputerSystem', 'Win32_OptionalFeature', 'Win32_Processor'))) { throw 'Access denied' }
    if ($ClassName -eq 'Win32_OperatingSystem') { return [pscustomobject]@{ BuildNumber = $w.Build; Caption = 'Windows 11 Pro'; Version = '10.0'; OSArchitecture = '64-bit'; LastBootUpTime = 'a while ago' } }
    if ($ClassName -eq 'Win32_ComputerSystem') { return [pscustomobject]@{ Manufacturer = 'TEST'; Model = 'FAKE'; HypervisorPresent = $(if ($null -ne $w.HypervisorPresent) { $w.HypervisorPresent } else { $w.State -eq 'Ready' }) } }
    if ($ClassName -eq 'Win32_Processor') { return [pscustomobject]@{ Name = 'Fake CPU'; VirtualizationFirmwareEnabled = $w.FirmwareEnabled } }
    if ($ClassName -eq 'Win32_OptionalFeature') { return [pscustomobject]@{ Name = 'VirtualMachinePlatform'; InstallState = $(if ($w.PlatformEnabled) { 1 } else { 2 }) } }
    if ($ClassName -eq 'Win32_Process') {
        if ($w.KeepAlive) { return [pscustomobject]@{ CommandLine = 'wsl.exe -d OdooEdu -u root --exec sleep infinity' } }
        return @()
    }
}

# the pending-restart lookup in Windows servicing: key present = exit code 0, absent = 1
function global:reg.exe {
    Note ('reg ' + ($args -join ' '))
    $w = $global:World
    $verb = '' + $args[0]
    $target = '' + $args[1]
    if ($verb -eq 'import') {   # the .reg file the installer wrote: read it back the way Windows would
        if ($w.RegImportExit -ne 0) { $global:LASTEXITCODE = $w.RegImportExit; return 'ERROR: Access is denied.' }
        $text = Get-Content -Raw -Encoding Unicode -Path $target
        $key = [regex]::Match($text, '(?m)^\[HKEY_CURRENT_USER\\(.+)\]\s*$').Groups[1].Value
        $values = @{}
        foreach ($m in [regex]::Matches($text, '(?m)^"([^"]+)"=(?:"((?:[^"\\]|\\.)*)"|dword:([0-9a-f]{8}))\s*$')) {
            if ($m.Groups[3].Success) { $values[$m.Groups[1].Value] = [Convert]::ToInt32($m.Groups[3].Value, 16) }
            else { $values[$m.Groups[1].Value] = [regex]::Replace($m.Groups[2].Value, '\\(.)', '$1') }
        }
        $w.Registry[$key] = $values
        $global:LASTEXITCODE = 0
        return 'The operation completed successfully.'
    }
    if ($target -like 'HKCU\*') {   # the per-user key: query = does it exist, delete = remove it
        $key = $target.Substring(5)
        $present = $w.Registry.ContainsKey($key)
        if ($verb -eq 'delete') { $w.Registry.Remove($key) }
        $global:LASTEXITCODE = $(if ($present) { 0 } else { 1 })
        return
    }
    $global:LASTEXITCODE = $(if ($w.RebootPending) { 0 } else { 1 })   # servicing keys: present = a restart is pending
}

# the System event log: one "event log service started" (6005) per boot
function global:Get-WinEvent {
    param($FilterHashtable, $MaxEvents, $LogName, $ErrorAction)
    $w = $global:World
    Note 'get-winevent'
    if ($w.EventsUnreadable) { throw 'Attempted to perform an unauthorized operation.' }
    if (($w.RestartsSince -eq 0) -and $w.RestartsNoMatchError) {
        throw (New-Object System.Management.Automation.ErrorRecord((New-Object System.Exception 'No events were found that match the specified selection criteria.'), 'NoMatchingEventsFound,Microsoft.PowerShell.Commands.GetWinEventCommand', 'ObjectNotFound', $null))
    }
    if ($w.RestartsSince -le 0) { return @() }   # (1..0 would count down: one event, not none)
    return @(1..$w.RestartsSince | ForEach-Object { [pscustomobject]@{ Id = 6005 } })
}

function global:systeminfo.exe {
    Note 'systeminfo'
    return @('Host Name: FAKE', 'Hyper-V Requirements:          VM Monitor Mode Extensions: Yes', '                               Virtualization Enabled In Firmware: Yes')
}

function global:Start-Process {
    param($FilePath, $ArgumentList, $Verb, [switch]$Wait, [switch]$PassThru, $WindowStyle, $WorkingDirectory)
    $w = $global:World
    if ($Verb -eq 'RunAs') {
        Note 'elevate'
        if ($w.UacDeclined) { throw 'The operation was canceled by the user.' }
        $file = [regex]::Match([string]$ArgumentList, '-File "([^"]+)"').Groups[1].Value
        $global:ElevatedScript = Get-Content -Raw $file
        $log = [regex]::Match($global:ElevatedScript, '\*> "([^"]+)"').Groups[1].Value
        Set-Content -Path $log -Value $w.EnableOutput
        $w.State = $w.StateAfterEnable
        if ($w.EnableExit -eq 0) { $w.PlatformEnabled = $true; $w.RebootPending = ($w.StateAfterEnable -ne 'Ready') }
        return [pscustomobject]@{ ExitCode = $w.EnableExit }
    }
    Note ('start-process ' + $FilePath + ' ' + ($ArgumentList -join ' ') + $(if ($WorkingDirectory) { ' [in ' + $WorkingDirectory + ']' } else { '' }))
    if ($PassThru) { return [pscustomobject]@{ ExitCode = 0 } }
}

function global:Start-Sleep { param($Seconds, $Milliseconds) }
function global:Unblock-File { param([Parameter(ValueFromPipeline = $true)]$InputObject) process { } }
function global:Start-Job { param($ScriptBlock, $ArgumentList) Note 'start-job: open the browser when the app answers'; return $null }
function global:Read-Host { param($Prompt) Note ('read-host ' + $Prompt); return $global:World.Answer }
function global:Invoke-WebRequest {
    param($Uri, [switch]$UseBasicParsing, $TimeoutSec)
    Note "web $Uri"
    if ($global:World.AppUp) { return [pscustomobject]@{ StatusCode = 200 } }
    throw 'Unable to connect to the remote server'
}

# the Start menu shortcut object (WScript.Shell); everything else goes to the real New-Object
function global:New-Object {
    if ($args -contains '-ComObject') {
        $shell = [pscustomobject]@{}
        Add-Member -InputObject $shell -MemberType ScriptMethod -Name CreateShortcut -Value {
            param($Path)
            $link = [pscustomobject]@{ Path = $Path; TargetPath = ''; Arguments = ''; WorkingDirectory = ''; Description = ''; IconLocation = '' }
            Add-Member -InputObject $link -MemberType ScriptMethod -Name Save -Value { [void]$global:Links.Add($this); Note ('shortcut ' + $this.Path) }
            return $link
        }
        return $shell
    }
    & (Get-Command New-Object -CommandType Cmdlet) @args
}

# Before anything runs: every pretend command must be the one that would really run here. If one is
# not (a host that resolves names differently), stop - rather than let a test touch this machine,
# for instance by raising a real administrator prompt.
foreach ($pretend in @('wsl.exe', 'curl.exe', 'robocopy.exe', 'reg.exe', 'systeminfo.exe', 'Get-WinEvent', 'Get-CimInstance', 'Start-Process', 'Start-Sleep', 'Unblock-File', 'Start-Job', 'Read-Host', 'Invoke-WebRequest', 'New-Object')) {
    $found = @(Get-Command $pretend -ErrorAction SilentlyContinue)[0]
    if (($null -eq $found) -or ($found.CommandType -ne 'Function') -or $found.ModuleName) {
        throw "Refusing to run: the pretend '$pretend' is not what would run here, so a test could touch this real machine."
    }
}

# a small pretend download of the app, with the Windows line endings a ZIP can bring
function New-FakeSource {
    $src = Join-Path $work 'source'
    if (Test-Path $src) { Remove-Item -Recurse -Force $src }
    New-Item -ItemType Directory -Force -Path (Join-Path $src 'windows') | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $src 'install.sh'), "#!/usr/bin/env bash`r`necho install`r`n")
    [System.IO.File]::WriteAllText((Join-Path $src 'windows/provision.sh'), "#!/usr/bin/env bash`r`necho provision`r`n")
    [System.IO.File]::WriteAllText((Join-Path $src 'pyproject.toml'), "[project]`r`nname = 'x'`r`n")
    return $src
}

# a finished install, as far as start/stop/uninstall/diagnose can tell
function Write-FakeInstall {
    New-Item -ItemType Directory -Force -Path $installDir | Out-Null
    Write-InstallConfig -InstallDir $installDir -Distro 'OdooEdu' -User 'edu'
}

function Run-Install([string]$Source, [string]$Image = 'ignored') {
    $r = @(Invoke-OdooEduInstall -SourceDir $Source -ScriptsDir $windows -InstallDir $installDir)
    return [int]$r[-1]
}

# the same, but also what it printed (Write-Host goes to the information stream)
function Run-InstallFull([string]$Source) {
    $r = @(Invoke-OdooEduInstall -SourceDir $Source -ScriptsDir $windows -InstallDir $installDir 6>&1)
    $text = (@($r | Where-Object { $_ -isnot [int] }) | ForEach-Object { '' + $_ }) -join "`n"
    return [pscustomobject]@{ Code = [int]$r[-1]; Text = $text }
}

# ---- 1. the files ------------------------------------------------------------

Write-Host '== the files'
foreach ($file in Get-ChildItem $windows -File) {
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    if ($file.Extension -in '.ps1', '.cmd') {
        Check ($file.Name + ' is plain ASCII (Windows PowerShell 5.1 can misread anything else)') (-not ($bytes | Where-Object { $_ -gt 127 }))
    }
    if ($file.Extension -eq '.cmd') {
        $text = [System.Text.Encoding]::ASCII.GetString($bytes)
        Check ($file.Name + ' has Windows line endings throughout') (($text -split "`n").Count -gt 2 -and ($text -notmatch "[^`r]`n"))
        Check ($file.Name + ' lets an unsigned script run (-ExecutionPolicy Bypass)') ($text -match 'ExecutionPolicy Bypass')
    }
    if ($file.Extension -eq '.ps1') {
        $errors = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$null, [ref]$errors)
        Check ($file.Name + ' parses') ($errors.Count -eq 0)
    }
}
Check 'provision.sh starts with a bash shebang' ((Get-Content (Join-Path $windows 'provision.sh') -TotalCount 1) -eq '#!/usr/bin/env bash')

. (Join-Path $windows 'lib.ps1')

# ---- 2. reading wsl.exe ------------------------------------------------------

Write-Host "`n== Invoke-Wsl turns wsl.exe's output into text"
Reset-World
$global:World.WslRaw = @{ Exit = 5; Text = "W`0S`0L`0 `0o`0k`0" }   # what older WSL prints: UTF-16 read as bytes
$r = Invoke-Wsl @('--status') -Capture
Check 'NUL characters from UTF-16 output are removed' ($r.Text -eq 'WSL ok')
Check 'the exit code is kept' ($r.ExitCode -eq 5)
$r = Invoke-Wsl @('--status')
Check 'without -Capture nothing leaks into the return value' (($r -is [pscustomobject]) -and ($r.ExitCode -eq 5))

Write-Host "`n== telling what is wrong with WSL"
Reset-World
$global:World.State = 'NeedsComponent'
Check 'WSL installed but Virtual Machine Platform off (this laptop) is NeedsComponent' ((Get-WslState) -eq 'NeedsComponent')
$global:World.State = 'Ready'
Check 'a healthy WSL is Ready' ((Get-WslState) -eq 'Ready')
$global:World.State = 'NotInstalled'
Check 'no WSL at all is NotInstalled' ((Get-WslState) -eq 'NotInstalled')
Check 'with no distribution at all the list is empty, not the explanation text' (@(Get-WslDistros).Count -eq 0)
$global:World.State = 'Ready'; $global:World.Distros = @()
Check 'a healthy WSL with no Linux installed yet is Ready, although wsl.exe exits with 255' ((Get-WslState) -eq 'Ready')
$global:World.Distros = @('Ubuntu')
Check 'a healthy WSL with a Linux installed is Ready' ((Get-WslState) -eq 'Ready')
$global:World.Distros = @()
$global:World.WslRaw = @{ Exit = 255; Text = 'Standardversion: 2' }
$global:World.HypervisorPresent = $false
Check 'in a language it does not know, no hypervisor still means WSL cannot start' ((Get-WslState) -eq 'NeedsComponent')
$global:World.HypervisorPresent = $true
Check 'in a language it does not know, a running hypervisor and an answer mean Ready' ((Get-WslState) -eq 'Ready')
$global:World.HypervisorPresent = $null; $global:World.FactsUnreadable = $true
Check 'when Windows will not say, wsl.exe answering is taken as Ready' ((Get-WslState) -eq 'Ready')
$global:World.WslRaw = @{ Exit = 1; Text = '' }
Check 'no answer from wsl.exe at all is NotInstalled' ((Get-WslState) -eq 'NotInstalled')
$global:World.WslRaw = $null; $global:World.FactsUnreadable = $false; $global:World.State = 'NotInstalled'
$global:World.Distros = @('OdooEdu', 'Ubuntu')
Check 'distributions are listed by name' ((Get-WslDistros) -contains 'OdooEdu')

# ---- 3. the Ubuntu image -----------------------------------------------------

Write-Host "`n== finding and downloading the Ubuntu image"
Reset-World
$image = Get-UbuntuImage
Check 'the newest release wins, and 24.04.10 beats 24.04.5' ($image.Name -eq 'ubuntu-24.04.10-wsl-amd64.wsl')
Check 'its checksum is the one published for that file' ($image.Sha256 -eq $hashNewest)
Check 'it comes from releases.ubuntu.com' ($image.Url -eq 'https://releases.ubuntu.com/noble/ubuntu-24.04.10-wsl-amd64.wsl')
$global:World.Sums = "aaaa *ubuntu-24.04.5-live-server-amd64.iso`n"
Check 'a release list without the WSL image is an error that says so' (Throws { Get-UbuntuImage } 'Could not find the Ubuntu 24.04 WSL image')
$global:World.Sums = $sums

$cache = Join-Path $work 'cache'
if (Test-Path $cache) { Remove-Item -Recurse -Force $cache }
$file = Save-UbuntuImage -Image $image -CacheDir $cache
Check 'the image is saved as .tar.gz' ($file.EndsWith('ubuntu-24.04.10-wsl-amd64.tar.gz') -and (Test-Path $file))
Check 'no half-downloaded .part file is left' (-not (Test-Path ($file + '.part')))
$before = Count-Of 'curl*-o*'
$file2 = Save-UbuntuImage -Image $image -CacheDir $cache
Check 'a download that is already there and matches is not downloaded again' ($file2 -eq $file -and (Count-Of 'curl*-o*') -eq $before)

Remove-Item -Recurse -Force $cache
$wrongImage = [pscustomobject]@{ Name = $image.Name; Url = $image.Url; Sha256 = ('f' * 64) }
Check 'a download that does not match the published checksum is refused' (Throws { Save-UbuntuImage -Image $wrongImage -CacheDir $cache } 'does not match the checksum')
Check 'and is thrown away' (-not (Test-Path (Join-Path $cache 'ubuntu-24.04.10-wsl-amd64.tar.gz*')))

Write-Host "`n== line endings"
$crlf = Join-Path $work 'crlf.sh'
[System.IO.File]::WriteAllBytes($crlf, [byte[]](0xEF, 0xBB, 0xBF) + [System.Text.Encoding]::UTF8.GetBytes("#!/bin/bash`r`necho hi`r`n"))
ConvertTo-LfFile $crlf
$bytes = [System.IO.File]::ReadAllBytes($crlf)
Check 'a script saved with Windows line endings and a BOM becomes plain LF without the BOM' (($bytes[0] -eq 0x23) -and (-not ($bytes -contains 13)))

# ---- 4. a fresh laptop: WSL cannot start yet ---------------------------------

Write-Host "`n== the first run on a fresh PC (Virtual Machine Platform off)"
Reset-World
$src = New-FakeSource
$global:World.State = 'NeedsComponent'
$global:World.StateAfterEnable = 'NeedsComponent'
$code = Run-Install $src
Check 'it asks for a restart (exit code 10)' ($code -eq 10)
Check 'it asked Windows for administrator rights' (Called 'elevate')
Check 'what runs elevated is the documented wsl --install --no-distribution' ($global:ElevatedScript -match 'wsl\.exe --install --no-distribution')
Check 'the elevated step writes its output to a log it can show afterwards' ($global:ElevatedScript -match '\*> ".+enable-wsl\.log"')
Check 'nothing was downloaded yet' (-not (Called 'curl*'))
Check 'no Linux system was created yet' (-not (Called 'wsl --import*'))
Check 'no shortcuts yet' ($global:Links.Count -eq 0)

Write-Host "`n== WSL that turns on without needing a restart"
Reset-World
$src = New-FakeSource
$global:World.State = 'NotInstalled'
$global:World.StateAfterEnable = 'Ready'
Check 'it carries straight on and finishes' ((Run-Install $src) -eq 0)
Check 'the Linux system was created' (Called 'wsl --import OdooEdu*')

Write-Host "`n== declining the administrator prompt"
Reset-World
$src = New-FakeSource
$global:World.State = 'NeedsComponent'
$global:World.UacDeclined = $true
Check 'it says the permission was not given' (Throws { Run-Install $src } 'permission to turn WSL on and it was not given')
Check 'and nothing else happened' (-not (Called 'curl*') -and -not (Called 'wsl --import*'))

Write-Host "`n== the administrator step fails"
Reset-World
$src = New-FakeSource
$global:World.State = 'NeedsComponent'
$global:World.EnableExit = 5
Check 'it stops with the exit code' (Throws { Run-Install $src } 'exit code 5')

# ---- 4b. a restart that did not finish the job ------------------------------

Write-Host "`n== reading what Windows says about virtualization"
Reset-World
$w = $global:World
$w.PlatformEnabled = $true; $w.RebootPending = $true; $w.HypervisorPresent = $false; $w.FirmwareEnabled = $true
$f = Get-VirtualizationFacts
Check 'the component, the pending restart, the hypervisor and the firmware setting are all read' (
    ($f.PlatformEnabled -eq $true) -and ($f.RebootPending -eq $true) -and ($f.HypervisorPresent -eq $false) -and ($f.FirmwareEnabled -eq $true))
Check 'the pending restart is looked up in all three places Windows servicing uses' (
    (Called 'reg query*Component Based Servicing\RebootPending') -and (Called 'reg query*PackagesPending') -and (Called 'reg query*SessionsPending'))
$w.RebootPending = $false
Check 'nothing pending reads as False' ((Get-VirtualizationFacts).RebootPending -eq $false)
$w.FactsUnreadable = $true
$f = Get-VirtualizationFacts
Check 'what cannot be read is unknown ($null), never a guess' (($null -eq $f.PlatformEnabled) -and ($null -eq $f.HypervisorPresent) -and ($null -eq $f.FirmwareEnabled))

Write-Host "`n== deciding what to do about a WSL that cannot start"
function New-Facts($Platform, $Pending, $Hypervisor, $Firmware) {
    return [pscustomobject]@{ PlatformEnabled = $Platform; RebootPending = $Pending; HypervisorPresent = $Hypervisor; FirmwareEnabled = $Firmware }
}
Check 'component off: turn it on' ((Get-WslPlan 'NeedsComponent' (New-Facts $false $false $false $true)) -eq 'Enable')
Check 'component on, Windows has not finished: restart again (do not ask for rights again)' ((Get-WslPlan 'NeedsComponent' (New-Facts $true $true $false $true)) -eq 'RestartAgain')
Check 'component on, whether a restart is pending is unknown: restart again' ((Get-WslPlan 'NeedsComponent' (New-Facts $true $null $false $true)) -eq 'RestartAgain')
Check 'component on, nothing pending, hypervisor not running: explain the hypervisor' ((Get-WslPlan 'NeedsComponent' (New-Facts $true $false $false $true)) -eq 'HypervisorOff')
Check 'virtualization off in the firmware: say so, whatever else is pending' ((Get-WslPlan 'NeedsComponent' (New-Facts $true $true $false $false)) -eq 'FirmwareOff')
Check 'nothing readable: do what a fresh PC gets (turn it on)' ((Get-WslPlan 'NeedsComponent' (New-Facts $null $null $null $null)) -eq 'Enable')
Check 'WSL missing altogether, the component on and a restart pending: restart first' ((Get-WslPlan 'NotInstalled' (New-Facts $true $true $false $true)) -eq 'RestartFirst')
Check 'WSL missing altogether, the component on, nothing pending: install it' ((Get-WslPlan 'NotInstalled' (New-Facts $true $false $false $true)) -eq 'Enable')

Write-Host "`n== counting the restarts since the component was switched on"
Reset-World
$global:World.RestartsSince = 2
Check 'restarts come from the System event log (one start event per boot)' ((Get-RestartsSince (Get-Date).AddHours(-1)) -eq 2)
$global:World.RestartsSince = 0; $global:World.RestartsNoMatchError = $true
Check 'PowerShell reports "no events" as an error: that means zero restarts' ((Get-RestartsSince (Get-Date).AddHours(-1)) -eq 0)
$global:World.EventsUnreadable = $true
Check 'a log that cannot be read is unknown, not zero' ($null -eq (Get-RestartsSince (Get-Date).AddHours(-1)))
$f = New-Facts $true $true $false $true
Check 'after one restart: still "restart again"' ((Get-WslPlan 'NeedsComponent' $f 1) -eq 'RestartAgain')
Check 'after two restarts: Windows is stuck, another restart will not help' ((Get-WslPlan 'NeedsComponent' $f 2) -eq 'StuckServicing')
Check 'restarts that cannot be counted: keep asking for a restart, as before' ((Get-WslPlan 'NeedsComponent' $f $null) -eq 'RestartAgain')
Check 'the stuck verdict needs a pending restart: nothing pending is the hypervisor case' ((Get-WslPlan 'NeedsComponent' (New-Facts $true $false $false $true) 5) -eq 'HypervisorOff')

Write-Host "`n== the installer remembers when it switched the component on"
Reset-World
$src = New-FakeSource
$global:World.State = 'NeedsComponent'; $global:World.StateAfterEnable = 'NeedsComponent'
[void](Run-Install $src)
Check 'it writes a marker with the time, right after the administrator step' ((Read-RestartMarker $installDir) -is [datetime])
Remove-Item (Join-Path $installDir 'wsl-enable.json')
Reset-World
$src = New-FakeSource
$global:World.State = 'NeedsComponent'; $global:World.PlatformEnabled = $true; $global:World.RebootPending = $true
[void](Run-Install $src)
Check 'a pending component it did not switch on itself gets a marker the first time it is seen' ((Read-RestartMarker $installDir) -is [datetime])

Write-Host "`n== Windows restarted twice and is still stuck (what this laptop did)"
Reset-World
$src = New-FakeSource
$w = $global:World
$w.State = 'NeedsComponent'; $w.PlatformEnabled = $true; $w.RebootPending = $true; $w.RestartsSince = 2
Write-RestartMarker $installDir
Check 'it stops asking for restarts and says the problem is inside Windows' (Throws { Run-Install $src } 'restarted twice.*problem inside Windows')
Check 'with what to try, in order: Windows Update, then DISM and sfc, then bcdedit' (Throws { Run-Install $src } 'Windows Update.*DISM /Online /Cleanup-Image /RestoreHealth.*sfc /scannow.*bcdedit /set hypervisorlaunchtype auto')
Check 'and without asking for administrator rights again' (-not (Called 'elevate'))

Write-Host "`n== Windows is already waiting for a restart from something else (an update)"
Check 'component not on yet, restart pending, none since: have it done first' ((Get-WslPlan 'NeedsComponent' (New-Facts $false $true $false $true) 0) -eq 'RestartFirst')
Check 'restarts that cannot be counted: still have it done first' ((Get-WslPlan 'NeedsComponent' (New-Facts $false $true $false $true) $null) -eq 'RestartFirst')
Check 'WSL missing altogether, restart pending: first too' ((Get-WslPlan 'NotInstalled' (New-Facts $false $true $false $true) 0) -eq 'RestartFirst')
Check 'one restart did not clear it: the flag is stale, go ahead and enable' ((Get-WslPlan 'NeedsComponent' (New-Facts $false $true $false $true) 1) -eq 'Enable')
Check 'nothing pending: enable' ((Get-WslPlan 'NeedsComponent' (New-Facts $false $false $false $true) 0) -eq 'Enable')
Reset-World
$src = New-FakeSource
$w = $global:World
$w.State = 'NeedsComponent'; $w.PlatformEnabled = $false; $w.RebootPending = $true; $w.RestartsSince = 0
Check 'the installer asks for that restart (exit code 10)' ((Run-Install $src) -eq 10)
Check 'and changes nothing: no administrator prompt, no download' ((-not (Called 'elevate')) -and (-not (Called 'curl*')))
Check 'it starts counting restarts from now' ((Read-RestartMarker $installDir) -is [datetime])
$w.RestartsSince = 1; $w.StateAfterEnable = 'NeedsComponent'
Check 'after one restart with the flag still set, it goes ahead and turns the component on (and asks for the restart that follows)' ((Run-Install $src) -eq 10 -and (Called 'elevate'))

Write-Host "`n== the second run after a restart that did not finish the job (this laptop's real situation)"
Reset-World
$src = New-FakeSource
$w = $global:World
$w.State = 'NeedsComponent'; $w.PlatformEnabled = $true; $w.RebootPending = $true
$code = Run-Install $src
Check 'it asks for a restart again (exit code 10)' ($code -eq 10)
Check 'it does not ask Windows for administrator rights a second time' (-not (Called 'elevate'))
Check 'nothing was downloaded or created' ((-not (Called 'curl*')) -and (-not (Called 'wsl --import*')))

Write-Host "`n== component on, nothing pending, hypervisor still not running"
Reset-World
$src = New-FakeSource
$w = $global:World
$w.State = 'NeedsComponent'; $w.PlatformEnabled = $true; $w.RebootPending = $false; $w.HypervisorPresent = $false
Check 'it says how to switch the hypervisor on' (Throws { Run-Install $src } 'bcdedit /set hypervisorlaunchtype auto')
Check 'and does not ask for administrator rights' (-not (Called 'elevate'))

Write-Host "`n== virtualization switched off in the BIOS"
Reset-World
$src = New-FakeSource
$w = $global:World
$w.State = 'NeedsComponent'; $w.PlatformEnabled = $true; $w.FirmwareEnabled = $false
Check 'it says to turn it on in the BIOS/UEFI setup' (Throws { Run-Install $src } 'BIOS/UEFI')

Write-Host "`n== facts Windows will not give"
Reset-World
$src = New-FakeSource
$w = $global:World
$w.State = 'NeedsComponent'; $w.FactsUnreadable = $true; $w.StateAfterEnable = 'NeedsComponent'
Check 'it behaves as on a fresh PC: asks for rights and a restart' ((Run-Install $src) -eq 10 -and (Called 'elevate'))

# ---- 5. the full install -----------------------------------------------------

Write-Host "`n== the full install, after the restart"
Reset-World
$src = New-FakeSource
$code = Run-Install $src
Check 'it finishes with 0' ($code -eq 0)
Check 'the release list was read from Ubuntu' (Called 'curl*releases.ubuntu.com/noble/SHA256SUMS')
Check 'the newest WSL image was downloaded' (Called 'curl*releases.ubuntu.com/noble/ubuntu-24.04.10-wsl-amd64.wsl')
Check 'the image was imported as OdooEdu, as WSL 2' (Called 'wsl --import OdooEdu * --version 2')
Check 'the imported file is the verified .tar.gz' (Called 'wsl --import OdooEdu*ubuntu-24.04.10-wsl-amd64.tar.gz*')
Check 'the staging folder was translated to a Linux path from inside the new distro' (Called 'wsl -d OdooEdu --exec wslpath -a *stage')
Check 'provisioning ran as root with the staged app and the user name' (Called 'wsl -d OdooEdu -u root --exec bash /mnt/c/fake/stage/windows/provision.sh /mnt/c/fake/stage edu')
$stagedInstall = Join-Path $installDir 'stage/install.sh'
$stagedProvision = Join-Path $installDir 'stage/windows/provision.sh'
Check 'install.sh and provision.sh were staged' ((Test-Path $stagedInstall) -and (Test-Path $stagedProvision))
Check 'and given Linux line endings (bash cannot run CRLF scripts)' (-not ([System.IO.File]::ReadAllBytes($stagedInstall) -contains 13) -and -not ([System.IO.File]::ReadAllBytes($stagedProvision) -contains 13))
Check 'order: import, then provision, then restart of the distro, then the Docker check' (
    ((Index-Of 'wsl --import*') -lt (Index-Of '*provision.sh*')) -and
    ((Index-Of '*provision.sh*') -lt (Index-Of 'wsl --terminate OdooEdu')) -and
    ((Index-Of 'wsl --terminate OdooEdu') -lt (Index-Of 'wsl -d OdooEdu -u edu --exec docker info')))
Check 'the settings file records the distro, user and port' ((Get-Content (Join-Path $installDir 'config.json') -Raw | ConvertFrom-Json) |
    ForEach-Object { ($_.distro -eq 'OdooEdu') -and ($_.user -eq 'edu') -and ($_.port -eq 5001) })
foreach ($name in 'lib.ps1', 'start.ps1', 'stop.ps1', 'uninstall.ps1', 'diagnose.ps1', 'verify.ps1') {
    Check ("$name was copied next to the settings") (Test-Path (Join-Path $installDir $name))
}
$leaves = @($global:Links | ForEach-Object { Split-Path -Leaf $_.Path })
Check 'Start menu entries for start, stop and uninstall plus a desktop icon were made' (
    ($leaves.Count -eq 4) -and
    (@($leaves | Where-Object { $_ -eq 'Odoo Edu Manager.lnk' }).Count -eq 2) -and
    (@($leaves | Where-Object { $_ -eq 'Stop Odoo Edu Manager.lnk' }).Count -eq 1) -and
    (@($leaves | Where-Object { $_ -eq 'Uninstall Odoo Edu Manager.lnk' }).Count -eq 1))
Check 'one of the start entries is on the desktop, the others in the Start menu' (
    (@($global:Links | Where-Object { $_.Path -like (Join-Path $work 'Desktop*') }).Count -eq 1) -and
    (@($global:Links | Where-Object { $_.Path -like (Join-Path $work 'StartMenu*') }).Count -eq 3))
$uninstallLink = @($global:Links | Where-Object { (Split-Path -Leaf $_.Path) -eq 'Uninstall Odoo Edu Manager.lnk' })[0]
Check 'the Uninstall entry runs the installed uninstall.ps1 with -Pause (so the result can be read)' (
    ($uninstallLink.Arguments -match '-ExecutionPolicy Bypass') -and ($uninstallLink.Arguments -like '*OdooEduManager*uninstall.ps1" -Pause'))
Check 'and does not start inside the folder it deletes' (($uninstallLink.WorkingDirectory) -and ($uninstallLink.WorkingDirectory -ne $installDir))
$appsKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\OdooEduManager'
$entry = $global:World.Registry[$appsKey]
Check 'it is listed in Settings > Apps, per user (HKEY_CURRENT_USER, no administrator rights)' ($null -ne $entry)
Check 'with its name, icon and install folder' (($entry.DisplayName -eq 'Odoo Edu Manager') -and ($entry.DisplayIcon -like '*wsl.exe,0') -and ($entry.InstallLocation -eq $installDir))
Check 'and an uninstall command that runs the installed uninstall.ps1 with -Pause, quotes intact' (
    $entry.UninstallString -eq ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}" -Pause' -f (Join-Path $installDir 'uninstall.ps1')))
Check 'which Windows cannot offer to modify or repair' (($entry.NoModify -eq 1) -and ($entry.NoRepair -eq 1))
Check 'and the temporary .reg file is not left behind' (-not (Get-ChildItem ([System.IO.Path]::GetTempPath()) -Filter 'oem-uninstall-*.reg' -ErrorAction SilentlyContinue))
$start = @($global:Links | Where-Object { (Split-Path -Leaf $_.Path) -eq 'Odoo Edu Manager.lnk' })[0]
Check 'the shortcut runs the installed start.ps1 with the execution policy bypassed' (
    ($start.TargetPath -like '*powershell.exe') -and ($start.Arguments -match '-ExecutionPolicy Bypass') -and ($start.Arguments -like '*OdooEduManager*start.ps1*'))

Write-Host "`n== running it again is an update"
$global:Calls.Clear()
$global:Links.Clear()
$update = Run-InstallFull $src
Check 'it finishes with 0' ($update.Code -eq 0)
Check 'the existing Linux system and its data are kept (no import)' (-not (Called 'wsl --import*'))
Check 'nothing is downloaded again' (-not (Called 'curl*'))
Check 'the app is updated all the same' (Called '*provision.sh*')
Check 'the Linux is NOT restarted: that would stop the Odoo instances people have running' (-not (Called 'wsl --terminate*'))
Check 'Docker is still checked' (Called 'wsl -d OdooEdu -u edu --exec docker info')
Check 'the settings WSL reads were looked at before and after provisioning' (
    ((Count-Of 'wsl -d OdooEdu -u root --exec cat /etc/wsl.conf') -eq 2) -and
    ((Index-Of 'wsl -d OdooEdu -u root --exec cat /etc/wsl.conf') -lt (Index-Of '*provision.sh*')))
Check 'people are told to reopen the app to get the new version' ($update.Text -match 'close its window and open it again')
Check 'and how to remove it later' ($update.Text -match 'Uninstall Odoo Edu Manager.*Settings > Apps')

Write-Host "`n== an update that changes WSL's own settings"
$global:Calls.Clear()
$global:World.ProvisionChangesConf = $true
$changed = Run-InstallFull $src
Check 'it finishes with 0' ($changed.Code -eq 0)
Check 'the Linux is restarted, because new WSL settings only apply after that' (Called 'wsl --terminate OdooEdu')
Check 'and Docker is checked after the restart' ((Index-Of 'wsl --terminate OdooEdu') -lt (Index-Of 'wsl -d OdooEdu -u edu --exec docker info'))
$global:World.ProvisionChangesConf = $false

Write-Host "`n== Settings > Apps refuses the entry"
Reset-World
$src = New-FakeSource
$global:World.RegImportExit = 5
$refused = Run-InstallFull $src
Check 'the install still completes, with the Start menu entries' (($refused.Code -eq 0) -and ($global:Links.Count -eq 4))
Check 'and says the entry could not be made, pointing to the Start menu one' (($refused.Text -match 'could not be listed in Settings > Apps') -and ($refused.Text -match 'Access is denied') -and ($refused.Text -match 'Start menu still works'))
Check 'nothing half-made is left in the registry' ($global:World.Registry.Count -eq 0)

Write-Host "`n== installing without shortcuts"
Reset-World
$src = New-FakeSource
$bare = @(Invoke-OdooEduInstall -SourceDir $src -ScriptsDir $windows -InstallDir $installDir -NoShortcuts 6>&1)
Check 'no Start menu entries and no Settings > Apps entry' (($global:Links.Count -eq 0) -and ($global:World.Registry.Count -eq 0) -and -not (Called 'reg import*'))

Write-Host "`n== a look-alike name is not the same distribution"
Reset-World
$src = New-FakeSource
$global:World.Distros = @('OdooEdu-old', 'Ubuntu')
Check 'OdooEdu is created even though OdooEdu-old exists' ((Run-Install $src) -eq 0 -and (Called 'wsl --import OdooEdu *'))

Write-Host "`n== an older WSL that does not know --version"
Reset-World
$src = New-FakeSource
$global:World.ImportRejectsVersion = $true
Check 'the import is retried without it and succeeds' ((Run-Install $src) -eq 0 -and (Count-Of 'wsl --import*') -eq 2 -and $global:World.Distros -contains 'OdooEdu')

# ---- 6. what goes wrong ------------------------------------------------------

Write-Host "`n== virtualization unavailable when importing"
Reset-World
$src = New-FakeSource
$global:World.ImportExit = 1
$global:World.ImportText = 'WSL2 is not supported: Error code: Wsl/Service/CreateInstance/CreateVm/HCS/HCS_E_HYPERV_NOT_INSTALLED 0x80370102 virtualization'
Check 'the message says what to do about it' (Throws { Run-Install $src } 'Virtual Machine Platform')
Check 'and still shows what WSL said' (Throws { Run-Install $src } '0x80370102')

Write-Host "`n== provisioning fails"
Reset-World
$src = New-FakeSource
$global:World.ProvisionExit = 3
Check 'it stops and says so' (Throws { Run-Install $src } 'failed \(exit code 3\)')
Check 'no Start menu entries are made for a broken install' ($global:Links.Count -eq 0)
Check 'and no settings file is written' (-not (Test-Path (Join-Path $installDir 'config.json')))

Write-Host "`n== Docker slow to come up"
Reset-World
$src = New-FakeSource
$global:World.DockerUp = $false
Check 'it is a warning, not a failure: the install completes' ((Run-Install $src) -eq 0 -and $global:Links.Count -eq 4)
Check 'Docker is asked to start explicitly while waiting' (Called 'wsl -d OdooEdu -u root --exec sh -c*systemctl start docker*')

Write-Host "`n== a computer that cannot do it"
Reset-World
$src = New-FakeSource
$global:World.Build = 18362
Check 'an old Windows 10 is refused with its build number' (Throws { Run-Install $src } 'build 18362')
Reset-World
$env:PROCESSOR_ARCHITECTURE = 'ARM64'
Check 'an ARM computer is refused' (Throws { Run-Install $src } 'Intel/AMD')
$env:PROCESSOR_ARCHITECTURE = 'AMD64'
Reset-World
Check 'a folder without install.sh is refused with advice' (Throws { Run-Install $work } 'Cannot find install.sh')

# ---- 7. the scripts people double-click --------------------------------------

Write-Host "`n== install.ps1 as double-clicked"
Reset-World
& (Join-Path $windows 'install.ps1')
Check 'it exits 0' ($LASTEXITCODE -eq 0)
Check 'it keeps a log' (Test-Path (Join-Path $installDir 'install.log'))
Check 'the Linux system and the settings exist afterwards' ((Called 'wsl --import OdooEdu*') -and (Test-Path (Join-Path $installDir 'config.json')))

Reset-World
$global:World.State = 'NeedsComponent'
$global:World.StateAfterEnable = 'NeedsComponent'
& (Join-Path $windows 'install.ps1')
Check 'when Windows must restart first it exits 10' ($LASTEXITCODE -eq 10)

Reset-World
$global:World.ProvisionExit = 3
& (Join-Path $windows 'install.ps1') | Out-Null
Check 'a failure exits 1' ($LASTEXITCODE -eq 1)
Check 'and the log says what happened' ((Get-Content (Join-Path $installDir 'install.log') -Raw) -match 'failed')

Write-Host "`n== start.ps1 (the Start menu entry)"
Reset-World
& (Join-Path $windows 'start.ps1') | Out-Null
Check 'not installed yet: it says so and exits 1' ($LASTEXITCODE -eq 1)

Reset-World
Write-FakeInstall
& (Join-Path $windows 'start.ps1') | Out-Null
Check 'the Linux system is missing: it says so and exits 1' ($LASTEXITCODE -eq 1)

Reset-World
Write-FakeInstall
$global:World.Distros = @('OdooEdu')
& (Join-Path $windows 'start.ps1') | Out-Null
Check 'a hidden keep-alive keeps Linux (and the Odoo instances) up after the window closes' (Called 'start-process wsl.exe -d OdooEdu -u root --exec sleep infinity')
Check 'it waits for Docker as the app user' (Called 'wsl -d OdooEdu -u edu --exec docker info')
Check 'it opens the browser once the app answers' (Called 'start-job*')
Check 'it runs the app in the foreground, from the installed tool' (Called "wsl -d OdooEdu -u edu --exec bash -lc exec ~/.local/bin/odoo-edu-manager")
Check 'the window waits for Enter when the app stops' (Called 'read-host*')

Reset-World
Write-FakeInstall
$global:World.Distros = @('OdooEdu')
$global:World.KeepAlive = $true
& (Join-Path $windows 'start.ps1') | Out-Null
Check 'a keep-alive that is already running is not started twice' (-not (Called 'start-process wsl.exe*sleep infinity'))

Reset-World
Write-FakeInstall
$global:World.Distros = @('OdooEdu')
$global:World.AppUp = $true
& (Join-Path $windows 'start.ps1') | Out-Null
Check 'already running: it just opens the browser' ((Called 'start-process http://localhost:5001*') -and -not (Called 'wsl -d OdooEdu -u edu --exec bash -lc*'))

Write-Host "`n== stop.ps1"
Reset-World
Write-FakeInstall
$stopText = (& (Join-Path $windows 'stop.ps1') 6>&1 | Out-String)
Check 'it ends the Linux system (which stops the keep-alive and every instance)' (Called 'wsl --terminate OdooEdu')
Check 'it says the instances are kept and must be started again from the dashboard (the app does not start them by itself)' (
    ($stopText -match 'press Start') -and ($stopText -notmatch 'starts them again'))

Write-Host "`n== uninstall.ps1"
$appsKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\OdooEduManager'
Reset-World
Write-FakeInstall
$global:World.Distros = @('OdooEdu', 'Ubuntu')
$global:World.Answer = 'no'
& (Join-Path $windows 'uninstall.ps1') | Out-Null
Check 'anything but YES changes nothing' (-not (Called 'wsl --unregister*'))
$global:World.Answer = 'yes'
& (Join-Path $windows 'uninstall.ps1') | Out-Null
Check 'a lower-case yes is not enough either' (-not (Called 'wsl --unregister*'))
Check 'and it removes no Settings > Apps entry either' (-not (Called 'reg delete*'))

# a finished install as the installer leaves it: the four shortcuts, the Settings > Apps entry, WSL's empty Start menu folder
New-Item -ItemType Directory -Force -Path $env:ODOO_EDU_MANAGER_START_MENU_DIR, $env:ODOO_EDU_MANAGER_DESKTOP_DIR, (Join-Path $env:ODOO_EDU_MANAGER_START_MENU_DIR 'OdooEdu') | Out-Null
foreach ($shortcut in @(
        (Join-Path $env:ODOO_EDU_MANAGER_START_MENU_DIR 'Odoo Edu Manager.lnk'), (Join-Path $env:ODOO_EDU_MANAGER_START_MENU_DIR 'Stop Odoo Edu Manager.lnk'),
        (Join-Path $env:ODOO_EDU_MANAGER_START_MENU_DIR 'Uninstall Odoo Edu Manager.lnk'), (Join-Path $env:ODOO_EDU_MANAGER_DESKTOP_DIR 'Odoo Edu Manager.lnk'))) { Set-Content -Path $shortcut -Value 'x' }
$global:World.Registry[$appsKey] = @{ DisplayName = 'Odoo Edu Manager' }
$global:Calls.Clear()
$global:World.Answer = 'YES'
& (Join-Path $windows 'uninstall.ps1') | Out-Null
Check 'the shortcuts it made are removed, the Uninstall one too' (-not (Get-ChildItem $env:ODOO_EDU_MANAGER_START_MENU_DIR, $env:ODOO_EDU_MANAGER_DESKTOP_DIR -Filter '*.lnk' -ErrorAction SilentlyContinue))
Check 'the Settings > Apps entry is removed' ((Called 'reg delete HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\OdooEduManager /f') -and -not $global:World.Registry.ContainsKey($appsKey))
Check 'the empty Start menu folder WSL made for the Linux is removed' (-not (Test-Path (Join-Path $env:ODOO_EDU_MANAGER_START_MENU_DIR 'OdooEdu')))
Check 'YES removes the Linux system' (Called 'wsl --unregister OdooEdu')
Check 'and only that one: another distribution stays' ($global:World.Distros -contains 'Ubuntu' -and $global:World.Distros -notcontains 'OdooEdu')
Check 'its own folder is scheduled for removal' (Called 'start-process cmd.exe /c*rmdir /s /q*OdooEduManager*')
Check 'by a helper that does not start inside that folder (Windows would refuse to delete it)' ((Called 'start-process cmd.exe /c*rmdir /s /q*[[]in *') -and -not (Called 'start-process cmd.exe /c*[[]in *OdooEduManager]'))
Check 'it does not wait for a key when started without -Pause' (-not (Called 'read-host Press Enter*'))

Reset-World
Write-FakeInstall
$global:World.Distros = @('OdooEdu')
New-Item -ItemType Directory -Force -Path (Join-Path $env:ODOO_EDU_MANAGER_START_MENU_DIR 'OdooEdu') | Out-Null
Set-Content -Path (Join-Path $env:ODOO_EDU_MANAGER_START_MENU_DIR 'OdooEdu/something.lnk') -Value 'x'
$global:World.Answer = 'YES'
& (Join-Path $windows 'uninstall.ps1') | Out-Null
Check 'a Start menu folder with anything in it is left alone' (Test-Path (Join-Path $env:ODOO_EDU_MANAGER_START_MENU_DIR 'OdooEdu/something.lnk'))
Remove-Item -Recurse -Force (Join-Path $env:ODOO_EDU_MANAGER_START_MENU_DIR 'OdooEdu')

Reset-World
Write-FakeInstall
$global:World.Distros = @('OdooEdu')
$global:World.Answer = 'no'
& (Join-Path $windows 'uninstall.ps1') -Pause | Out-Null
Check '-Pause keeps the window open even when nothing was removed' ((Called 'read-host Press Enter to close') -and -not (Called 'wsl --unregister*'))
$global:Calls.Clear()
& (Join-Path $windows 'uninstall.ps1') -Yes -Pause | Out-Null
Check '-Yes does not ask, removes it, and -Pause then waits for a key' ((Called 'wsl --unregister OdooEdu') -and -not (Called 'read-host Type YES*') -and (Called 'read-host Press Enter to close'))

Write-Host "`n== diagnose.ps1"
Reset-World
Write-FakeInstall
$global:World.Distros = @('OdooEdu')
& (Join-Path $windows 'diagnose.ps1') | Out-Null
$report = Join-Path $installDir 'diagnose.txt'
Check 'it writes a report' (Test-Path $report)
$text = Get-Content $report -Raw
Check 'with Windows, WSL and Docker sections' (($text -match '--- Windows') -and ($text -match '--- wsl --status') -and ($text -match '--- docker info'))
Check 'and the state the installer sees' ($text -match 'WSL state seen by the installer')
Check 'and how it reads virtualization, the last start and the Hyper-V requirements' (($text -match 'Virtualization as the installer reads it') -and ($text -match 'Windows started at') -and ($text -match 'Hyper-V requirements'))

# ---- 8. verify.ps1: the health check an AI (or a person) runs after installing -----------

Write-Host "`n== verify.ps1"

# a healthy, finished install with the app running and one instance up
function New-HealthyWorld {
    Reset-World
    Write-FakeInstall
    $w = $global:World
    $w.Distros = @('OdooEdu')
    $w.WslConf = "[boot]`nsystemd = true`n`n[user]`ndefault = edu"
    $w.Containers = @{ 'odoo-manager-db' = 'running'; 'odoo-manager-code-server' = 'running' }
    $w.Instances = @('odoo-inst-test1|Up 2 minutes|0.0.0.0:8069->8069/tcp, [::]:8069->8069/tcp, 8071-8072/tcp, 0.0.0.0:8070->5678/tcp')
    $w.Http = @{
        'http://localhost:5001/help' = '200'; 'http://localhost:5001/' = '200'; 'http://localhost:5001/new' = '200'
        'http://localhost:5001/settings' = '200'; 'http://localhost:5001/databases' = '200'
        'http://localhost:8443/' = '302'
        'http://localhost:8069/web/login' = '200'; 'http://test1.localhost:8069/web/login' = '200'
    }
    foreach ($n in 'lib.ps1', 'start.ps1', 'stop.ps1', 'uninstall.ps1', 'diagnose.ps1') { Set-Content -Path (Join-Path $installDir $n) -Value 'x' }
    Set-Content -Path (Join-Path $installDir 'install.log') -Value @(
        'Windows PowerShell transcript start', 'Start time: 20261020134606',
        'Host Application: powershell.exe -File C:\pkg\windows\install.ps1',
        '==> First-run setup: the shared database, the list of Odoo versions and the code-server',
        'All done.', 'End time: 20261020134629')
    foreach ($dir in 'StartMenu', 'Desktop') {
        New-Item -ItemType Directory -Force -Path (Join-Path $work $dir) | Out-Null
        Get-ChildItem (Join-Path $work $dir) -Filter '*.lnk' | Remove-Item -Force
    }
    foreach ($n in 'Odoo Edu Manager.lnk', 'Stop Odoo Edu Manager.lnk', 'Uninstall Odoo Edu Manager.lnk') { Set-Content -Path (Join-Path $work "StartMenu/$n") -Value 'x' }
    $w.Registry['Software\Microsoft\Windows\CurrentVersion\Uninstall\OdooEduManager'] = @{ DisplayName = 'Odoo Edu Manager' }
    Set-Content -Path (Join-Path $work 'Desktop/Odoo Edu Manager.lnk') -Value 'x'
}

function Run-Verify {
    $text = (& (Join-Path $windows 'verify.ps1') 6>&1 | Out-String)
    return [pscustomobject]@{ Code = $LASTEXITCODE; Text = $text }
}

New-HealthyWorld
$v = Run-Verify
Check 'a healthy install: nothing failed, exit code 0' (($v.Code -eq 0) -and ($v.Text -match 'RESULT: PASS') -and ($v.Text -notmatch '\[FAIL\]'))
foreach ($expected in 'installation record', 'last install run', 'first-run setup', 'Linux distribution', 'wsl.conf: systemd', 'systemd', 'Docker', 'docker group',
    'app installed', 'app version', 'shared database container', 'code-server container', 'the app answers', 'page /databases', 'code-server answers',
    'instance test1 answers', 'instance test1 by name', 'Start menu entries', 'desktop icon', 'Settings > Apps entry') {
    Check ("it checks: $expected") ($v.Text -match ('\[PASS\] ' + [regex]::Escape($expected)))
}
Check 'it reports how long the last install took and which folder it ran from' (($v.Text -match 'last install duration - 23 seconds') -and ($v.Text -match 'last install ran from - .*install\.ps1'))
Check 'it lists the instance with its port' ($v.Text -match '\[INFO\] instance test1 - Up 2 minutes, port 8069')
Check 'it saves the same report next to install.log' ((Test-Path (Join-Path $installDir 'verify.txt')) -and ((Get-Content (Join-Path $installDir 'verify.txt') -Raw) -match 'RESULT: PASS'))
$wslCalls = @($global:Calls | Where-Object { $_ -like 'wsl *' })
$allowed = @('wsl --list --verbose', 'wsl --list --quiet',
    'wsl -d OdooEdu -u root --exec cat /etc/wsl.conf', 'wsl -d OdooEdu -u root --exec systemctl is-system-running',
    'wsl -d OdooEdu -u edu --exec docker info --format *', 'wsl -d OdooEdu -u edu --exec id -nG',
    'wsl -d OdooEdu -u edu --exec bash -lc test -x ~/.local/bin/odoo-edu-manager *', 'wsl -d OdooEdu -u edu --exec bash -lc free -m *',
    'wsl -d OdooEdu -u edu --exec docker inspect --format *', 'wsl -d OdooEdu -u edu --exec docker ps -a --filter *')
$unexpected = @($wslCalls | Where-Object { $call = $_; -not (@($allowed | Where-Object { $call -like $_ }).Count) })
Check 'it only ever runs read-only commands (nothing outside this allow-list reaches wsl.exe)' (($wslCalls.Count -gt 8) -and ($unexpected.Count -eq 0))
Check 'and it never asks for rights, downloads or launches anything' (-not (Called 'elevate*') -and -not (Called 'start-process*') -and -not (Called 'robocopy*') -and -not (Called 'curl*-o *.tar.gz*'))
Check 'and it only looks at the registry, never writes to it' ((Called 'reg query HKCU*OdooEduManager*') -and -not (Called 'reg import*') -and -not (Called 'reg delete*') -and -not (Called 'reg add*'))

Write-Host "`n== verify.ps1: things that are wrong"
Reset-World
$v = Run-Verify
Check 'not installed: it fails and says to run install.cmd' (($v.Code -eq 1) -and ($v.Text -match '\[FAIL\] installation record') -and ($v.Text -match 'run install\.cmd'))

New-HealthyWorld; $global:World.Distros = @()
$v = Run-Verify
Check 'the Linux is gone: it fails and stops' (($v.Code -eq 1) -and ($v.Text -match '\[FAIL\] Linux distribution') -and ($v.Text -notmatch 'Docker'))

New-HealthyWorld; $global:World.DistroVersion = 1
Check 'a WSL 1 distribution fails' ((Run-Verify).Text -match '\[FAIL\] Linux distribution.*version 1')

New-HealthyWorld; $global:World.WslConf = "[user]`ndefault = edu"
$v = Run-Verify
Check 'systemd off in wsl.conf fails' (($v.Code -eq 1) -and ($v.Text -match '\[FAIL\] wsl\.conf: systemd'))

New-HealthyWorld; $global:World.Systemd = 'degraded'
$v = Run-Verify
Check 'systemd degraded is a warning, not a failure' (($v.Code -eq 0) -and ($v.Text -match '\[WARN\] systemd'))

New-HealthyWorld; $global:World.DockerUp = $false
$v = Run-Verify
Check 'Docker not answering fails with what Docker said' (($v.Code -eq 1) -and ($v.Text -match '\[FAIL\] Docker.*Cannot connect'))

New-HealthyWorld; $global:World.AppBinary = $false
Check 'the app missing in Linux fails' ((Run-Verify).Text -match '\[FAIL\] app installed')

New-HealthyWorld; $global:World.AppHasSetup = $false
$v = Run-Verify
Check 'an older app without the first-run setup is a warning that explains --help' (($v.Code -eq 0) -and ($v.Text -match '\[WARN\] app version.*older version'))

New-HealthyWorld; $global:World.Http = @{ 'http://localhost:8443/' = '302' }
$v = Run-Verify
Check 'the app not running is a warning, and the page checks are skipped' (($v.Code -eq 0) -and ($v.Text -match '\[WARN\] the app answers.*not running') -and ($v.Text -match '\[SKIP\] the web pages') -and ($v.Text -notmatch 'page /'))

New-HealthyWorld; $global:World.Http['http://localhost:5001/'] = '503'
$v = Run-Verify
Check 'the dashboard page failing (Docker down page) is a failure with the status' (($v.Code -eq 1) -and ($v.Text -match '\[FAIL\] page / - answered 503'))

New-HealthyWorld; $global:World.Http['http://localhost:8069/web/login'] = '000'
$v = Run-Verify
Check 'an instance that does not answer yet is a warning' (($v.Code -eq 0) -and ($v.Text -match '\[WARN\] instance test1 answers'))

New-HealthyWorld; $global:World.Instances = @('odoo-inst-test1|Exited (0) 41 seconds ago|')
$v = Run-Verify
Check 'a stopped instance is only listed (it is not a fault), and is not probed' (($v.Code -eq 0) -and ($v.Text -match '\[INFO\] instance test1 - Exited') -and ($v.Text -notmatch 'instance test1 answers'))

New-HealthyWorld; $global:World.Containers = @{ 'odoo-manager-db' = 'exited' }
$v = Run-Verify
Check 'a stopped database and a missing code-server are warnings' (($v.Code -eq 0) -and ($v.Text -match '\[WARN\] shared database container.*exited') -and ($v.Text -match '\[WARN\] code-server container.*does not exist'))

New-HealthyWorld; Set-Content -Path (Join-Path $installDir 'install.log') -Value @('Start time: 20261020134606', '==> Step 2 of 5 - Windows Subsystem for Linux (WSL)')
$v = Run-Verify
Check 'a last install run that never reached "All done." fails' (($v.Code -eq 1) -and ($v.Text -match '\[FAIL\] last install run - it did not finish'))

New-HealthyWorld; Set-Content -Path (Join-Path $installDir 'install.log') -Value @('Start time: 20261020103451', 'RESTART WINDOWS NOW (Start > Power > Restart)', 'End time: 20261020103454')
$v = Run-Verify
Check 'a run that asked for a restart is a warning, not a failure' (($v.Code -eq 0) -and ($v.Text -match '\[WARN\] last install run.*restart'))

New-HealthyWorld; Remove-Item (Join-Path $installDir 'stop.ps1')
Check 'an installed script that went missing fails' ((Run-Verify).Text -match '\[FAIL\] installed scripts.*stop\.ps1')

New-HealthyWorld; Get-ChildItem (Join-Path $work 'StartMenu') | Remove-Item -Force; Get-ChildItem (Join-Path $work 'Desktop') | Remove-Item -Force
$v = Run-Verify
Check 'missing shortcuts are warnings' (($v.Code -eq 0) -and ($v.Text -match '\[WARN\] Start menu entries') -and ($v.Text -match '\[WARN\] desktop icon'))

New-HealthyWorld; $global:World.Registry.Clear()
$v = Run-Verify
Check 'no Settings > Apps entry is a warning that points to the Start menu one' (($v.Code -eq 0) -and ($v.Text -match '\[WARN\] Settings > Apps entry.*Start menu entry'))

New-HealthyWorld; Remove-Item (Join-Path $work 'StartMenu/Uninstall Odoo Edu Manager.lnk')
Check 'a missing Uninstall shortcut is named in the warning' ((Run-Verify).Text -match '\[WARN\] Start menu entries - missing: Uninstall Odoo Edu Manager.lnk')

# ---- done --------------------------------------------------------------------

Remove-TestFolder
Write-Host ''
Write-Host ("  {0} passed, {1} failed" -f $global:Pass, $global:Fail)
if ($global:Fail -gt 0) { exit 1 }
