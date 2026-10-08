@echo off
rem Double-click to run Earshot and open the report.
cd /d "%~dp0"
set PY=py -3
%PY% --version >nul 2>nul
if errorlevel 1 set PY=python
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python 3 is not installed on this computer. Ask IT to install it, then try again.
  pause
  exit /b 1
)
%PY% earshot.py
if not errorlevel 1 start "" report.html
echo.
pause
