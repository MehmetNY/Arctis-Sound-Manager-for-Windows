# Which properties tell a program that PLAYS sound from one that only CAPTURES an endpoint (OBS, recorders, the helper itself)?
# Runs after build_native.ps1 and setup_audio.ps1 (build\audioprobe.exe, "CABLE Input" exist).
$script:LogFile = "probe.log"
. ./ci/common.ps1
$exe = Join-Path $PWD "build\audioprobe.exe"
$work = Join-Path $PWD "work"; New-Item -ItemType Directory -Force $work | Out-Null
function Spawn([string]$tag, [string[]]$a) {
    $p = Start-Process -FilePath $exe -ArgumentList $a -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work "$tag.out") -RedirectStandardError (Join-Path $work "$tag.err")
    Log "spawned $tag pid=$($p.Id): $($a -join ' ')"
    return $p
}
function Kill($p) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
function Show([string]$title, [string[]]$a) { Log ""; Log "----- $title"; (& $exe @a 2>&1 | Out-String -Width 250).TrimEnd() | ForEach-Object { Log $_ } }

Log "===== sessions with only a tone playing (A) / + endpoint loopback recorder (B) / + process loopback capture of A (C)"
$A = Spawn "tone" @("tone", "--device", "CABLE Input", "--amp", "0.3", "--secs", "60")
Start-Sleep -Milliseconds 1500
Show "P1: tone only" @("sessions", "--verbose")
$B = Spawn "rec" @("record", "--device", "CABLE Input", "--loopback", "--secs", "50")
Start-Sleep -Milliseconds 1500
Show "P2: tone + endpoint-loopback recorder (pid $($B.Id) is the recorder)" @("sessions", "--verbose")
$C = Spawn "cap" @("capture", "--pid", "$($A.Id)", "--secs", "45")
Start-Sleep -Milliseconds 1500
Show "P3: + process-loopback capture of the tone (pid $($C.Id) is the capture client)" @("sessions", "--verbose")
Log ""; Log "pids: tone=$($A.Id) endpoint-recorder=$($B.Id) process-capture=$($C.Id)"
# a recorder that had been started BEFORE any tone, and a recorder that stays up after the tone ends
Kill $A; Start-Sleep -Milliseconds 2500
Show "P4: tone gone, recorders still up" @("sessions", "--verbose")
Kill $B; Kill $C
Log "`nPROBE SESSIONS DONE"
