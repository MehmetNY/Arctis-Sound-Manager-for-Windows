# Try to install one driver the way our installer would, with hard timeouts. -Which vad | vbcable
param([string]$Which)
$script:LogFile = "install-$Which.log"
. ./ci/common.ps1
System-Info
Run "start audio service" { Set-Service Audiosrv -StartupType Automatic; Start-Service Audiosrv; Get-Service Audiosrv | Format-Table Name, Status | Out-String }
Run "pip sounddevice" { python -m pip install --quiet sounddevice numpy 2>&1 | Out-String }
Devices "before"
$work = Join-Path $PWD "work"; New-Item -ItemType Directory -Force $work | Out-Null
$h = @{ "User-Agent" = "asm-probe"; "Authorization" = "Bearer $env:GH_TOKEN" }

if ($Which -eq "vad") {
    $dir = Join-Path $work "vad"; New-Item -ItemType Directory -Force $dir | Out-Null
    Run "download" {
        $rel = Invoke-RestMethod -Headers $h "https://api.github.com/repos/VirtualDrivers/Virtual-Audio-Driver/releases/latest"
        foreach ($a in $rel.assets) {
            $dest = Join-Path $dir $a.name
            Invoke-WebRequest -Headers $h -Uri $a.browser_download_url -OutFile $dest
            if ($dest -like "*.zip") { Expand-Archive -Force $dest (Join-Path $dir ([IO.Path]::GetFileNameWithoutExtension($a.name))) }
        }
        Get-ChildItem $dir -Recurse -File | ForEach-Object { "{0}  {1}" -f $_.Length, $_.FullName.Substring($dir.Length) }
    }
    $inf = (Get-ChildItem $dir -Recurse -File -Filter *.inf | Select-Object -First 1).FullName
    Log "inf = $inf"
    $hwid = (Select-String -Path $inf -Pattern 'ROOT\\[A-Za-z0-9_]+' -AllMatches | ForEach-Object { $_.Matches.Value } | Select-Object -First 1)
    Log "hardware id = $hwid"
    Run "pnputil add-driver (timed)" { "see below" }
    @"
pnputil /add-driver '$inf' /install
"@ | Set-Content (Join-Path $work "step1.ps1")
    [void](Invoke-Timed (Join-Path $work "step1.ps1") @() 120 "pnputil-add")
    Run "driver store" { pnputil /enum-drivers | Select-String -Pattern "virtual|Virtual" -Context 2, 6 | Out-String }
    @"
`$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition (Get-Content (Join-Path '$PWD' 'ci/setupapi.cs') -Raw)
[Dev]::Create('$hwid', '$inf', '{4d36e96c-e325-11ce-bfc1-08002be10318}', 'MEDIA')
"@ | Set-Content (Join-Path $work "step2.ps1")
    [void](Invoke-Timed (Join-Path $work "step2.ps1") @() 120 "devnode")
    Start-Sleep 8
    Run "setupapi.dev.log (tail)" {
        $f = "C:\Windows\INF\setupapi.dev.log"; if (Test-Path $f) { Select-String -Path $f -Pattern "irtual|ignature|Code Integrity|Trusted|0x0000" | Select-Object -Last 50 | ForEach-Object { $_.Line } }
    }
}
else {
    $dir = Join-Path $work "vbcable"; New-Item -ItemType Directory -Force $dir | Out-Null
    Run "download" {
        foreach ($n in "VBCABLE_Driver_Pack45.zip", "VBCABLE_Driver_Pack44.zip", "VBCABLE_Driver_Pack43.zip") {
            try { Invoke-WebRequest -UserAgent "Mozilla/5.0" -Uri "https://download.vb-audio.com/Download_CABLE/$n" -OutFile (Join-Path $dir "pack.zip"); "downloaded $n"; break } catch { "fail $n" }
        }
        Expand-Archive -Force (Join-Path $dir "pack.zip") (Join-Path $dir "x")
        Get-ChildItem $dir -Recurse -File | ForEach-Object { "{0}  {1}" -f $_.Length, $_.FullName.Substring($dir.Length) }
    }
    $x = Join-Path $dir "x"
    $setup = (Get-ChildItem $x -Recurse -Filter "VBCABLE_Setup_x64.exe" | Select-Object -First 1)
    Run "A) silent install WITHOUT pre-trusting the publisher" { "see timed step" }
    @"
Start-Process -FilePath '$($setup.FullName)' -ArgumentList '-i','-h' -WorkingDirectory '$($setup.DirectoryName)' -Wait
"@ | Set-Content (Join-Path $work "vb1.ps1")
    $ok = Invoke-Timed (Join-Path $work "vb1.ps1") @() 100 "vbcable-plain"
    if (-not $ok) {
        Run "B) trust the publisher first, then silent install" {
            $cat = Get-ChildItem $x -Recurse -Filter "vbaudio_cable64_win10.cat" | Select-Object -First 1
            $cert = (Get-AuthenticodeSignature $cat.FullName).SignerCertificate
            $cer = Join-Path $dir "vbaudio.cer"; [IO.File]::WriteAllBytes($cer, $cert.Export("Cert"))
            Import-Certificate -FilePath $cer -CertStoreLocation Cert:\LocalMachine\TrustedPublisher | Out-Null
            "trusted publisher: $($cert.Subject)"
        }
        [void](Invoke-Timed (Join-Path $work "vb1.ps1") @() 100 "vbcable-trusted")
    }
    Start-Sleep 8
}
Devices "after"
Run "portaudio devices" { python -m sounddevice 2>&1 | Out-String }
Log "`nINSTALL DONE"
