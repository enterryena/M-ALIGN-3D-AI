#Requires -Version 5.1
<#
M-ALIGN per-user data + executable removal. Not a generic file cleaner.
- Removes ONLY %LOCALAPPDATA%\MALIGN and known M-ALIGN test-created temp directories.
- Removes ONLY the explicitly given executable when its basename is MALIGN_Desktop.exe.
- Does NOT touch Windows event logs, browser history, user documents, downloads or registry.
- Other apps/OS may retain records that no application can safely guarantee to erase.
#>
param(
    [string]$AppExePath = '',
    [int]$WaitPid = 0,
    [switch]$NoDialog,
    [switch]$DataOnly
)
$ErrorActionPreference = 'Stop'

function Notify([string]$title, [string]$message, [bool]$problem) {
    if (-not $NoDialog) {
        try {
            Add-Type -AssemblyName System.Windows.Forms
            $icon = if ($problem) { [System.Windows.Forms.MessageBoxIcon]::Error } else { [System.Windows.Forms.MessageBoxIcon]::Information }
            [void][System.Windows.Forms.MessageBox]::Show($message, $title, [System.Windows.Forms.MessageBoxButtons]::OK, $icon)
        } catch { Write-Host "$title`n$message" }
    } else { Write-Host "$title`n$message" }
}

try {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw 'LOCALAPPDATA 환경 변수를 찾을 수 없습니다. 안전을 위해 삭제하지 않습니다.' }
    $profileRoot = [IO.Path]::GetFullPath($env:LOCALAPPDATA).TrimEnd('\')
    $appData = [IO.Path]::GetFullPath((Join-Path $profileRoot 'MALIGN')).TrimEnd('\')
    # Defensive check: the target MUST be an exact MALIGN child of LOCALAPPDATA.
    if ([IO.Path]::GetDirectoryName($appData) -ine $profileRoot -or [IO.Path]::GetFileName($appData) -cne 'MALIGN') {
        throw 'MALIGN 데이터 폴더 경로 검증 실패. 삭제하지 않았습니다.'
    }
    $exe = $null
    if (-not $DataOnly -and -not [string]::IsNullOrWhiteSpace($AppExePath)) {
        $exe = [IO.Path]::GetFullPath($AppExePath)
        if ([IO.Path]::GetFileName($exe) -ine 'MALIGN_Desktop.exe') { throw '실행 파일 이름이 MALIGN_Desktop.exe가 아닙니다. 다른 파일의 삭제는 거부합니다.' }
    }
    if ($WaitPid -gt 0) {
        for ($i = 0; $i -lt 150; $i++) {
            if (-not (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue)) { break }
            Start-Sleep -Milliseconds 200
        }
        if (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue) { throw 'M-ALIGN 프로그램 종료를 기다렸지만 아직 실행 중입니다. 창을 종료한 뒤 다시 실행하세요.' }
    }
    # A one-file PyInstaller EXE may have a short-lived launcher parent
    # in addition to the GUI process. Wait for both to exit, but NEVER
    # terminate any process or delete a live application's database.
    for ($i = 0; $i -lt 75; $i++) {
        if (-not (Get-Process -Name 'MALIGN_Desktop' -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 200
    }
    if (Get-Process -Name 'MALIGN_Desktop' -ErrorAction SilentlyContinue) {
        throw '다른 M-ALIGN 실행 파일이 아직 켜져 있습니다. 모두 종료하고 제거를 다시 실행하세요.'
    }
    if (Test-Path -LiteralPath $appData) {
        # Refuse deletion if something has linked another directory into the
        # application data tree. Never follow reparse points outside MALIGN.
        $links = @(Get-ChildItem -LiteralPath $appData -Recurse -Force -ErrorAction Stop | Where-Object {
            $_.Attributes -band [IO.FileAttributes]::ReparsePoint
        })
        if ($links.Count -gt 0) { throw '앱 데이터 폴더 안에 외부 폴더 연결(정션/링크)이 있습니다. 자동 제거를 중단합니다.' }
        $done = $false
        for ($attempt = 0; $attempt -lt 12; $attempt++) {
            try {
                Remove-Item -LiteralPath $appData -Recurse -Force -ErrorAction Stop
                $done = $true
                break
            } catch {
                if ($attempt -eq 11) { throw }
                Start-Sleep -Milliseconds (200 + $attempt * 100)
            }
        }
        if (-not $done -or (Test-Path -LiteralPath $appData)) { throw '사례 데이터 폴더 삭제를 확인할 수 없습니다.' }
    }
    # Clean precisely named, disposable temp folders created by our package tests.
    # Never delete generic %TEMP%\MALIGN* or PyInstaller _MEI* folders.
    $tempRoot = $env:TEMP
    if (-not [string]::IsNullOrWhiteSpace($tempRoot) -and (Test-Path -LiteralPath $tempRoot)) {
        $knownTestPrefixes = @('malign_durable_test_', 'malign_win32_regression_', 'malign_browser_bridge_', 'malign_stable_path_')
        foreach ($folder in (Get-ChildItem -LiteralPath $tempRoot -Directory -Force -ErrorAction SilentlyContinue)) {
            foreach ($prefix in $knownTestPrefixes) {
                if ($folder.Name.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase) -and -not ($folder.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                    try { Remove-Item -LiteralPath $folder.FullName -Recurse -Force -ErrorAction Stop }
                    catch { Write-Warning "기존 테스트 임시 폴더는 정리하지 못했습니다: $($folder.FullName)" }
                    break
                }
            }
        }
    }
    if ($null -ne $exe -and (Test-Path -LiteralPath $exe)) {
        $removed = $false
        for ($attempt = 0; $attempt -lt 15; $attempt++) {
            try { Remove-Item -LiteralPath $exe -Force -ErrorAction Stop; $removed = $true; break }
            catch { if ($attempt -eq 14) { throw }; Start-Sleep -Milliseconds (200 + $attempt * 100) }
        }
        if (-not $removed -or (Test-Path -LiteralPath $exe)) { throw '실행 파일을 제거하지 못했습니다.' }
        # Remove EXE directory ONLY if empty; never delete documents or unpacked source.
        $exeDir = [IO.Path]::GetDirectoryName($exe)
        if ($exeDir -and (Test-Path -LiteralPath $exeDir)) {
            $items = @(Get-ChildItem -LiteralPath $exeDir -Force -ErrorAction SilentlyContinue)
            if ($items.Count -eq 0) { try { Remove-Item -LiteralPath $exeDir -Force -ErrorAction Stop } catch {} }
        }
    }
    Notify 'M-ALIGN 제거 완료' "M-ALIGN의 현재 Windows 사용자 데이터(사례/백업/로그/설정)와 지정된 실행 파일을 제거했습니다.`n`n다운로드한 ZIP, 복사본 EXE, 브라우저 방문 기록, Windows 운영체제 자체 기록은 별도로 남을 수 있습니다." $false
    exit 0
} catch {
    Notify 'M-ALIGN 제거 실패' "안전을 위해 제거가 중단되었습니다.`n오류: $($_.Exception.Message)`n`n다른 M-ALIGN 창이 열려 있는지 확인하고 제거를 다시 실행하세요." $true
    exit 1
}
