@echo off
rem Adalya Paneli - starts the panel for the whole site network (live, port 8080).
rem Put a shortcut to this file in the Startup folder (Win+R, shell:startup) so it starts with Windows.
cd /d "%~dp0"
title Adalya Paneli
call :findpython || goto :nopython
:loop
%PY% adalya_panel.py --lan --no-browser --port 8080
echo Panel stopped. Restarting in 10 seconds (close this window to stop it for good)...
timeout /t 10 /nobreak >nul
goto loop

:findpython
for %%P in (python py python3) do (
  %%P -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && (set "PY=%%P" & exit /b 0)
)
exit /b 1

:nopython
echo.
echo Python 3.9 or newer was not found.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" during setup,
echo then start this file again.
echo.
pause
exit /b 1
