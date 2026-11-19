import json, os, subprocess, sys, tempfile, urllib.request, urllib.error, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
records=[
 {'uid':'test|scenario|seed1|1','outcome':'success','saved_at':'2026-11-11T19:00:00+09:00','saved_order':1,'source_seed':1,'trial_index':1,'scenario':'base','estimator':'history','final_worst_axis_mm':4.2,'controller_observable':{'sensor_uH':[1,2,3,4,5]},'evaluation_only':{'within_5mm':True}},
 {'uid':'test|scenario|seed1|2','outcome':'failure','saved_at':'2026-11-11T19:00:00+09:00','saved_order':2,'source_seed':1,'trial_index':2,'scenario':'base','estimator':'history','final_worst_axis_mm':10.0,'controller_observable':{'sensor_uH':[5,4,3,2,1]},'evaluation_only':{'within_5mm':False}},
]
def request(info,path,data=None,auth=True):
  header={'X-MASKIT-Token':info['token'] if auth else 'invalid'}
  body=None
  if data is not None:
    header['Content-Type']='application/json'
    body=json.dumps(data).encode()
  req=urllib.request.Request(info['url']+path.lstrip('/'),data=body,headers=header,method='POST' if data is not None else 'GET')
  with urllib.request.urlopen(req,timeout=10) as res: return json.load(res)

def start(path):
    proc=subprocess.Popen([sys.executable,str(ROOT/'desktop_host.py'),'--headless','--data-dir',str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
    line=proc.stdout.readline().strip()
    if not line:
        # The original test converted the REAL startup failure into a confusing
        # JSONDecodeError. Expose stderr for the actual failure instead.
        stderr=proc.stderr.read() if proc.poll() is not None else '(no startup JSON)'
        raise RuntimeError('MAS-kit test host startup failed: '+stderr)
    info=json.loads(line)
    return proc,info

with tempfile.TemporaryDirectory(prefix='maskit_durable_test_') as base:
  p,info=start(base)
  try:
    assert request(info,'api/health')['ok']
    assert request(info,'api/add',{'records':records})['added']==2
    assert request(info,'api/add',{'records':records})['added']==0
    assert request(info,'api/stats')['total']==2
    assert request(info,'api/stats')['success']==1
    try:request(info,'api/stats',auth=False)
    except urllib.error.HTTPError as err: assert err.code==403
    else: raise AssertionError('unauthorized accepted')
  finally:
    p.terminate();p.wait(timeout=6)
  p,info=start(base)
  try:
    assert request(info,'api/stats')['total']==2, 'restart lost saved cases'
    assert len(request(info,'api/all'))==2
    assert request(info,'api/add',{'records':[dict(records[0],uid='test|scenario|seed2|1',source_seed=2)]})['added']==1
    assert request(info,'api/stats')['total']==3
    print('PASS: durable SQLite after restart; duplicate suppression; authorization; total=3',flush=True)
  finally:
    p.terminate();p.wait(timeout=6)
