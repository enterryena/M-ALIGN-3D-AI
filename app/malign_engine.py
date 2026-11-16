# -*- coding: utf-8 -*-
"""M-ALIGN Matrix5 engine — Python port of the v3.5.4 JavaScript engine.

Ported operation-for-operation from MALIGN_Desktop.html (v3.5.4 commit 18ae279):
rng, PCHIP, response model, measurement (truth), grid estimator, history
estimator, 4-corner rigid fit and the full closed loop (run_once).
The same seed gives the same results as the JS engine; tests/test_engine_port.py
checks this against node.

numpy is used only to evaluate many candidate positions at once. For every
candidate the arithmetic is done in the same order as the JS code, so the
float64 results are identical.

Split for the kit/main-server architecture:
  kit side  : measure (hardware in reality) -> grid_estimate + corner_quality
  server side: history_estimate (needs the moves the server applied), rigid_fit
"""
import json
import math
import pathlib

import numpy as np

HEADS = ['Xp', 'Xm', 'Yp', 'Ym', 'Ref']
CORNERS = [(-2926.5, 1129.5), (2926.5, 1129.5), (2926.5, -1129.5), (-2926.5, -1129.5)]
DATA_FILE = pathlib.Path(__file__).with_name('matrix5_data.json')
FEM_RANGE_MM = 30.0   # Maxwell samples cover +-30 mm; beyond is extrapolation
GAP_FIT_LIMIT = 3.0   # estimator clamps the fitted gap change to +-3 mm

PRESETS = {
    'ideal':  {'modelBias': 0, 'noise': .1, 'gapSd': 0, 'tiltSd': 0, 'tolC': .7, 'act': .2},
    'target': {'modelBias': .25, 'noise': .2, 'gapSd': .3, 'tiltSd': .05, 'tolC': .7, 'act': .2},
    'base':   {'modelBias': .5, 'noise': .3, 'gapSd': 1, 'tiltSd': .2, 'tolC': 3, 'act': .5},
}
BASE = {'R0': 30, 'theta0': .1, 'K': .8, 'iters': 6, 'tolMM': 5, 'fitGap': True, 'mirror': True,
        'useHead': [1, 1, 1, 1, 1], 'searchMM': 36, 'stopMM': 1}


def settings(scenario='base', estimator='history', **over):
    p = dict(BASE)
    p.update(PRESETS[scenario])
    p['estimator'] = estimator
    p.update(over)
    return p


# --------------------------------------------------------------------------- RNG (xorshift32 + Box-Muller)
class Rng:
    def __init__(self, seed):
        self.s = (int(seed) & 0xFFFFFFFF) or 1

    def __call__(self):
        s = self.s
        s = (s ^ (s << 13)) & 0xFFFFFFFF
        s = s ^ (s >> 17)
        s = (s ^ (s << 5)) & 0xFFFFFFFF
        self.s = s
        return s / 4294967296

    def n(self):
        u = 0.0
        while u == 0:
            u = self()
        v = self()
        return math.sqrt(-2 * math.log(u)) * math.cos(2 * math.pi * v)


# --------------------------------------------------------------------------- PCHIP
class Pchip:
    """Fritsch-Carlson PCHIP, flat outside the data (same as the JS)."""

    def __init__(self, xs, ys):
        n = len(xs)
        h = [xs[i + 1] - xs[i] for i in range(n - 1)]
        d = [(ys[i + 1] - ys[i]) / h[i] for i in range(n - 1)]
        m = [0.0] * n
        m[0] = d[0]
        m[n - 1] = d[n - 2]
        for i in range(1, n - 1):
            if d[i - 1] * d[i] <= 0:
                m[i] = 0
            else:
                w1 = 2 * h[i] + h[i - 1]
                w2 = h[i] + 2 * h[i - 1]
                m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])
        self.xs, self.ys, self.h, self.m, self.n = list(xs), list(ys), h, m, n
        self._a = [np.asarray(v, dtype=float) for v in (xs, ys, h, m)]

    def __call__(self, x):
        xs, ys, h, m, n = self.xs, self.ys, self.h, self.m, self.n
        if x <= xs[0]:
            return ys[0]
        if x >= xs[n - 1]:
            return ys[n - 1]
        k = 0
        while x > xs[k + 1]:
            k += 1
        t = (x - xs[k]) / h[k]
        t2 = t * t
        t3 = t2 * t
        return ((2 * t3 - 3 * t2 + 1) * ys[k] + (t3 - 2 * t2 + t) * h[k] * m[k]
                + (-2 * t3 + 3 * t2) * ys[k + 1] + (t3 - t2) * h[k] * m[k + 1])

    def vec(self, x):
        x = np.asarray(x, dtype=float)
        xs, ys, h, m = self._a
        n = self.n
        k = np.clip(np.searchsorted(xs, x, side='left') - 1, 0, n - 2)  # first k with x <= xs[k+1]
        t = (x - xs[k]) / h[k]
        t2 = t * t
        t3 = t2 * t
        out = ((2 * t3 - 3 * t2 + 1) * ys[k] + (t3 - 2 * t2 + t) * h[k] * m[k]
               + (-2 * t3 + 3 * t2) * ys[k + 1] + (t3 - t2) * h[k] * m[k + 1])
        out = np.where(x <= xs[0], ys[0], out)
        return np.where(x >= xs[n - 1], ys[n - 1], out)


# --------------------------------------------------------------------------- response model
class Model:
    def __init__(self, data=None):
        if data is None:
            data = json.loads(DATA_FILE.read_text(encoding='utf-8'))
        C = data['cases']
        L0 = C['P1_center']['L']
        self.L0 = list(L0)

        def dL(cid):
            return [v - L0[i] for i, v in enumerate(C[cid]['L'])]
        zero = [0, 0, 0, 0, 0]
        xs = [-30, -15, -5, 0, 5, 15, 30]
        x_rows = [dL('P5_xm30'), dL('P5_xm15'), dL('P2_xm5'), zero, dL('P2_xp5'), dL('P5_xp15'), dL('P5_xp30')]
        y5m, y5, y15, y30 = dL('P2_ym5'), dL('P2_yp5'), dL('P5_yp15'), dL('P5_yp30')

        def mir(r):  # y < -5 mirrored about y=0; Ref held at its -5 mm value (assumed, as JS)
            return [r[0], r[1], r[3], r[2], y5m[4]]
        y_rows = [mir(y30), mir(y15), y5m, zero, y5, y15, y30]
        self.fx = [Pchip(xs, [r[h] for r in x_rows]) for h in range(5)]
        self.fy = [Pchip(xs, [r[h] for r in y_rows]) for h in range(5)]
        d33 = dL('P1_offaxis_30_30')
        self.cxy = [(d33[h] - self.fx[h](30) - self.fy[h](30)) / 900 for h in range(5)]
        g18, g22, g40 = dL('P4_gap18'), dL('P4_gap22'), dL('P6_gap40')
        self.gz = [Pchip([-2, 0, 2, 20], [g18[h], 0, g22[h], g40[h]]) for h in range(5)]
        self.gslope = [(g22[h] - g18[h]) / 4 for h in range(5)]
        self.kp, self.kr = dL('P4_pitch_p1'), dL('P4_roll_p1')
        self.R, self.S = 40, 0.5
        self.N = int(round(2 * self.R / self.S)) + 1
        grid = -self.R + np.arange(self.N) * self.S
        # Separable tables: ax[h][i] = fx(x_i), ay[h][j] = fy(y_j). tab = ax + ay + cxy*x*y (computed on demand).
        self.ax = np.array([[self.fx[h](float(v)) for v in grid] for h in range(5)])
        self.ay = np.array([[self.fy[h](float(v)) for v in grid] for h in range(5)])
        self.grid = grid

    def resp(self, x, y):
        return [self.fx[k](x) + self.fy[k](y) + self.cxy[k] * x * y for k in range(5)]

    def tab(self, h, I, J):
        x = -self.R + I * self.S
        y = -self.R + J * self.S
        return self.ax[h][I] + self.ay[h][J] + self.cxy[h] * x * y

    def model_size(self):
        """Values a device needs for the grid estimator (separable form)."""
        return int(self.ax.size + self.ay.size + 5 + 5)


def frame(k, mirror=True):
    if not mirror:
        return (1, 1)
    return (1 if CORNERS[k][0] > 0 else -1, 1 if CORNERS[k][1] > 0 else -1)


# --------------------------------------------------------------------------- truth: one corner reading
def measure(M, st, ex, ey, rnd, P):
    out = M.resp(ex, ey)
    for h in range(5):
        out[h] = out[h] + (M.gz[h](st['dz']) + M.kp[h] * st['pitch'] + M.kr[h] * st['roll']
                           + st['bias'][h] + P['noise'] * rnd.n())
    return out


# --------------------------------------------------------------------------- kit side: grid estimator
def _search(cost_fn, c0, lim, N, best=math.inf):
    """Coarse 2 mm grid then fine 0.5 mm, strict '<' in row-major order (as the JS loops)."""
    ci = np.arange(c0 - lim, c0 + lim + 1, 4)
    I, J = np.meshgrid(ci, ci, indexing='ij')
    c = cost_fn(I.ravel(), J.ravel())
    bi = bj = c0
    if c.size:
        a = int(np.argmin(c))
        if c[a] < best:
            best, bi, bj = float(c[a]), int(I.ravel()[a]), int(J.ravel()[a])
    fi, fj = bi, bj
    fr_i = np.arange(bi - 5, bi + 6)
    fr_j = np.arange(bj - 5, bj + 6)
    I2, J2 = np.meshgrid(fr_i, fr_j, indexing='ij')
    I2, J2 = I2.ravel(), J2.ravel()
    ok = (I2 >= 0) & (J2 >= 0) & (I2 < N) & (J2 < N)
    I2, J2 = I2[ok], J2[ok]
    c2 = cost_fn(I2, J2)
    if c2.size:
        a = int(np.argmin(c2))
        if c2[a] < best:
            best, fi, fj = float(c2[a]), int(I2[a]), int(J2[a])
    return best, fi, fj


def grid_estimate(M, meas, P):
    """Local-frame (x, y) of the hole from one 5-head reading. Also returns the fitted gap change."""
    w, g = P['useHead'], M.gslope
    gg = 0.0
    for h in range(5):
        if w[h]:
            gg += g[h] * g[h]
    fit = P['fitGap'] and gg > 0

    def parts(I, J):
        r = [None] * 5
        gr = np.zeros(I.shape)
        for h in range(5):
            if w[h]:
                r[h] = meas[h] - M.tab(h, I, J)
                gr = gr + g[h] * r[h]
        dz = np.clip(gr / gg, -3, 3) if fit else np.zeros(I.shape)
        c = np.zeros(I.shape)
        for h in range(5):
            if w[h]:
                e = r[h] - g[h] * dz
                c = c + e * e
        return c, dz

    lim = int(round(P['searchMM'] / M.S))
    c0 = int(round(M.R / M.S))
    best, fi, fj = _search(lambda I, J: parts(I, J)[0], c0, lim, M.N)
    dz = float(parts(np.array([fi]), np.array([fj]))[1][0])
    return {'x': -M.R + fi * M.S, 'y': -M.R + fj * M.S, 'rss': best, 'dz': dz}


def corner_quality(M, meas, est, P, head_ok=None):
    """Kit-side reliability of ONE corner (local frame). Thresholds are first guesses for stage 3 tuning."""
    head_ok = head_ok or [True] * 5
    extrap = abs(est['x']) > FEM_RANGE_MM or abs(est['y']) > FEM_RANGE_MM
    gap_out = abs(est['dz']) >= GAP_FIT_LIMIT - 1e-9
    bad_heads = sum(1 for v in head_ok if not v)
    rss = est['rss']
    if bad_heads >= 2 or not math.isfinite(rss):
        grade = 'invalid'
    elif extrap or gap_out or bad_heads == 1 or rss > RSS_CAUTION:
        grade = 'caution'
    else:
        grade = 'good'
    return {'rss_uH2': rss, 'extrapolated': extrap, 'gap_out_of_range': gap_out,
            'head_ok': list(head_ok), 'grade': grade}


RSS_CAUTION = 1.0  # uH^2: residual of a clean reading is ~5*noise^2 (0.05..0.45); provisional


# --------------------------------------------------------------------------- server side: history estimator
def history_estimate(M, hist, P, fallback):
    """hist: [{'m': 5 readings, 'ox': local offset, 'oy': local offset}] — readings + COMMANDED moves only."""
    if len(hist) < 2:
        return fallback()
    x0, y0 = hist[0]['ox'], hist[0]['oy']
    travel = 0.0
    for q in range(1, len(hist)):
        travel = max(travel, math.hypot(hist[q]['ox'] - x0, hist[q]['oy'] - y0))
    if not travel > 1e-9:
        return fallback()
    N, S, R, w, n = M.N, M.S, M.R, P['useHead'], len(hist)
    lim = int(round(P['searchMM'] / S))
    c0 = int(round(R / S))

    def score(I, J):
        x = -R + I * S
        y = -R + J * S
        s = [np.zeros(I.shape) for _ in range(5)]
        ss = [np.zeros(I.shape) for _ in range(5)]
        for k in range(n):
            ob = hist[k]
            px = x + ob['ox']
            py = y + ob['oy']
            for h in range(5):
                if w[h]:
                    predicted = M.fx[h].vec(x + ob['ox']) + M.fy[h].vec(y + ob['oy']) + M.cxy[h] * px * py
                    v = ob['m'][h] - predicted
                    s[h] = s[h] + v
                    ss[h] = ss[h] + v * v
        c = np.zeros(I.shape)
        for h in range(5):
            if w[h]:
                c = c + np.maximum(0, ss[h] - s[h] * s[h] / n)
        return c

    best, fi, fj = _search(score, c0, lim, N)
    if not math.isfinite(best):
        return fallback()
    return {'x': -R + fi * S, 'y': -R + fj * S, 'rss': best, 'historyUsed': n}


# --------------------------------------------------------------------------- server side: 4-corner rigid fit
def rigid_fit(est):
    """Least-squares (dx, dy, dtheta[rad]) of 4 container-frame corner estimates (same as JS)."""
    sx = sy = 0.0
    for k in range(4):
        sx += est[k][0]
        sy += est[k][1]
    sx /= 4
    sy /= 4
    num = den = 0.0
    for k in range(4):
        X2, Y2 = CORNERS[k]
        num += X2 * (est[k][1] - sy) - Y2 * (est[k][0] - sx)
        den += X2 * X2 + Y2 * Y2
    return sx, sy, num / den


# --------------------------------------------------------------------------- world (truth) for one job
TOL_MODELS = ('orig', 'iso')
CAPTURES = ('pm5', 'head', 'float10')


def iso_tol(zs, cast):
    """ISO 668-based corner deviations (20 ft): corner-centre distances S/P within +3/-5 mm, diagonal difference
    <= 13 mm (limits taken as 3 sigma) + 0.5 mm casting scatter. zs = 3 standard normals (S, P, shear), cast = 4x2."""
    clip = lambda v, lo, hi: max(lo, min(hi, v))
    dS = clip(-1 + 8 / 6 * zs[0], -5, 3)
    dP = clip(-1 + 8 / 6 * zs[1], -5, 3)
    sh = clip(13 / 3 * zs[2], -13, 13) / 1.87
    out = []
    for k, (X, Y) in enumerate(CORNERS):
        gx, gy = (1 if X > 0 else -1), (1 if Y > 0 else -1)
        out.append([gx * dS / 2 + gy * sh / 2 + 0.5 * cast[k][0], gy * dP / 2 + 0.5 * cast[k][1]])
    return out


def capture_ok(errs, P):
    """Twistlock engagement. pm5: original +-5 mm both axes. head: rigid ISO twistlock in the 124.5 x 63.5 mm top
    aperture (head ~104 x 56 mm) -> about +-10 mm along the container, +-3.8 mm across. float10: floating twistlocks."""
    cap = P.get('capture', 'pm5')
    cx, cy = (10.0, 3.8) if cap == 'head' else (10.0, 10.0) if cap == 'float10' else (P['tolMM'], P['tolMM'])
    return all(abs(e[0]) <= cx and abs(e[1]) <= cy for e in errs)


class World:
    """Container + spreader truth for one job, drawn exactly like the JS runOnce."""

    def __init__(self, P, rnd):
        self.P, self.rnd = P, rnd
        r0 = P['R0'] * math.sqrt(rnd())
        a0 = 2 * math.pi * rnd()
        self.tx, self.ty = r0 * math.cos(a0), r0 * math.sin(a0)
        self.th = P['theta0'] * math.pi / 180 * rnd.n()
        self.tol, self.st = [], []
        for _ in range(4):
            self.tol.append([P['tolC'] * rnd.n(), P['tolC'] * rnd.n()])
            b = [P['modelBias'] * rnd.n() for _ in range(5)]
            dz = P['gapSd'] * rnd.n()
            pitch = P['tiltSd'] * rnd.n()
            roll = P['tiltSd'] * rnd.n()
            self.st.append({'dz': dz, 'pitch': pitch, 'roll': roll, 'bias': b})
        if P.get('tolModel') == 'iso':      # same draw order as the JS engine (after the original draws)
            zs = [rnd.n(), rnd.n(), rnd.n()]
            cast = [[rnd.n(), rnd.n()] for _ in range(4)]
            self.tol = iso_tol(zs, cast)
        self.extrap = False

    def errs(self):
        out = []
        for q in range(4):
            X, Y = CORNERS[q]
            out.append([self.tx - self.th * Y + self.tol[q][0], self.ty + self.th * X + self.tol[q][1]])
        return out

    def read_corner(self, M, k, E=None):
        """Sensor reading of corner k (local frame) = what the kit unit measures."""
        E = E or self.errs()
        f = frame(k, self.P['mirror'])
        lx, ly = f[0] * E[k][0], f[1] * E[k][1]
        if abs(lx) > 30 or abs(ly) > 30:
            self.extrap = True
        return measure(M, self.st[k], lx, ly, self.rnd, self.P)

    def apply_move(self, sx, sy, dth):
        """Machine applies the commanded correction (K already applied by caller), with actuator error."""
        P = self.P
        self.tx -= sx + P['act'] * self.rnd.n()
        self.ty -= sy + P['act'] * self.rnd.n()
        self.th -= dth


# --------------------------------------------------------------------------- reference closed loop (= JS runOnce)
def run_once(M, P, rnd):
    w = World(P, rnd)
    e0 = w.errs()
    hist = [[], [], [], []] if P['estimator'] == 'history' else None
    it = 0
    while it <= P['iters']:
        E = w.errs()
        if it == P['iters']:
            break
        est = []
        for k in range(4):
            meas = w.read_corner(M, k, E)
            f = frame(k, P['mirror'])
            if hist is not None:
                hist[k].append({'m': list(meas), 'ox': 0.0, 'oy': 0.0})
                es = history_estimate(M, hist[k], P, lambda: grid_estimate(M, meas, P)) \
                    if len(hist[k]) >= 2 else grid_estimate(M, meas, P)
            else:
                es = grid_estimate(M, meas, P)
            est.append([f[0] * es['x'], f[1] * es['y']])
        sx, sy, dth = rigid_fit(est)
        if hist is not None:
            cmdX, cmdY, cmdTh = -P['K'] * sx, -P['K'] * sy, -P['K'] * dth
            for k in range(4):
                lf = frame(k, P['mirror'])
                CX, CY = CORNERS[k]
                deX = lf[0] * (cmdX - cmdTh * CY)
                deY = lf[1] * (cmdY + cmdTh * CX)
                for ob in hist[k]:
                    ob['ox'] -= deX
                    ob['oy'] -= deY
        w.apply_move(P['K'] * sx, P['K'] * sy, P['K'] * dth)
        small = all(abs(e[0]) <= P['stopMM'] and abs(e[1]) <= P['stopMM'] for e in est)
        if small:
            break
        it += 1
    EF = w.errs()
    worst = max(max(abs(v[0]), abs(v[1])) for v in EF)
    worst0 = max(max(abs(v[0]), abs(v[1])) for v in e0)
    return {'ok': capture_ok(EF, P), 'ok0': capture_ok(e0, P), 'worst': worst, 'worst0': worst0,
            'steps': it, 'extrap': w.extrap}
