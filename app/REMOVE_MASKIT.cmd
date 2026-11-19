@echo off
setlocal EnableExtensions
chcp 65001 >nul
title MAS-kit - Complete Uninstall
cd /d "%~dp0"
echo ======================================================
echo MAS-kit removal (this Windows user account)
echo ======================================================
echo This will PERMANENTLY remove ALL saved cases,
echo backups, logs, and settings in %%LOCALAPPDATA%%\MASKIT.
echo It will also remove dist\MASKIT_Desktop.exe if present.
echo It will NOT delete your downloads or unrelated files.
echo.
set /p "CONFIRM=To uninstall, type DELETE and press Enter: "
if /i not "%CONFIRM%"=="DELETE" (
 echo Cancelled. No files removed.
 pause
 exit /b 1
)
set "MASKIT_EXE=%~dp0dist\MASKIT_Desktop.exe"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0UNINSTALL_MASKIT.ps1" -AppExePath "%MASKIT_EXE%" -NoDialog
set "RET=%errorlevel%"
if not "%RET%"=="0" (
 echo.
 echo Uninstall did not fully succeed. See error above.
 pause
 exit /b %RET%
)
echo.
echo Uninstall finished. You may now delete this extracted builder folder and downloaded ZIP.
pause
exit /b 0
