# Builds the Windows installer end to end (what the windows-installer workflow runs; works on a developer PC too).
#   ./installer/build.ps1                -> build\ASM-Setup-<version>.exe
# Needs: Python 3.13 with the packages in installer\requirements.txt, MSVC (for the native helper), Inno Setup 6.
param([string]$Version = "", [switch]$SkipVbCable)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root
if (-not $Version) { $Version = (Select-String -Path pyproject.toml -Pattern '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value }
Write-Host "== ASM $Version"

# 1. the native helper (per-application audio tap)
./ci/build_native.ps1

# 2. the icon
$ico = Join-Path $PSScriptRoot "asm.ico"
python -c "from PIL import Image; im = Image.open(r'src/arctis_sound_manager/gui/images/asm_logo.png').convert('RGBA'); im.save(r'$ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"

# 3. Python side
python -m pip install --no-deps . | Out-Host                       # registers the package metadata PyInstaller copies
python -m PyInstaller --noconfirm --distpath build\dist --workpath build\pyi installer\asm.spec | Out-Host
$app = "build\dist\ArctisSoundManager"
if (-not (Test-Path "$app\ArctisSoundManager.exe")) { throw "PyInstaller produced no ArctisSoundManager.exe" }
Copy-Item build\asm-audiotap.exe $app\ -Force
Copy-Item installer\THIRD-PARTY-NOTICES.txt $app\ -Force
Copy-Item LICENSE $app\LICENSE.txt -Force

# 4. the RNNoise library (AI noise cancelling): it lives in the pyrnnoise wheel
$rn = python -c "import importlib.util, pathlib; s = importlib.util.find_spec('pyrnnoise'); print(next((str(p) for d in (s.submodule_search_locations if s else []) for p in pathlib.Path(d).glob('*rnnoise*.dll')), ''))"
if ($rn) { Copy-Item $rn "$app\rnnoise.dll" -Force; Write-Host "rnnoise: $rn" } else { Write-Warning "no rnnoise.dll found: noise cancelling will be unavailable" }

# 5. VB-CABLE driver pack, unmodified (donationware; see installer\THIRD-PARTY-NOTICES.txt)
if (-not $SkipVbCable) {
    $vb = Join-Path $root "build\vbcable"; New-Item -ItemType Directory -Force $vb | Out-Null
    $zip = Join-Path $vb "pack.zip"
    foreach ($n in "VBCABLE_Driver_Pack45.zip", "VBCABLE_Driver_Pack44.zip") {
        try { Invoke-WebRequest -UserAgent "Mozilla/5.0" -Uri "https://download.vb-audio.com/Download_CABLE/$n" -OutFile $zip; Write-Host "downloaded $n"; break } catch { Write-Host "could not download $n" }
    }
    if (-not (Test-Path $zip)) { throw "VB-CABLE pack could not be downloaded" }
    Remove-Item "$vb\x" -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive -Force $zip "$vb\x"
    $dest = "$app\vbcable"; New-Item -ItemType Directory -Force $dest | Out-Null
    Copy-Item "$vb\x\*" $dest -Recurse -Force
}

# 6. the installer
$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) { $iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe" }
& $iscc /Qp "/DAppVersion=$Version" "/DSourceDir=$root\$app" "/DOutDir=$root\build" installer\asm.iss
Get-ChildItem build\ASM-Setup-*.exe | Format-Table Name, Length | Out-Host
