"""Re-run every stored batch case of a MASKIT_casebank_backup_v1 export from its seed with the defined equations
(maskit_engine = v3.4 JS runOnce) and compare sensor readings, estimates, commands, true errors and outcome.
usage: python tools/verify_casebank.py <casebank.json> [out.json]
"""
import sys, json, math, numpy as np
from multiprocessing import Pool
import pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import maskit_engine as E, maskit_link
P = E.settings('base', 'history'); M = None
def init():
    global M; M = maskit_link._load_model()
def replay_one(x, rnd):
    """Re-run the job from its seed with the defined equations; compare every stored number."""
    w = E.World(P, rnd); hist = [[], [], [], []]; rounds = []
    e0 = w.errs(); it = 0
    while it < P['iters']:
        Eg = w.errs(); est = []; uh = []
        for k in range(4):
            meas = w.read_corner(M, k, Eg); uh.append(list(meas)); f = E.frame(k, P['mirror'])
            hist[k].append({'m': list(meas), 'ox': 0.0, 'oy': 0.0})
            es = E.history_estimate(M, hist[k], P, lambda: E.grid_estimate(M, meas, P)) if len(hist[k]) >= 2 else E.grid_estimate(M, meas, P)
            est.append([f[0] * es['x'], f[1] * es['y']])
        sx, sy, dth = E.rigid_fit(est)
        cmdX, cmdY, cmdTh = -P['K'] * sx, -P['K'] * sy, -P['K'] * dth
        for k in range(4):
            lf = E.frame(k, P['mirror']); CX, CY = E.CORNERS[k]
            for ob in hist[k]: ob['ox'] -= lf[0] * (cmdX - cmdTh * CY); ob['oy'] -= lf[1] * (cmdY + cmdTh * CX)
        w.apply_move(P['K'] * sx, P['K'] * sy, P['K'] * dth)
        rounds.append((uh, est, (P['K'] * sx, P['K'] * sy, P['K'] * dth * 180 / math.pi)))
        it += 1
        if all(abs(a) <= P['stopMM'] and abs(b) <= P['stopMM'] for a, b in est): break
    ef = w.errs(); R = x['controller_observable']['rounds']; ev = x['evaluation_only']
    d = {'n_rounds': len(R) != len(rounds),
         'init': float(np.abs(np.array(e0) - ev['initial_true_corner_error_mm']).max()),
         'final': float(np.abs(np.array(ef) - ev['final_true_corner_error_mm']).max()),
         'outcome': (max(max(abs(a), abs(b)) for a, b in ef) <= P['tolMM']) != (x['outcome'] == 'success'),
         'extrap': bool(w.extrap) != bool(x['extrapolated'])}
    du = de = dc = 0.0
    for (uh, est, cmd), r in zip(rounds, R):
        du = max(du, float(np.abs(np.array(uh) - r['sensor_uH']).max()))
        de = max(de, float(np.abs(np.array(est) - r['estimated_corner_errors_mm']).max()))
        c = r['commanded_x_y_rotation']; dc = max(dc, abs(cmd[0] - c['x_mm']), abs(cmd[1] - c['y_mm']), abs(cmd[2] - c['theta_deg']))
    d.update(uH=du, est=de, cmd=dc, frozen=x['frozen_conditions'] != {k: P[k] for k in x['frozen_conditions']}, iters=x['iterations'] != len(R))
    return d
def replay_batch(recs):
    recs = sorted(recs, key=lambda r: r['trial_index']); assert [r['trial_index'] for r in recs] == list(range(1, len(recs) + 1))
    rnd = E.Rng(recs[0]['source_seed']); return [replay_one(x, rnd) for x in recs]
if __name__ == '__main__':
    import collections
    recs = json.load(open(sys.argv[1], encoding='utf-8'))['records']
    g = collections.defaultdict(list)
    for r in recs:
        if r['origin'] == 'batch': g[r['source_seed']].append(r)
    with Pool(2, initializer=init) as p: out = p.map(replay_batch, list(g.values()), chunksize=1)
    flat = [y for b in out for y in b]
    if len(sys.argv) > 2: json.dump(flat, open(sys.argv[2], 'w'))
    for k in flat[0]:
        v = [x[k] for x in flat]
        print(k, ('mismatch %d / %d' % (sum(v), len(v))) if isinstance(v[0], bool) else 'max |diff| %.1e' % max(v))
