"""Stage-3 main-server brain: rigid fit, outlier, 5 priority cases, disagreement, interlock,
and the v3.5.4 mode still reproducing the reference trial by trial."""
import json, math, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import maskit_engine as E  # noqa: E402
import main_controller as C  # noqa: E402
import sim_job  # noqa: E402

M = E.Model()
REF = json.loads((ROOT / 'tests' / 'fixtures' / 'js_reference_v354.json').read_text(encoding='utf-8'))

# --- rigid fit: equal weights == v3.5.4; subsets recover an exact rigid motion
tx, ty, th = 3.0, -2.0, math.radians(0.05)
exact = [[tx - th * Y, ty + th * X] for X, Y in E.CORNERS]
assert C.fit_rigid(exact, [1, 1, 1, 1]) == E.rigid_fit(exact)
for w in ([1, 0, 1, 0], [0, 1, 0, 1], [1, 1, 1, 0], [0.3, 1, 0.7, 1]):
    f = C.fit_rigid(exact, w)
    assert abs(f[0] - tx) < 1e-9 and abs(f[1] - ty) < 1e-9 and abs(f[2] - th) < 1e-12, (w, f)


def units(est, grades=('good',) * 4):
    return [{'sensor_uH': [0.0] * 5, 'est_mm': None if e is None else {'x': e[0], 'y': e[1]},
             'quality': {'grade': g}} for e, g in zip(est, grades)]


def brain(**kw):
    return C.ControllerBrain(M, kw.pop('strategy', 'weighted'), estimator='grid', **kw)


# --- outlier corner dropped (weighted only)
bad = [list(e) for e in exact]
bad[2] = [bad[2][0] + 20, bad[2][1]]
d = brain().decide(units(bad), {'status': 'none'})
assert d['dropped_corner'] == 3 and d['case'] == 'P3' and abs(d['fused_mm'][0] - tx) < 1e-9
assert brain(strategy='equal').decide(units(bad), {'status': 'none'})['dropped_corner'] is None

# --- priority cases
mach = {'status': 'valid', 'x_mm': tx, 'y_mm': ty, 'theta_rad': th}
assert brain().decide(units(exact), mach)['case'] == 'P1'
assert brain().decide(units(exact), dict(mach, status='unstable'))['case'] == 'P2'
assert brain().decide(units(exact), {'status': 'none'})['case'] == 'P3'
assert brain().decide(units(exact, ('good', 'caution', 'caution', 'caution')), mach)['case'] == 'P4'
d = brain().decide(units(exact), mach)
assert (d['w_machine'], d['w_kit']) == (0.7, 0.3)

# --- both unreliable: resense twice, then safe stop (no lowering)
b = brain()
nothing = units([None] * 4, ('invalid',) * 4)
acts = [b.decide(nothing, {'status': 'none'}, resense=i > 0)['action'] for i in range(3)]
assert acts == ['resense', 'resense', 'stop'], acts

# --- disagreement: first resense, then trust a consistent kit (machine flagged)
b = brain()
far = dict(mach, x_mm=tx + 15)
assert b.decide(units(exact), far)['action'] == 'resense'
d = b.decide(units(exact), far, resense=True)
assert d['case'] == 'KIT_OVERRIDE' and d['w_machine'] == 0.0

# --- interlock: never lower before convergence; after max rounds a verification decides
b = brain(max_rounds=2)
big = [[e[0] + 30, e[1]] for e in exact]   # 0.8 x 30 leaves 6+ mm after the move
assert b.decide(units(big), {'status': 'none'})['action'] == 'move'
assert b.decide(units(big), {'status': 'none'})['action'] == 'move_then_verify'
assert b.decide(units(big), {'status': 'none'})['action'] == 'stop'          # predicted 6+ mm after the move -> no lowering
small = [[0.4, -0.3]] * 4
assert brain().decide(units(small), {'status': 'none'})['action'] == 'lower_after_move'

# --- v3.5.4 mode through the full virtual site == reference
for key, est, n in (('base_base_2026', 'base', 200), ('base_history_2026', 'history', 40)):
    P, w, a = E.settings('base', est), E.Rng(2026), E.Rng(1)
    for i in range(n):
        r = sim_job.run_job(M, P, w, a, strategy='v354')
        assert int(r['ok']) == REF[key][i][0] and abs(r['worst'] - REF[key][i][1]) < 1e-9, (key, i)

# --- target hardware, normal machine sensor: no dangerous lowering
P, w, a = E.settings('target', 'history'), E.Rng(5), E.Rng(6)
out = [sim_job.run_job(M, P, w, a, 'weighted', 'normal')['outcome'] for _ in range(25)]
assert out.count('false_lower') == 0 and out.count('success') >= 22, out
print('PASS: main-server brain — rigid fit, outlier, priority P1-P5, disagreement, interlock, v3.5.4 mode identical', flush=True)
