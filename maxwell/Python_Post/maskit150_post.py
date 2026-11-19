#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MAS-kit v1.5.0 post-processing (normal CPython 3.8+, no numpy needed).

Usage
  python maskit150_post.py collect       <output_dir>   # tables + all checks available so far
  python maskit150_post.py predict-blind <output_dir>   # needs P1_center + all four P2 cases
  python maskit150_post.py check-blind   <output_dir>   # after P3_blind_m2_p4 is solved

All numbers are FEM model results of one model family (5-winding Matrix, AFAST tier).
They are not hardware accuracy. Checks print PASS / FAIL / N/A, never fabricate data.
"""
from __future__ import print_function, division
import csv, json, math, os, re, sys, hashlib, datetime

PY2 = sys.version_info[0] == 2


def rcsv(path):
    f = open(path, 'rb') if PY2 else open(path, 'r', encoding='utf-8-sig', newline='')
    try:
        rows = [r for r in csv.reader(f) if r]
    finally:
        f.close()
    if rows and rows[0] and rows[0][0].startswith('\xef\xbb\xbf'):
        rows[0][0] = rows[0][0][3:]
    if rows and rows[0] and rows[0][0].startswith(u'\ufeff'):
        rows[0][0] = rows[0][0][1:]
    return rows


def wopen(path):
    return open(path, 'wb') if PY2 else open(path, 'w', newline='')


def now():
    return datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')

HEADS = ['Xp', 'Xm', 'Yp', 'Ym', 'Ref']
WIND = {'Winding_1': 'Xp', 'Winding_2': 'Xm', 'Winding_3': 'Yp', 'Winding_4': 'Ym', 'Winding_5': 'Ref'}
UNIT = {'h': 1e6, 'mh': 1e3, 'uh': 1.0, 'nh': 1e-3, 'ohm': 1e3, 'mohm': 1.0, 'kohm': 1e6}
AFAST_CENTER = 1108.28077776481
AFAST_DIFF = 9.36781460527
TOL_AFAST = 0.5
STAB = 0.3          # +- uH same-pass stability window
K_GAIN = 0.55
BLIND = (-2.0, 4.0)


def cval(s):
    t = str(s).strip().replace(' ', '').replace('i', 'j').replace('I', 'j')
    return complex(t) if 'j' in t else float(t)


def parse_header(h):
    m = re.search(r'\.(\w)\((Winding_\d),(Winding_\d)\)\s*\[([^\]]*)\]', h)
    if not m:
        return None
    unit = m.group(4).strip().lower()
    return m.group(1), WIND[m.group(2)], WIND[m.group(3)], unit


def read_matrix(path):
    rows = rcsv(path)
    head, val = rows[0], rows[1]
    out = {}
    for h, v in zip(head[1:], val[1:]):
        p = parse_header(h)
        if not p:
            continue
        q, a, b, unit = p
        scale = UNIT.get(unit, None)
        if scale is None:
            raise SystemExit('Unknown unit ' + unit + ' in ' + path)
        if not str(v).strip():
            raise SystemExit('EMPTY matrix value in ' + path + ' (matrix not computed; re-run with v1.5.6)')
        x = cval(v)
        out[(a, b)] = x * scale if not isinstance(x, complex) else complex(x.real * scale, x.imag * scale)
    return out


def read_per_pass(path):
    if not os.path.isfile(path):
        return None
    rows = rcsv(path)
    head = rows[0]
    out = {}
    for r in rows[1:]:
        m = re.search(r'\d+', r[0])
        if not m:
            continue
        p = int(m.group(0))
        d = {}
        for h, v in zip(head[1:], r[1:]):
            ph = parse_header(h)
            if ph and v.strip():
                d[(ph[1], ph[2])] = float(v) * UNIT[ph[3]]
                d[(ph[2], ph[1])] = d[(ph[1], ph[2])]
        out[p] = d
    return out


def read_optional(path):
    # R/Z are informative only; never stop the analysis because of them.
    try:
        return read_matrix(path) if os.path.isfile(path) else None
    except SystemExit:
        return None
    except Exception:
        return None


def latest_cases(root):
    croot = os.path.join(root, 'cases')
    best = {}
    if not os.path.isdir(croot):
        return best
    for d in sorted(os.listdir(croot)):
        rp = os.path.join(croot, d, 'result.json')
        if not os.path.isfile(rp):
            continue
        r = json.load(open(rp))
        if r.get('status') != 'SOLVED_PROVISIONAL' or not r.get('solve_requested'):
            continue
        lp = os.path.join(croot, d, 'L_matrix.csv')
        if not os.path.isfile(lp):
            continue
        cid = r['case']['id']
        try:
            Lm = read_matrix(lp)
        except SystemExit:
            print('SKIP (empty/invalid matrix, e.g. v1.5.5 run): ' + d)
            continue
        if len(Lm) != 25:
            print('SKIP (incomplete matrix): ' + d)
            continue
        best[cid] = {'dir': os.path.join(croot, d), 'result': r,
                     'L': Lm,
                     'R': read_optional(os.path.join(croot, d, 'R_matrix.csv')),
                     'pp': read_per_pass(os.path.join(croot, d, 'L_per_pass.csv'))}
    return best


def diag(case):
    return [case['L'][(h, h)] for h in HEADS]


def pinv52(H):
    a = sum(r[0] * r[0] for r in H); b = sum(r[0] * r[1] for r in H); c = sum(r[1] * r[1] for r in H)
    det = a * c - b * b
    if abs(det) < 1e-15:
        raise SystemExit('Sensitivity matrix singular')
    inv = [[c / det, -b / det], [-b / det, a / det]]
    return [[inv[i][0] * H[k][0] + inv[i][1] * H[k][1] for k in range(len(H))] for i in range(2)]


def mul(P, v):
    return [sum(P[i][k] * v[k] for k in range(len(v))) for i in range(len(P))]


def same_pass_range(a, b, head, n=4):
    if not a['pp'] or not b['pp']:
        return None
    common = sorted(set(a['pp']) & set(b['pp']))[-n:]
    if len(common) < 2:
        return None
    d = [b['pp'][p].get((head, head), float('nan')) - a['pp'][p].get((head, head), float('nan'))
         for p in common]
    return {'passes': common, 'diffs_uH': d, 'range_uH': max(d) - min(d)}


def verdict(ok):
    return 'PASS' if ok else 'FAIL'


def sensitivity(C):
    need = ['P1_center', 'P2_xp5', 'P2_xm5', 'P2_yp5', 'P2_ym5']
    if not all(k in C for k in need):
        return None
    L0 = diag(C['P1_center'])
    xp, xm, yp, ym = [diag(C[k]) for k in need[1:]]
    H = [[(xp[i] - xm[i]) / 10.0, (yp[i] - ym[i]) / 10.0] for i in range(5)]
    one = {HEADS[i]: {'x_plus': (xp[i] - L0[i]) / 5.0, 'x_minus': (L0[i] - xm[i]) / 5.0,
                      'y_plus': (yp[i] - L0[i]) / 5.0, 'y_minus': (L0[i] - ym[i]) / 5.0}
           for i in range(5)}
    return L0, H, one


def collect(root):
    C = latest_cases(root)
    rep = {'generated': now(),
           'cases_found': sorted(C), 'checks': {}, 'grade': 'SINGLE_MODEL_FAMILY_FEM_NOT_HARDWARE'}
    ck = rep['checks']
    # long table
    with wopen(os.path.join(root, 'matrix5_long.csv')) as f:
        w = csv.writer(f)
        w.writerow(['case', 'dx_mm', 'dy_mm', 'dz_mm', 'pitch_deg', 'roll_deg', 'i_head', 'j_head',
                    'L_uH', 'R_mOhm', 'per_pass_available', 'case_dir'])
        for cid, c in sorted(C.items()):
            cs = c['result']['case']
            for a in HEADS:
                for b in HEADS:
                    r = c['R'].get((a, b)) if c['R'] else None
                    w.writerow([cid, cs['dx'], cs['dy'], cs['dz'], cs['pitch_deg'], cs['roll_deg'], a, b,
                                '%.6f' % c['L'][(a, b)], '' if r is None else '%.6f' % r,
                                bool(c['pp']), c['dir']])
    for cid, c in C.items():
        asym = max(abs(c['L'][(a, b)] - c['L'][(b, a)]) for a in HEADS for b in HEADS)
        mut = max(abs(c['L'][(a, b)]) / c['L'][(a, a)] for a in HEADS for b in HEADS if a != b)
        ck.setdefault('per_case', {})[cid] = {
            'matrix_symmetry_max_abs_uH': asym, 'symmetry': verdict(asym < 1e-3 * 1100),
            'max_mutual_over_self_pct': 100 * mut,
            'self_L_uH': dict(zip(HEADS, diag(c)))}
    # stage 1
    if 'P1_center' in C:
        c = C['P1_center']['L']
        d = c[('Xp', 'Xp')] - AFAST_CENTER
        ck['P1_center_vs_AFAST'] = {'Lxp_uH': c[('Xp', 'Xp')], 'minus_AFAST_uH': d,
                                    'verdict': verdict(abs(d) <= TOL_AFAST)}
        ck['P1_baseline_asymmetry_uH'] = {'Xp_minus_Xm': c[('Xp', 'Xp')] - c[('Xm', 'Xm')],
                                          'Yp_minus_Ym': c[('Yp', 'Yp')] - c[('Ym', 'Ym')],
                                          'v101_was': {'Xp_minus_Xm': -14.32, 'Yp_minus_Ym': 2.12}}
        if 'P1_offaxis_30_30' in C:
            dd = C['P1_offaxis_30_30']['L'][('Xp', 'Xp')] - c[('Xp', 'Xp')]
            ck['P1_offaxis_diff_vs_AFAST'] = {'diff_uH': dd, 'AFAST_uH': AFAST_DIFF,
                                              'verdict': verdict(abs(dd - AFAST_DIFF) <= TOL_AFAST),
                                              'same_pass': same_pass_range(C['P1_center'], C['P1_offaxis_30_30'], 'Xp')}
    # stage 2
    s = sensitivity(C)
    if s:
        L0, H, one = s
        stab = {}
        good_x = good_y = 0
        for i, h in enumerate(HEADS):
            rx = [same_pass_range(C['P1_center'], C[k], h) for k in ('P2_xp5', 'P2_xm5')]
            ry = [same_pass_range(C['P1_center'], C[k], h) for k in ('P2_yp5', 'P2_ym5')]
            wx = max([r['range_uH'] for r in rx if r] or [float('nan')])
            wy = max([r['range_uH'] for r in ry if r] or [float('nan')])
            sigx, sigy = abs(H[i][0]) * 5, abs(H[i][1]) * 5
            if math.isnan(wx):
                wx = 2 * STAB  # per-pass matrix unavailable: assumed window from AFAST same-pass study
            if math.isnan(wy):
                wy = 2 * STAB
            okx = wx <= 2 * STAB and sigx >= 3 * max(wx, 1e-9)
            oky = wy <= 2 * STAB and sigy >= 3 * max(wy, 1e-9)
            good_x += okx; good_y += oky
            stab[h] = {'H_uH_per_mm': H[i], 'signal_5mm_uH': [sigx, sigy],
                       'same_pass_range_uH': [wx, wy], 'usable_x': okx, 'usable_y': oky,
                       'one_sided_uH_per_mm': one[h],
                       'one_sided_sign_consistent_x': one[h]['x_plus'] * one[h]['x_minus'] > 0,
                       'one_sided_sign_consistent_y': one[h]['y_plus'] * one[h]['y_minus'] > 0}
        sv = singular2(H)
        ck['P2_sensitivity'] = {'heads': stab, 'usable_heads_x': good_x, 'usable_heads_y': good_y,
                                'verdict': verdict(good_x >= 2 and good_y >= 2),
                                'H_singular_values': sv, 'condition': sv[0] / sv[1],
                                'note': 'if per-pass L is unavailable, stability window = assumed 0.6 uH (AFAST same-pass study); confirm with P6_center_strict'}
        json.dump({'L0_uH': L0, 'H_uH_per_mm': H, 'heads': HEADS},
                  open(os.path.join(root, 'H_matrix5_P2.json'), 'w'), indent=2)
        P = pinv52(H)
        # stage 3: one-step contraction at diagonal points
        out = {}
        for cid, e in (('P3_diag_p3_p3', (3.0, 3.0)), ('P3_diag_p3_m3', (3.0, -3.0))):
            if cid in C:
                dL = [diag(C[cid])[i] - L0[i] for i in range(5)]
                est = mul(P, dL)
                en = [e[0] - K_GAIN * est[0], e[1] - K_GAIN * est[1]]
                ratio = math.hypot(*en) / math.hypot(*e)
                out[cid] = {'true_e_mm': e, 'estimate_mm': est, 'e_after_one_step_mm': en,
                            'contraction_ratio': ratio, 'verdict': verdict(ratio < 1.0)}
        if out:
            ck['P3_diagonal_contraction_K0.55'] = out
        # stage 4
        p4 = {}
        if 'P4_pitch_p1' in C:
            d = [diag(C['P4_pitch_p1'])[i] - L0[i] for i in range(5)]
            p4['dL_per_deg_pitch_uH'] = dict(zip(HEADS, d)); p4['pitch_1deg_bias_mm'] = mul(P, d)
        if 'P4_roll_p1' in C:
            d = [diag(C['P4_roll_p1'])[i] - L0[i] for i in range(5)]
            p4['dL_per_deg_roll_uH'] = dict(zip(HEADS, d)); p4['roll_1deg_bias_mm'] = mul(P, d)
        if 'P4_gap18' in C and 'P4_gap22' in C:
            d = [(diag(C['P4_gap22'])[i] - diag(C['P4_gap18'])[i]) / 4.0 for i in range(5)]
            p4['dL_dz_uH_per_mm'] = dict(zip(HEADS, d)); p4['common_gap_1mm_bias_mm'] = mul(P, d)
        if p4:
            p4['bias_limit_note'] = 'compare |bias_mm| with the alignment tolerance; same model family only'
            ck['P4_bias_coefficients'] = p4
        # stage 5
        p5 = {}
        base_dx = L0[0] - L0[1]; base_dy = L0[2] - L0[3]
        for cid, ax in (('P2_xp5', 'x'), ('P5_xp15', 'x'), ('P5_xp30', 'x'),
                        ('P2_xm5', 'x'), ('P5_xm15', 'x'), ('P5_xm30', 'x'),
                        ('P2_yp5', 'y'), ('P5_yp15', 'y'), ('P5_yp30', 'y')):
            if cid in C:
                dl = diag(C[cid])
                p5[cid] = (dl[0] - dl[1] - base_dx) if ax == 'x' else (dl[2] - dl[3] - base_dy)
        if p5:
            sx = [p5[k] for k in ('P2_xp5', 'P5_xp15', 'P5_xp30') if k in p5]
            sy = [p5[k] for k in ('P2_yp5', 'P5_yp15', 'P5_yp30') if k in p5]
            ck['P5_capture_differential_uH'] = {
                'values': p5,
                'x_sign_consistent': all(v * sx[0] > 0 for v in sx) if sx else None,
                'y_sign_consistent': all(v * sy[0] > 0 for v in sy) if sy else None}
            # v1.5.12: can +x and -x be told apart by Xp-Xm at the same distance?
            xdir = {}
            for a, b in (('P2_xp5', 'P2_xm5'), ('P5_xp15', 'P5_xm15'), ('P5_xp30', 'P5_xm30')):
                if a in p5 and b in p5:
                    xdir[a.split('_')[1][2:] + 'mm'] = {
                        'plus_uH': p5[a], 'minus_uH': p5[b], 'separation_uH': p5[a] - p5[b],
                        'opposite_sign': p5[a] * p5[b] < 0,
                        'separable_over_1uH_noise': abs(p5[a] - p5[b]) > 1.0}
            if xdir:
                ck['P5_x_direction_uH'] = xdir
    if 'P6_center_strict' in C and 'P1_center' in C:
        ck['P6_mesh_strict_minus_afast_uH'] = dict(zip(HEADS, [diag(C['P6_center_strict'])[i] -
                                                            diag(C['P1_center'])[i] for i in range(5)]))
    json.dump(rep, open(os.path.join(root, 'MATRIX5_CHECKS.json'), 'w'), indent=2, default=str)
    print(json.dumps(rep, indent=2, default=str))
    return rep


def singular2(H):
    a = sum(r[0] * r[0] for r in H); b = sum(r[0] * r[1] for r in H); c = sum(r[1] * r[1] for r in H)
    tr, det = a + c, a * c - b * b
    disc = math.sqrt(max(tr * tr / 4 - det, 0))
    return [math.sqrt(tr / 2 + disc), math.sqrt(max(tr / 2 - disc, 0))]


def predict_blind(root):
    C = latest_cases(root)
    s = sensitivity(C)
    if not s:
        raise SystemExit('Need P1_center and P2_xp5/xm5/yp5/ym5 solved first.')
    L0, H, _ = s
    pred = [L0[i] + H[i][0] * BLIND[0] + H[i][1] * BLIND[1] for i in range(5)]
    path = os.path.join(root, 'BLIND_PREDICTION.json')
    if os.path.exists(path):
        raise SystemExit('BLIND_PREDICTION.json already exists; never overwrite a blind prediction.')
    doc = {'created': now(), 'point_mm': BLIND,
           'predicted_self_L_uH': dict(zip(HEADS, pred)), 'model': 'linear L0 + H e from P1/P2',
           'source_cases': {k: C[k]['dir'] for k in ('P1_center', 'P2_xp5', 'P2_xm5', 'P2_yp5', 'P2_ym5')}}
    txt = json.dumps(doc, indent=2)
    open(path, 'w').write(txt)
    print(txt)
    print('SHA256', hashlib.sha256(txt.encode()).hexdigest(), '(write this down / send it before solving)')


def check_blind(root):
    C = latest_cases(root)
    path = os.path.join(root, 'BLIND_PREDICTION.json')
    if 'P3_blind_m2_p4' not in C or not os.path.isfile(path):
        raise SystemExit('Need BLIND_PREDICTION.json and solved P3_blind_m2_p4.')
    doc = json.load(open(path))
    L0, H, _ = sensitivity(C)
    act = diag(C['P3_blind_m2_p4'])
    err = {h: act[i] - doc['predicted_self_L_uH'][h] for i, h in enumerate(HEADS)}
    est = mul(pinv52(H), [act[i] - L0[i] for i in range(5)])
    out = {'L_prediction_error_uH': err, 'estimated_position_mm': est, 'true_mm': BLIND,
           'position_error_mm': math.hypot(est[0] - BLIND[0], est[1] - BLIND[1])}
    print(json.dumps(out, indent=2))
    json.dump(out, open(os.path.join(root, 'BLIND_CHECK.json'), 'w'), indent=2)


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] not in ('collect', 'predict-blind', 'check-blind'):
        print(__doc__)
        sys.exit(1)
    {'collect': collect, 'predict-blind': predict_blind, 'check-blind': check_blind}[sys.argv[1]](sys.argv[2])
