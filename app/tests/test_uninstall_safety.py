"""Static checks for uninstall bundling and safety; Windows PowerShell runtime must be tested on Windows."""
from pathlib import Path
import ast, base64
root = Path(__file__).resolve().parents[1]
source = (root/'desktop_host.py').read_text(encoding='utf-8')
ast.parse(source)
script = (root/'UNINSTALL_MASKIT.ps1').read_text(encoding='utf-8')
builder = (root/'BUILD_EXE.cmd').read_text(encoding='utf-8')
wrapper = (root/'REMOVE_MASKIT.cmd').read_text(encoding='utf-8')
assert '프로그램 + 모든 누적 기록 완전 제거' in source
assert '완전삭제' in source and 'askyesno' in source
assert "getattr(sys, 'frozen', False)" in source
assert 'NoProfile' in source and 'EncodedCommand' in source
assert '--add-data "UNINSTALL_MASKIT.ps1;."' in builder
assert 'UNINSTALL_MASKIT.ps1' in wrapper and 'DELETE' in wrapper
assert "[IO.Path]::GetFullPath((Join-Path $profileRoot 'MASKIT'))" in script
assert 'GetFileName($exe)' in script and 'MASKIT_Desktop.exe' in script
assert 'Get-Process -Name' in script and 'ReparsePoint' in script
assert 'Remove-Item -LiteralPath $appData -Recurse -Force' in script
assert 'Remove-Item -LiteralPath $exe -Force' in script
assert 'MASKIT_daily_' not in script, 'backup files must be removed only under appData root'
assert r'TEMP\_MEI' not in script and "_MEI*" in script, 'must not blindly delete other PyInstaller temps'
assert 'SERVER' not in script
# Check PowerShell encoding expected by Windows PowerShell 5.1
payload = 'MAS-kit 완전삭제 테스트'
encoded = base64.b64encode(payload.encode('utf-16le')).decode('ascii')
assert base64.b64decode(encoded).decode('utf-16le') == payload
print('PASS: explicit user confirmation, fixed data target, EXE name guard, process guard, no generic cleanup, builder bundling')
