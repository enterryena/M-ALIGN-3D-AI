"""Reject sqlite3.connect(...) used as a context manager without explicit close.

A sqlite3.Connection context manager commits/rolls back only; it does NOT close
its Windows file handle, which can block TemporaryDirectory.cleanup().
"""
from pathlib import Path
import ast
ROOT = Path(__file__).resolve().parents[1]
FILES = [*ROOT.glob('tests/test_*.py'), ROOT/'desktop_host.py', ROOT/'malign_link.py',
         ROOT/'server_example'/'malign_main_server.py']
for file in FILES:
    root = ast.parse(file.read_text(encoding='utf-8'), filename=str(file))
    for node in ast.walk(root):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            expr = item.context_expr
            if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == 'connect':
                if isinstance(expr.func.value, ast.Name) and expr.func.value.id == 'sqlite3':
                    raise AssertionError(f'{file}:{node.lineno}: bare with sqlite3.connect() leaks a handle on Windows')
print('PASS: no bare sqlite3.connect context managers in desktop host/tests')
