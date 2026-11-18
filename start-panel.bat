@echo off
rem Adalya Paneli - starts the panel for the whole site network.
rem Put a shortcut to this file in the Startup folder (Win+R, shell:startup) so it starts with Windows.
cd /d "%~dp0"
title Adalya Paneli
:loop
python adalya_panel.py --lan --no-browser
echo Panel stopped. Restarting in 10 seconds (close this window to stop it for good)...
timeout /t 10 /nobreak >nul
goto loop
