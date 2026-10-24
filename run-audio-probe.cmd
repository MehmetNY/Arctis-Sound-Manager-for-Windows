@echo off
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"
rem Salt okunur ses probu: hicbir sey calmaz, kaydetmez, degistirmez.
rem Eksik paketler (sounddevice/numpy/scipy/pycaw) icin kullanici duzeyinde kurulum onerir.
py -c "import sounddevice, numpy, scipy, pycaw" >nul 2>nul
if errorlevel 1 (
  echo Prob daha ayrintili olabilmesi icin sounddevice, numpy, scipy ve pycaw paketlerini kullanabilir.
  echo Bunlar ses motoru icin de gerekli. Simdi kurulsun mu? [E]vet / [H]ayir  ^(10 sn sonra Evet^)
  choice /c EH /t 10 /d E /n >nul
  if not errorlevel 2 py -m pip install --user --disable-pip-version-check sounddevice numpy scipy pycaw
)
echo.
py -m arctis_sound_manager.scripts.win_audio_probe %*
echo.
echo Rapor 'asm-audio-probe.json' dosyasina da yazildi. Yukaridaki metni ve gerekirse dosyayi paylasin.
pause
