# Builds the two native tools with MSVC into build\ : asm-audiotap.exe (the product) and audioprobe.exe (a test tool).
$ErrorActionPreference = "Stop"
$vsw = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
$vs = & $vsw -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
$vcvars = Join-Path $vs "VC\Auxiliary\Build\vcvars64.bat"
New-Item -ItemType Directory -Force build | Out-Null
$libs = "ole32.lib mmdevapi.lib uuid.lib propsys.lib user32.lib winmm.lib advapi32.lib"
foreach ($t in @(@("native\audiotap\audiotap.cpp", "asm-audiotap.exe"), @("native\audioprobe\audioprobe.cpp", "audioprobe.exe"))) {
    cmd /c "call `"$vcvars`" >nul && cl /nologo /EHsc /O2 /std:c++17 /utf-8 /W3 /MT $($t[0]) /Fe:build\$($t[1]) /Fo:build\ $libs" 2>&1 | Write-Host
    if (-not (Test-Path "build\$($t[1])")) { throw "build of $($t[1]) failed" }
}
Get-ChildItem build\*.exe | Format-Table Name, Length | Out-String | Write-Host
