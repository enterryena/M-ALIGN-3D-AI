@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if not errorlevel 1 (
  py -3 "desktop_host.py"
) else (
  python "desktop_host.py"
)
if errorlevel 1 pause
