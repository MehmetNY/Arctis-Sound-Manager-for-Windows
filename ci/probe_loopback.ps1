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

Run "summary (RESULT lines)" { Select-String -Path $script:LogFile -Pattern "^RESULT|^-----" | ForEach-Object { $_.Line } }
Log "`nPROBE DONE"
