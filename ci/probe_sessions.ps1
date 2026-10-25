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

# P5: does a session's own volume change what the session's meter shows / what a loopback capture client receives?
function MeterOf([int]$id) {
    $lines = (& $exe sessions --verbose 2>&1 | Out-String -Width 250) -split "`r?`n"
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i] -match "pid=$id ") { return $lines[$i + 7].Trim() } }
    return "(session $id not found)"
}
function Coarse([string]$file) {
    $t = (Get-Content $file | Where-Object { $_ -like "TRACE*" } | Select-Object -First 1)
    if (-not $t) { return "(no trace)" }
    $v = ($t -split ":", 2)[1].Trim() -split "\s+" | ForEach-Object { [double]$_ }
    $o = @(); for ($i = 0; $i + 25 -le $v.Count; $i += 25) { $o += ("{0:0.0000}" -f (($v[$i..($i + 24)] | Measure-Object -Average).Average)) }
    return ($o -join " ")
}
Log ""; Log "----- P5a the meter of a playing session (A) at volume 1, then at 1e-8, then back"
$A = Spawn "tone5" @("tone", "--device", "CABLE Input", "--amp", "0.3", "--secs", "60")
Start-Sleep -Milliseconds 1500
Log "meter at 1:    $(MeterOf $A.Id)"
& $exe setvol --pid $A.Id --vol 0.00000001 | Out-Null; Start-Sleep -Milliseconds 700
Log "meter at 1e-8: $(MeterOf $A.Id)"
& $exe setvol --pid $A.Id --vol 1 | Out-Null; Start-Sleep -Milliseconds 700
Log "meter at 1:    $(MeterOf $A.Id)"

Log ""; Log "----- P5b the recorder's OWN session volume: 1 for 1.5 s, 1e-8 for 1.5 s, then 1 again (200 ms means of the recorded level, tone plays all the time)"
$R = Spawn "rec5" @("record", "--device", "CABLE Input", "--loopback", "--secs", "4.8", "--trace")
Start-Sleep -Milliseconds 1500
Log "recorder meter before: $(MeterOf $R.Id)"
& $exe setvol --pid $R.Id --vol 0.00000001 | Out-Null
Log "t_setvol_low  $(Get-Date -Format HH:mm:ss.fff)"
Start-Sleep -Milliseconds 1200
Log "recorder meter at 1e-8: $(MeterOf $R.Id)"
& $exe setvol --pid $R.Id --vol 1 | Out-Null
Log "t_setvol_high $(Get-Date -Format HH:mm:ss.fff)"
[void]$R.WaitForExit(20000)
Log "recorded level per 200 ms: $(Coarse (Join-Path $work 'rec5.out'))"

Log ""; Log "----- P5c the same for a process-loopback client following the tone"
$C = Spawn "cap5" @("capture", "--pid", "$($A.Id)", "--secs", "4.8", "--trace")
Start-Sleep -Milliseconds 1500
& $exe setvol --pid $C.Id --vol 0.00000001 | Out-Null
Start-Sleep -Milliseconds 1200
& $exe setvol --pid $C.Id --vol 1 | Out-Null
[void]$C.WaitForExit(20000)
Log "captured level per 200 ms: $(Coarse (Join-Path $work 'cap5.out'))"
Get-Content (Join-Path $work 'cap5.out') | Where-Object { $_ -like "RESULT*" } | ForEach-Object { Log $_ }
Kill $A
Log "`nPROBE SESSIONS DONE"
