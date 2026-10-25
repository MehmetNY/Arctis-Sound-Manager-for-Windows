# Static look: who signed the drivers? (no installation, nothing can hang)
$script:LogFile = "static.log"
. ./ci/common.ps1
System-Info
$work = Join-Path $PWD "work"; New-Item -ItemType Directory -Force $work | Out-Null
$h = @{ "User-Agent" = "asm-probe"; "Authorization" = "Bearer $env:GH_TOKEN" }
Run "VAD releases" {
    $rel = Invoke-RestMethod -Headers $h "https://api.github.com/repos/VirtualDrivers/Virtual-Audio-Driver/releases"
    foreach ($r in $rel) { "{0}  {1}  prerelease={2}" -f $r.tag_name, $r.name, $r.prerelease; foreach ($a in $r.assets) { "    {0}  {1}" -f $a.name, $a.browser_download_url } }
    ($rel[0].body)
}
$vad = Join-Path $work "vad"; New-Item -ItemType Directory -Force $vad | Out-Null
Run "VAD download + extract" {
    $rel = Invoke-RestMethod -Headers $h "https://api.github.com/repos/VirtualDrivers/Virtual-Audio-Driver/releases/latest"
    foreach ($a in $rel.assets) {
        $dest = Join-Path $vad $a.name
        Invoke-WebRequest -Headers $h -Uri $a.browser_download_url -OutFile $dest
        if ($dest -like "*.zip") { Expand-Archive -Force $dest (Join-Path $vad ([IO.Path]::GetFileNameWithoutExtension($a.name))) }
    }
    Get-ChildItem $vad -Recurse -File | ForEach-Object { "{0}  {1}" -f $_.Length, $_.FullName.Substring($vad.Length) }
}
Run "VAD signatures" { Get-ChildItem $vad -Recurse -File -Include *.sys, *.cat, *.dll, *.exe | ForEach-Object { SigInfo $_.FullName $vad } }
Run "VAD inf" { Get-ChildItem $vad -Recurse -File -Filter *.inf | ForEach-Object { "--- " + $_.FullName; Get-Content $_.FullName | Select-Object -First 90 } }

$vb = Join-Path $work "vbcable"; New-Item -ItemType Directory -Force $vb | Out-Null
Run "VBCABLE download + extract" {
    $ok = $false
    foreach ($n in "VBCABLE_Driver_Pack45.zip", "VBCABLE_Driver_Pack44.zip", "VBCABLE_Driver_Pack43.zip") {
        try { Invoke-WebRequest -UserAgent "Mozilla/5.0" -Uri "https://download.vb-audio.com/Download_CABLE/$n" -OutFile (Join-Path $vb "pack.zip"); "downloaded $n"; $ok = $true; break } catch { "fail $n : $($_.Exception.Message)" }
    }
    if ($ok) { Expand-Archive -Force (Join-Path $vb "pack.zip") (Join-Path $vb "x") }
    Get-ChildItem $vb -Recurse -File | ForEach-Object { "{0}  {1}" -f $_.Length, $_.FullName.Substring($vb.Length) }
}
Run "VBCABLE signatures" { Get-ChildItem $vb -Recurse -File -Include *.sys, *.cat, *.exe | ForEach-Object { SigInfo $_.FullName $vb } }
Log "`nSTATIC DONE"
