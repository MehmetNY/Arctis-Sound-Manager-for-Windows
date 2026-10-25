# Install the real installer silently on a clean runner and look at what it did (files, virtual microphone driver, Start with Windows).
param([Parameter(Mandatory = $true)][string]$Installer)
$script:LogFile = "chain-install.log"
. ./ci/common.ps1
System-Info
Run "start audio service, allow microphone access" {
    Set-Service Audiosrv -StartupType Automatic; Start-Service Audiosrv
    foreach ($root in "HKLM:", "HKCU:") { foreach ($sub in "", "\NonPackaged") {
        $k = "$root\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone$sub"
        New-Item -Path $k -Force | Out-Null; Set-ItemProperty -Path $k -Name Value -Value Allow } }
    "ok"
}
Devices "before the installer"
Run "installer file" { Get-Item $Installer | Select-Object Name, Length | Format-List | Out-String }
$work = Join-Path $PWD "work"; New-Item -ItemType Directory -Force $work | Out-Null
$exitFile = Join-Path $work "inst.exit"; Remove-Item $exitFile -ErrorAction SilentlyContinue
@"
`$p = Start-Process -FilePath '$((Resolve-Path $Installer).Path)' -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/TASKS=autostart','/LOG=$((Join-Path $PWD 'chain-setup.log'))' -PassThru -Wait
Set-Content -Path '$exitFile' -Value `$p.ExitCode
"@ | Set-Content (Join-Path $work "inst.ps1")
$finished = Invoke-Timed (Join-Path $work "inst.ps1") @() 420 "installer"
$code = if (Test-Path $exitFile) { (Get-Content $exitFile).Trim() } else { "none" }
Log "INSTALLER finished=$finished exit=$code"
Start-Sleep 8
Devices "after the installer"
Run "installed files" {
    $app = Join-Path $env:ProgramFiles "Arctis Sound Manager"
    if (Test-Path $app) { Get-ChildItem $app | Select-Object Name, Length | Format-Table | Out-String } else { "MISSING: $app" }
    if (Test-Path (Join-Path $app "unins000.exe")) { "uninstaller present" } else { "MISSING uninstaller" }
}
Run "virtual microphone driver" {
    Get-ChildItem (Join-Path $env:windir "System32\drivers") -Filter "vbaudio*" | Select-Object Name, Length | Format-Table | Out-String
}
Run "start with windows (HKCU Run)" {
    $r = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -ErrorAction SilentlyContinue
    "ArctisSoundManager = " + $r.ArctisSoundManager
    $a = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" -ErrorAction SilentlyContinue
    "StartupApproved present: " + [bool]($a -and $a.PSObject.Properties.Name -contains "ArctisSoundManager")
}
Run "installer log (tail)" { if (Test-Path chain-setup.log) { Get-Content chain-setup.log -Tail 40 } else { "no log" } }
if ($code -ne "0") { Log "INSTALL FAILED"; exit 1 }
