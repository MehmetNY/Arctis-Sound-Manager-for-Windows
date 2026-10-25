# Does the frozen program start and can it do its jobs? (run on the PyInstaller folder, before installing anything)
param([string]$AppDir = "build\dist\ArctisSoundManager")
$script:LogFile = "smoke.log"
. ./ci/common.ps1
$exe = Join-Path (Resolve-Path $AppDir) "ArctisSoundManager.exe"
function Frozen([string[]]$a, [int]$timeout = 120) {
    $o = Join-Path $PWD "work\frozen.out"; $e = Join-Path $PWD "work\frozen.err"
    New-Item -ItemType Directory -Force (Split-Path $o) | Out-Null
    $p = Start-Process -FilePath $exe -ArgumentList $a -PassThru -Wait:$false -RedirectStandardOutput $o -RedirectStandardError $e -NoNewWindow
    if (-not $p.WaitForExit($timeout * 1000)) { Stop-Process -Id $p.Id -Force; Log "TIMEOUT after $timeout s: $($a -join ' ')" }
    Log "exit code: $($p.ExitCode)   args: $($a -join ' ')"
    foreach ($f in $o, $e) { if (Test-Path $f) { $t = (Get-Content $f -Raw); if ($t) { Log ($t.TrimEnd()) } } }
}
System-Info
Run "folder" { Get-ChildItem $AppDir | Select-Object Name, Length | Format-Table | Out-String; "size MB: " + [math]::Round(((Get-ChildItem $AppDir -Recurse | Measure-Object Length -Sum).Sum / 1MB), 1) }
Run "version" { Frozen @("--version") 60 }
Run "audio devices as the engine sees them (needs sounddevice / PortAudio inside the bundle)" { Frozen @("--engine", "--list-devices") 60 }
Run "engine benchmark (compiled kernels = numba works inside the bundle)" { Frozen @("--engine", "--bench") 300 }
Run "numba cache dir" { $d = Join-Path $env:LOCALAPPDATA "arctis_manager\numba-cache"; if (Test-Path $d) { Get-ChildItem $d -Recurse | Measure-Object | Select-Object Count | Out-String } else { "none" } }
Run "rnnoise" { Get-ChildItem $AppDir -Recurse -Filter "*rnnoise*" | Select-Object FullName | Out-String }
Run "helper" { Test-Path (Join-Path $AppDir "asm-audiotap.exe") }
Run "tray logs" { foreach ($f in Get-ChildItem (Join-Path $env:LOCALAPPDATA "arctis_manager\logs") -ErrorAction SilentlyContinue) { "--- $($f.Name)"; Get-Content $f.FullName -Tail 30 } }
Log "`nSMOKE DONE"
