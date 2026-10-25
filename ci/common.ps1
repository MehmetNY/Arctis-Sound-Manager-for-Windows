# Shared helpers for the probe scripts.
$ErrorActionPreference = "Continue"
function Log([string]$m) { Add-Content -Path $script:LogFile -Value $m -Encoding utf8; Write-Host $m }
function Run([string]$title, [scriptblock]$sb) {
    Log ""; Log "===== $title"
    try { (& $sb 2>&1 | Out-String -Width 220).TrimEnd() | ForEach-Object { Log $_ } } catch { Log "EXC: $_" }
}
Add-Type -AssemblyName System.Windows.Forms, System.Drawing -ErrorAction SilentlyContinue
function Shot([string]$path) {
    try {
        $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
        $bmp = New-Object Drawing.Bitmap $b.Width, $b.Height
        $g = [Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
        $bmp.Save($path, [Drawing.Imaging.ImageFormat]::Png)
        Log "screenshot saved: $path"
    } catch { Log "screenshot failed: $_" }
}
function Windows() {
    Get-Process | Where-Object { $_.MainWindowTitle } | ForEach-Object { "{0,6} {1,-24} '{2}'" -f $_.Id, $_.ProcessName, $_.MainWindowTitle }
}
# Run a script file in a child pwsh; after $Seconds give up, log the open windows, take a screenshot, kill it.
function Invoke-Timed([string]$Script, [string[]]$ArgList, [int]$Seconds, [string]$Tag) {
    $p = Start-Process pwsh -ArgumentList (@("-NoProfile", "-File", $Script) + $ArgList) -PassThru -NoNewWindow
    $deadline = (Get-Date).AddSeconds($Seconds); $nextPeek = (Get-Date).AddSeconds(15)
    while (-not $p.HasExited -and (Get-Date) -lt $deadline) {
        Start-Sleep 2
        if ((Get-Date) -gt $nextPeek) { Log "[$Tag] still running; open windows:"; (Windows) | ForEach-Object { Log "    $_" }; $nextPeek = (Get-Date).AddSeconds(30) }
    }
    if (-not $p.HasExited) {
        Log "[$Tag] TIMEOUT after $Seconds s"; (Windows) | ForEach-Object { Log "    $_" }
        Shot (Join-Path $PWD "hang-$Tag.png")
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        return $false
    }
    Log "[$Tag] exit code $($p.ExitCode)"; return $true
}
function Chain($cert) {
    $c = New-Object Security.Cryptography.X509Certificates.X509Chain
    $c.ChainPolicy.RevocationMode = "NoCheck"
    [void]$c.Build($cert)
    ($c.ChainElements | ForEach-Object { "      -> " + $_.Certificate.Subject }) -join "`n"
}
function SigInfo($path, $root) {
    $s = Get-AuthenticodeSignature $path
    "{0}`n   status={1}`n   signer={2}`n   chain:`n{3}" -f $path.Substring($root.Length), $s.Status, $s.SignerCertificate.Subject, $(if ($s.SignerCertificate) { Chain $s.SignerCertificate } else { "" })
}
function Devices([string]$when) {
    Run "sound devices ($when)" {
        Get-CimInstance Win32_SoundDevice | Format-List Name, Status, PNPDeviceID | Out-String
        Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue | Format-Table FriendlyName, Status, InstanceId -AutoSize | Out-String -Width 250
        Get-PnpDevice -Class MEDIA -ErrorAction SilentlyContinue | Format-List FriendlyName, Status, Problem, InstanceId | Out-String
    }
}
function System-Info() {
    Run "os" { (Get-CimInstance Win32_OperatingSystem | Select-Object Caption, BuildNumber, Version | Format-List | Out-String) }
    Run "secure boot" { Confirm-SecureBootUEFI }
    Run "bcdedit current" { bcdedit /enum '{current}' }
    Run "audio services" { Get-Service Audiosrv, AudioEndpointBuilder | Format-Table Name, Status, StartType -AutoSize | Out-String }
}
