"""A job cut off (abort / yard stop) must not disturb the next one: its tool thread is still running for a
moment, but bundles go to the job they belong to and a stale thread may not touch a newer job.
Same seed alone == same seed right after an aborted job (8 times)."""
import sys, json, time, tempfile, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import maskit_link
tmp=pathlib.Path(tempfile.mkdtemp()); (tmp/'Link').mkdir(); (tmp/'Link'/'link_config.json').write_text(json.dumps({'sim_move_delay_s':0.05}))
rt=maskit_link.open_link(tmp/'Link'); site=rt.sim.site
def run(seed):
    site.start(seed=seed, controller='autopilot')
    while site.job['outcome'] is None: time.sleep(0.02)
    j=site.job; return j['outcome'], round(j['rs'].worst_mm(),4), [ (e['x_mm'],e['y_mm']) for e in j['events'] if e['kind']=='move']
def idle():
    for _ in range(200):
        if rt.link.status()['state']=='IDLE': return
        time.sleep(0.05)
ref=run(777); idle()
bad=0
for k in range(8):
    site.start(seed=1000+k, controller='autopilot'); time.sleep(0.15*(k%4+1)); site.abort('cut'); idle()
    r=run(777); idle()
    bad += r!=ref
rt.stop()
assert bad == 0, '%d of 8 jobs disturbed by an aborted job' % bad
print('PASS: aborted job does not disturb the next one (8/8 identical to a clean run)', flush=True)
