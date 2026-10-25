# Round 3: the pieces the first rounds could not answer.
#  E7  does a watcher get to lower a new session's volume before its first sound?
#  E8  is the per-program session volume remembered (for how long: this run of the audio service, or on disk)?
#  E9  can process loopback deliver more than two channels?
#  E13 what does a capture do when the program it follows exits? E15 does it pick up a program that starts rendering later?
$script:LogFile = "loopback.log"
. ./ci/common.ps1
System-Info
Run "start audio service" { Set-Service Audiosrv -StartupType Automatic; Start-Service Audiosrv; Get-Service Audiosrv | Format-Table Name, Status | Out-String }
$work = Join-Path $PWD "work"; New-Item -ItemType Directory -Force $work | Out-Null
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
Run "allow microphone access" {
    foreach ($root in "HKLM:", "HKCU:") { foreach ($sub in "", "\NonPackaged") {
        $k = "$root\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone$sub"
        New-Item -Path $k -Force | Out-Null; Set-ItemProperty -Path $k -Name Value -Value Allow } }
    Restart-Service Audiosrv -Force; Start-Sleep 4; "ok"
}
$exe = Join-Path $work "audioprobe.exe"
Run "build audioprobe" {
    $vsw = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    $vs = & $vsw -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    $vcvars = Join-Path $vs "VC\Auxiliary\Build\vcvars64.bat"
    cmd /c "call `"$vcvars`" >nul && cl /nologo /EHsc /O2 /std:c++17 ci\native\audioprobe.cpp /Fe:`"$exe`" /Fo:`"$work\\`" ole32.lib mmdevapi.lib uuid.lib propsys.lib user32.lib" 2>&1
    "built: $(Test-Path $exe)"
}
if (-not (Test-Path $exe)) { Log "NO PROBE BINARY"; exit 0 }
function AP([string[]]$a) { (& $exe @a 2>&1 | Out-String -Width 250).TrimEnd() | ForEach-Object { Log $_ } }
function Tone([string]$dev, [double]$amp, [int]$freq, [int]$secs, [string]$tag, [int]$channels = 0) {
    $args = @("tone", "--device", "`"$dev`"", "--amp", "$amp", "--freq", "$freq", "--secs", "$secs")
    if ($channels -gt 0) { $args += @("--channels", "$channels") }
    $p = Start-Process -FilePath $exe -ArgumentList $args -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work "tone-$tag.out") -RedirectStandardError (Join-Path $work "tone-$tag.err")
    Start-Sleep -Milliseconds 1500
    Log "tone[$tag] pid=$($p.Id): $(Get-Content (Join-Path $work "tone-$tag.out") -ErrorAction SilentlyContinue | Select-Object -First 1)"
    return $p
}
function Kill($p) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } }
function Step([string]$title, [scriptblock]$sb) { Log ""; Log "----- $title"; try { & $sb } catch { Log "EXC: $_" } }
function Dump([string]$file) { if (Test-Path $file) { Log (Get-Content $file -Raw) } }

# ---------------------------------------------------------------- E7
Step "E7b a watcher lowers every new session to 1e-6 at once; endpoint loopback shows what reaches the device from the first packet" {
    $w = Start-Process -FilePath $exe -ArgumentList @("watch", "--secs", "9", "--sv", "0.000001") -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work "e7b-watch.out")
    Start-Sleep -Milliseconds 1500
    $rec = Start-Process -FilePath $exe -ArgumentList @("record", "--device", "CABLE Input", "--loopback", "--secs", "5", "--trace") -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work "e7b.out")
    Start-Sleep -Milliseconds 1500
    $n = Start-Process -FilePath $exe -ArgumentList @("tone", "--device", "CABLE Input", "--amp", "0.5", "--secs", "3") -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work "e7b-tone.out")
    [void]$rec.WaitForExit(30000); [void]$w.WaitForExit(30000); Kill $n
    Dump (Join-Path $work "e7b-watch.out")
    Dump (Join-Path $work "e7b.out")
}

# ---------------------------------------------------------------- E8
function Probe-Persistence([string]$tag, [scriptblock]$between) {
    $w = Start-Process -FilePath $exe -ArgumentList @("watch", "--secs", "14") -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work "e8$tag-watch.out")
    Start-Sleep -Milliseconds 1200
    $a = Tone "CABLE Input" 0.3 440 30 "e8${tag}a"
    AP @("setvol", "--pid", "$($a.Id)", "--vol", "0.123")
    Start-Sleep -Milliseconds 800
    Kill $a
    Start-Sleep -Milliseconds 1500
    & $between
    $b = Tone "CABLE Input" 0.3 440 30 "e8${tag}b"
    AP @("sessions")
    Kill $b
    [void]$w.WaitForExit(30000)
    Dump (Join-Path $work "e8$tag-watch.out")
}
Step "E8a persistence: the same program starts again right away" { Probe-Persistence "a" { } }
Step "E8b persistence across a restart of the Windows audio service" { Probe-Persistence "b" { Restart-Service Audiosrv -Force; Start-Sleep -Seconds 5 } }
Step "E8c where does it live? registry diff around setting a volume (HKCU\Software\Microsoft, Multimedia, Audio)" {
    $before = Join-Path $work "reg-before.txt"; $after = Join-Path $work "reg-after.txt"
    reg export "HKCU\Software\Microsoft" $before /y | Out-Null
    $a = Tone "CABLE Input" 0.3 440 30 "e8c"
    AP @("setvol", "--pid", "$($a.Id)", "--vol", "0.321")
    Start-Sleep -Milliseconds 800
    Kill $a
    Start-Sleep -Milliseconds 1500
    reg export "HKCU\Software\Microsoft" $after /y | Out-Null
    $diff = Compare-Object (Get-Content $before) (Get-Content $after) | Where-Object { $_.InputObject -match "audio|volume|mixer|session|audioprobe|0\.321|PolicyConfig" } | Select-Object -First 40
    if ($diff) { $diff | ForEach-Object { Log ("{0} {1}" -f $_.SideIndicator, $_.InputObject) } } else { Log "no audio-related registry difference in HKCU\Software\Microsoft" }
    $all = Compare-Object (Get-Content $before) (Get-Content $after)
    Log "total differing lines in HKCU\Software\Microsoft: $(@($all).Count)"
}

# ---------------------------------------------------------------- E9 multichannel
$t = Tone "CABLE Input" 0.8 440 40 "e9" 8
Step "E9 an 8-channel program (channel k at k/8 of 0.8): process loopback asked for 8 channels, and for 6" {
    AP @("capture", "--pid", "$($t.Id)", "--secs", "3", "--channels", "8")
    AP @("capture", "--pid", "$($t.Id)", "--secs", "3", "--channels", "6")
    AP @("capture", "--pid", "$($t.Id)", "--secs", "3", "--channels", "2")
}
Kill $t

# ---------------------------------------------------------------- E13 / E15
Step "E13 the followed program exits after 2 s of a 6 s capture (trace: what is delivered afterwards?)" {
    $n = Start-Process -FilePath $exe -ArgumentList @("tone", "--device", "CABLE Input", "--amp", "0.5", "--secs", "4") -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work "e13-tone.out")
    Start-Sleep -Milliseconds 1500
    AP @("capture", "--pid", "$($n.Id)", "--secs", "6", "--trace")
    Kill $n
}
Step "E15 capture starts the moment the program is launched, before it renders anything (trace: when does sound appear?)" {
    $n = Start-Process -FilePath $exe -ArgumentList @("tone", "--device", "CABLE Input", "--amp", "0.5", "--secs", "5") -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $work "e15-tone.out")
    AP @("capture", "--pid", "$($n.Id)", "--secs", "3", "--trace")
    Kill $n
}

Run "summary" { Select-String -Path $script:LogFile -Pattern "^RESULT|^-----|^NEWSESSION|^WATCH|^SESSION|differ|^[<=>] " | ForEach-Object { $_.Line.Substring(0, [Math]::Min(300, $_.Line.Length)) } }
Log "`nPROBE DONE"
