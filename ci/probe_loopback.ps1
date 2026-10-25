# Experiments for the Windows audio architecture. Needs a Windows runner; installs VB-CABLE, builds ci/native/audioprobe.cpp.
#  E1  do the two playback ends of VB-CABLE ("CABLE Input", "CABLE In 16 Ch") feed the same cable?
#  E2  what does WASAPI *process* loopback see when the target app's session volume / mute / the endpoint volume / mute change?
#  E3  does process loopback work when the app renders to the other cable end, and do two captures run side by side?
$script:LogFile = "loopback.log"
. ./ci/common.ps1
System-Info
Run "start audio service" { Set-Service Audiosrv -StartupType Automatic; Start-Service Audiosrv; Get-Service Audiosrv | Format-Table Name, Status | Out-String }
$work = Join-Path $PWD "work"; New-Item -ItemType Directory -Force $work | Out-Null

# ---- VB-CABLE
$dir = Join-Path $work "vbcable"; New-Item -ItemType Directory -Force $dir | Out-Null
Run "download VB-CABLE" {
    foreach ($n in "VBCABLE_Driver_Pack45.zip", "VBCABLE_Driver_Pack44.zip") {
        try { Invoke-WebRequest -UserAgent "Mozilla/5.0" -Uri "https://download.vb-audio.com/Download_CABLE/$n" -OutFile (Join-Path $dir "pack.zip"); "downloaded $n"; break } catch { "fail $n" }
    }
    Expand-Archive -Force (Join-Path $dir "pack.zip") (Join-Path $dir "x")
}
$setup = (Get-ChildItem (Join-Path $dir "x") -Recurse -Filter "VBCABLE_Setup_x64.exe" | Select-Object -First 1)
@"
Start-Process -FilePath '$($setup.FullName)' -ArgumentList '-i','-h' -WorkingDirectory '$($setup.DirectoryName)' -Wait
"@ | Set-Content (Join-Path $work "vb1.ps1")
[void](Invoke-Timed (Join-Path $work "vb1.ps1") @() 120 "vbcable")
Start-Sleep 6

# ---- microphone privacy: capturing from "CABLE Output" is refused (0x80070005) while desktop apps may not use microphones
Run "allow microphone access" {
    foreach ($root in "HKLM:", "HKCU:") {
        foreach ($sub in "", "\NonPackaged") {
            $k = "$root\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone$sub"
            New-Item -Path $k -Force | Out-Null
            Set-ItemProperty -Path $k -Name Value -Value Allow
        }
    }
    Restart-Service Audiosrv -Force
    Start-Sleep 4
    "microphone consent set to Allow"
}

# ---- build the probe
$exe = Join-Path $work "audioprobe.exe"
Run "build audioprobe" {
    $built = $false
    $vsw = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vsw) {
        $vs = & $vsw -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        $vcvars = if ($vs) { Join-Path $vs "VC\Auxiliary\Build\vcvars64.bat" } else { $null }
        if ($vcvars -and (Test-Path $vcvars)) {
            "msvc: $vcvars"
            cmd /c "call `"$vcvars`" >nul && cl /nologo /EHsc /O2 /std:c++17 ci\native\audioprobe.cpp /Fe:`"$exe`" /Fo:`"$work\\`" ole32.lib mmdevapi.lib uuid.lib propsys.lib user32.lib" 2>&1
            $built = Test-Path $exe
        }
    }
    if (-not $built -and (Get-Command g++ -ErrorAction SilentlyContinue)) {
        "g++: " + (Get-Command g++).Source
        g++ -std=c++17 -O2 -o $exe ci/native/audioprobe.cpp -lole32 -lmmdevapi -luuid -lpropsys -static 2>&1
        $built = Test-Path $exe
    }
    "built: $built"
}
if (-not (Test-Path $exe)) { Log "NO PROBE BINARY"; exit 0 }

function AP([string[]]$a) { (& $exe @a 2>&1 | Out-String -Width 250).TrimEnd() | ForEach-Object { Log $_ } }
function Tone([string]$dev, [double]$amp, [int]$freq, [int]$secs, [string]$tag) {
    $p = Start-Process -FilePath $exe -ArgumentList @("tone", "--device", "`"$dev`"", "--amp", "$amp", "--freq", "$freq", "--secs", "$secs") -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work "tone-$tag.out") -RedirectStandardError (Join-Path $work "tone-$tag.err")
    Start-Sleep -Milliseconds 1800
    Log "tone[$tag] pid=$($p.Id) -> '$dev' amp=$amp freq=$freq; first line: $(Get-Content (Join-Path $work "tone-$tag.out") -ErrorAction SilentlyContinue | Select-Object -First 1)"
    return $p
}
function Kill($p) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
function Step([string]$title, [scriptblock]$sb) { Log ""; Log "----- $title"; try { & $sb } catch { Log "EXC: $_" } }

Run "devices" { & $exe list 2>&1 | Out-String -Width 250 }

# ---------------------------------------------------------------- E1
Step "E1 noise floor of CABLE Output (nothing playing)" { AP @("record", "--device", "CABLE Output", "--secs", "2") }
$t = Tone "CABLE Input" 0.5 440 14 "e1a"
Step "E1a tone -> 'CABLE Input'; record 'CABLE Output'" { AP @("record", "--device", "CABLE Output", "--secs", "3") }
Kill $t
$t = Tone "CABLE In 16" 0.5 440 14 "e1b"
Step "E1b tone -> 'CABLE In 16 Ch'; record 'CABLE Output'" { AP @("record", "--device", "CABLE Output", "--secs", "3") }
Kill $t
$t1 = Tone "CABLE Input" 0.5 440 14 "e1c1"; $t2 = Tone "CABLE In 16" 0.5 880 14 "e1c2"
Step "E1c both ends at once; record 'CABLE Output' (same pipe => RMS ~0.5, separate => ~0.35)" { AP @("record", "--device", "CABLE Output", "--secs", "3") }
Kill $t1; Kill $t2

# ---------------------------------------------------------------- E2
$t = Tone "CABLE Input" 0.5 440 120 "e2"
$tp = $t.Id
Step "sessions" { AP @("sessions") }
Step "E2.0 baseline: process loopback include(tone)" { AP @("capture", "--pid", "$tp", "--secs", "3") }
$idle = Start-Process -FilePath ping -ArgumentList @("-t", "127.0.0.1") -PassThru -WindowStyle Hidden
Step "E2.0b process loopback include(idle pid $($idle.Id)) => should be silence" { AP @("capture", "--pid", "$($idle.Id)", "--secs", "3") }
Step "E2.0c process loopback exclude(idle pid $($idle.Id)) => should contain the tone" { AP @("capture", "--pid", "$($idle.Id)", "--exclude", "--secs", "3") }
Kill $idle
foreach ($v in "0.1", "0.001", "0.00001") {
    Step "E2.1 session volume $v" {
        AP @("setvol", "--pid", "$tp", "--vol", $v)
        Start-Sleep -Milliseconds 500
        AP @("capture", "--pid", "$tp", "--secs", "3")
        AP @("record", "--device", "CABLE Output", "--secs", "2")
    }
}
AP @("setvol", "--pid", "$tp", "--vol", "1")
Step "E2.2 session mute" {
    AP @("mute", "--pid", "$tp", "--on"); Start-Sleep -Milliseconds 500
    AP @("capture", "--pid", "$tp", "--secs", "3")
    AP @("record", "--device", "CABLE Output", "--secs", "2")
    AP @("mute", "--pid", "$tp", "--off")
}
Step "E2.3 endpoint volume 0 on 'CABLE Input'" {
    AP @("epvol", "--device", "CABLE Input", "--vol", "0"); Start-Sleep -Milliseconds 500
    AP @("capture", "--pid", "$tp", "--secs", "3")
    AP @("record", "--device", "CABLE Output", "--secs", "2")
    AP @("epvol", "--device", "CABLE Input", "--vol", "1")
}
Step "E2.4 endpoint mute on 'CABLE Input'" {
    AP @("epmute", "--device", "CABLE Input", "--on"); Start-Sleep -Milliseconds 500
    AP @("capture", "--pid", "$tp", "--secs", "3")
    AP @("record", "--device", "CABLE Output", "--secs", "2")
    AP @("epmute", "--device", "CABLE Input", "--off")
}
Step "sessions (after)" { AP @("sessions") }
Kill $t

# ---------------------------------------------------------------- E3
$ta = Tone "CABLE Input" 0.5 440 40 "e3a"
$tb = Tone "CABLE In 16" 0.25 880 40 "e3b"
Step "E3.1 separate captures: A (0.5 -> rms .354) and B (0.25 -> rms .177)" {
    AP @("capture", "--pid", "$($ta.Id)", "--secs", "3")
    AP @("capture", "--pid", "$($tb.Id)", "--secs", "3")
}
Step "E3.2 both captures at the same time" {
    $ca = Start-Process -FilePath $exe -ArgumentList @("capture", "--pid", "$($ta.Id)", "--secs", "4") -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work "ca.out")
    $cb = Start-Process -FilePath $exe -ArgumentList @("capture", "--pid", "$($tb.Id)", "--secs", "4") -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work "cb.out")
    [void]$ca.WaitForExit(30000); [void]$cb.WaitForExit(30000)
    Log (Get-Content (Join-Path $work "ca.out") -Raw); Log (Get-Content (Join-Path $work "cb.out") -Raw)
}
Step "E3.3 does it matter which pid the process tree root is? capture include(parent powershell $PID) while A,B are children of it" {
    AP @("capture", "--pid", "$PID", "--secs", "3")
}
Kill $ta; Kill $tb


# ---------------------------------------------------------------- E5: the ramp when a session volume changes under a running capture
$t = Tone "CABLE Input" 0.5 440 40 "e5"
Step "E5 session volume 1 -> 1e-6 while capturing (trace = RMS of every 10 ms; a ramp or a click shows up here)" {
    AP @("capture", "--pid", "$($t.Id)", "--secs", "3", "--trace", "--svpid", "$($t.Id)", "--svvol", "0.000001", "--svat", "1500")
}
Step "E5b and back to 1 (the user's volume restored)" {
    AP @("capture", "--pid", "$($t.Id)", "--secs", "3", "--trace", "--svpid", "$($t.Id)", "--svvol", "1", "--svat", "1500")
}
foreach ($v in "0.000001", "0.0000001", "0.00000001") {
    Step "E6 session volume $v (expected rms $([double]$v * 0.35355))" {
        AP @("setvol", "--pid", "$($t.Id)", "--vol", $v); Start-Sleep -Milliseconds 400
        AP @("capture", "--pid", "$($t.Id)", "--secs", "2")
        AP @("record", "--device", "CABLE Output", "--secs", "2")
        AP @("record", "--device", "CABLE Input", "--loopback", "--secs", "2")
    }
}
AP @("setvol", "--pid", "$($t.Id)", "--vol", "1")
Step "E1a' (microphone access allowed now) tone -> CABLE Input; record CABLE Output; loopback of CABLE Input" {
    AP @("record", "--device", "CABLE Output", "--secs", "3")
    AP @("record", "--device", "CABLE Input", "--loopback", "--secs", "3")
}
Kill $t

# ---------------------------------------------------------------- E7: a new session, with and without a watcher that lowers its volume at once
Step "E7a baseline: loopback of CABLE Input, then a new tone starts (no watcher)" {
    $rec = Start-Process -FilePath $exe -ArgumentList @("record", "--device", "CABLE Input", "--loopback", "--secs", "5", "--trace") -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work "e7a.out")
    Start-Sleep -Milliseconds 1500
    $n = Start-Process -FilePath $exe -ArgumentList @("tone", "--device", "CABLE Input", "--amp", "0.5", "--secs", "3") -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work "e7a-tone.out")
    [void]$rec.WaitForExit(30000); Kill $n
    Log (Get-Content (Join-Path $work "e7a.out") -Raw)
}
Step "E7b a watcher sets every new session to 1e-6 the moment it appears" {
    $w = Start-Process -FilePath $exe -ArgumentList @("watch", "--secs", "9", "--sv", "0.000001") -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work "e7b-watch.out")
    Start-Sleep -Milliseconds 1500
    $rec = Start-Process -FilePath $exe -ArgumentList @("record", "--device", "CABLE Input", "--loopback", "--secs", "5", "--trace") -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work "e7b.out")
    Start-Sleep -Milliseconds 1500
    $n = Start-Process -FilePath $exe -ArgumentList @("tone", "--device", "CABLE Input", "--amp", "0.5", "--secs", "3") -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work "e7b-tone.out")
    [void]$rec.WaitForExit(30000); [void]$w.WaitForExit(30000); Kill $n
    Log (Get-Content (Join-Path $work "e7b-watch.out") -Raw)
    Log (Get-Content (Join-Path $work "e7b.out") -Raw)
}

# ---------------------------------------------------------------- E8: does Windows remember a session volume for the next run of the same program?
Step "E8 persistence: set 0.123 on a tone session, stop it, start the same program again and look at the volume of the new session" {
    $w = Start-Process -FilePath $exe -ArgumentList @("watch", "--secs", "12") -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work "e8-watch.out")
    Start-Sleep -Milliseconds 1200
    $a = Tone "CABLE Input" 0.3 440 30 "e8a"
    AP @("setvol", "--pid", "$($a.Id)", "--vol", "0.123")
    Start-Sleep -Milliseconds 800
    Kill $a
    Start-Sleep -Milliseconds 1500
    $b = Tone "CABLE Input" 0.3 440 30 "e8b"
    Start-Sleep -Milliseconds 800
    AP @("sessions")
    Kill $b
    [void]$w.WaitForExit(30000)
    Log (Get-Content (Join-Path $work "e8-watch.out") -Raw)
}

Run "summary (RESULT lines)" { Select-String -Path $script:LogFile -Pattern "^RESULT|^-----|^NEWSESSION|^WATCH" | ForEach-Object { $_.Line } }
Log "`nPROBE DONE"
