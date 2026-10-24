@echo off
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"
rem ASM ses motoru (sanal kablolar -> EQ / uzamsal ses / ChatMix -> kulaklik; mikrofon zinciri).
rem Normalde daemon motoru kendisi baslatir; bu dosya motoru elle, gorunur bir pencerede calistirmak
rem ve sorun gidermek icindir.
rem   run-audio-engine.cmd              motoru calistirir (kapatmak icin Ctrl+C)
rem   run-audio-engine.cmd devices      ses aygitlarini listeler
rem   run-audio-engine.cmd suggest      bu bilgisayar icin ilk ayar onerisini yazdirir
rem   run-audio-engine.cmd selftest     kurulumu sinar: aygitlar, her kanal icin kablo sinyali, CPU olcumu
rem   run-audio-engine.cmd bench        yalniz CPU olcumu
py -c "import sounddevice, numpy, scipy" >nul 2>nul
if errorlevel 1 (
  echo Ses motoru icin sounddevice, numpy ve scipy paketleri gerekli. Simdi kurulsun mu? [E]vet / [H]ayir  ^(10 sn sonra Evet^)
  choice /c EH /t 10 /d E /n >nul
  if not errorlevel 2 py -m pip install --user --disable-pip-version-check sounddevice numpy scipy
)
if /i "%~1"=="devices"  ( py -m arctis_sound_manager.win.audio.engine_main --list-devices & goto done )
if /i "%~1"=="suggest"  ( py -m arctis_sound_manager.win.audio.engine_main --suggest & goto done )
if /i "%~1"=="selftest" ( py -m arctis_sound_manager.win.audio.engine_main --selftest & goto done )
if /i "%~1"=="bench"    ( py -m arctis_sound_manager.win.audio.engine_main --bench & goto done )
echo ASM ses motoru calisiyor - kapatmak icin Ctrl+C. Gunluk: %USERPROFILE%\.config\arctis_manager\asm-audio-engine.log
py -m arctis_sound_manager.win.audio.engine_main %*
:done
echo.
echo Ses motoru durdu / islem bitti. Yukaridaki mesajlari okuyabilirsiniz.
pause
