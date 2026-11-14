# -*- coding: utf-8 -*-
"""Container yard geometry and a car-like path planner for the reach stacker.

Coordinates are the 3D view's ground plane: x (m), z (m), yaw (rad, three.js rotation about Y).
Forward of a vehicle with yaw = (sin yaw, cos yaw). A container's long side is its local x.

The reach stacker drives like a car: forward / reverse with steering limited to a minimum
turning radius -- it can never turn on the spot. Paths are found with a hybrid A* search
(arcs of fixed length at 5 curvatures x 2 directions) on a distance field of the known obstacles.
"""
import heapq
import math

import numpy as np

# ---- sizes (m)
L, W, H = 6.058, 2.438, 2.591          # 20 ft container
DOCK = W / 2 + 6.3                       # container centre is DOCK metres in front of the stacker origin
PRE = 5.0                                # line-up point: PRE metres behind the dock pose
R_MIN = 8.0                              # minimum turning radius
KAPPA = 1.0 / R_MIN
MARGIN = 0.3                             # planning clearance
BOUNDS = (-46.0, 46.0, -40.0, 34.5)      # where the stacker may drive (x0, x1, z0, z1)

# footprint: discs in stacker-local (x, z, r). Body behind/at the origin, spreader (and carried container) in front.
BODY = [(0.0, -5.0, 2.0), (0.0, -2.5, 2.0), (0.0, 0.0, 2.0), (0.0, 2.5, 1.9)]
FRONT = [(-2.4, DOCK, 1.6), (0.0, DOCK, 1.6), (2.4, DOCK, 1.6)]
FOOT = BODY + FRONT

# ---- layout
SLOT_X = (-24.0, -14.0, -4.0, 6.0, 16.0)
SLOTS = [('A%d' % (i + 1), x, 4.0, math.pi) for i, x in enumerate(SLOT_X)] + \
        [('B%d' % (i + 1), x, -14.0, 0.0) for i, x in enumerate(SLOT_X)]
BAYS = [('BAY1', -20.0, 32.0), ('BAY2', -4.0, 32.0), ('BAY3', 12.0, 32.0)]   # trucks face +x, container yaw 0
LANE_Z = 38.0                            # truck lane outside the yard
HOME = (34.0, -4.0, 0.0)
POLES = [(-36.0, -26.0, .45), (28.0, -27.0, .45), (-38.0, 20.0, .45), (30.0, 21.0, .45)]
BUILDINGS = [(40.0, -37.5, 0.0, 4.0, 2.0)]   # site office: rect (cx, cz, yaw, hx, hz)


def rot(yaw, lx, lz):
    c, s = math.cos(yaw), math.sin(yaw)
    return lx * c + lz * s, -lx * s + lz * c


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def footprint(x, z, yaw, discs=FOOT):
    c, s = math.cos(yaw), math.sin(yaw)
    return [(x + lx * c + lz * s, z - lx * s + lz * c, r) for lx, lz, r in discs]


def dock_pose(cx, cz, cyaw):
    dx, dz = rot(cyaw, 0.0, -DOCK)
    return (cx + dx, cz + dz, cyaw)


def predock_pose(cx, cz, cyaw):
    dx, dz = rot(cyaw, 0.0, -DOCK - PRE)
    return (cx + dx, cz + dz, cyaw)


# ---- shapes: ('rect', cx, cz, yaw, hx, hz) or ('circle', cx, cz, r)
def container_rect(x, z, yaw):
    return ('rect', x, z, yaw, L / 2, W / 2)


def shape_dist(sh, px, pz):
    if sh[0] == 'circle':
        return max(0.0, math.hypot(px - sh[1], pz - sh[2]) - sh[3])
    _, cx, cz, yaw, hx, hz = sh
    dx, dz = px - cx, pz - cz
    c, s = math.cos(yaw), math.sin(yaw)
    lx, lz = dx * c - dz * s, dx * s + dz * c
    return math.hypot(max(abs(lx) - hx, 0.0), max(abs(lz) - hz, 0.0))


def footprint_clearance(x, z, yaw, shapes, discs=FOOT):
    """Smallest gap between the footprint and the shapes (negative = overlap)."""
    best = 1e9
    for px, pz, r in footprint(x, z, yaw, discs):
        for sh in shapes:
            best = min(best, shape_dist(sh, px, pz) - r)
    return best


class DistanceField:
    """Distance (m) from each grid cell to the nearest known obstacle or the yard boundary."""
    RES = 0.5
    X0, X1, Z0, Z1 = -50.0, 50.0, -45.0, 40.0

    def __init__(self, shapes):
        self.nx = int((self.X1 - self.X0) / self.RES) + 1
        self.nz = int((self.Z1 - self.Z0) / self.RES) + 1
        xs = self.X0 + np.arange(self.nx) * self.RES
        zs = self.Z0 + np.arange(self.nz) * self.RES
        X, Z = np.meshgrid(xs, zs, indexing='ij')
        bx0, bx1, bz0, bz1 = BOUNDS
        d = np.minimum.reduce([X - bx0, bx1 - X, Z - bz0, bz1 - Z])
        d = np.maximum(d, 0.0)
        for sh in shapes:
            if sh[0] == 'circle':
                dd = np.maximum(np.hypot(X - sh[1], Z - sh[2]) - sh[3], 0.0)
            else:
                _, cx, cz, yaw, hx, hz = sh
                c, s = math.cos(yaw), math.sin(yaw)
                DX, DZ = X - cx, Z - cz
                lx, lz = DX * c - DZ * s, DX * s + DZ * c
                dd = np.hypot(np.maximum(np.abs(lx) - hx, 0), np.maximum(np.abs(lz) - hz, 0))
            d = np.minimum(d, dd)
        self.d = d
        self.flat = d.ravel().tolist()

    def at(self, x, z):
        i = int(round((x - self.X0) / self.RES))
        k = int(round((z - self.Z0) / self.RES))
        if i < 0 or k < 0 or i >= self.nx or k >= self.nz:
            return 0.0
        return self.flat[i * self.nz + k]

    def free(self, x, z, yaw, discs=FOOT, margin=MARGIN):
        c, s = math.cos(yaw), math.sin(yaw)
        for lx, lz, r in discs:
            if self.at(x + lx * c + lz * s, z - lx * s + lz * c) < r + margin:
                return False
        return True


def arc(x, z, yaw, kappa, s):
    """Pose after driving signed distance s (negative = reverse) at curvature kappa."""
    if abs(kappa) < 1e-9:
        return x + s * math.sin(yaw), z + s * math.cos(yaw), yaw
    y1 = yaw + kappa * s
    return x + (math.cos(yaw) - math.cos(y1)) / kappa, z + (math.sin(y1) - math.sin(yaw)) / kappa, y1


def _holonomic(df, gx, gz, cell=1.0, clear=2.0 + MARGIN):
    """Grid Dijkstra (8-connected) from the goal over cells where the stacker origin fits: the A* heuristic."""
    x0, z0 = df.X0, df.Z0
    nx = int((df.X1 - x0) / cell) + 1
    nz = int((df.Z1 - z0) / cell) + 1
    INF = float('inf')
    dist = [INF] * (nx * nz)
    ok = [df.at(x0 + i * cell, z0 + k * cell) >= clear for i in range(nx) for k in range(nz)]
    gi, gk = int(round((gx - x0) / cell)), int(round((gz - z0) / cell))
    start = gi * nz + gk
    dist[start] = 0.0
    pq = [(0.0, gi, gk)]
    steps = [(1, 0, 1), (-1, 0, 1), (0, 1, 1), (0, -1, 1), (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142)]
    while pq:
        d, i, k = heapq.heappop(pq)
        if d > dist[i * nz + k]:
            continue
        for di, dk, c in steps:
            a, b = i + di, k + dk
            if 0 <= a < nx and 0 <= b < nz:
                idx = a * nz + b
                if not ok[idx] and idx != start:
                    continue
                nd = d + c * cell
                if nd < dist[idx]:
                    dist[idx] = nd
                    heapq.heappush(pq, (nd, a, b))

    def h(x, z):
        i, k = int(round((x - x0) / cell)), int(round((z - z0) / cell))
        if 0 <= i < nx and 0 <= k < nz:
            return dist[i * nz + k]
        return INF
    return h


def plan(df, start, goal, discs=FOOT, tol_pos=0.7, tol_yaw=math.radians(10), max_expand=40000,
         step=2.5, rev_cost=1.8, switch_cost=6.0, weight=2.0, XY=1.0, NY=36):
    """Hybrid A*: car-like path from start to goal (x, z, yaw). Returns a list of samples
    (x, z, yaw, dir) every ~0.5 m, dir = +1 forward / -1 reverse, or None if no path."""
    sx, sz, syaw = start
    gx, gz, gyaw = goal
    if not df.free(gx, gz, gyaw, discs):
        return None
    h2 = _holonomic(df, gx, gz)
    if h2(sx, sz) == float('inf'):
        return None
    kappas = (-KAPPA, -KAPPA / 2, 0.0, KAPPA / 2, KAPPA)
    sub = 5

    def key(x, z, y):
        return (int(round(x / XY)), int(round(z / XY)), int(round(wrap(y) / (2 * math.pi / NY))) % NY)

    def heur(x, z, y):
        return max(h2(x, z), math.hypot(gx - x, gz - z)) + 1.5 * abs(wrap(gyaw - y))

    nodes = [(sx, sz, syaw, 0, None, None)]     # x, z, yaw, dir, parent, samples-from-parent
    best_g = {key(sx, sz, syaw): 0.0}
    pq = [(weight * heur(sx, sz, syaw), 0.0, 0)]
    expanded = 0
    while pq and expanded < max_expand:
        f, g, ni = heapq.heappop(pq)
        x, z, y, d0, _, _ = nodes[ni]
        if best_g.get(key(x, z, y), 1e18) < g - 1e-9:
            continue
        expanded += 1
        for dirn in (1, -1):
            for kp in kappas:
                samples = []
                ok = True
                for j in range(1, sub + 1):
                    px, pz, py = arc(x, z, y, kp, dirn * step * j / sub)
                    if not df.free(px, pz, py, discs):
                        ok = False
                        break
                    samples.append((px, pz, py, dirn))
                if not ok:
                    continue
                nx_, nz_, ny_ = samples[-1][:3]
                cost = step * (1.0 if dirn > 0 else rev_cost) + (switch_cost if d0 and d0 != dirn else 0.0) \
                    + 0.3 * step * abs(kp) / KAPPA
                g2 = g + cost
                hit = None
                for j, smp in enumerate(samples):
                    if math.hypot(smp[0] - gx, smp[1] - gz) < tol_pos and abs(wrap(smp[2] - gyaw)) < tol_yaw:
                        hit = j
                        break
                if hit is not None:
                    nodes.append((samples[hit][0], samples[hit][1], samples[hit][2], dirn, ni, samples[:hit + 1]))
                    return _collect(nodes, len(nodes) - 1, start)
                k2 = key(nx_, nz_, ny_)
                if g2 < best_g.get(k2, 1e18):
                    best_g[k2] = g2
                    nodes.append((nx_, nz_, ny_, dirn, ni, samples))
                    hh = heur(nx_, nz_, ny_)
                    if hh < float('inf'):
                        heapq.heappush(pq, (g2 + weight * hh, g2, len(nodes) - 1))
    return None


def _collect(nodes, ni, start):
    chunks = []
    while ni is not None:
        x, z, y, d, parent, samples = nodes[ni]
        if samples:
            chunks.append(samples)
        ni = parent
    out = [(start[0], start[1], start[2], 0)]
    for ch in reversed(chunks):
        out.extend(ch)
    if len(out) > 1:
        out[0] = (out[0][0], out[0][1], out[0][2], out[1][3])
    return out


def straight(p0, p1, dirn, ds=0.5):
    """Short line-up / back-out segment between two nearly aligned poses (Hermite blend, heading from tangent)."""
    x0, z0, y0 = p0
    x1, z1, y1 = p1
    dist = math.hypot(x1 - x0, z1 - z0)
    n = max(2, int(dist / ds))
    t0 = (math.sin(y0) * dirn * dist, math.cos(y0) * dirn * dist)
    t1 = (math.sin(y1) * dirn * dist, math.cos(y1) * dirn * dist)
    out = []
    for i in range(1, n + 1):
        t = i / n
        h00, h10, h01, h11 = 2 * t**3 - 3 * t**2 + 1, t**3 - 2 * t**2 + t, -2 * t**3 + 3 * t**2, t**3 - t**2
        x = h00 * x0 + h10 * t0[0] + h01 * x1 + h11 * t1[0]
        z = h00 * z0 + h10 * t0[1] + h01 * z1 + h11 * t1[1]
        dx = (6 * t**2 - 6 * t) * x0 + (3 * t**2 - 4 * t + 1) * t0[0] + (-6 * t**2 + 6 * t) * x1 + (3 * t**2 - 2 * t) * t1[0]
        dz = (6 * t**2 - 6 * t) * z0 + (3 * t**2 - 4 * t + 1) * t0[1] + (-6 * t**2 + 6 * t) * z1 + (3 * t**2 - 2 * t) * t1[1]
        yaw = math.atan2(dx, dz) if dirn > 0 else math.atan2(-dx, -dz)
        out.append((x, z, yaw if i < n else y1, dirn))
    return out


def path_length(path):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:]))


def max_turn_rate(path):
    """Largest |d yaw / ds| along a path (rad/m): a car-like path stays <= 1/R_MIN; turning on the spot = inf."""
    worst = 0.0
    for a, b in zip(path, path[1:]):
        ds = math.hypot(b[0] - a[0], b[1] - a[1])
        dy = abs(wrap(b[2] - a[2]))
        if ds < 1e-6:
            if dy > 1e-6:
                return float('inf')
            continue
        worst = max(worst, dy / ds)
    return worst
