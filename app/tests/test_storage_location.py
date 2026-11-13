"""Data path must remain constant after moving the program and restarting it."""
from pathlib import Path
from datetime import datetime
import json,os,shutil,subprocess,sys,tempfile,urllib.request,urllib.error,sqlite3
from contextlib import closing
ROOT=Path(__file__).resolve().parents[1]

def start(script, home, cwd):
    env=os.environ.copy();env['LOCALAPPDATA']=str(home)
    proc=subprocess.Popen([sys.executable,str(script),'--headless'],cwd=str(cwd),env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
    line=proc.stdout.readline().strip()
    if not line: raise AssertionError(f'Failed to start: {proc.communicate(timeout=3)}')
    return proc,json.loads(line)

def req(info,path,payload=None,token=True):
    headers={'X-MALIGN-Token':info['token'] if token else 'wrong'}
    raw=None
    if payload is not None:
        headers['Content-Type']='application/json'
        raw=json.dumps(payload).encode()
    request=urllib.request.Request(info['url']+path,data=raw,headers=headers,method='POST' if raw is not None else 'GET')
    with urllib.request.urlopen(request,timeout=12) as response:
        return json.load(response)

def stop(proc):
    proc.terminate();proc.wait(timeout=8)

with tempfile.TemporaryDirectory(prefix='malign_stable_path_') as tmp:
    base=Path(tmp); profile=base/'Windows_A';profile.mkdir()
    alternate=base/'release_in_another_location';alternate.mkdir()
    shutil.copy2(ROOT/'desktop_host.py',alternate/'desktop_host.py')
    shutil.copy2(ROOT/'MALIGN_Desktop.html',alternate/'MALIGN_Desktop.html')
    for name in ('malign_link.py', 'malign_engine.py', 'kit_device.py', 'matrix5_data.json', 'main_controller.py',
                 'reachstacker_sim.py', 'sim_site.py', 'sim_bridge.py', 'sim_job.py'):
        shutil.copy2(ROOT/name, alternate/name)
    expected=profile/'MALIGN'/'CaseBank'/'cases.sqlite3'
    p,info=start(ROOT/'desktop_host.py',profile,ROOT)
    try:
        assert Path(info['database'])==expected
        record={'uid':'persist_case_001','outcome':'success','saved_order':3,'saved_at':'2026-11-11T15:00:00+09:00','source_seed':333,'trial_index':1,'scenario':'base'}
        assert req(info,'api/add',{'records':[record]})['added']==1
        assert req(info,'api/stats')['total']==1
    finally:stop(p)
    # Program has moved / been replaced, but path and existing record must persist.
    p,info=start(alternate/'desktop_host.py',profile,alternate)
    try:
        assert Path(info['database'])==expected
        assert req(info,'api/stats')['success']==1
        assert len(list(expected.parent.joinpath('backups').glob('MALIGN_daily_*.sqlite3')))==1
        assert req(info,'api/add',{'records':[record]})['added']==0
        assert req(info,'api/clear',{'confirm':'CLEAR'})['ok']
        assert req(info,'api/stats')['total']==0
        backup=list(expected.parent.joinpath('backups').glob('MALIGN_preclear_*.sqlite3'))
        assert len(backup)==1
        with closing(sqlite3.connect(backup[0])) as connection:
            assert connection.execute('select count(*) from cases').fetchone()[0]==1
        # Bad shape cannot crash the service.
        try:
            req(info,'api/add',[])
        except urllib.error.HTTPError as err: assert err.code==400
        else: raise AssertionError('Bad JSON shape was accepted')
        assert req(info,'api/health')['ok']
    finally:stop(p)
    other=base/'Windows_B';other.mkdir()
    p,info=start(alternate/'desktop_host.py',other,alternate)
    try:
        assert Path(info['database'])!=expected
        assert req(info,'api/stats')['total']==0
    finally:stop(p)
print('PASS: same Windows profile always resolves the same absolute DB path after program move; daily backup; preclear backup; malformed request handling; different profile isolated',flush=True)
