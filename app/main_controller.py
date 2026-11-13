# -*- coding: utf-8 -*-
"""Rule-based main-server brain (stage 3). Deterministic: same inputs -> same decision.

Per sensing (one kit bundle + one machine-sensor reading) it
  1. estimates each corner (server-side history estimator; unit grid estimate as fallback),
  2. combines the 4 corners into ONE rigid misalignment (x, y, theta) with one of
       'equal'    : all 4 corners equal (v3.5.4)
       'diagonal' : fixed reference = diagonal pair, corners 1 and 3 only
       'weighted' : reliability weights (good 1 / caution 0.7 / invalid 0) + drop one outlier corner
  3. sets the priority between the machine's own sensors and the kit (5 cases),
  4. decides: move / resense / verify / lower / stop.

Priority cases (the team's design):
  P1 normal               machine valid  + kit good      -> machine 0.7, kit 0.3 (kit cross-checks)
  P2 machine unstable     machine unstable + kit usable  -> kit 0.8, machine 0.2
  P3 machine no info      machine none   + kit usable    -> kit only
  P4 neither alone enough machine valid + kit caution, or unstable + caution -> 0.5 / 0.5
  P5 both unreliable      kit invalid and machine not valid -> hold: resense, then safe stop
  Disagreement: machine and kit differ by > DISAGREE_MM -> resense; if it persists:
  kit internally consistent -> trust the kit (machine suspect); else machine 'valid' ->
  trust the machine (existing sensor first); else safe stop.
Lowering interlock: lower only when every usable corner is within STOP_MM (converged)
  and the kit is not invalid. After max rounds without that, one verification sensing
  after the last move: lower only if the predicted worst corner <= LOWER_OK_MM;
  otherwise no lowering (job stopped safely).
"""
import math

import malign_engine as E

GRADE_W = {'good': 1.0, 'caution': 0.7, 'invalid': 0.0}   # tuned in stage 3 (tools/compare_strategies.py)
OUTLIER_MM = 12.0         # corner residual from the rigid fit treated as an outlier (container tolerance up to ~3 mm sd)
KIT_RESID_OK_MM = 4.0     # rigid-fit residual for a "consistent" kit
DISAGREE_MM = 6.0         # machine vs kit misalignment difference
LOWER_OK_MM = 4.0         # predicted worst corner after the move (twistlock tolerance is 5 mm)
STOP_MM = 1.0             # all corners within this -> converged
CORNER_R_MM = 3137.0      # twistlock distance from the spreader centre (20 ft)
MAX_HOLD = 2              # resenses per round before a safe stop
PRIORITY_W = {'P1': (0.7, 0.3), 'P2': (0.2, 0.8), 'P3': (0.0, 1.0), 'P4': (0.5, 0.5)}   # (machine, kit)
CASE_KO = {'P1': '정상: 기존 센서 우선, 키트 교차 검증', 'P2': '기존 센서 불안정: 키트 우선',
           'P3': '기존 센서 정보 없음: 키트만 사용', 'P4': '단독 판단 불충분: 상호 보완',
           'P5': '둘 다 신뢰도 부족: 보정 보류', 'KIT_OVERRIDE': '기존 센서 불일치: 일관된 키트 채택',
           'MACHINE_OVERRIDE': '키트 불확실·불일치: 기존 센서 채택',
           'V354': 'v3.5.4 방식: 키트 4코너 동일 가중'}


def fit_rigid(est, w):
    """Weighted LS rigid misalignment (tx, ty, th) with corner error model (tx - th*Y, ty + th*X).

    For 4 corners with equal weights this is exactly malign_engine.rigid_fit."""
    idx = [k for k in range(4) if est[k] is not None and w[k] > 0]
    if len(idx) == 4 and all(w[k] == w[idx[0]] for k in idx):
        return E.rigid_fit(est)
    if not idx:
        return None
    W = sum(w[k] for k in idx)
    sx = sum(w[k] * est[k][0] for k in idx) / W
    sy = sum(w[k] * est[k][1] for k in idx) / W
    if len(idx) == 1:
        return sx, sy, 0.0
    cx = sum(w[k] * E.CORNERS[k][0] for k in idx) / W
    cy = sum(w[k] * E.CORNERS[k][1] for k in idx) / W
    num = den = 0.0
    for k in idx:
        X, Y = E.CORNERS[k][0] - cx, E.CORNERS[k][1] - cy
        num += w[k] * (X * (est[k][1] - sy) - Y * (est[k][0] - sx))
        den += w[k] * (X * X + Y * Y)
    th = num / den
    return sx + th * cy, sy - th * cx, th


def corner_model(fit, k):
    tx, ty, th = fit
    X, Y = E.CORNERS[k]
    return tx - th * Y, ty + th * X


def residuals(est, fit, idx):
    out = {}
    for k in idx:
        mx, my = corner_model(fit, k)
        out[k] = math.hypot(est[k][0] - mx, est[k][1] - my)
    return out


class ControllerBrain:
    def __init__(self, M, strategy='weighted', use_machine=True, interlock=True, max_rounds=6, K=0.8,
                 estimator='history', P=None):
        if strategy not in ('equal', 'diagonal', 'weighted', 'v354'):
            raise ValueError('strategy')
        self.M = M
        self.strategy = strategy
        self.use_machine = use_machine and strategy != 'v354'
        self.interlock = interlock and strategy != 'v354'
        self.max_rounds, self.K, self.estimator = max_rounds, K, estimator
        self.P = dict(E.BASE, useHead=[1] * 5, **(P or {}))
        self.hist = [[], [], [], []]
        self.rounds = 0
        self.holds = 0
        self.disagree_seen = False
        self.verifying = False
        self.decisions = []

    # ---- 1. per-corner estimates
    def _corner_estimates(self, units, resense):
        est, method, grade = [], [], []
        for k, u in enumerate(units):
            f = E.frame(k, True)
            q = u.get('quality') or {}
            grade.append(q.get('grade', 'good'))
            kit = u.get('est_mm')
            if kit is None:
                est.append(None)
                method.append('none')
                continue
            kit_local = {'x': f[0] * kit['x'], 'y': f[1] * kit['y']}
            es, m = kit_local, 'kit_grid'
            if self.estimator == 'history':
                entry = {'m': list(u['sensor_uH']), 'ox': 0.0, 'oy': 0.0}
                if resense and self.hist[k]:
                    self.hist[k][-1] = entry
                else:
                    self.hist[k].append(entry)
                if len(self.hist[k]) >= 2:
                    es = E.history_estimate(self.M, self.hist[k], self.P, lambda: kit_local)
                    m = 'history' if 'historyUsed' in es else 'kit_grid'
            est.append([f[0] * es['x'], f[1] * es['y']])
            method.append(m)
        return est, method, grade

    # ---- 2. combine corners
    def _combine(self, est, grade):
        if self.strategy in ('equal', 'v354'):
            w = [1.0 if e is not None else 0.0 for e in est]
        elif self.strategy == 'diagonal':
            w = [1.0 if (k in (0, 2) and est[k] is not None) else 0.0 for k in range(4)]
        else:
            gw = getattr(self, 'grade_w', GRADE_W)
            w = [gw.get(grade[k], 0.0) if est[k] is not None else 0.0 for k in range(4)]
        fit = fit_rigid(est, w)
        dropped = None
        idx = [k for k in range(4) if w[k] > 0]
        if fit is not None and self.strategy == 'weighted' and len(idx) >= 3:
            res = residuals(est, fit, idx)
            k_bad = max(res, key=res.get)
            if res[k_bad] > getattr(self, 'outlier_mm', OUTLIER_MM):
                dropped = k_bad
                w[k_bad] = 0.0
                fit = fit_rigid(est, w)
                idx = [k for k in range(4) if w[k] > 0]
        resid = max(residuals(est, fit, idx).values()) if fit is not None and len(idx) >= 3 else None
        good = sum(1 for k in idx if grade[k] == 'good')
        if fit is None:
            status = 'invalid'
        elif len(idx) >= 3 and good >= 3 and (resid is None or resid <= KIT_RESID_OK_MM):
            status = 'good'
        elif len(idx) >= 2:
            status = 'caution'
        else:
            status = 'invalid'
        if fit is not None and len(idx) == 1:
            status = 'caution'   # translation only
        return {'fit': fit, 'weights': w, 'dropped': dropped, 'used': idx, 'resid_mm': resid, 'status': status,
                'rotation_ok': len(idx) >= 3 or set(idx) in ({0, 2}, {1, 3})}

    # ---- 3+4. decide
    def decide(self, units, machine, resense=False):
        if not resense and not self.verifying:
            self.rounds += 1
        est, method, grade = self._corner_estimates(units, resense)
        kit = self._combine(est, grade)
        mstat = (machine or {}).get('status', 'none') if self.use_machine else 'none'
        mfit = (machine['x_mm'], machine['y_mm'], machine['theta_rad']) if mstat in ('valid', 'unstable') else None
        d = {'round': self.rounds, 'resense': resense, 'verify': self.verifying, 'corner_est_mm': est,
             'method': method, 'grade': grade, 'kit_status': kit['status'], 'weights': kit['weights'],
             'dropped_corner': None if kit['dropped'] is None else kit['dropped'] + 1, 'kit_resid_mm': kit['resid_mm'],
             'machine_status': mstat, 'machine_fit': mfit}

        # priority case
        if self.strategy == 'v354':
            case = 'V354'
        elif kit['status'] == 'invalid' and mstat != 'valid':
            case = 'P5'
        elif kit['status'] == 'invalid':
            case = 'P1'            # machine valid, kit useless -> machine alone (still P1 priority)
        elif mstat == 'none':
            case = 'P3'
        elif mstat == 'unstable':
            case = 'P2' if kit['status'] == 'good' else 'P4'
        else:
            case = 'P1' if kit['status'] == 'good' else 'P4'
        if case not in ('P5', 'V354') and mfit is not None and kit['fit'] is not None:
            diff = max(abs(mfit[0] - kit['fit'][0]), abs(mfit[1] - kit['fit'][1]))
            limit = DISAGREE_MM + 0.3 * max(abs(kit['fit'][0]), abs(kit['fit'][1]))   # far away: both less precise
            d['disagree_mm'], d['disagree_limit_mm'] = diff, limit
            if diff > limit:
                if not self.disagree_seen:
                    self.disagree_seen = True
                    return self._hold(d, 'disagree', est)
                if kit['status'] == 'good':
                    case = 'KIT_OVERRIDE'          # kit internally consistent -> machine sensor suspect
                elif mstat == 'valid':
                    case = 'MACHINE_OVERRIDE'      # kit uncertain, machine says valid -> existing sensor first
                else:
                    return self._stop(d, 'sensors_disagree')
        d['case'] = case
        d['case_ko'] = CASE_KO[case]
        if case == 'P5' or (kit['fit'] is None and mfit is None):
            return self._hold(d, 'no_reliable_data', est)

        if case == 'MACHINE_OVERRIDE':
            wm, wk = 1.0, 0.0
        elif case in ('V354', 'P3', 'KIT_OVERRIDE') or mfit is None:
            wm, wk = 0.0, 1.0
        elif kit['fit'] is None:
            wm, wk = 1.0, 0.0
        else:
            wm, wk = PRIORITY_W[case]
        kf = kit['fit'] or (0.0, 0.0, 0.0)
        mf = mfit or (0.0, 0.0, 0.0)
        fused = tuple(wm * a + wk * b for a, b in zip(mf, kf)) if case != 'V354' else kf
        if case in ('P3', 'KIT_OVERRIDE') or (wm == 0.0):
            fused = kf
        elif wk == 0.0:
            fused = mf
        if not kit['rotation_ok'] and wm == 0.0:
            fused = (fused[0], fused[1], 0.0)
        d.update(w_machine=wm, w_kit=wk, fused_mm=fused)
        self.holds = 0

        # predicted corners after the move; v3.5.4 convergence on measured estimates
        K = self.K
        move = (K * fused[0], K * fused[1], K * fused[2])
        usable = [k for k in range(4) if est[k] is not None and (kit['weights'][k] > 0 or self.strategy in ('v354', 'equal'))]
        pred = []
        for k in range(4):
            if est[k] is None:
                pred.append(None)
                continue
            X, Y = E.CORNERS[k]
            pred.append([est[k][0] - (move[0] - move[2] * Y), est[k][1] - (move[1] + move[2] * X)])
        pred_worst = max(max(abs(pred[k][0]), abs(pred[k][1])) for k in usable) if usable else math.inf
        converged = bool(usable) and all(abs(est[k][0]) <= STOP_MM and abs(est[k][1]) <= STOP_MM for k in usable)
        # With the machine's sensor in the mix, the per-corner container tolerance never reaches 0:
        # treat a near-zero fused misalignment plus a safe predicted worst corner as converged.
        fused_small = (max(abs(fused[0]), abs(fused[1])) <= STOP_MM and abs(fused[2]) * CORNER_R_MM <= STOP_MM)
        if not converged and wm > 0 and fused_small and pred_worst <= LOWER_OK_MM:
            converged = True
        d.update(correction={'x_mm': move[0], 'y_mm': move[1], 'theta_rad': move[2], 'theta_deg': math.degrees(move[2])},
                 predicted_worst_mm=pred_worst, converged=converged)

        if self.verifying:                      # verification sensing: no further correction rounds
            self.verifying = False
            ok = converged or pred_worst <= LOWER_OK_MM
            d['action'] = 'lower_after_move' if ok else 'stop'
            d['reason'] = 'verified' if ok else 'verify_failed'
            return self._record(d)
        if not self.interlock:                  # v3.5.4: always lower after the loop
            d['action'] = 'lower_after_move' if (converged or self.rounds >= self.max_rounds) else 'move'
            return self._record(d)
        if converged and kit['status'] != 'invalid':
            d['action'] = 'lower_after_move'
        elif self.rounds >= self.max_rounds:
            d['action'] = 'move_then_verify'
            self.verifying = True
        else:
            d['action'] = 'move'
        return self._record(d)

    def apply_history_offsets(self, correction):
        """The server knows what it commanded: update every corner's history offsets."""
        if self.estimator != 'history':
            return
        cmdX, cmdY, cmdTh = -correction['x_mm'], -correction['y_mm'], -correction['theta_rad']
        for k in range(4):
            lf = E.frame(k, True)
            CX, CY = E.CORNERS[k]
            deX = lf[0] * (cmdX - cmdTh * CY)
            deY = lf[1] * (cmdY + cmdTh * CX)
            for ob in self.hist[k]:
                ob['ox'] -= deX
                ob['oy'] -= deY

    def _hold(self, d, reason, est):
        self.holds += 1
        d.setdefault('case', 'P5')
        d.setdefault('case_ko', CASE_KO.get(d['case'], ''))
        if self.holds > MAX_HOLD:
            return self._stop(d, reason)
        d['action'] = 'resense'
        d['reason'] = reason
        return self._record(d)

    def _stop(self, d, reason):
        d.setdefault('case', 'P5')
        d.setdefault('case_ko', CASE_KO.get(d['case'], ''))
        d['action'] = 'stop'
        d['reason'] = reason
        return self._record(d)

    def _record(self, d):
        self.decisions.append(d)
        return d
