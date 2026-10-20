# diagnose.ps1 - collects what is needed to work out why something does not work.
# It only reads; it writes diagnose.txt next to the install log and prints the same.
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'lib.ps1')

$installDir = Join-Path $env:LOCALAPPDATA 'OdooEduManager'
$out = New-Object System.Collections.ArrayList
function Add-Section([string]$Title, $Body) {
    [void]$out.Add('')
    [void]$out.Add('--- ' + $Title)
    foreach ($line in @($Body)) { [void]$out.Add('' + $line) }
}
function Safe([scriptblock]$Block) {
    $ErrorActionPreference = 'Stop'   # so a failing probe becomes a line in the report, not red noise
    try { & $Block } catch { 'failed: ' + $_.Exception.Message }
}

Add-Section 'Windows' (Safe { Get-CimInstance Win32_OperatingSystem | ForEach-Object { '{0} {1} build {2} {3}' -f $_.Caption, $_.Version, $_.BuildNumber, $_.OSArchitecture } })
Add-Section 'PowerShell' $PSVersionTable.PSVersion.ToString()
Add-Section 'Virtualization' (Safe { Get-CimInstance Win32_ComputerSystem | ForEach-Object { '{0} {1} - hypervisor present: {2}' -f $_.Manufacturer, $_.Model, $_.HypervisorPresent } })
Add-Section 'Free disk space on C: (GB)' (Safe { [math]::Round((Get-PSDrive C).Free / 1GB, 1) })
Add-Section 'wsl --version' (Safe { (Invoke-Wsl @('--version') -Capture).Text })
Add-Section 'wsl --status' (Safe { (Invoke-Wsl @('--status') -Capture).Text })
Add-Section 'wsl --list --verbose' (Safe { (Invoke-Wsl @('--list', '--verbose') -Capture).Text })
Add-Section 'WSL state seen by the installer' (Safe { Get-WslState })
Add-Section 'Virtualization as the installer reads it' (Safe { (Get-VirtualizationFacts | Format-List | Out-String).Trim() })
Add-Section 'Windows started at' (Safe { (Get-CimInstance Win32_OperatingSystem).LastBootUpTime })
Add-Section 'Hyper-V requirements (systeminfo)' (Safe { (systeminfo.exe | Select-String -Pattern 'Hyper-V Requirements' -Context 0, 6 | Out-String).Trim() })

$config = Read-InstallConfig -InstallDir $installDir
if ($null -eq $config) {
    Add-Section 'Installation' 'not installed (no config.json)'
}
else {
    Add-Section 'Installation' ('distro={0} user={1} port={2}' -f $config.distro, $config.user, $config.port)
    Add-Section 'docker info (inside Linux)' (Safe { (Invoke-Wsl @('-d', $config.distro, '-u', $config.user, '--exec', 'docker', 'info', '--format', '{{.ServerVersion}} on {{.OperatingSystem}}') -Capture).Text })
    Add-Section 'is the app answering?' (Safe {
        try { 'HTTP {0}' -f (Invoke-WebRequest -UseBasicParsing -Uri ('http://localhost:{0}/help' -f $config.port) -TimeoutSec 3).StatusCode }
        catch { 'no: ' + $_.Exception.Message }
    })
    Add-Section 'Linux: uptime, memory, disk' (Safe { (Invoke-Wsl @('-d', $config.distro, '-u', $config.user, '--exec', 'sh', '-c', 'uptime; free -m | head -2; df -h / | tail -1') -Capture).Text })
}
$log = Join-Path $installDir 'install.log'
if (Test-Path $log) { Add-Section 'last lines of install.log' (Get-Content -Path $log -Tail 40) }

$text = $out -join "`r`n"
$target = Join-Path $installDir 'diagnose.txt'
if (Test-Path $installDir) { Set-Content -Path $target -Value $text -Encoding ASCII }
Write-Host $text
Write-Host ''
if (Test-Path $installDir) { Write-Host ('Saved to ' + $target) }
