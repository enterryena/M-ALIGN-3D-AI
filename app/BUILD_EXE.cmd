@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title M-ALIGN Windows EXE Builder

echo ================================================================
echo  M-ALIGN desktop EXE builder - persistent case history

echo ================================================================
echo.
if not exist "desktop_host.py" (
  echo ERROR: desktop_host.py missing. Extract the whole ZIP first.
  pause
  exit /b 1
)
if not exist "malign_link.py" (
  echo ERROR: malign_link.py missing. Extract the whole ZIP first.
  pause
  exit /b 1
)
if not exist "MALIGN_Desktop.html" (
  echo ERROR: MALIGN_Desktop.html missing. Extract the whole ZIP first.
  pause
  exit /b 1
)
where py >nul 2>&1
if not errorlevel 1 (
  set "PYCMD=py -3"
) else (
  where python >nul 2>&1
  if errorlevel 1 (
    echo Python 3 not found. Install Python 3 for Windows, then run BUILD_EXE.cmd again.
    echo The Windows EXE is not precompiled in this ZIP.
    pause
    exit /b 1
  )
  set "PYCMD=python"
)
if not exist ".build_env\Scripts\python.exe" (
  echo [1/4] Creating isolated build environment...
  %PYCMD% -m venv ".build_env"
  if errorlevel 1 goto :failed
)
set "PYBIN=.build_env\Scripts\python.exe"
"%PYBIN%" -c "import sys; assert sys.version_info >= (3, 10), 'Python 3.10+ required'"
if errorlevel 1 goto :failed
echo [2/4] Checking PyInstaller...
"%PYBIN%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
  echo Installing PyInstaller in the isolated build environment.
  echo This first installation may require an internet connection.
  "%PYBIN%" -m pip install pyinstaller
  if errorlevel 1 goto :failed
)
echo [3/4] Testing persistent storage, backup handles and stable data location...
"%PYBIN%" tests\test_uninstall_safety.py
if errorlevel 1 goto :failed
"%PYBIN%" tests\test_no_sqlite_handle_leaks.py
if errorlevel 1 goto :failed
"%PYBIN%" tests\test_backup_windows_regression.py
if errorlevel 1 goto :failed
"%PYBIN%" tests\test_persistence.py
if errorlevel 1 goto :failed
"%PYBIN%" tests\test_storage_location.py
if errorlevel 1 goto :failed
"%PYBIN%" tests\test_kit_link.py
if errorlevel 1 goto :failed
echo [4/4] Building one-file GUI EXE...
"%PYBIN%" -m PyInstaller --noconfirm --clean --onefile --windowed --name "MALIGN_Desktop" --hidden-import malign_link --add-data "MALIGN_Desktop.html;." --add-data "UNINSTALL_MALIGN.ps1;." "desktop_host.py"
if errorlevel 1 goto :failed
if not exist "dist\MALIGN_Desktop.exe" goto :failed
echo.
echo SUCCESS: "%CD%\dist\MALIGN_Desktop.exe"
echo Data is saved separately to %%LOCALAPPDATA%%\MALIGN\CaseBank\cases.sqlite3
echo IMPORTANT: This EXE is not code-signed. Windows may display a publisher warning.
echo The EXE can be moved; the saved database remains in the user's profile.
explorer.exe "%CD%\dist"
pause
exit /b 0
:failed
echo.
echo BUILD FAILED. Read the error above. No existing case database was modified.
pause
exit /b 1
