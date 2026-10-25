# The runners have no sound card. VB-CABLE gives them endpoints ("CABLE Input" plays the headset's part).
$script:LogFile = "setup.log"
. ./ci/common.ps1
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
