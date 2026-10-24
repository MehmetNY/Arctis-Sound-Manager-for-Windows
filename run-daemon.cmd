@echo off
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"
rem Normal calisma: INFO. Ayrintili kayit icin:  run-daemon.cmd debug
set "ARCTIS_LOG_LEVEL=info"
if /i "%~1"=="debug" set "ARCTIS_LOG_LEVEL=debug"
echo Arctis Sound Manager daemon [log: %ARCTIS_LOG_LEVEL%] - kapatmak icin Ctrl+C (kulakligi donanim moduna geri alir)
py -m arctis_sound_manager.scripts.daemon
echo.
echo Daemon durdu. Yukaridaki mesajlari okuyabilirsiniz.
pause
