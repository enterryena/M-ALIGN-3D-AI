"""Lowering gate from accumulated lowerings (AI version):
- off until 60 lowerings on the same hardware are on record; uses only the kit's own estimates, never the truth
- similar past lowerings mostly failed -> no lowering: outcome 'gate_hold' (re-approach reason), nothing learned from it
- same seed: with the gate passing, the job is identical to a run without a gate
- the same container keeps its corner tolerance across attempts."""
import sys, json, time, tempfile, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import case_memory as CM, maskit_link

# store / lookup
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
    m = CM.CaseMemory(pathlib.Path(d) / 'c.sqlite3')
    f_bad, f_good = [4.0, 3.5], [0.8, 0.6]
    assert m.lower_p('base', f_bad) is None                       # nothing on record: off
    for i in range(59):
        m.add_lower('base', [4.0 + .01 * i, 3.5], False)
    assert m.lower_p('base', f_bad) is None                       # 59 < 60: still off
    m.add_lower('base', [4.2, 3.4], False)
    for i in range(80):
        m.add_lower('base', [0.8 + .005 * i, 0.6], True)
    g = m.lower_p('base', f_bad); assert g and g['p'] < .3, g
    g = m.lower_p('base', f_good); assert g and g['p'] > .9, g
    assert m.lower_p('target', f_bad) is None                     # other hardware: its own record
    m2 = CM.CaseMemory(pathlib.Path(d) / 'c.sqlite3')              # persisted
    assert len(m2.lower) == 140 and m2.summary()['gate']['lowerings'] == 140
    assert CM.failure_reason('gate_hold', []) == 'gate_hold' and 'gate_hold' in CM.REAPPROACH
    assert CM.lower_features([[1, 2], None, [0, 0], [0, 0]]) is None

tmp = pathlib.Path(tempfile.mkdtemp()); (tmp / 'Link').mkdir()
(tmp / 'Link' / 'link_config.json').write_text(json.dumps({'sim_move_delay_s': 0.05}))
rt = maskit_link.open_link(tmp / 'Link'); site = rt.sim.site
def idle():
    for _ in range(200):
        if rt.link.status()['state'] == 'IDLE': return
        time.sleep(0.05)
def run(seed, **kw):
    idle(); site.start(seed=seed, controller='autopilot', hardware='target', **kw)
    while site.job['outcome'] is None: time.sleep(0.02)
    j = site.job
    return j['outcome'], round(j['rs'].worst_mm(), 4), [(e['x_mm'], e['y_mm']) for e in j['events'] if e['kind'] == 'move'], j
seen = []
ref = run(4242)
passing = run(4242, lower_gate=lambda f: (seen.append(f), {'p': .9, 'n': 100, 'th': .3})[1])
assert ref[:3] == passing[:3], 'a passing gate must not change the job'
assert seen and len(seen[-1]) == 2 and passing[3]['lower_features'] == seen[-1], (seen, passing[3]['lower_features'])
held = run(4242, lower_gate=lambda f: {'p': .05, 'n': 100, 'th': .3})
assert held[0] == 'gate_hold' and not any(e['kind'] == 'lower' for e in held[3]['events']), held[0]
assert held[2] == ref[2], 'up to the gate the job is the same'
# same container -> same corner tolerance on every approach
tz = [[.5, -1.], [1.2, .3], [-.7, .8], [0., -1.5]]
a = run(11, container_tol=tz)[3]['rs'].world.tol; b = run(99, container_tol=tz)[3]['rs'].world.tol
assert a == b == [[.7 * x, .7 * y] for x, y in tz], (a, b)
rt.stop()
print('PASS: lowering gate — off below 60 lowerings, per hardware, persisted; hold -> gate_hold (re-approach, not learned); '
      'passing gate leaves the job unchanged; same container keeps its tolerance', flush=True)
