@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if not errorlevel 1 (
  set "PYCMD=py -3"
) else (
  set "PYCMD=python"
)
%PYCMD% -c "import numpy" >nul 2>&1
if errorlevel 1 (
  echo numpy is required for the kit calculation. Installing once ^(internet needed^)...
  %PYCMD% -m pip install --user numpy
)
%PYCMD% "desktop_host.py"
if errorlevel 1 pause
