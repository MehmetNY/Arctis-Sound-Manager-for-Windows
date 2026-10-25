# Probe: can a Microsoft-signed virtual audio driver be installed silently on this Windows, without test signing?
param([string]$Out = "probe.log")
$ErrorActionPreference = "Continue"
function Log([string]$m) { Add-Content -Path $Out -Value $m -Encoding utf8; Write-Host $m }
function Run([string]$title, [scriptblock]$sb) {
    Log ""; Log "===== $title"
    try { (& $sb 2>&1 | Out-String -Width 220).TrimEnd() | ForEach-Object { Log $_ } } catch { Log "EXC: $_" }
}

Run "os" { (Get-CimInstance Win32_OperatingSystem | Select-Object Caption, BuildNumber, Version | Format-List | Out-String) }
Run "secure boot" { Confirm-SecureBootUEFI }
Run "bcdedit current" { bcdedit /enum '{current}' }
Run "code-integrity policy key" { Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' | Format-List }
Run "audio services" {
    Get-Service Audiosrv, AudioEndpointBuilder | Format-Table Name, Status, StartType -AutoSize | Out-String
}
Run "start audio service" {
    Set-Service Audiosrv -StartupType Automatic; Start-Service Audiosrv; Get-Service Audiosrv | Format-Table Name, Status | Out-String
}
function Devices([string]$when) {
    Run "sound devices ($when)" {
        Get-CimInstance Win32_SoundDevice | Format-List Name, Status, PNPDeviceID | Out-String
        Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue | Format-Table FriendlyName, Status, InstanceId -AutoSize | Out-String -Width 250
    }
    Run "portaudio ($when)" { python -m sounddevice 2>&1 | Out-String }
}
Run "pip sounddevice" { python -m pip install --quiet sounddevice numpy 2>&1 | Out-String }
Devices "before"

$work = Join-Path $PWD "probe_work"; New-Item -ItemType Directory -Force $work | Out-Null
$h = @{ "User-Agent" = "asm-probe"; "Authorization" = "Bearer $env:GH_TOKEN" }

# ---------------- 1) VirtualDrivers/Virtual-Audio-Driver (MIT) ----------------
Run "VAD: release list" {
    $rel = Invoke-RestMethod -Headers $h "https://api.github.com/repos/VirtualDrivers/Virtual-Audio-Driver/releases"
    foreach ($r in $rel) { "{0}  {1}" -f $r.tag_name, $r.name; foreach ($a in $r.assets) { "    {0}  {1}" -f $a.name, $a.browser_download_url } }
    $rel[0] | ConvertTo-Json -Depth 3 | Out-File (Join-Path $work "latest.json")
}
$vad = Join-Path $work "vad"; New-Item -ItemType Directory -Force $vad | Out-Null
Run "VAD: download latest assets" {
    $rel = Invoke-RestMethod -Headers $h "https://api.github.com/repos/VirtualDrivers/Virtual-Audio-Driver/releases/latest"
    foreach ($a in $rel.assets) {
        $dest = Join-Path $vad $a.name
        Invoke-WebRequest -Headers $h -Uri $a.browser_download_url -OutFile $dest
        if ($dest -like "*.zip") { Expand-Archive -Force $dest (Join-Path $vad ([IO.Path]::GetFileNameWithoutExtension($a.name))) }
    }
    Get-ChildItem $vad -Recurse -File | ForEach-Object { "{0}  {1}" -f $_.Length, $_.FullName.Substring($vad.Length) }
}
Run "VAD: signatures" {
    Get-ChildItem $vad -Recurse -File -Include *.sys, *.cat, *.dll, *.exe | ForEach-Object {
        $s = Get-AuthenticodeSignature $_.FullName
        "{0}`n   status={1}`n   signer={2}`n   issuer={3}" -f $_.FullName.Substring($vad.Length), $s.Status, $s.SignerCertificate.Subject, $s.SignerCertificate.Issuer
    }
}
Run "VAD: inf" {
    Get-ChildItem $vad -Recurse -File -Filter *.inf | ForEach-Object { "--- " + $_.FullName; Get-Content $_.FullName | Select-Object -First 80 }
}
$setupapi = @"
using System; using System.Runtime.InteropServices; using System.Text;
public static class Dev {
  [StructLayout(LayoutKind.Sequential)] public struct SP_DEVINFO_DATA { public int cbSize; public Guid ClassGuid; public int DevInst; public IntPtr Reserved; }
  [DllImport("setupapi.dll", SetLastError=true)] public static extern IntPtr SetupDiCreateDeviceInfoList(ref Guid g, IntPtr h);
  [DllImport("setupapi.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool SetupDiCreateDeviceInfoW(IntPtr s, string name, ref Guid g, string desc, IntPtr h, int flags, ref SP_DEVINFO_DATA d);
  [DllImport("setupapi.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool SetupDiSetDeviceRegistryPropertyW(IntPtr s, ref SP_DEVINFO_DATA d, int prop, byte[] buf, int size);
  [DllImport("setupapi.dll", SetLastError=true)] public static extern bool SetupDiCallClassInstaller(int fn, IntPtr s, ref SP_DEVINFO_DATA d);
  [DllImport("setupapi.dll", SetLastError=true)] public static extern bool SetupDiDestroyDeviceInfoList(IntPtr s);
  [DllImport("newdev.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool UpdateDriverForPlugAndPlayDevicesW(IntPtr h, string hwid, string inf, int flags, out bool reboot);
  public static string Create(string hwid, string inf, string classGuid, string className) {
    Guid g = new Guid(classGuid);
    IntPtr set = SetupDiCreateDeviceInfoList(ref g, IntPtr.Zero);
    if (set == (IntPtr)(-1)) return "CreateDeviceInfoList failed " + Marshal.GetLastWin32Error();
    SP_DEVINFO_DATA d = new SP_DEVINFO_DATA(); d.cbSize = Marshal.SizeOf(d);
    if (!SetupDiCreateDeviceInfoW(set, className, ref g, null, IntPtr.Zero, 1 /*DICD_GENERATE_ID*/, ref d)) return "CreateDeviceInfo failed " + Marshal.GetLastWin32Error();
    byte[] b = Encoding.Unicode.GetBytes(hwid + "\0\0");
    if (!SetupDiSetDeviceRegistryPropertyW(set, ref d, 1 /*SPDRP_HARDWAREID*/, b, b.Length)) return "SetHardwareId failed " + Marshal.GetLastWin32Error();
    if (!SetupDiCallClassInstaller(0x19 /*DIF_REGISTERDEVICE*/, set, ref d)) return "RegisterDevice failed " + Marshal.GetLastWin32Error();
    bool reboot;
    if (!UpdateDriverForPlugAndPlayDevicesW(IntPtr.Zero, hwid, inf, 1 /*INSTALLFLAG_FORCE*/, out reboot)) return "UpdateDriver failed " + Marshal.GetLastWin32Error() + " (0x" + Marshal.GetLastWin32Error().ToString("x") + ")";
    SetupDiDestroyDeviceInfoList(set);
    return "ok reboot=" + reboot;
  }
}
"@
Add-Type -TypeDefinition $setupapi
Run "VAD: install driver package + one device" {
    $inf = Get-ChildItem $vad -Recurse -File -Filter *.inf | Select-Object -First 1
    "inf = $($inf.FullName)"
    $hwid = (Select-String -Path $inf.FullName -Pattern 'ROOT\\[A-Za-z0-9_]+' -AllMatches | ForEach-Object { $_.Matches.Value } | Select-Object -First 1)
    "hardware id = $hwid"
    pnputil /add-driver $inf.FullName /install
    "--- creating devnode"
    [Dev]::Create($hwid, $inf.FullName, "{4d36e96c-e325-11ce-bfc1-08002be10318}", "MEDIA")
    Start-Sleep 8
}
Run "VAD: device state" { Get-PnpDevice -Class MEDIA -ErrorAction SilentlyContinue | Format-List FriendlyName, Status, Problem, InstanceId | Out-String }
Run "VAD: setupapi.dev.log tail" {
    $f = "C:\Windows\INF\setupapi.dev.log"; if (Test-Path $f) { Select-String -Path $f -Pattern "Virtual|VirtualAudio|signature|Signature|Code Integrity|0x0000" -Context 0,1 | Select-Object -Last 40 | ForEach-Object { $_.Line } }
}
Devices "after VAD"

# ---------------- 2) VB-CABLE (donationware, allowed to be bundled) ----------------
$vb = Join-Path $work "vbcable"; New-Item -ItemType Directory -Force $vb | Out-Null
Run "VBCABLE: download" {
    $ok = $false
    foreach ($n in "VBCABLE_Driver_Pack45.zip", "VBCABLE_Driver_Pack44.zip", "VBCABLE_Driver_Pack43.zip") {
        try { Invoke-WebRequest -UserAgent "Mozilla/5.0" -Uri "https://download.vb-audio.com/Download_CABLE/$n" -OutFile (Join-Path $vb "pack.zip"); "downloaded $n"; $ok = $true; break } catch { "fail $n : $($_.Exception.Message)" }
    }
    if ($ok) { Expand-Archive -Force (Join-Path $vb "pack.zip") (Join-Path $vb "x") }
    Get-ChildItem $vb -Recurse -File | ForEach-Object { "{0}  {1}" -f $_.Length, $_.FullName.Substring($vb.Length) }
}
Run "VBCABLE: signatures" {
    Get-ChildItem $vb -Recurse -File -Include *.sys, *.cat, *.exe | ForEach-Object {
        $s = Get-AuthenticodeSignature $_.FullName
        "{0}`n   status={1}`n   signer={2}`n   issuer={3}" -f $_.FullName.Substring($vb.Length), $s.Status, $s.SignerCertificate.Subject, $s.SignerCertificate.Issuer
    }
}
Run "VBCABLE: trust publisher + silent install" {
    $x = Join-Path $vb "x"
    $cat = Get-ChildItem $x -Recurse -Filter "vbaudio_cable64_win10.cat" | Select-Object -First 1
    if ($cat) {
        $cert = (Get-AuthenticodeSignature $cat.FullName).SignerCertificate
        $cer = Join-Path $vb "vbaudio.cer"; [IO.File]::WriteAllBytes($cer, $cert.Export("Cert"))
        Import-Certificate -FilePath $cer -CertStoreLocation Cert:\LocalMachine\TrustedPublisher | Out-Null
        "trusted publisher: $($cert.Subject)"
    } else { "no win10 cat found" }
    $setup = Get-ChildItem $x -Recurse -Filter "VBCABLE_Setup_x64.exe" | Select-Object -First 1
    "setup = $($setup.FullName)"
    $p = Start-Process -FilePath $setup.FullName -ArgumentList "-i", "-h" -WorkingDirectory $setup.DirectoryName -PassThru
    if (-not $p.WaitForExit(150000)) { "setup still running after 150 s (probably a dialog): killing"; Stop-Process -Id $p.Id -Force } else { "exit code $($p.ExitCode)" }
    Start-Sleep 8
}
Devices "after VBCABLE"
Run "VBCABLE: problem devices" { Get-PnpDevice -Class MEDIA -ErrorAction SilentlyContinue | Format-List FriendlyName, Status, Problem, InstanceId | Out-String }
Log "`nPROBE DONE"
