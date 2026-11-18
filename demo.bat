@echo off
rem Adalya Paneli - demo with a simulated site and NVR. Double-click to try every page.
rem Changes nothing in the real files; demo data lives in data-demo\ and is recreated at each start.
cd /d "%~dp0"
title Adalya Paneli (demo)
call :findpython || goto :nopython
%PY% adalya_panel.py --demo
pause
exit /b

:findpython
for %%P in (python py python3) do (
  %%P -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && (set "PY=%%P" & exit /b 0)
)
exit /b 1

:nopython
echo.
echo Python 3.9 or newer was not found.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" during setup,
echo then double-click this file again.
echo.
pause
exit /b 1
