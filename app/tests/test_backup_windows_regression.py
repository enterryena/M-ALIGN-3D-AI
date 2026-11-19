"""Cross-platform regression for WinError 32 and leftover temp files."""
import os, sqlite3, tempfile, sys
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from desktop_host import CaseBank
with tempfile.TemporaryDirectory(prefix='maskit_win32_regression_') as td:
    folder=Path(td);bank=CaseBank(folder/'cases.sqlite3')
    r={'uid':'u1','outcome':'success','saved_order':1,'saved_at':'2026-11-11T00:00:00Z'}
    assert bank.add([r])==1
    # Left behind by v3.5.1. Must not reuse, rename, or delete it.
    old=folder/'backups'/'MASKIT_daily_20261111.building.sqlite3'
    old.parent.mkdir(exist_ok=True)
    old.write_bytes(b'v3.5.1 locked temporary backup - must be left alone')
    snap=bank.daily_backup()
    assert snap and snap.exists()
    assert old.exists() and old.read_bytes().startswith(b'v3.5.1')
    with closing(sqlite3.connect(snap)) as c:
        assert c.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        assert c.execute('SELECT COUNT(*) FROM cases').fetchone()[0]==1
    # The reader must be CLOSED before Windows temp-directory cleanup.
    assert bank.daily_backup()==snap
    assert len(list(old.parent.glob('MASKIT_daily_????????.sqlite3')))==1
    assert not list(old.parent.glob('MASKIT_daily_*.new_*.sqlite3'))
    # Simulate a brief Windows sharing violation during final rename.
    import unittest.mock
    from datetime import timedelta, datetime
    other=CaseBank(folder/'other_database'/'cases.sqlite3')
    assert other.add([r])==1
    real_rename=Path.rename
    calls=[0]
    def fail_once(self, target):
        calls[0]+=1
        if calls[0]==1:
            raise PermissionError(32, 'simulated WinError 32 during rename')
        return real_rename(self,target)
    with unittest.mock.patch.object(Path, 'rename', fail_once):
        assert other.daily_backup().exists()
    assert calls[0]>=2
print('PASS: closed backup reader; unique temp; no old-file interference; rename retry; repeatable daily backup')
