@echo off
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src"
rem Normal calisma: INFO. Ayrintili kayit icin:  run-gui.cmd debug
set "ARCTIS_LOG_LEVEL=info"
if /i "%~1"=="debug" set "ARCTIS_LOG_LEVEL=debug"
py -m arctis_sound_manager.scripts.gui
echo.
echo GUI kapandi. Yukaridaki mesajlari okuyabilirsiniz.
pause
