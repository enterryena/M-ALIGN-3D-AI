# -*- coding: utf-8 -*-
"""Case memory of the main server: every alignment is stored, and the next one is steered toward
what succeeded in similar situations.

Situation (features) = what the server actually knows after the kit's FIRST sensing:
  hardware preset, machine sensor status (valid / unstable / none), kit overall status, kit corners
  graded invalid, disagreement kit <-> machine sensor, distance of the first estimate (bucket),
  and - on a retry - why the previous attempt failed.
(The simulator's hidden truth - e.g. that a machine sensor is silently biased - is stored for the
report only and never used for the choice.)

Plan = how the server runs the correction loop (it never touches the lowering interlock / guard):
  STD       weighted priority, machine sensor used, gain K 0.8, up to 6 rounds  (the original default)
  KIT       kit first (machine sensor ignored), K 0.8, 6 rounds
  SOFT      machine sensor used, gentler gain K 0.6, up to 8 rounds
  KIT_SOFT  kit first, K 0.6, 8 rounds

Choice: similarity-weighted success rate of each plan over the stored cases (Beta prior), plus a small
exploration bonus for plans with little evidence in this situation. On a retry the failure reason first
narrows the candidates (disagreement -> kit-first plans, not converged -> more rounds, false lowering ->
gentle + re-approach) and the plan that just failed is not repeated.
"""
import json
import math
import pathlib
import sqlite3
import threading
import time
from contextlib import contextmanager

PLANS = {
    'STD':      {'K': 0.8, 'use_machine': True,  'max_rounds': 6, 'ko': '기본 (기존 센서 함께, K 0.8, 6회)'},
    'KIT':      {'K': 0.8, 'use_machine': False, 'max_rounds': 6, 'ko': '키트 우선 (기존 센서 제외, K 0.8, 6회)'},
    'SOFT':     {'K': 0.6, 'use_machine': True,  'max_rounds': 8, 'ko': '완만 보정 (K 0.6, 8회)'},
    'KIT_SOFT': {'K': 0.6, 'use_machine': False, 'max_rounds': 8, 'ko': '키트 우선 + 완만 (K 0.6, 8회)'},
}
DEFAULT = 'STD'
REASON_KO = {'disagree': '키트-기존 센서 불일치', 'not_converged': '수렴 부족(하강 조건 미달)',
             'false_lower': '결합 실패(오하강)', 'low_reliability': '키트 신뢰도 부족', 'far': '초기 오차 큼',
             'aborted': '중단', 'gate_hold': '사례 기반 하강 보류', None: '-'}
# which plans make sense after each failure reason
RETRY_PLANS = {'disagree': ('KIT', 'KIT_SOFT'), 'not_converged': ('SOFT', 'KIT_SOFT', 'KIT'),
               'false_lower': ('KIT_SOFT', 'SOFT'), 'low_reliability': tuple(PLANS), 'far': tuple(PLANS),
               'aborted': tuple(PLANS), 'gate_hold': ('KIT_SOFT', 'SOFT', 'KIT')}
REAPPROACH = ('false_lower', 'low_reliability', 'far', 'gate_hold')
FEATURE_W = {'machine': 3.0, 'disagree': 2.0, 'kit': 1.5, 'invalid': 1.5, 'hardware': 1.0, 'dist': 1.0, 'prev': 1.0}
PRIOR_N, PRIOR_P, PRIOR_STD = 1.0, 0.5, 0.65
EXPLORE = 0.12
RETRY_BONUS = 0.08
FALSE_LOWER_PENALTY = 4.0   # a false lowering is far worse than a safe stop: never trade safety for success rate


def dist_bucket(mm):
    if mm is None:
        return 'none'
    return 'near' if mm < 10 else 'mid' if mm < 20 else 'far'


def features_from(decision, hardware, attempt=0, prev_reason=None):
    """Situation after the first kit sensing (only what the server can know)."""
    d = decision or {}
    est = [e for e in (d.get('corner_est_mm') or []) if e is not None]
    worst = max((max(abs(e[0]), abs(e[1])) for e in est), default=None)
    grades = d.get('grade') or []
    dis = d.get('disagree_mm') is not None and d.get('disagree_limit_mm') is not None and \
        d['disagree_mm'] > d['disagree_limit_mm']
    if d.get('reason') == 'disagree':
        dis = True
    return {'hardware': hardware, 'machine': d.get('machine_status', 'none'), 'kit': d.get('kit_status', 'invalid'),
            'invalid': sum(1 for g in grades if g == 'invalid') + (1 if d.get('dropped_corner') else 0),
            'disagree': bool(dis), 'dist': dist_bucket(worst), 'first_est_worst_mm': None if worst is None else round(worst, 2),
            'attempt': attempt, 'prev': prev_reason}


def failure_reason(outcome, decisions, first_features=None):
    if outcome == 'success':
        return None
    if outcome == 'false_lower':
        return 'false_lower'
    if outcome == 'aborted':
        return 'aborted'
    if outcome == 'gate_hold':
        return 'gate_hold'
    reasons = [d.get('reason') for d in decisions or []]
    if any(r in ('disagree', 'sensors_disagree') for r in reasons) or any(d.get('case') in ('KIT_OVERRIDE', 'MACHINE_OVERRIDE') for d in decisions or []):
        return 'disagree'
    if any(r == 'no_reliable_data' for r in reasons):
        return 'low_reliability'
    if first_features and first_features.get('dist') == 'far':
        return 'far'
    return 'not_converged'


def distance(a, b):
    s = 0.0
    for k, w in FEATURE_W.items():
        if k == 'invalid':
            s += w * min(1.0, abs((a.get(k) or 0) - (b.get(k) or 0)))
        elif k == 'prev':
            if (a.get('prev') or b.get('prev')) and a.get('prev') != b.get('prev'):
                s += w
        elif a.get(k) != b.get(k):
            s += w
    return s


class CaseMemory:
    def __init__(self, path=None):
        self.path = pathlib.Path(path) if path else None
        self.lock = threading.Lock()
        self.cases = []
        self.lower = []        # (hardware, worst_est_mm, rigid_resid_mm, ok): what the kit saw just before each lowering
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._con() as con:
                con.execute('''CREATE TABLE IF NOT EXISTS yard_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, job_id TEXT, task TEXT,
                    features TEXT NOT NULL, plan TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT,
                    rounds INTEGER, moves INTEGER, sim_truth TEXT)''')
                for f, p, o, r in con.execute('SELECT features, plan, outcome, reason FROM yard_cases ORDER BY id'):
                    self.cases.append({'features': json.loads(f), 'plan': p, 'ok': o == 'success', 'outcome': o, 'reason': r})
                con.execute('''CREATE TABLE IF NOT EXISTS lower_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, hardware TEXT NOT NULL,
                    worst REAL NOT NULL, resid REAL NOT NULL, ok INTEGER NOT NULL)''')
                for h, w, r, ok in con.execute('SELECT hardware, worst, resid, ok FROM lower_cases ORDER BY id'):
                    self.lower.append((h, w, r, ok))

    @contextmanager
    def _con(self):
        con = sqlite3.connect(str(self.path), timeout=30)   # always close (Windows file locks)
        try:
            with con:
                yield con
        finally:
            con.close()

    # ---- store
    def add(self, features, plan, outcome, job_id=None, task=None, rounds=None, moves=None, sim_truth=None, reason=None):
        if outcome == 'success':
            reason = None
        elif reason is None:
            reason = failure_reason(outcome, [], features)
        rec = {'features': features, 'plan': plan, 'ok': outcome == 'success', 'outcome': outcome, 'reason': reason}
        with self.lock:
            self.cases.append(rec)
            if self.path:
                with self._con() as con:
                    con.execute('INSERT INTO yard_cases(at, job_id, task, features, plan, outcome, reason, rounds, moves, sim_truth) '
                                'VALUES (?,?,?,?,?,?,?,?,?,?)',
                                (time.time(), job_id, task, json.dumps(features, ensure_ascii=False), plan, outcome, reason,
                                 rounds, moves, json.dumps(sim_truth or {}, ensure_ascii=False)))
        return rec

    # ---- choose
    def score(self, features, candidates=None):
        cands = list(candidates or PLANS)
        with self.lock:
            cases = list(self.cases)
        sw = {p: 0.0 for p in cands}
        ss = {p: 0.0 for p in cands}
        sf = {p: 0.0 for p in cands}
        nsim = 0
        for c in cases:
            if c['plan'] not in sw:
                continue
            w = math.exp(-distance(features, c['features']))
            if w < 0.02:
                continue
            nsim += 1 if w > 0.3 else 0
            sw[c['plan']] += w
            ss[c['plan']] += w * (1.0 if c['ok'] else 0.0)
            sf[c['plan']] += w * (1.0 if c.get('outcome') == 'false_lower' else 0.0)
        total = sum(sw.values())
        out = {}
        for p in cands:
            prior = PRIOR_STD if p == DEFAULT else PRIOR_P
            rate = (ss[p] + PRIOR_N * prior) / (sw[p] + PRIOR_N)
            fl = (sf[p] + PRIOR_N * 0.01) / (sw[p] + PRIOR_N)
            bonus = EXPLORE * math.sqrt(math.log(1.0 + total + 1.0) / (1.0 + sw[p]))
            out[p] = {'rate': rate, 'false_lower': fl, 'evidence': sw[p], 'score': rate - FALSE_LOWER_PENALTY * fl + bonus}
        return out, nsim

    @staticmethod
    def safe_plans(features):
        """Safety rule before any learning: dropping the machine sensor's cross-check (kit-first plans) is allowed
        only when the kit's first sensing is internally good, when the machine sensor contradicts the kit, or when
        there is no valid machine sensor to cross-check with anyway."""
        kit_ok = features.get('kit') == 'good' or features.get('disagree') or features.get('machine') != 'valid'
        return [p for p in PLANS if PLANS[p]['use_machine'] or kit_ok]

    def choose(self, features, tried=(), reason=None):
        safe = self.safe_plans(features)
        cands = [p for p in safe if p not in tried] or safe
        prefer = set(RETRY_PLANS.get(reason) or ()) if reason else set()
        sc, nsim = self.score(features, cands)
        # the failure reason is a preference (bonus), not a restriction: learned false-lowering risk still wins
        key = {p: sc[p]['score'] + (RETRY_BONUS if p in prefer else 0.0) for p in cands}
        best = max(cands, key=lambda p: (key[p], p == DEFAULT))
        why = ('재시도(%s) → 우선 %s, ' % (REASON_KO.get(reason, reason), '/'.join(p for p in cands if p in prefer) or '-')
               if reason else '') + '유사 사례 %d건 · %s 예상 성공률 %.0f%% · 오하강 위험 %.1f%%' % (
            nsim, best, 100 * sc[best]['rate'], 100 * sc[best]['false_lower'])
        return {'plan': best, **{k: v for k, v in PLANS[best].items()}, 'similar': nsim,
                'expected': round(sc[best]['rate'], 3), 'false_lower_risk': round(sc[best]['false_lower'], 3), 'scores': {p: round(v['rate'], 3) for p, v in sc.items()},
                'why': why, 'reapproach': reason in REAPPROACH}

    # ---- report
    def summary(self):
        with self.lock:
            cases = list(self.cases)
        by = {}
        for c in cases:
            b = by.setdefault(c['plan'], [0, 0, 0])
            b[0] += 1
            b[1] += c['ok']
            b[2] += c.get('outcome') == 'false_lower'
        recent = cases[-50:]
        return {'cases': len(cases), 'success': sum(c['ok'] for c in cases),
                'recent_rate': None if not recent else round(sum(c['ok'] for c in recent) / len(recent), 3),
                'false_lower': sum(c.get('outcome') == 'false_lower' for c in cases),
                'by_plan': {p: {'n': v[0], 'ok': v[1], 'false_lower': v[2]} for p, v in by.items()},
                'gate': self.gate_summary()}


# --------------------------------------------------------------------------- lowering gate
# Just before lowering, the server looks at the kit's last corner estimates (what it really sees, never the truth):
# the worst estimated corner and the spread a rigid move cannot remove. Among the K most similar past LOWERINGS on
# the same hardware, if fewer than GATE_TH locked, it does not lower: hold -> re-approach (or a signalman later).
# Held jobs never get an outcome, so they are not learned from. Off until GATE_MIN lowerings are on record.
GATE_K, GATE_MIN, GATE_TH = 60, 60, 0.30


def lower_features(corner_est):
    """(worst estimated corner, rigid-fit residual) in mm from 4 [x, y] estimates, or None if a corner is missing."""
    if not corner_est or len(corner_est) != 4 or any(e is None for e in corner_est):
        return None
    import maskit_engine as E
    sx, sy, th = E.rigid_fit(corner_est)
    worst = max(max(abs(e[0]), abs(e[1])) for e in corner_est)
    resid = max(max(abs(e[0] - (sx - th * Y)), abs(e[1] - (sy + th * X))) for e, (X, Y) in zip(corner_est, E.CORNERS))
    return [round(worst, 4), round(resid, 4)]


def _lower_p(self, hardware, f, k=GATE_K):
    """Share of the k most similar past lowerings (same hardware) that locked; None while too few are on record."""
    with self.lock:
        pts = [(w, r, ok) for h, w, r, ok in self.lower if h == hardware]
    if f is None or len(pts) < max(k, GATE_MIN):
        return None
    pts.sort(key=lambda p: (p[0] - f[0]) ** 2 + (p[1] - f[1]) ** 2)
    return {'p': sum(p[2] for p in pts[:k]) / k, 'n': len(pts), 'th': GATE_TH}


def _add_lower(self, hardware, f, ok):
    if f is None:
        return
    with self.lock:
        self.lower.append((hardware, f[0], f[1], 1 if ok else 0))
    if self.path:
        with self._con() as con:
            con.execute('INSERT INTO lower_cases (at, hardware, worst, resid, ok) VALUES (?,?,?,?,?)',
                        (time.time(), hardware, f[0], f[1], 1 if ok else 0))


def _gate_summary(self):
    with self.lock:
        n = len(self.lower)
        ok = sum(p[3] for p in self.lower)
    return {'lowerings': n, 'locked': ok, 'active_from': GATE_MIN, 'threshold': GATE_TH}


CaseMemory.lower_p = _lower_p
CaseMemory.add_lower = _add_lower
CaseMemory.gate_summary = _gate_summary
