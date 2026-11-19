# -*- coding: utf-8 -*-
"""Main-server side of MAS-kit: combine the 4 units' reports into ONE correction.

Stage 1 (this file): the kit reports per-corner readings/estimates; the server
  - keeps each corner's reading history together with the moves IT commanded,
  - runs the history estimator (only the server knows the commanded moves),
  - fits one rigid move (x, y, rotation) to the 4 corners, applies gain K,
  - decides whether the job can end (sensor-only criterion).
Stage 3 will add: reach-stacker sensor comparison, per-corner priority/weights,
  outlier rejection and the lowering interlock.

run_job_split() runs kit-side and server-side logic exactly as split here and must
reproduce the v3.5.4 single-program results trial by trial.
"""
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import maskit_engine as E  # noqa: E402
import kit_device  # noqa: E402

SERVER_DEFAULTS = {'estimator': 'history', 'K': 0.8, 'stopMM': 1.0, 'max_rounds': 6, 'mirror': True,
                   'useHead': [1, 1, 1, 1, 1], 'searchMM': 36, 'fitGap': True}


class JobBrain:
    """State the main server keeps for one job."""

    def __init__(self, M, **settings):
        self.M = M
        self.P = dict(SERVER_DEFAULTS, **settings)
        self.hist = [[], [], [], []]
        self.rounds = 0
        self.decisions = []

    def decide(self, units, resense=False):
        """units: the bundle's 4 units (corner order) with 'sensor_uH' and 'est_mm' {x,y} (container frame).

        Returns the correction to command (already x K) and whether to end."""
        P, M = self.P, self.M
        if not resense:
            self.rounds += 1
        est, method = [], []
        for k, u in enumerate(units):
            f = E.frame(k, P['mirror'])
            kit = u.get('est_mm')
            kit_local = None if kit is None else {'x': f[0] * kit['x'], 'y': f[1] * kit['y']}
            if P['estimator'] == 'history' and kit_local is not None:
                entry = {'m': list(u['sensor_uH']), 'ox': 0.0, 'oy': 0.0}
                if resense and self.hist[k]:
                    self.hist[k][-1] = entry            # re-measurement replaces this round's reading
                else:
                    self.hist[k].append(entry)
                if len(self.hist[k]) >= 2:
                    es = E.history_estimate(M, self.hist[k], P, lambda: kit_local)
                    method.append('history' if 'historyUsed' in es else 'kit_grid')
                else:
                    es = kit_local
                    method.append('kit_grid')
            else:
                es = kit_local
                method.append('kit_grid' if es is not None else 'none')
            est.append(None if es is None else [f[0] * es['x'], f[1] * es['y']])
        if any(e is None for e in est):
            # Stage 1: no corner weighting yet -> cannot fit safely. Stage 3 handles missing corners.
            d = {'hold': True, 'reason': 'corner_without_estimate', 'corner_est_mm': est, 'method': method}
            self.decisions.append(d)
            return d
        sx, sy, dth = E.rigid_fit(est)
        K = P['K']
        if P['estimator'] == 'history':
            cmdX, cmdY, cmdTh = -K * sx, -K * sy, -K * dth
            for k in range(4):
                lf = E.frame(k, P['mirror'])
                CX, CY = E.CORNERS[k]
                deX = lf[0] * (cmdX - cmdTh * CY)
                deY = lf[1] * (cmdY + cmdTh * CX)
                for ob in self.hist[k]:
                    ob['ox'] -= deX
                    ob['oy'] -= deY
        converged = all(abs(e[0]) <= P['stopMM'] and abs(e[1]) <= P['stopMM'] for e in est)
        d = {'hold': False, 'correction': {'x_mm': K * sx, 'y_mm': K * sy, 'theta_rad': K * dth,
                                           'theta_deg': math.degrees(K * dth)},
             'corner_est_mm': est, 'method': method, 'converged': converged,
             'end': converged or self.rounds >= P['max_rounds'], 'round': self.rounds}
        self.decisions.append(d)
        return d


def run_job_split(M, P, rnd):
    """Same physics as maskit_engine.run_once, but kit-side and server-side logic separated.

    world (truth) -> each unit: reading -> kit_device (own corner only)
    -> server JobBrain (history + rigid fit + K) -> world moves the spreader."""
    w = E.World(P, rnd)
    e0 = w.errs()
    brain = JobBrain(M, estimator=P['estimator'], K=P['K'], stopMM=P['stopMM'], max_rounds=P['iters'],
                     mirror=P['mirror'], useHead=P['useHead'], searchMM=P['searchMM'], fitGap=P['fitGap'])
    it = 0
    while it <= P['iters']:
        E_ = w.errs()
        if it == P['iters']:
            break
        units = []
        for k in range(4):
            reading = w.read_corner(M, k, E_)
            r = kit_device.process_reading(M, k, reading, mirror=P['mirror'])
            units.append({'sensor_uH': reading, 'est_mm': {'x': r['est_mm'][0], 'y': r['est_mm'][1]},
                          'quality': r['quality']})
        d = brain.decide(units)
        c = d['correction']
        w.apply_move(c['x_mm'], c['y_mm'], c['theta_rad'])
        if d['converged']:
            break
        it += 1
    EF = w.errs()
    worst = max(max(abs(v[0]), abs(v[1])) for v in EF)
    worst0 = max(max(abs(v[0]), abs(v[1])) for v in e0)
    return {'ok': worst <= P['tolMM'], 'worst': worst, 'worst0': worst0, 'steps': it, 'decisions': brain.decisions}
