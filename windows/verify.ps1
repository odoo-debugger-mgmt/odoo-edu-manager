# verify.ps1 - checks that an installed Odoo Edu Manager works.
#
# It only reads: it installs nothing, stops nothing and changes no setting. (If the Linux is
# stopped it is started for the checks, and stops again by itself a little later.)
#
# One line per check:
#   [PASS] works    [FAIL] broken    [WARN] worth a look    [INFO] a fact    [SKIP] not checked
# then a RESULT line. The same text is saved to verify.txt next to install.log. The exit code is
# 0 when nothing failed and 1 when something did.
#
# The web pages can only be checked while the app runs, so open "Odoo Edu Manager" from the
# Start menu first. Without it the page checks are skipped with a warning (that is not a failure).
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib.ps1')

# These must match docker_manager.py (a test compares them): the shared database container, the
# code-server container and its port, and the prefix of every instance container.
$DbContainer = 'odoo-manager-db'
$CodeServerContainer = 'odoo-manager-code-server'
$CodeServerPort = 8443
$InstancePrefix = 'odoo-inst-'

$installDir = Join-Path $env:LOCALAPPDATA 'OdooEduManager'
$report = New-Object System.Collections.ArrayList
$count = @{ PASS = 0; FAIL = 0; WARN = 0; INFO = 0; SKIP = 0 }
$colors = @{ PASS = 'Green'; FAIL = 'Red'; WARN = 'Yellow'; INFO = 'Gray'; SKIP = 'DarkGray' }
$config = $null

function Add-Check {
    param([string]$Status, [string]$Name, [string]$Detail = '')
    $count[$Status] = $count[$Status] + 1
    $line = '[{0}] {1}' -f $Status, $Name
    if ($Detail) { $line = $line + ' - ' + $Detail }
    [void]$report.Add($line)
    Write-Host $line -ForegroundColor $colors[$Status]
}

function Complete-Verification {
    $verdict = 'PASS'
    if ($count['FAIL'] -gt 0) { $verdict = 'FAIL' }
    $summary = 'RESULT: {0} - {1} passed, {2} failed, {3} warnings, {4} skipped' -f $verdict, $count['PASS'], $count['FAIL'], $count['WARN'], $count['SKIP']
    [void]$report.Add('')
    [void]$report.Add($summary)
    Write-Host ''
    Write-Host $summary -ForegroundColor $colors[$verdict]
    if (Test-Path $installDir) {
        $target = Join-Path $installDir 'verify.txt'
        Set-Content -Path $target -Value ($report -join "`r`n") -Encoding ASCII
        Write-Host ('Saved to ' + $target)
    }
    if ($verdict -eq 'FAIL') { exit 1 }
    exit 0
}

# run a program inside the Linux; --exec means no shell, so ask for one (bash -lc) when needed
function Invoke-Linux {
    param([string[]]$Command, [string]$As = '')
    if (-not $As) { $As = $config.user }
    return (Invoke-Wsl (@('-d', $config.distro, '-u', $As, '--exec') + $Command) -Capture)
}

function Get-HttpCode {
    param([string]$Url)
    $out = & curl.exe -s -o NUL -w '%{http_code}' --max-time 8 $Url 2>$null
    return ('' + $out).Trim()
}

# the lines of the most recent install run in install.log (a PowerShell transcript per run)
function Get-LastInstallRun {
    param([string]$LogPath)
    if (-not (Test-Path $LogPath)) { return $null }
    $lines = @(Get-Content -Path $LogPath)
    $first = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^Start time: \d{14}') { $first = $i }
    }
    if ($first -lt 0) { return $null }
    return @($lines[$first..($lines.Count - 1)])
}

Write-Host 'Odoo Edu Manager - verification (read-only)' -ForegroundColor Cyan
Write-Host ''

# ---- Windows and the installation record ------------------------------------------------
try {
    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    Add-Check 'INFO' 'Windows' ('{0}, build {1}, PowerShell {2}' -f $os.Caption, $os.BuildNumber, $PSVersionTable.PSVersion)
}
catch {
    Add-Check 'INFO' 'Windows' ('PowerShell ' + $PSVersionTable.PSVersion)
}

$config = Read-InstallConfig -InstallDir $installDir
if ($null -eq $config) {
    Add-Check 'FAIL' 'installation record' ('no config.json in ' + $installDir + ' - Odoo Edu Manager is not installed here (run install.cmd)')
    Complete-Verification
}
Add-Check 'PASS' 'installation record' ('distro {0}, user {1}, port {2}' -f $config.distro, $config.user, $config.port)

$missing = @('lib.ps1', 'start.ps1', 'stop.ps1', 'uninstall.ps1', 'diagnose.ps1') | Where-Object { -not (Test-Path (Join-Path $installDir $_)) }
if (@($missing).Count -eq 0) { Add-Check 'PASS' 'installed scripts' 'start, stop, uninstall and diagnose are in place' }
else { Add-Check 'FAIL' 'installed scripts' ('missing from ' + $installDir + ': ' + (@($missing) -join ', ')) }

# ---- the last install run -------------------------------------------------------------
$run = Get-LastInstallRun -LogPath (Join-Path $installDir 'install.log')
if ($null -eq $run) {
    Add-Check 'WARN' 'last install run' 'install.log is missing or has no run in it'
}
else {
    $runText = $run -join "`n"
    if ($runText -match 'All done\.') { Add-Check 'PASS' 'last install run' 'it finished ("All done.")' }
    elseif ($runText -match 'RESTART WINDOWS NOW') { Add-Check 'WARN' 'last install run' 'it asked for a restart of Windows and has not been run again since' }
    else { Add-Check 'FAIL' 'last install run' 'it did not finish (there is no "All done." in install.log)' }
    if (($runText -match 'Start time: (\d{14})') -and ($runText -match 'End time: (\d{14})')) {
        $started = [datetime]::ParseExact(([regex]::Match($runText, 'Start time: (\d{14})').Groups[1].Value), 'yyyyMMddHHmmss', $null)
        $ended = [datetime]::ParseExact(([regex]::Match($runText, 'End time: (\d{14})').Groups[1].Value), 'yyyyMMddHHmmss', $null)
        Add-Check 'INFO' 'last install duration' ('{0} seconds (started {1})' -f [int]($ended - $started).TotalSeconds, $started.ToString('yyyy-MM-dd HH:mm'))
    }
    $ranFrom = $run | Where-Object { $_ -match '^Host Application:' } | Select-Object -First 1
    if ($ranFrom) { Add-Check 'INFO' 'last install ran from' (($ranFrom -replace '^Host Application:\s*', '')) }
    if ($runText -match 'First-run setup') { Add-Check 'PASS' 'first-run setup' 'the last install did it (database, versions, code-server)' }
    else { Add-Check 'WARN' 'first-run setup' 'the last install did not do it (an older version, or --no-setup)' }
}

$programs = Get-StartMenuFolder
$desktop = Get-DesktopFolder
$entries = @('Odoo Edu Manager.lnk', 'Stop Odoo Edu Manager.lnk', 'Uninstall Odoo Edu Manager.lnk') | Where-Object { -not (Test-Path (Join-Path $programs $_)) }
if (@($entries).Count -eq 0) { Add-Check 'PASS' 'Start menu entries' 'Odoo Edu Manager, Stop Odoo Edu Manager and Uninstall Odoo Edu Manager' }
else { Add-Check 'WARN' 'Start menu entries' ('missing: ' + (@($entries) -join ', ')) }
if (Test-Path (Join-Path $desktop 'Odoo Edu Manager.lnk')) { Add-Check 'PASS' 'desktop icon' 'Odoo Edu Manager' }
else { Add-Check 'WARN' 'desktop icon' 'not there (it may have been removed by the user)' }
if (Test-Uninstaller) { Add-Check 'PASS' 'Settings > Apps entry' 'listed, with an Uninstall button' }
else { Add-Check 'WARN' 'Settings > Apps entry' 'not listed (installed by an older version, or the entry was removed); the Start menu entry Uninstall Odoo Edu Manager still works' }

# ---- the Linux ------------------------------------------------------------------------
$distro = $config.distro
if (-not ((Get-WslDistros) -contains $distro)) {
    Add-Check 'FAIL' 'Linux distribution' ('"{0}" is not registered with WSL (wsl -l -v) - run install.cmd' -f $distro)
    Complete-Verification
}
$state = '?'
$version = '?'
foreach ($line in ((Invoke-Wsl @('--list', '--verbose') -Capture).Text -split "`n")) {
    $m = [regex]::Match($line, ('^\s*\*?\s*' + [regex]::Escape($distro) + '\s+(\S+)\s+(\d+)\s*$'))
    if ($m.Success) { $state = $m.Groups[1].Value; $version = $m.Groups[2].Value }
}
if ($version -eq '2') { Add-Check 'PASS' 'Linux distribution' ('{0} is a WSL 2 system ({1})' -f $distro, $state) }
else { Add-Check 'FAIL' 'Linux distribution' ('{0} should be WSL 2 but wsl -l -v says version {1}' -f $distro, $version) }

$conf = (Invoke-Linux @('cat', '/etc/wsl.conf') -As 'root').Text
if ($conf -match '(?im)^\s*systemd\s*=\s*true\s*$') { Add-Check 'PASS' 'wsl.conf: systemd' 'switched on, so Docker starts with the Linux' }
else { Add-Check 'FAIL' 'wsl.conf: systemd' 'systemd=true is not set in /etc/wsl.conf' }
if ($conf -match ('(?im)^\s*default\s*=\s*' + [regex]::Escape($config.user) + '\s*$')) { Add-Check 'PASS' 'wsl.conf: default user' $config.user }
else { Add-Check 'WARN' 'wsl.conf: default user' ('default={0} is not set in /etc/wsl.conf' -f $config.user) }

$sd = (Invoke-Linux @('systemctl', 'is-system-running') -As 'root').Text.Trim()
if ($sd -eq 'running') { Add-Check 'PASS' 'systemd' 'running' }
elseif (($sd -eq 'degraded') -or ($sd -eq 'starting')) { Add-Check 'WARN' 'systemd' ($sd + ' (a unit failed or the Linux is still starting; Docker below decides)') }
else { Add-Check 'FAIL' 'systemd' ('says "' + $sd + '"') }

$dockerInfo = Invoke-Linux @('docker', 'info', '--format', '{{.ServerVersion}}')
if (($dockerInfo.ExitCode -eq 0) -and $dockerInfo.Text) { Add-Check 'PASS' 'Docker' ('the engine answers: ' + $dockerInfo.Text.Trim()) }
else { Add-Check 'FAIL' 'Docker' ('docker info as ' + $config.user + ' failed: ' + $dockerInfo.Text) }
$groups = (Invoke-Linux @('id', '-nG')).Text
if ($groups -match '\bdocker\b') { Add-Check 'PASS' 'docker group' ($config.user + ' is in it') }
else { Add-Check 'WARN' 'docker group' ($config.user + ' is not in the docker group') }

$app = (Invoke-Linux @('bash', '-lc', 'test -x ~/.local/bin/odoo-edu-manager && echo binary=yes || echo binary=no; uv tool list 2>/dev/null | grep odoo-edu-manager; grep -rqs first_run_setup $(uv tool dir)/odoo-edu-manager/lib && echo setup=present || echo setup=missing')).Text
if ($app -match 'binary=yes') {
    $listed = ($app -split "`n" | Where-Object { $_ -match '^odoo-edu-manager\s' } | Select-Object -First 1)
    Add-Check 'PASS' 'app installed' ('' + $listed)
}
else { Add-Check 'FAIL' 'app installed' '~/.local/bin/odoo-edu-manager is missing - run install.cmd' }
if ($app -match 'setup=present') { Add-Check 'PASS' 'app version' 'has the first-run setup (--setup, --help)' }
else { Add-Check 'WARN' 'app version' 'an older version: no first-run setup, and --help would start a server - update with the newest install.cmd' }

$res = (Invoke-Linux @('bash', '-lc', 'free -m | head -2 | tail -1; df -h / | tail -1')).Text -split "`n"
foreach ($line in $res) { if ($line.Trim()) { Add-Check 'INFO' 'Linux memory/disk' ($line.Trim() -replace '\s+', ' ') } }
$vm = @(Get-Process -Name 'vmmem*' -ErrorAction SilentlyContinue)
if ($vm.Count -gt 0) { Add-Check 'INFO' 'WSL memory in Windows' ('{0} MB (vmmem)' -f [int](($vm | Measure-Object -Property WorkingSet64 -Sum).Sum / 1MB)) }

# ---- the containers --------------------------------------------------------------------
foreach ($item in @(@{ Name = $DbContainer; What = 'shared database' }, @{ Name = $CodeServerContainer; What = 'code-server' })) {
    $r = Invoke-Linux @('docker', 'inspect', '--format', '{{.State.Status}}', $item.Name)
    $status = $r.Text.Trim()
    if (($r.ExitCode -eq 0) -and ($status -eq 'running')) { Add-Check 'PASS' ($item.What + ' container') 'running' }
    elseif ($r.ExitCode -eq 0) { Add-Check 'WARN' ($item.What + ' container') ('exists but is ' + $status + ' (opening the app starts the database; Settings starts code-server)') }
    else { Add-Check 'WARN' ($item.What + ' container') 'does not exist yet (the first-run setup or the first start of the app creates it)' }
}

$instances = @{}
$listing = Invoke-Linux @('docker', 'ps', '-a', '--filter', 'label=role=odoo', '--format', '{{.Names}}|{{.Status}}|{{.Ports}}')
foreach ($line in ($listing.Text -split "`n")) {
    $parts = $line.Trim() -split '\|'
    if (($parts.Count -ge 3) -and $parts[0].StartsWith($InstancePrefix)) {
        $name = $parts[0].Substring($InstancePrefix.Length)
        $port = ''
        $m = [regex]::Match($parts[2], ':(\d+)->8069/tcp')
        if ($m.Success) { $port = $m.Groups[1].Value }
        $instances[$name] = @{ Status = $parts[1]; Port = $port }
        Add-Check 'INFO' ('instance ' + $name) ('{0}, port {1}' -f $parts[1], $(if ($port) { $port } else { 'none' }))
    }
}
if ($instances.Count -eq 0) { Add-Check 'INFO' 'instances' 'none yet (create one on the New instance page)' }

# ---- what answers from Windows ---------------------------------------------------------
$appUrl = 'http://localhost:{0}' -f $config.port
$help = Get-HttpCode ($appUrl + '/help')
if ($help -eq '200') {
    Add-Check 'PASS' 'the app answers' ($appUrl + '/help is 200')
    foreach ($path in @('/', '/new', '/settings', '/databases')) {
        $code = Get-HttpCode ($appUrl + $path)
        if ($code -eq '200') { Add-Check 'PASS' ('page ' + $path) '200' }
        else { Add-Check 'FAIL' ('page ' + $path) ('answered ' + $code + ' (the page "/" says Docker is not reachable when Docker is down)') }
    }
}
else {
    Add-Check 'WARN' 'the app answers' ($appUrl + '/help gave ' + $help + ' - the app is not running; open "Odoo Edu Manager" from the Start menu and run this again')
    Add-Check 'SKIP' 'the web pages' 'the app is not running'
}

$cs = Get-HttpCode ('http://localhost:{0}/' -f $CodeServerPort)
if (($cs -eq '200') -or ($cs -eq '302')) { Add-Check 'PASS' 'code-server answers' ('port {0} gave {1}' -f $CodeServerPort, $cs) }
else { Add-Check 'WARN' 'code-server answers' ('port {0} gave {1} - start it on the Settings page' -f $CodeServerPort, $cs) }

foreach ($name in ($instances.Keys | Sort-Object)) {
    $inst = $instances[$name]
    if (($inst.Status -like 'Up*') -and $inst.Port) {
        $code = Get-HttpCode ('http://localhost:{0}/web/login' -f $inst.Port)
        if ($code -eq '200') { Add-Check 'PASS' ('instance ' + $name + ' answers') ('localhost:{0}/web/login is 200' -f $inst.Port) }
        else { Add-Check 'WARN' ('instance ' + $name + ' answers') ('localhost:{0}/web/login gave {1} (a new instance needs a minute or two to start)' -f $inst.Port, $code) }
        $named = Get-HttpCode ('http://{0}.localhost:{1}/web/login' -f $name, $inst.Port)
        if ($named -eq '200') { Add-Check 'PASS' ('instance ' + $name + ' by name') ('{0}.localhost:{1} is 200' -f $name, $inst.Port) }
        else { Add-Check 'WARN' ('instance ' + $name + ' by name') ('{0}.localhost gave {1} here (browsers resolve *.localhost themselves, curl on old Windows may not)' -f $name, $named) }
    }
}

Complete-Verification
