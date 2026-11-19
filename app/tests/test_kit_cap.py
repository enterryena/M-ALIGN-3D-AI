"""The kit refuses rounds beyond its safety cap (max_rounds 10). The server must stop on its own instead of
waiting for answers that never come (regression: seed 700096 waited minutes: sense -> refused move -> sense...)."""
import sys, json, time, tempfile, pathlib, random
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import maskit_link
tmp = pathlib.Path(tempfile.mkdtemp()); (tmp / 'Link').mkdir()
(tmp / 'Link' / 'link_config.json').write_text(json.dumps({'sim_move_delay_s': 0.0}))
rt = maskit_link.open_link(tmp / 'Link'); site = rt.sim.site; site.approach_s = 0.0
r = random.Random(7096); tol = [[r.gauss(0, 1), r.gauss(0, 1)] for _ in range(4)]
site.start(seed=700096, controller='autopilot', hardware='base', container_tol=tol)
t0 = time.time()
while site.job['outcome'] is None and time.time() - t0 < 25:
    time.sleep(0.05)
j = site.job; dt = time.time() - t0
rt.stop()
assert j['outcome'] == 'held' and dt < 10, (j['outcome'], dt)
assert any('kit round limit' in l['text'] for l in j['log']), [l['text'] for l in j['log'][-5:]]
print('PASS: kit round cap — the server stops itself (%.1f s) instead of waiting on refused rounds' % dt, flush=True)
