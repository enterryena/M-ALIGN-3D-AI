// MAS-kit AI yard — in-browser port of the desktop program's yard (app/yard_world.py, yard_actors.py, yard_ops.py,
// case_memory.py) so the whole simulation runs from a single HTML file, no Python / EXE needed.
// The alignment uses the original Matrix5 engine (window.MASKIT + MASKIT_HISTORY) with the kit + reach stacker
// camera / infrared fusion of the stand-alone demo. Optional: Ollama (local) for the main server's decisions.
(function (root) {
'use strict';
const PI = Math.PI;
// ============================================================== geometry (yard_world.py)
const L = 6.058, W = 2.438, H = 2.591;
const DOCK = W / 2 + 6.3, PRE = 5.0, R_MIN = 8.0, KAPPA = 1 / R_MIN, MARGIN = 0.3;
const BOUNDS = [-46, 46, -40, 34.5];
const BODY = [[0, -5, 2], [0, -2.5, 2], [0, 0, 2], [0, 2.5, 1.9]];
const FRONT = [[-2.4, DOCK, 1.6], [0, DOCK, 1.6], [2.4, DOCK, 1.6]];
const FOOT = BODY.concat(FRONT);
const SLOT_X = [-24, -14, -4, 6, 16];
const SLOTS = SLOT_X.map((x, i) => ['A' + (i + 1), x, 4, PI]).concat(SLOT_X.map((x, i) => ['B' + (i + 1), x, -14, 0]));
const BAYS = [['BAY1', -20, 32], ['BAY2', -4, 32], ['BAY3', 12, 32]];
const LANE_Z = 38, HOME = [34, -4, 0];
const POLES = [[-36, -26, .45], [28, -27, .45], [-38, 20, .45], [30, 21, .45]];
const BUILDINGS = [[40, -37.5, 0, 4, 2]];
const WALK_NODES = {SW: [-47.5, -41.6], W: [-47.5, -5], NW: [-47.5, 30], P1: [-24, -5], P2: [-14, -5], P3: [-4, -5], P4: [6, -5],
  P5: [16, -5], E: [47.5, -5], SE: [47.5, -41.6], S: [35.2, -41.6], OF: [35.2, -39.6]};
const WALK_EDGES = [['SW', 'W'], ['W', 'NW'], ['W', 'P1'], ['P1', 'P2'], ['P2', 'P3'], ['P3', 'P4'], ['P4', 'P5'], ['P5', 'E'],
  ['E', 'SE'], ['SE', 'S'], ['S', 'SW'], ['S', 'OF']];
const WORK_SPOTS = ['NW', 'OF', 'P1', 'P2', 'P3', 'P4', 'P5'];
const WALK_HALF_W = 1.1;
const CAR_LOOP = [[-52, -46.5], [52, -46.5], [52, 41.5], [-52, 41.5]];
const PARKING = [18, -50, PI / 2];

const rot = (yaw, lx, lz) => { const c = Math.cos(yaw), s = Math.sin(yaw); return [lx * c + lz * s, -lx * s + lz * c]; };
const wrap = a => ((a + PI) % (2 * PI) + 2 * PI) % (2 * PI) - PI;
function footprint(x, z, yaw, discs = FOOT){ const c = Math.cos(yaw), s = Math.sin(yaw);
  return discs.map(([lx, lz, r]) => [x + lx * c + lz * s, z - lx * s + lz * c, r]); }
const dockPose = (cx, cz, cy) => { const [dx, dz] = rot(cy, 0, -DOCK); return [cx + dx, cz + dz, cy]; };
const predockPose = (cx, cz, cy) => { const [dx, dz] = rot(cy, 0, -DOCK - PRE); return [cx + dx, cz + dz, cy]; };
const containerRect = (x, z, yaw) => ['rect', x, z, yaw, L / 2, W / 2];
function shapeDist(sh, px, pz){
  if (sh[0] === 'circle') return Math.max(0, Math.hypot(px - sh[1], pz - sh[2]) - sh[3]);
  const [, cx, cz, yaw, hx, hz] = sh, dx = px - cx, dz = pz - cz, c = Math.cos(yaw), s = Math.sin(yaw);
  const lx = dx * c - dz * s, lz = dx * s + dz * c;
  return Math.hypot(Math.max(Math.abs(lx) - hx, 0), Math.max(Math.abs(lz) - hz, 0));
}
function clearance(x, z, yaw, shapes, discs = FOOT){
  let best = 1e9;
  for (const [px, pz, r] of footprint(x, z, yaw, discs)) for (const sh of shapes) best = Math.min(best, shapeDist(sh, px, pz) - r);
  return best;
}
function inflate(sh, m){ return sh[0] === 'circle' ? ['circle', sh[1], sh[2], sh[3] + m] : sh.slice(0, 4).concat([sh[4] + m, sh[5] + m]); }

class DistanceField {
  constructor(shapes){
    this.R = .5; this.X0 = -50; this.X1 = 50; this.Z0 = -45; this.Z1 = 40;
    this.nx = Math.round((this.X1 - this.X0) / this.R) + 1; this.nz = Math.round((this.Z1 - this.Z0) / this.R) + 1;
    const d = new Float32Array(this.nx * this.nz);
    const [bx0, bx1, bz0, bz1] = BOUNDS;
    for (let i = 0; i < this.nx; i++){ const x = this.X0 + i * this.R;
      for (let k = 0; k < this.nz; k++){ const z = this.Z0 + k * this.R;
        let v = Math.max(0, Math.min(x - bx0, bx1 - x, z - bz0, bz1 - z));
        for (const sh of shapes){ const q = shapeDist(sh, x, z); if (q < v) v = q; }
        d[i * this.nz + k] = v; } }
    this.d = d;
  }
  at(x, z){ const i = Math.round((x - this.X0) / this.R), k = Math.round((z - this.Z0) / this.R);
    if (i < 0 || k < 0 || i >= this.nx || k >= this.nz) return 0; return this.d[i * this.nz + k]; }
  free(x, z, yaw, discs = FOOT, margin = MARGIN){ const c = Math.cos(yaw), s = Math.sin(yaw);
    for (const [lx, lz, r] of discs) if (this.at(x + lx * c + lz * s, z - lx * s + lz * c) < r + margin) return false; return true; }
}
function arc(x, z, yaw, k, s){
  if (Math.abs(k) < 1e-9) return [x + s * Math.sin(yaw), z + s * Math.cos(yaw), yaw];
  const y1 = yaw + k * s; return [x + (Math.cos(yaw) - Math.cos(y1)) / k, z + (Math.sin(y1) - Math.sin(yaw)) / k, y1];
}
class Heap { constructor(){ this.a = []; }
  push(v){ const a = this.a; a.push(v); let i = a.length - 1; while (i){ const p = (i - 1) >> 1; if (a[p][0] <= v[0]) break; a[i] = a[p]; i = p; } a[i] = v; }
  pop(){ const a = this.a, top = a[0], last = a.pop(); if (!a.length) return top; let i = 0;
    for (;;){ let l = 2 * i + 1, r = l + 1, m = i; if (l < a.length && a[l][0] < last[0]) m = l; if (r < a.length && a[r][0] < (m === i ? last[0] : a[m][0])) m = r;
      if (m === i) break; a[i] = a[m]; i = m; } a[i] = last; return top; }
  get size(){ return this.a.length; } }
function holonomic(df, gx, gz){
  const cell = 1, x0 = df.X0, z0 = df.Z0, nx = Math.round((df.X1 - x0) / cell) + 1, nz = Math.round((df.Z1 - z0) / cell) + 1;
  const dist = new Float64Array(nx * nz).fill(Infinity), ok = new Uint8Array(nx * nz);
  for (let i = 0; i < nx; i++) for (let k = 0; k < nz; k++) ok[i * nz + k] = df.at(x0 + i * cell, z0 + k * cell) >= 2 + MARGIN ? 1 : 0;
  const gi = Math.round((gx - x0) / cell), gk = Math.round((gz - z0) / cell), start = gi * nz + gk;
  dist[start] = 0; const pq = new Heap(); pq.push([0, gi, gk]);
  const st = [[1, 0, 1], [-1, 0, 1], [0, 1, 1], [0, -1, 1], [1, 1, 1.4142], [1, -1, 1.4142], [-1, 1, 1.4142], [-1, -1, 1.4142]];
  while (pq.size){ const [d, i, k] = pq.pop(); if (d > dist[i * nz + k]) continue;
    for (const [di, dk, c] of st){ const a = i + di, b = k + dk; if (a < 0 || b < 0 || a >= nx || b >= nz) continue;
      const idx = a * nz + b; if (!ok[idx] && idx !== start) continue; const nd = d + c * cell;
      if (nd < dist[idx]){ dist[idx] = nd; pq.push([nd, a, b]); } } }
  return (x, z) => { const i = Math.round((x - x0) / cell), k = Math.round((z - z0) / cell);
    return (i >= 0 && k >= 0 && i < nx && k < nz) ? dist[i * nz + k] : Infinity; };
}
function plan(df, start, goal, {tolPos = .7, tolYaw = 10 * PI / 180, maxExpand = 40000, step = 2.5, revCost = 1.8,
  switchCost = 6, weight = 2, XY = 1, NY = 36} = {}){
  const [sx, sz, sy] = start, [gx, gz, gy] = goal;
  if (!df.free(gx, gz, gy)) return null;
  const h2 = holonomic(df, gx, gz); if (h2(sx, sz) === Infinity) return null;
  const kap = [-KAPPA, -KAPPA / 2, 0, KAPPA / 2, KAPPA], sub = 5;
  const key = (x, z, y) => Math.round(x / XY) + ',' + Math.round(z / XY) + ',' + ((Math.round(wrap(y) / (2 * PI / NY)) % NY + NY) % NY);
  const heur = (x, z, y) => Math.max(h2(x, z), Math.hypot(gx - x, gz - z)) + 1.5 * Math.abs(wrap(gy - y));
  const nodes = [[sx, sz, sy, 0, -1, null]], best = new Map([[key(sx, sz, sy), 0]]), pq = new Heap();
  pq.push([weight * heur(sx, sz, sy), 0, 0]); let expanded = 0;
  while (pq.size && expanded < maxExpand){
    const [, g, ni] = pq.pop(); const [x, z, y, d0] = nodes[ni];
    if ((best.get(key(x, z, y)) ?? 1e18) < g - 1e-9) continue; expanded++;
    for (const dirn of [1, -1]) for (const kp of kap){
      const smp = []; let ok = true;
      for (let j = 1; j <= sub; j++){ const p = arc(x, z, y, kp, dirn * step * j / sub); if (!df.free(p[0], p[1], p[2])){ ok = false; break; } smp.push([p[0], p[1], p[2], dirn]); }
      if (!ok) continue;
      const [nx, nz, ny] = smp[smp.length - 1];
      const g2 = g + step * (dirn > 0 ? 1 : revCost) + (d0 && d0 !== dirn ? switchCost : 0) + .3 * step * Math.abs(kp) / KAPPA;
      let hit = -1; for (let j = 0; j < smp.length; j++) if (Math.hypot(smp[j][0] - gx, smp[j][1] - gz) < tolPos && Math.abs(wrap(smp[j][2] - gy)) < tolYaw){ hit = j; break; }
      if (hit >= 0){ nodes.push([smp[hit][0], smp[hit][1], smp[hit][2], dirn, ni, smp.slice(0, hit + 1)]); return collect(nodes, nodes.length - 1, start); }
      const k2 = key(nx, nz, ny);
      if (g2 < (best.get(k2) ?? 1e18)){ best.set(k2, g2); nodes.push([nx, nz, ny, dirn, ni, smp]); const hh = heur(nx, nz, ny);
        if (hh < Infinity) pq.push([g2 + weight * hh, g2, nodes.length - 1]); }
    }
  }
  return null;
}
function collect(nodes, ni, start){
  const chunks = []; while (ni >= 0){ const n = nodes[ni]; if (n[5]) chunks.push(n[5]); ni = n[4]; }
  const out = [[start[0], start[1], start[2], 0]]; for (const ch of chunks.reverse()) out.push(...ch);
  if (out.length > 1) out[0][3] = out[1][3]; return out;
}
function straight(p0, p1, dirn, ds = .5){
  const [x0, z0, y0] = p0, [x1, z1, y1] = p1, dist = Math.hypot(x1 - x0, z1 - z0), n = Math.max(2, Math.floor(dist / ds));
  const t0 = [Math.sin(y0) * dirn * dist, Math.cos(y0) * dirn * dist], t1 = [Math.sin(y1) * dirn * dist, Math.cos(y1) * dirn * dist], out = [];
  for (let i = 1; i <= n; i++){ const t = i / n;
    const h00 = 2 * t ** 3 - 3 * t ** 2 + 1, h10 = t ** 3 - 2 * t ** 2 + t, h01 = -2 * t ** 3 + 3 * t ** 2, h11 = t ** 3 - t ** 2;
    const x = h00 * x0 + h10 * t0[0] + h01 * x1 + h11 * t1[0], z = h00 * z0 + h10 * t0[1] + h01 * z1 + h11 * t1[1];
    const dx = (6 * t * t - 6 * t) * x0 + (3 * t * t - 4 * t + 1) * t0[0] + (-6 * t * t + 6 * t) * x1 + (3 * t * t - 2 * t) * t1[0];
    const dz = (6 * t * t - 6 * t) * z0 + (3 * t * t - 4 * t + 1) * t0[1] + (-6 * t * t + 6 * t) * z1 + (3 * t * t - 2 * t) * t1[1];
    const yaw = dirn > 0 ? Math.atan2(dx, dz) : Math.atan2(-dx, -dz); out.push([x, z, i < n ? yaw : y1, dirn]); }
  return out;
}
const pathLen = p => { let s = 0; for (let i = 1; i < p.length; i++) s += Math.hypot(p[i][0] - p[i - 1][0], p[i][1] - p[i - 1][1]); return s; };
const cumOf = p => { const c = [0]; for (let i = 1; i < p.length; i++) c.push(c[i - 1] + Math.hypot(p[i][0] - p[i - 1][0], p[i][1] - p[i - 1][1])); return c; };
function interp(path, cum, s){
  if (s <= 0) return path[0].slice(0, 3); if (s >= cum[cum.length - 1]) return path[path.length - 1].slice(0, 3);
  let lo = 0, hi = cum.length - 1; while (hi - lo > 1){ const m = (lo + hi) >> 1; if (cum[m] <= s) lo = m; else hi = m; }
  const a = path[lo], b = path[hi], f = (s - cum[lo]) / Math.max(1e-9, cum[hi] - cum[lo]), dy = wrap(b[2] - a[2]);
  return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + dy * f];
}
function idxOf(cum, s){ let lo = 0, hi = cum.length - 1; while (hi - lo > 1){ const m = (lo + hi) >> 1; if (cum[m] <= s) lo = m; else hi = m; } return lo; }
function segDist(px, pz, a, b){ const dx = b[0] - a[0], dz = b[1] - a[1], L2 = dx * dx + dz * dz;
  const t = L2 ? Math.max(0, Math.min(1, ((px - a[0]) * dx + (pz - a[1]) * dz) / L2)) : 0; return Math.hypot(px - a[0] - t * dx, pz - a[1] - t * dz); }
const onWalkway = (x, z) => WALK_EDGES.some(([a, b]) => segDist(x, z, WALK_NODES[a], WALK_NODES[b]) <= WALK_HALF_W + .2);
function walkRoute(a, b){
  const adj = {}; for (const [u, v] of WALK_EDGES){ const d = Math.hypot(WALK_NODES[u][0] - WALK_NODES[v][0], WALK_NODES[u][1] - WALK_NODES[v][1]);
    (adj[u] = adj[u] || []).push([v, d]); (adj[v] = adj[v] || []).push([u, d]); }
  const dist = {[a]: 0}, prev = {}, done = new Set();
  while (true){ let u = null, du = Infinity; for (const k in dist) if (!done.has(k) && dist[k] < du){ du = dist[k]; u = k; }
    if (u === null || u === b) break; done.add(u);
    for (const [v, w] of adj[u]) if (du + w < (dist[v] ?? Infinity)){ dist[v] = du + w; prev[v] = u; } }
  const out = [b]; while (out[out.length - 1] !== a) out.push(prev[out[out.length - 1]]); return out.reverse();
}
const nearestNode = (x, z) => Object.keys(WALK_NODES).reduce((b, k) => Math.hypot(WALK_NODES[k][0] - x, WALK_NODES[k][1] - z) < Math.hypot(WALK_NODES[b][0] - x, WALK_NODES[b][1] - z) ? k : b);
function carLoopPath(step = .5){
  const pts = [], n = CAR_LOOP.length, r = 6;
  for (let i = 0; i < n; i++){ const p0 = CAR_LOOP[(i + n - 1) % n], p1 = CAR_LOOP[i], p2 = CAR_LOOP[(i + 1) % n];
    let di = [p1[0] - p0[0], p1[1] - p0[1]], li = Math.hypot(...di); di = [di[0] / li, di[1] / li];
    let dO = [p2[0] - p1[0], p2[1] - p1[1]], lo = Math.hypot(...dO); dO = [dO[0] / lo, dO[1] / lo];
    const a = [p1[0] - di[0] * r, p1[1] - di[1] * r], b = [p1[0] + dO[0] * r, p1[1] + dO[1] * r];
    const ya = Math.atan2(di[0], di[1]), yb = Math.atan2(dO[0], dO[1]);
    pts.push([a[0], a[1], ya]); for (const p of straight([a[0], a[1], ya], [b[0], b[1], yb], 1, step)) pts.push(p.slice(0, 3)); }
  const out = [];
  for (let i = 0; i < pts.length; i++){ const p = pts[i], q = pts[(i + 1) % pts.length]; out.push(p);
    const Lq = Math.hypot(q[0] - p[0], q[1] - p[1]), k = Math.floor(Lq / step);
    for (let j = 1; j < k; j++){ const t = j / k; out.push([p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t, p[2]]); } }
  return out;
}
const carPoints = (x, z, yaw) => { const out = []; for (const lx of [-.95, 0, .95]) for (const lz of [-2.2, 0, 2.2]){ const [dx, dz] = rot(yaw, lx, lz); out.push([x + dx, z + dz]); } return out; };
const bodyGap = (sh, x, z, yaw) => Math.min(...carPoints(x, z, yaw).map(([px, pz]) => shapeDist(sh, px, pz)));
const TRUCK_BODY_GAP = .6;   // car body <-> truck body; the parallel lanes (3.5 m apart) leave ~1.2 m: no conflict
const truckShape = (x, z, yaw) => ['rect', x + 1.5 * Math.sin(yaw), z + 1.5 * Math.cos(yaw), yaw, 1.3, 6.5];
const carShape = (x, z, yaw) => ['rect', x, z, yaw, .95, 2.2];

// ============================================================== case memory (case_memory.py)
const PLANS = {STD: {K: .8, use_machine: true, max_rounds: 6, ko: '기본 (기존 센서 함께, K 0.8, 6회)'},
  KIT: {K: .8, use_machine: false, max_rounds: 6, ko: '키트 우선 (기존 센서 제외, K 0.8, 6회)'},
  SOFT: {K: .6, use_machine: true, max_rounds: 8, ko: '완만 보정 (K 0.6, 8회)'},
  KIT_SOFT: {K: .6, use_machine: false, max_rounds: 8, ko: '키트 우선 + 완만 (K 0.6, 8회)'}};
const REASON_KO = {disagree: '키트-기존 센서 불일치', not_converged: '수렴 부족(하강 조건 미달)', false_lower: '결합 실패(오하강)',
  low_reliability: '센서 신뢰도 부족', far: '초기 오차 큼', aborted: '중단', gate_hold: '사례 기반 하강 보류'};
const RETRY_PLANS = {disagree: ['KIT', 'KIT_SOFT'], not_converged: ['SOFT', 'KIT_SOFT', 'KIT'], false_lower: ['KIT_SOFT', 'SOFT'],
  low_reliability: Object.keys(PLANS), far: Object.keys(PLANS), aborted: Object.keys(PLANS), gate_hold: ['KIT_SOFT', 'SOFT', 'KIT']};
const REAPPROACH = ['false_lower', 'low_reliability', 'far', 'gate_hold'];
const GATE_K = 60, GATE_MIN = 60, GATE_TH = .3;
const FEATURE_W = {machine: 3, disagree: 2, kit: 1.5, env: 1.5, hardware: 1, dist: 1, prev: 1};
class CaseMemory {
  constructor(storeKey){ this.key = storeKey; this.cases = []; this.lower = [];
    try { this.cases = JSON.parse(localStorage.getItem(storeKey) || '[]'); this.lower = JSON.parse(localStorage.getItem(storeKey + '_lower') || '[]'); } catch (e){ this.cases = []; this.lower = []; } }
  save(){ try { localStorage.setItem(this.key, JSON.stringify(this.cases.slice(-2000))); localStorage.setItem(this.key + '_lower', JSON.stringify(this.lower.slice(-20000))); } catch (e){} }
  // lowering gate: what the kit saw just before each past lowering (never the truth) -> did it lock
  addLower(hw, f, ok){ if (!f) return; this.lower.push([hw, f[0], f[1], ok ? 1 : 0]); this.save(); }
  lowerP(hw, f){ const pts = this.lower.filter(p => p[0] === hw); if (!f || pts.length < Math.max(GATE_K, GATE_MIN)) return null;
    pts.sort((a, b) => ((a[1] - f[0]) ** 2 + (a[2] - f[1]) ** 2) - ((b[1] - f[0]) ** 2 + (b[2] - f[1]) ** 2));
    return {p: pts.slice(0, GATE_K).reduce((s, q) => s + q[3], 0) / GATE_K, n: pts.length, th: GATE_TH}; }
  add(f, plan, outcome, reason){ this.cases.push({f, plan, ok: outcome === 'success', outcome, reason: outcome === 'success' ? null : reason}); this.save(); }
  static dist(a, b){ let s = 0; for (const [k, w] of Object.entries(FEATURE_W)){
      if (k === 'prev'){ if ((a.prev || b.prev) && a.prev !== b.prev) s += w; } else if (a[k] !== b[k]) s += w; } return s; }
  static safe(f){ const kitOk = f.kit === 'good' || f.disagree || f.machine !== 'valid';
    return Object.keys(PLANS).filter(p => PLANS[p].use_machine || kitOk); }
  score(f, cands){ const sw = {}, ss = {}, sf = {}; for (const p of cands){ sw[p] = ss[p] = sf[p] = 0; } let nsim = 0;
    for (const c of this.cases){ if (!(c.plan in sw)) continue; const w = Math.exp(-CaseMemory.dist(f, c.f)); if (w < .02) continue;
      if (w > .3) nsim++; sw[c.plan] += w; ss[c.plan] += w * (c.ok ? 1 : 0); sf[c.plan] += w * (c.outcome === 'false_lower' ? 1 : 0); }
    const total = Object.values(sw).reduce((a, b) => a + b, 0), out = {};
    for (const p of cands){ const prior = p === 'STD' ? .65 : .5, rate = (ss[p] + prior) / (sw[p] + 1), fl = (sf[p] + .01) / (sw[p] + 1);
      out[p] = {rate, fl, ev: sw[p], score: rate - 4 * fl + .12 * Math.sqrt(Math.log(total + 2) / (1 + sw[p]))}; }
    return [out, nsim]; }
  choose(f, tried = [], reason = null){
    const safe = CaseMemory.safe(f), cands = safe.filter(p => !tried.includes(p)); if (!cands.length) cands.push(...safe);
    const prefer = new Set(reason ? RETRY_PLANS[reason] || [] : []); const [sc, nsim] = this.score(f, cands);
    let best = cands[0]; for (const p of cands){ const v = sc[p].score + (prefer.has(p) ? .08 : 0), b = sc[best].score + (prefer.has(best) ? .08 : 0);
      if (v > b || (v === b && p === 'STD')) best = p; }
    const why = (reason ? `재시도(${REASON_KO[reason] || reason}) → 우선 ${cands.filter(p => prefer.has(p)).join('/') || '-'}, ` : '') +
      `유사 사례 ${nsim}건 · ${best} 예상 성공률 ${Math.round(100 * sc[best].rate)}% · 오하강 위험 ${(100 * sc[best].fl).toFixed(1)}%`;
    return Object.assign({plan: best, similar: nsim, expected: sc[best].rate, why, reapproach: REAPPROACH.includes(reason)}, PLANS[best]); }
  summary(){ const c = this.cases, r = c.slice(-50), by = {};
    for (const x of c){ const b = by[x.plan] = by[x.plan] || {n: 0, ok: 0, false_lower: 0}; b.n++; b.ok += x.ok; b.false_lower += x.outcome === 'false_lower'; }
    return {cases: c.length, success: c.filter(x => x.ok).length, recent_rate: r.length ? r.filter(x => x.ok).length / r.length : null, by_plan: by,
      gate: {lowerings: this.lower.length, locked: this.lower.reduce((s, q) => s + q[3], 0), active_from: GATE_MIN, threshold: GATE_TH}}; }
  clear(){ this.cases = []; this.lower = []; this.save(); }
}

// ============================================================== alignment (kit + camera + infrared)
const ENVS = {
  clear: {name: '맑은 낮', cam: {sd: 1.5, q: .95, drop: 0, out: 0}, ir: {sd: 2.5, q: .9, drop: 0, out: 0}},
  night: {name: '야간', cam: {sd: 6, q: .4, drop: .2, out: .05}, ir: {sd: 2.5, q: .9, drop: 0, out: 0}},
  rain: {name: '비', cam: {sd: 4, q: .55, drop: .1, out: .05}, ir: {sd: 7, q: .35, drop: .25, out: .05}},
  fog: {name: '짙은 안개', cam: {sd: 12, q: .15, drop: .5, out: .1}, ir: {sd: 9, q: .25, drop: .4, out: .05}},
  glare: {name: '강한 역광', cam: {sd: 2, q: .8, drop: .15, out: .35}, ir: {sd: 5, q: .55, drop: .1, out: 0}}};
const PRESETS = {ideal: {modelBias: 0, noise: .1, gapSd: 0, tiltSd: 0, tolC: .7, act: .2},
  target: {modelBias: .25, noise: .2, gapSd: .3, tiltSd: .05, tolC: .7, act: .2},
  base: {modelBias: .5, noise: .3, gapSd: 1, tiltSd: .2, tolC: 3, act: .5}};
const BASEP = {R0: 30, theta0: .1, K: .8, iters: 6, tolMM: 5, fitGap: true, mirror: true, useHead: [1, 1, 1, 1, 1], searchMM: 36, stopMM: 1};
const CMM = [[-2926.5, 1129.5], [2926.5, 1129.5], [2926.5, -1129.5], [-2926.5, -1129.5]];
// ISO 668-based corner deviations (20 ft) and twistlock engagement (same as maskit_engine.iso_tol / capture_ok)
function isoTol(zs, cast){ const clip = (v, lo, hi) => Math.max(lo, Math.min(hi, v)); const dS = clip(-1 + 8 / 6 * zs[0], -5, 3), dP = clip(-1 + 8 / 6 * zs[1], -5, 3), sh = clip(13 / 3 * zs[2], -13, 13) / 1.87;
  return CMM.map(([X, Y], k) => { const gx = X > 0 ? 1 : -1, gy = Y > 0 ? 1 : -1; return [gx * dS / 2 + gy * sh / 2 + .5 * cast[k][0], gy * dP / 2 + .5 * cast[k][1]]; }); }
function captureOk(errs, cap){ const [cx, cy] = cap === 'head' ? [10, 3.8] : cap === 'float10' ? [10, 10] : [5, 5]; return errs.every(e => Math.abs(e[0]) <= cx && Math.abs(e[1]) <= cy); }
const PRIO = {cam: 1, ir: .8, kit: .5}, PRIO_FINE = {cam: .5, ir: .4, kit: 1.2};
function fuse(r, fine){
  const names = ['cam', 'ir', 'kit'], w = {}, note = {}, PR = fine ? PRIO_FINE : PRIO;
  for (const n of names){ w[n] = r[n] ? PR[n] * r[n].q : 0; note[n] = r[n] ? (r[n].q >= .7 ? '정상' : '저하') : (n === 'kit' ? '미사용' : '측정 불가'); }
  let disagree = false;
  for (const n of names){ if (!r[n] || !w[n]) continue; const others = names.filter(m => m !== n && r[m] && w[m] > 0); if (!others.length) continue;
    const Wt = others.reduce((s, m) => s + w[m], 0), rx = others.reduce((s, m) => s + w[m] * r[m].x, 0) / Wt, ry = others.reduce((s, m) => s + w[m] * r[m].y, 0) / Wt;
    const d = Math.max(Math.abs(r[n].x - rx), Math.abs(r[n].y - ry)), lim = 6 + .3 * Math.max(Math.abs(rx), Math.abs(ry));
    const agree = others.length >= 2 ? Math.max(Math.abs(r[others[0]].x - r[others[1]].x), Math.abs(r[others[0]].y - r[others[1]].y)) <= lim : PR[others[0]] * r[others[0]].q > w[n];
    if (d > lim && agree){ w[n] *= .1; note[n] = '불일치 → 의존도 낮춤'; disagree = true; } }
  const tot = names.reduce((s, n) => s + w[n], 0), pct = {}; for (const n of names) pct[n] = tot ? w[n] / tot : 0;
  if (tot < .25) return {ok: false, pct, note, tot, disagree};
  const f = ['x', 'y', 'th'].map(k => names.reduce((s, n) => s + (w[n] ? w[n] * r[n][k] : 0), 0) / tot);
  return {ok: true, fx: f[0], fy: f[1], fth: f[2], pct, note, tot, disagree};
}
function rigid(est){ let sx = 0, sy = 0, num = 0, den = 0; for (const v of est){ sx += v[0]; sy += v[1]; } sx /= 4; sy /= 4;
  est.forEach((v, k) => { const [X, Y] = CMM[k]; num += X * (v[1] - sy) - Y * (v[0] - sx); den += X * X + Y * Y; }); return [sx, sy, num / den]; }

// ============================================================== the yard
const ROAD_V = 4, CAR_ACC = 1.5, CAR_DEC = 3, CAR_GAP_MACHINE = 6, CAR_GAP_PERSON = 5, YIELD_M = 5;
const SENSOR_RANGE = 16, SAFE_GAP = .6, PERSON_GAP = 3, TRUCK_CLEAR = .8, V_FWD = 3, V_REV = 1.5, ACC = .8, DEC = 1.5;
const STOP_BEFORE = {worker: 3, car: 2.5, truck: 2.5, parked_car: 1.5, cone: 1.5, pallet: 1.5};
const MANUAL_S = 40, HOLD_S = 90, TRAVEL_H = H + 2.6, CARRY_LIFT = 2.2, TRUCK_BED = 1.5, KIT_GAP = .02, HOIST_V = .45, CREW_S = 15, CAPACITY = SLOTS.length;
const COLORS = ['#d2601a', '#2a6fb0', '#2f8a4a', '#9b2d2d', '#7a7a7a', '#c8a024', '#5b3f8c', '#1f7a7a'];

class Yard {
  constructor(o = {}){
    this.E = root.MASKIT; this.M = this.E.buildModel(root.MASKIT_DATA); this.HIST = root.MASKIT_HISTORY;
    this.seed = o.seed != null && o.seed !== '' ? Number(o.seed) >>> 0 : (Math.random() * 4294967296) >>> 0 || 1;
    this.rnd = this.E.rng(this.seed); this.speed = o.speed || 1; this.controller = o.controller || 'autopilot';
    this.aiUrl = o.ai_url || 'http://127.0.0.1:11434'; this.aiModel = o.ai_model || 'qwen2.5:7b'; this.aiFail = 0;
    this.hardware = o.hardware || 'target'; this.env = o.env || 'clear'; this.vary = !!o.vary; this.machine = o.machine || 'normal'; this.tolModel = o.tol_model || 'orig'; this.capture = o.capture || 'pm5'; this.people = o.people !== false; this.debrisOn = o.debris !== false;
    this.memory = o.memory; this.t = 0; this.running = false; this.logLines = []; this.waiters = [];
    this.stats = {inbound: 0, outbound: 0, user: 0, horns: 0, detours: 0, clears: 0, waits: 0, contacts: 0, align_fail: 0,
      guard_refusals: 0, ai_decisions: 0, rule_decisions: 0, retries: 0, retry_success: 0, cases_added: 0, manual_locks: 0};
    this.metrics = {worker_min_gap_lawful: 1e9, worker_steps: 0, worker_on_walkway: 0};
    this.nCont = 0; this.containers = {}; this.slots = {}; for (const [id, x, z, yaw] of SLOTS) this.slots[id] = {id, x, z, yaw, holder: null, reserved: null};
    this.bays = {}; for (const [id, x, z] of BAYS) this.bays[id] = {id, x, z, truck: null};
    this.trucks = {}; ['T1', 'T2', 'T3'].forEach((id, i) => { this.trucks[id] = {id, state: 'away', bay: null, mode: null, container: null,
      x: -90, z: LANE_Z, yaw: PI / 2, path: null, s: 0, cum: null, due: 2 + 18 * i, parked_t: null, served: false}; });
    this.st = {x: HOME[0], z: HOME[1], yaw: HOME[2], v: 0, dir: 1, mode: 'idle', path: null, cum: null, s: 0, h: TRAVEL_H, h_target: TRAVEL_H,
      carry: null, horn_t: -9, pause: 0, blocked_by: null, block_dist: null, ignore: new Set()};
    this.debris = []; this.nDebris = 0; this.nextDebris = 35; this.temp = []; this.workers = []; this.car = null;
    const ids = Object.keys(this.slots); for (let i = 0; i < 7; i++){ const k = Math.floor(this.rnd() * ids.length), sid = ids.splice(k, 1)[0], s = this.slots[sid];
      const c = this.newContainer(s.x, s.z, s.yaw, 0); Object.assign(c, {where: 'slot', slot: sid, since: -60 - 540 * this.rnd()}); s.holder = c.id; }
    if (this.people){ ['W', 'P3', 'OF'].forEach((n, i) => this.workers.push(this.newWorker('W' + (i + 1), n))); this.car = this.newCar(); }
    this.task = null; this.requests = []; this.nReq = 0; this.nTask = 0; this.alert = null; this.align = null; this.decision = null; this.planInfo = null;
  }
  log(who, text){ this.logLines.push({t: Math.round(this.t * 10) / 10, at: Date.now() + this.logLines.length, who, text}); if (this.logLines.length > 220) this.logLines.splice(0, 20); }
  newContainer(x, z, yaw, y){ this.nCont++; const id = 'C' + String(this.nCont).padStart(2, '0');
    // the box's own corner-casting tolerance (standard normal, scaled by tolC): fixed for its life, so a retry meets the same box
    const zr = this.E.rng(((this.seed * 7919 + this.nCont) >>> 0) || 1), tolZ = [0, 1, 2, 3].map(() => [zr.n(), zr.n()]), isoZ = [zr.n(), zr.n(), zr.n()];
    return this.containers[id] = {id, color: COLORS[(this.nCont - 1) % COLORS.length], x, z, yaw, y, where: null, slot: null, truck: null, since: this.t, tolZ, isoZ}; }
  gateKey(){ return this.hardware + (this.tolModel === 'iso' ? '|iso' : '') + (this.capture !== 'pm5' ? '|' + this.capture : ''); }
  // ---------------------------------------------------------------- time
  waitSim(sec){ const end = this.t + sec; return this.waitUntil(() => this.t >= end); }
  waitUntil(pred, timeout = null){ const end = timeout == null ? Infinity : this.t + timeout;
    return new Promise(res => { if (!this.running || pred()) return res(this.running); this.waiters.push({pred, end, res}); }); }
  _wake(){ const keep = []; for (const w of this.waiters){ if (!this.running){ w.res(false); continue; }
      let ok = false; try { ok = w.pred(); } catch (e){ ok = true; } if (ok) w.res(true); else if (this.t >= w.end) w.res(false); else keep.push(w); } this.waiters = keep; }
  start(){ this.running = true;
    this.log('서버', `야드 운영 시작 · 제어: ${this.controller === 'ai' ? 'Ollama AI (' + this.aiModel + ')' : '규칙 자동조종'} · 컨테이너 ${Object.values(this.slots).filter(s => s.holder).length}개 / 지정 구역 ${CAPACITY}칸 · 트럭 3대 순환 · 환경 ${ENVS[this.env].name}`);
    this.control(); return this; }
  stop(){ this.running = false; this._wake(); }
  advance(realDt){ if (!this.running) return; let n = Math.min(60, Math.round(realDt * this.speed / .1)); while (n-- > 0) this.step(.1); this._wake(); }
  // ---------------------------------------------------------------- shapes
  staticShapes(exclude = [], temp = true){
    const sh = POLES.map(([x, z, r]) => ['circle', x, z, r]).concat(BUILDINGS.map(b => ['rect', ...b]));
    for (const c of Object.values(this.containers)) if (c.where === 'slot' && !exclude.includes(c.id)) sh.push(containerRect(c.x, c.z, c.yaw));
    for (const b of Object.values(this.bays)){ const tid = b.truck; if (!tid || exclude.includes(tid)) continue;
      sh.push(truckShape(b.x, b.z, PI / 2), containerRect(b.x, b.z, 0)); const t = this.trucks[tid];
      if (t.state === 'arriving' || t.state === 'leaving'){ sh.push(this.swingZone(b, t.state)); for (const r of this.truckSweep(t)) sh.push(inflate(r, TRUCK_CLEAR)); } }
    for (const d of this.debris) if (d.known) sh.push(inflate(d.shape, SAFE_GAP));
    if (this.car && this.car.state === 'parked') sh.push(inflate(carShape(this.car.x, this.car.z, this.car.yaw), SAFE_GAP));
    if (temp) sh.push(...this.temp); return sh;
  }
  swingZone(bay, state){ return ['rect', bay.x + (state === 'arriving' ? -5.5 : 5.5), 33.25, 0, 14.5, 4.25]; }
  truckSweep(t, ahead = 32, step = 2){ const out = [truckShape(t.x, t.z, t.yaw)]; const end = t.cum[t.cum.length - 1];
    for (let d = step; d <= ahead && t.s + d <= end; d += step) out.push(truckShape(...interp(t.path, t.cum, t.s + d))); return out; }
  walkPoint(){ for (let i = 0; i < 200; i++){ const x = -40 + 80 * this.rnd(), z = -34 + 60 * this.rnd();
      if (this.staticShapes([], false).every(s => shapeDist(s, x, z) > 2) && clearance(this.st.x, this.st.z, this.st.yaw, [['circle', x, z, .4]]) > 3) return [x, z]; } return [0, 20]; }
  dynamic(){ const out = [];
    for (const w of this.workers) out.push([{id: w.id, kind: 'worker', moving: true, name: '작업자 ' + w.id + (w.jay ? ' (무단횡단)' : '')}, ['circle', w.x, w.z, .4]]);
    if (this.car){ const c = this.car; if (c.state === 'parked') out.push([{id: c.id, kind: 'parked_car', moving: false, known: true, name: '주차 차량 V1'}, carShape(c.x, c.z, c.yaw)]);
      else out.push([{id: c.id, kind: 'car', moving: true, name: '서비스 차량 V1' + (c.state === 'road_stop' ? ' (도로 정차)' : '')}, carShape(c.x, c.z, c.yaw)]); }
    for (const d of this.debris) out.push([{id: d.id, kind: d.kind, moving: false, known: d.known, name: (d.kind === 'cone' ? '라바콘 ' : '팔레트 ') + d.id}, d.shape]);
    for (const t of Object.values(this.trucks)) if (t.state === 'arriving' || t.state === 'leaving'){
      const info = {id: t.id, kind: 'truck', moving: true, name: '트럭 ' + t.id}; out.push([info, truckShape(t.x, t.z, t.yaw)]);
      const sw = this.truckSweep(t); if (clearance(this.st.x, this.st.z, this.st.yaw, sw) > TRUCK_CLEAR)
        for (const s of sw) out.push([Object.assign({}, info, {name: '트럭 ' + t.id + ' 진입·출차 경로', zone: true}), s]); }
    return out; }
  // ---------------------------------------------------------------- world step
  step(dt){ this.t += dt; this.stepTrucks(dt); this.stepWorkers(dt); this.stepCar(dt); this.stepDebris(); this.stepStacker(dt);
    const st = this.st; st.h += Math.max(-HOIST_V * dt, Math.min(HOIST_V * dt, st.h_target - st.h)); }
  setTruckPath(t){ t.cum = cumOf(t.path); t.s = 0; [t.x, t.z, t.yaw] = t.path[0]; }
  stepTrucks(dt){
    for (const t of Object.values(this.trucks)){
      if (t.state === 'away' && this.t >= t.due){
        const bay = Object.values(this.bays).find(b => !b.truck); if (!bay){ t.due = this.t + 5; continue; }
        const occ = Object.values(this.slots).filter(s => s.holder).length, tr = Object.values(this.trucks);
        const incoming = tr.filter(x => ['arriving', 'parked'].includes(x.state) && x.mode === 'inbound').length;
        const outgoing = tr.filter(x => ['arriving', 'parked'].includes(x.state) && x.mode === 'outbound').length;
        Object.assign(t, {state: 'arriving', bay: bay.id, mode: occ + incoming - outgoing >= CAPACITY ? 'outbound' : 'inbound', served: false}); bay.truck = t.id;
        t.path = [[-90, LANE_Z, PI / 2, 1], [bay.x - 18, LANE_Z, PI / 2, 1]].concat(straight([bay.x - 18, LANE_Z, PI / 2], [bay.x, bay.z, PI / 2], 1, 1)); this.setTruckPath(t);
        if (t.mode === 'inbound'){ const c = this.newContainer(t.x, t.z, 0, TRUCK_BED); Object.assign(c, {where: 'truck', truck: t.id}); t.container = c.id;
          this.log('트럭', `${t.id} 입고 트럭 도착 중 (${c.id} 적재) → ${bay.id}`); }
        else { t.container = null; this.log('트럭', `${t.id} 반출용 빈 트럭 도착 중 → ${bay.id} (야드 ${occ}/${CAPACITY}칸 참)`); }
      } else if (t.state === 'arriving' || t.state === 'leaving') this.moveTruck(t, dt);
      else if (t.state === 'parked' && t.served){
        const gap = clearance(this.st.x, this.st.z, this.st.yaw, [truckShape(t.x, t.z, t.yaw)]);
        if (gap > 1.5 && this.st.mode !== 'aligning'){ const bay = this.bays[t.bay]; t.state = 'leaving';
          t.path = [[t.x, t.z, t.yaw, 1]].concat(straight([bay.x, bay.z, PI / 2], [bay.x + 18, LANE_Z, PI / 2], 1, 1), [[95, LANE_Z, PI / 2, 1]]); this.setTruckPath(t);
          this.log('트럭', `${t.id} 출발 (${t.container ? '컨테이너 ' + t.container + ' 싣고 반출' : '빈 차'})`); } } }
  }
  moveTruck(t, dt){
    const bay = this.bays[t.bay], st = this.st;
    if (t.state === 'arriving' && t.x < bay.x - 18.5 && t.x > bay.x - 30 && clearance(st.x, st.z, st.yaw, this.truckSweep(t)) < .5) return;
    if (t.state === 'leaving' && t.s < 1 && clearance(st.x, st.z, st.yaw, this.truckSweep(t)) < .5) return;
    const fx = Math.sin(t.yaw), fz = Math.cos(t.yaw);
    for (const o of Object.values(this.trucks)) if (o !== t && (o.state === 'arriving' || o.state === 'leaving')){
      const ahead = (o.x - t.x) * fx + (o.z - t.z) * fz, lat = Math.abs((o.x - t.x) * fz - (o.z - t.z) * fx); if (ahead > 0 && ahead < 17 && lat < 3) return; }
    const sh = truckShape(...interp(t.path, t.cum, t.s + 6));
    let gap = clearance(st.x, st.z, st.yaw, [sh]); for (const w of this.workers) gap = Math.min(gap, shapeDist(sh, w.x, w.z) - .4);
    if (this.car){ const cg = bodyGap(sh, this.car.x, this.car.z, this.car.yaw);   // trucks have priority; emergency stop only
      if (cg < .3){ t.carHold = (t.carHold || 0) + dt; if (t.carHold < 10 || cg < .1 || this.car.v > .05) return; dt *= .25; } else t.carHold = 0; }
    const still = st.v < .05, need = still ? 1 : 2;
    if (gap < need){ t.hold = (t.hold || 0) + dt; if (t.hold < 8 || gap < (still ? .5 : .8)) return; dt *= .25; }
    else { t.hold = 0; if (gap < 3) dt *= .4; }
    t.s = Math.min(t.cum[t.cum.length - 1], t.s + 5 * dt); [t.x, t.z, t.yaw] = interp(t.path, t.cum, t.s);
    const c = this.containers[t.container]; if (c && c.where === 'truck') Object.assign(c, {x: t.x, z: t.z, yaw: t.yaw - PI / 2, y: TRUCK_BED});
    if (t.s >= t.cum[t.cum.length - 1] - 1e-6){
      if (t.state === 'arriving'){ t.state = 'parked'; t.parked_t = this.t; t.yaw = PI / 2; this.log('트럭', `${t.id} ${t.bay} 정차 완료 (${t.mode === 'inbound' ? '하역 대기' : '상차 대기'})`); }
      else { if (t.container) delete this.containers[t.container]; bay.truck = null;
        Object.assign(t, {state: 'away', bay: null, container: null, due: this.t + 15 + 20 * this.rnd(), served: false}); } }
  }
  pathPointAhead(ahead){ const st = this.st; if (st.mode !== 'driving' || !st.path || st.cum[st.cum.length - 1] - st.s < ahead + 2) return null; return interp(st.path, st.cum, st.s + ahead); }
  stepDebris(){
    for (const d of this.debris.slice()) if (d.clear_at != null && this.t >= d.clear_at){ this.debris.splice(this.debris.indexOf(d), 1); this.log('현장', `작업반이 ${d.id} 치움`); }
    if (!this.debrisOn || this.t < this.nextDebris || this.debris.length >= 3) return;
    this.nextDebris = this.t + 55 + 45 * this.rnd();
    const p = this.pathPointAhead(20 + 6 * this.rnd()), [x, z] = p ? [p[0], p[1]] : this.walkPoint();
    if (clearance(this.st.x, this.st.z, this.st.yaw, [['circle', x, z, 1]]) < 3) return;
    this.nDebris++; const kind = this.rnd() < .5 ? 'cone' : 'pallet', yaw = PI * this.rnd();
    this.debris.push({id: 'O' + this.nDebris, kind, x, z, yaw, shape: kind === 'cone' ? ['circle', x, z, .45] : ['rect', x, z, yaw, .6, .5], known: false, clear_at: null});
  }
  // ---------------------------------------------------------------- people (yard_actors.py)
  stackerSweep(ahead = 25){ const st = this.st, d = footprint(st.x, st.z, st.yaw);
    if ((st.mode === 'driving' || st.mode === 'blocked') && st.path){ const end = st.cum[st.cum.length - 1];
      for (let a = 1; a <= ahead && st.s + a <= end; a++){ const p = interp(st.path, st.cum, st.s + a); d.push(...footprint(p[0], p[1], p[2])); } }
    return d; }
  newWorker(id, node){ const [x, z] = WALK_NODES[node];
    return {id, x, z, yaw: 0, route: [], v: 1.2, pause: 1 + 5 * this.rnd(), state: 'work', node, jay: false, stubborn: false, frozen: 0, yield_until: -1}; }
  planTrip(w){ const here = w.node || nearestNode(w.x, w.z), spots = WORK_SPOTS.filter(k => k !== here), dst = spots[Math.floor(this.rnd() * spots.length)];
    if (this.rnd() < .1 && (this.st.mode === 'driving' || this.st.mode === 'blocked') && this.t > w.yield_until){
      const p = this.pathPointAhead(10 + 6 * this.rnd());
      if (p){ Object.assign(w, {route: [[p[0], p[1]], WALK_NODES[dst]], node: dst, jay: true, v: 1.3, state: 'jay', stubborn: this.rnd() < .35}); return; } }
    let route = walkRoute(here, dst).map(n => WALK_NODES[n]); if (Math.hypot(route[0][0] - w.x, route[0][1] - w.z) < .3) route = route.slice(1);
    Object.assign(w, {route, node: dst, jay: false, v: 1.1 + .3 * this.rnd(), state: 'walk', stubborn: false}); }
  discGap(x, z, r, sweep){ let g = 1e9; for (const [dx, dz, dr] of sweep) g = Math.min(g, Math.hypot(x - dx, z - dz) - dr - r); return g; }
  evade(w, sweep){
    const here = this.discGap(w.x, w.z, .4, sweep), gapN = k => this.discGap(WALK_NODES[k][0], WALK_NODES[k][1], .4, sweep);
    if (onWalkway(w.x, w.z)){
      let e = WALK_EDGES[0], bd = 1e9; for (const ed of WALK_EDGES){ const d = segDist(w.x, w.z, WALK_NODES[ed[0]], WALK_NODES[ed[1]]); if (d < bd){ bd = d; e = ed; } }
      const pathGap = k => { const [tx, tz] = WALK_NODES[k]; let g = 1e9; for (const f of [.1, .25, .4, .55, .7, .85, 1]) g = Math.min(g, this.discGap(w.x + f * (tx - w.x), w.z + f * (tz - w.z), .4, sweep)); return g; };
      const end = [e[0], e[1]].sort((a, b) => ((pathGap(b) >= here - .2) - (pathGap(a) >= here - .2)) || (gapN(b) - gapN(a)))[0];
      if (gapN(end) <= here + .3) return this.stepAway(w, sweep, here);
      let pick = end; if (gapN(end) < YIELD_M + 3) for (const k of Object.keys(WALK_NODES).sort((a, b) => gapN(b) - gapN(a))){
        if (walkRoute(end, k).every(n => gapN(n) > here)){ pick = k; break; } }
      Object.assign(w, {route: [WALK_NODES[end]].concat(walkRoute(end, pick).slice(1).map(n => WALK_NODES[n])), node: pick, pause: 0, state: 'evade', stubborn: false, jay: false, v: 1.5, yield_until: this.t + 60});
      return true; }
    if (this.stepAway(w, sweep, here)){ const n = nearestNode(...w.route[0]); w.route.push(WALK_NODES[n]); w.node = n; return true; }
    return false; }
  stepAway(w, sweep, here){ const statics = this.staticShapes([], false); let best = null, bg = here + .5;
    for (let k = 0; k < 16; k++){ const a = k * PI / 8; for (const r of [4, 7, 10]){ const x = w.x + r * Math.sin(a), z = w.z + r * Math.cos(a);
        if (statics.some(s => shapeDist(s, x, z) < .8)) continue;
        const ok = [.25, .5, .75, 1].every(f => { const px = w.x + f * (x - w.x), pz = w.z + f * (z - w.z); return this.discGap(px, pz, .4, sweep) > .6 && !statics.some(s => shapeDist(s, px, pz) < .5); });
        const g = this.discGap(x, z, .4, sweep); if (ok && g > bg){ best = [x, z]; bg = g; } } }
    if (!best) return false; Object.assign(w, {route: [best], node: null, pause: 0, state: 'evade', stubborn: false, jay: false, v: 1.5, yield_until: this.t + 60}); return true; }
  stepWorkers(dt){
    if (!this.workers.length) return;
    const sweep = this.stackerSweep(), moving = this.st.mode === 'driving' || this.st.mode === 'blocked';
    for (const w of this.workers){
      if (moving && (w.state === 'work' || w.state === 'yield') && this.discGap(w.x, w.z, .4, sweep) < YIELD_M - 1 && this.evade(w, sweep)) continue;
      if (w.pause > 0){ w.pause -= dt; if (w.pause <= 0 && w.state === 'phone'){ w.state = 'jay'; w.stubborn = false; } continue; }
      if (!w.route.length){ if (['walk', 'jay', 'yield', 'evade'].includes(w.state)){ w.state = 'work'; w.pause = 6 + 14 * this.rnd(); w.jay = false; continue; } this.planTrip(w); continue; }
      const [tx, tz] = w.route[0], d = Math.hypot(tx - w.x, tz - w.z);
      if (d < .25){ w.x = tx; w.z = tz; w.route.shift(); if (w.jay && w.stubborn && w.route.length === 1){ w.state = 'phone'; w.pause = 20 + 20 * this.rnd(); } continue; }
      const ux = (tx - w.x) / d, uz = (tz - w.z) / d, nx = w.x + ux * 1.5, nz = w.z + uz * 1.5;
      const gNow = this.discGap(w.x, w.z, .4, sweep), gNext = this.discGap(nx, nz, .4, sweep);
      if (gNext < 1 && gNext < gNow){ w.frozen += dt; if (w.frozen > 3){ w.frozen = 0; this.evade(w, sweep); } continue; }
      w.frozen = 0;
      if (!w.jay && w.state !== 'evade' && moving && gNext < YIELD_M && gNext <= gNow){ w.state = 'yield'; continue; }
      if (w.state !== 'evade') w.state = w.jay ? 'jay' : 'walk';
      const v = w.v * (gNow < YIELD_M && moving ? 1.4 : 1), s = Math.min(d, v * dt);
      w.yaw = Math.atan2(ux, uz); w.x += ux * s; w.z += uz * s;
    }
    if (moving) for (const w of this.workers){ this.metrics.worker_steps++; this.metrics.worker_on_walkway += onWalkway(w.x, w.z) ? 1 : 0;
      if (!w.jay && w.state !== 'phone' && this.st.v > .1){ const g = clearance(this.st.x, this.st.z, this.st.yaw, [["circle", w.x, w.z, .4]]); if (g < this.metrics.worker_min_gap_lawful){ this.metrics.worker_min_gap_lawful = g; this.metrics.wg_at = [Math.round(this.t), w.id, w.state, this.st.mode, this.st.dir, Math.round(w.x), Math.round(w.z), onWalkway(w.x, w.z)]; } } }
  }
  hornEffect(force = false){ const st = this.st;
    for (const w of this.workers){ if (Math.hypot(w.x - st.x, w.z - st.z) > (force ? 60 : 30)) continue;
      if (this.rnd() < (w.stubborn && w.state === 'phone' ? .35 : .85)) this.evade(w, this.stackerSweep()); }
    const c = this.car; if (c && c.state === 'road_stop' && (force || Math.hypot(c.x - st.x, c.z - st.z) < 35) && this.rnd() < .75){ c.until = this.t; this.log('현장', '서비스 차량 V1 경적 듣고 출발'); } }
  // ---------------------------------------------------------------- service car
  newCar(){ const loop = carLoopPath(), cum = cumOf(loop), Lp = cum[cum.length - 1] + Math.hypot(loop[0][0] - loop[loop.length - 1][0], loop[0][1] - loop[loop.length - 1][1]);
    return {id: 'V1', x: loop[0][0], z: loop[0][1], yaw: loop[0][2], v: 0, state: 'drive', loop, cum, L: Lp, s: 0, detour: null, until: -1, next_event: 30, yielding: false, min_gap: 1e9, brake: false}; }
  loopPose(c, s){ s = ((s % c.L) + c.L) % c.L; if (s >= c.cum[c.cum.length - 1]){ const a = c.loop[c.loop.length - 1], b = c.loop[0], f = (s - c.cum[c.cum.length - 1]) / Math.max(1e-9, c.L - c.cum[c.cum.length - 1]);
      return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + wrap(b[2] - a[2]) * f]; } return interp(c.loop, c.cum, s); }
  carPoseAt(c, ahead){ if (c.detour){ const s = c.detour_s + ahead, end = c.detour_cum[c.detour_cum.length - 1];
      if (s <= end) return interp(c.detour, c.detour_cum, s); return c.park_after ? c.detour[c.detour.length - 1].slice(0, 3) : this.loopPose(c, c.rejoin + s - end); }
    return this.loopPose(c, c.s + ahead); }
  loopSNear(c, x, z){ let bd = 1e9, bs = 0; c.loop.forEach((p, i) => { const d = Math.hypot(p[0] - x, p[1] - z); if (d < bd){ bd = d; bs = c.cum[i]; } }); return bs; }
  carAhead(c, poses){ const st = this.st, sweep = this.stackerSweep(), me0 = carShape(c.x, c.z, c.yaw);
    const inside = sweep.some(([dx, dz, dr]) => shapeDist(me0, dx, dz) < dr + 1), stk = inside ? footprint(st.x, st.z, st.yaw) : sweep;
    // trucks have right of way: do not enter where a truck is or is about to be (next 20 m); if the car got there first,
    // only the truck's body counts and the car drives on out of the zone
    const trucks = Object.values(this.trucks).filter(t => t.state !== 'away').map(t => {
      const zone = (t.state === 'arriving' || t.state === 'leaving') ? this.truckSweep(t, 20) : [truckShape(t.x, t.z, t.yaw)];
      return [t, zone, zone.some(sh => bodyGap(sh, c.x, c.z, c.yaw) < TRUCK_BODY_GAP)]; });
    for (const [d, [x, z, y]] of poses){ const me = carShape(x, z, y);
      if (stk.some(([dx, dz, dr]) => shapeDist(me, dx, dz) < dr + 1)) return [d, inside ? 3 : CAR_GAP_MACHINE, 'stacker'];
      for (const [t, zone, inZone] of trucks){
        if (inZone){ if (bodyGap(truckShape(t.x, t.z, t.yaw), x, z, y) < .3) return [d, 1, 'truck']; }
        else if (zone.some(sh => bodyGap(sh, x, z, y) < TRUCK_BODY_GAP)) return [d, CAR_GAP_MACHINE, 'truck']; }
      for (const w of this.workers) if (shapeDist(me, w.x, w.z) < 1.6) return [d, CAR_GAP_PERSON, 'person']; }
    return [null, 0, null]; }
  stepCar(dt){
    const c = this.car; if (!c) return;
    if (c.state === 'parked' || c.state === 'road_stop'){ c.v = 0; if (this.t < c.until) return;
      if (c.state === 'parked'){ const [px, pz, py] = PARKING; c.detour = straight([px, pz, py], [px + 8, CAR_LOOP[0][1], PI / 2], 1, .5); c.detour_cum = cumOf(c.detour); c.detour_s = 0; c.rejoin = this.loopSNear(c, px + 8, CAR_LOOP[0][1]); }
      c.state = 'drive'; c.next_event = this.t + 40 + 50 * this.rnd(); }
    if (!c.detour && this.t > c.next_event){ c.next_event = this.t + 40 + 50 * this.rnd(); const r = this.rnd();
      if (r < .35) c.want_park = true; else if (r < .5){ c.state = 'road_stop'; c.until = this.t + 25 + 20 * this.rnd(); c.v = 0; this.log('현장', '서비스 차량 V1 도로에 정차 (위반, 비상등)'); return; } }
    if (c.want_park && !c.detour && Math.abs(c.z - CAR_LOOP[0][1]) < .6 && c.x > PARKING[0] - 14 && c.x < PARKING[0] - 8 && Math.abs(c.yaw - PI / 2) < .2){
      c.detour = straight([c.x, c.z, PI / 2], PARKING, 1, .5); c.detour_cum = cumOf(c.detour); c.detour_s = 0; c.park_after = true; c.want_park = false; }
    const look = Math.max(12, c.v * 2 + CAR_GAP_MACHINE + 4), poses = []; for (let d = 1; d <= look; d++) poses.push([d, this.carPoseAt(c, d)]);
    const [hit, gap, what] = this.carAhead(c, poses); c.yielding = what === 'stacker';
    let want = ROAD_V; if (hit != null) want = Math.min(want, Math.sqrt(2 * CAR_DEC * Math.max(0, hit - gap - c.v * .5)));
    c.v = c.v < want ? Math.min(want, c.v + CAR_ACC * dt) : Math.max(want, c.v - CAR_DEC * 1.5 * dt);
    c.brake = c.v < want - .1 || (hit != null && c.v < .3); if (c.v < .02){ c.v = 0; return; }
    const ds = c.v * dt;
    if (c.detour){ c.detour_s += ds; const end = c.detour_cum[c.detour_cum.length - 1];
      if (c.detour_s >= end){ [c.x, c.z, c.yaw] = c.detour[c.detour.length - 1];
        if (c.park_after){ c.state = 'parked'; c.v = 0; c.park_after = false; c.detour = null; c.until = this.t + 20 + 25 * this.rnd(); this.log('현장', '서비스 차량 V1 주차 구역에 주차'); return; }
        c.s = c.rejoin; c.detour = null; } else [c.x, c.z, c.yaw] = interp(c.detour, c.detour_cum, c.detour_s); }
    else { c.s = (c.s + ds) % c.L; [c.x, c.z, c.yaw] = this.loopPose(c, c.s); }
    if (c.v > .5){ const me = carShape(c.x, c.z, c.yaw); let g = clearance(this.st.x, this.st.z, this.st.yaw, [me]);
      for (const w of this.workers) g = Math.min(g, shapeDist(me, w.x, w.z) - .4); c.min_gap = Math.min(c.min_gap, g); }
  }
  // ---------------------------------------------------------------- stacker driving + obstacle sensors
  detect(){ const st = this.st; if (!st.path) return [null, null];
    const dyn = this.dynamic().filter(([, sh]) => shapeDist(sh, st.x, st.z) < SENSOR_RANGE); if (!dyn.length) return [null, null];
    const s0 = st.s, end = st.cum[st.cum.length - 1];
    for (let s = s0; s <= Math.min(end, s0 + SENSOR_RANGE); s += .7){ const [x, z, y] = interp(st.path, st.cum, s);
      for (const [info, sh] of dyn){ if (st.ignore.has(info.id)) continue;
        const lim = info.zone ? TRUCK_CLEAR : info.known ? .3 : info.kind === 'worker' ? PERSON_GAP : SAFE_GAP;
        if (clearance(x, z, y, [sh]) < lim) return [[info, sh], s - s0]; } }
    return [null, null]; }
  stepStacker(dt){
    const st = this.st;
    let contact = null; for (const [info, sh] of this.dynamic()) if (info.kind !== 'truck' && clearance(st.x, st.z, st.yaw, [sh]) < 0){ contact = info; break; }
    if (contact && st.contact !== contact.id){ this.stats.contacts++; this.log('안전장치', '접촉 경고: ' + contact.name); } st.contact = contact ? contact.id : null;
    if (st.mode !== 'driving') return; if (st.pause > 0){ st.pause -= dt; return; }
    const [hit, dist] = this.detect(), path = st.path, cum = st.cum, i = Math.max(0, Math.min(cum.length - 1, idxOf(cum, st.s)));
    const d0 = path[Math.min(i + 1, path.length - 1)][3] || 1; let j = i + 1; while (j < path.length && (path[j][3] || d0) === d0) j++;
    const stopS = j < path.length ? cum[j - 1] : cum[cum.length - 1]; let room = stopS - st.s;
    if (hit){ const [info] = hit; if (!info.moving) for (const d of this.debris) if (d.id === info.id) d.known = true; room = Math.min(room, Math.max(0, dist - (STOP_BEFORE[info.kind] || 1.5))); }
    let vmax = d0 > 0 ? V_FWD : V_REV; if (this.workers.some(w => clearance(st.x, st.z, st.yaw, [['circle', w.x, w.z, .4]]) < 8)) vmax = Math.min(vmax, 1);
    const vAllow = Math.min(vmax, Math.sqrt(2 * DEC * Math.max(0, room)));
    st.v = st.v < vAllow ? Math.min(vAllow, st.v + ACC * dt) : Math.max(vAllow, st.v - (!hit || dist > 3 ? DEC : 4) * dt); st.dir = d0;
    if (hit && (st.v < .05 || room < .05) && dist <= (STOP_BEFORE[hit[0].kind] || 1.5) + .8){ st.v = 0; st.mode = 'blocked'; st.blocked_by = hit; st.block_dist = Math.round(dist * 10) / 10; return; }
    st.s = Math.min(stopS, st.s + st.v * dt); [st.x, st.z, st.yaw] = interp(path, cum, st.s);
    if (st.s >= stopS - 1e-6){ st.v = 0; if (stopS >= cum[cum.length - 1] - 1e-6){ st.mode = 'arrived'; [st.x, st.z, st.yaw] = path[path.length - 1]; } else { st.pause = .8; st.s += 1e-3; } }
  }
  setPath(path, ignore = []){ Object.assign(this.st, {path, cum: cumOf(path), s: 0, mode: 'driving', v: 0, blocked_by: null, ignore: new Set(ignore)}); }
  // ---------------------------------------------------------------- tasks
  status(){ const occ = Object.values(this.slots).filter(s => s.holder).map(s => ({slot: s.id, container: s.holder, in_yard_s: Math.round(this.t - this.containers[s.holder].since)}));
    return {time_s: Math.round(this.t), capacity: CAPACITY, occupied: occ, free_slots: Object.values(this.slots).filter(s => !s.holder && !s.reserved).map(s => s.id),
      trucks_waiting: Object.values(this.trucks).filter(t => t.state === 'parked' && !t.served).map(t => ({truck: t.id, bay: t.bay, type: t.mode, container: t.container, waiting_s: Math.round(this.t - t.parked_t)})),
      user_requests: this.requests.map(r => ({id: r.id, container: r.container, to: r.dest}))}; }
  request(container, dest){
    const c = this.containers[container]; if (!c || !['slot', 'truck'].includes(c.where)) throw new Error('옮길 수 없는 컨테이너: ' + container);
    if (this.task && this.task.container === container) throw new Error(container + ' 는 지금 작업 중입니다');
    if (this.slots[dest]){ if (this.slots[dest].holder || this.slots[dest].reserved) throw new Error(dest + ' 는 비어 있지 않습니다'); }
    else if (this.trucks[dest]){ const t = this.trucks[dest]; if (t.state !== 'parked' || t.container || t.served || c.where !== 'slot') throw new Error(dest + ' 에는 실을 수 없습니다 (정차한 빈 트럭만)'); }
    else throw new Error('목적지 오류: ' + dest);
    this.requests = this.requests.filter(r => r.container !== container); this.nReq++;
    const r = {id: 'R' + this.nReq, container, dest, t: this.t}; this.requests.push(r); this.log('사용자', `우선 지시 ${r.id}: ${container} → ${dest}`); return r; }
  taskFromRequest(r){ const c = this.containers[r.container]; if (!c || !['slot', 'truck'].includes(c.where)) return null;
    return {kind: c.where === 'truck' ? 'unload' : (this.trucks[r.dest] ? 'load' : 'move'), source: c.where === 'slot' ? c.slot : c.truck, dest: r.dest, request: r.id}; }
  checkTask(t){ if (this.requests.length){ const w = this.taskFromRequest(this.requests[0]); if (w && (w.source !== t.source || w.dest !== t.dest)) return `user request ${this.requests[0].id} has priority: start it first`; }
    if (t.kind === 'unload'){ const tr = this.trucks[t.source]; if (!tr || tr.state !== 'parked' || tr.served || !tr.container) return t.source + ' is not a parked truck with a container'; }
    else if (t.kind === 'load' || t.kind === 'move'){ if (!this.slots[t.source] || !this.slots[t.source].holder) return 'slot ' + t.source + ' holds no container'; }
    else return 'kind must be unload / load / move';
    if (t.kind === 'load'){ const tr = this.trucks[t.dest]; if (!tr || tr.state !== 'parked' || tr.container || tr.served) return t.dest + ' is not a parked empty truck'; }
    else { const s = this.slots[t.dest]; if (!s) return 'destination must be a slot A1-B5'; if (s.holder || s.reserved) return 'slot ' + t.dest + ' is not free'; }
    return null; }
  checkAction(a, act){ if (act === 'horn' && !a.moving) return 'horn does nothing for a fixed object: detour or request_clear';
    if (act === 'clear' && a.moving && !(a.kind === 'worker' && a.horns >= 3)) return 'the ground crew clears fixed objects only (a supervisor escorts a person only after 3 horns)';
    if (!['horn', 'wait', 'detour', 'clear', 'stop'].includes(act)) return 'unknown action'; return null; }
  ruleTask(s){ if (this.requests.length) return this.taskFromRequest(this.requests[0]);
    const held = c => ((this.containers[c] || {}).holdUntil || -1) > this.t;   // failed box: other work first
    const tws = s.trucks_waiting.slice().sort((a, b) => b.waiting_s - a.waiting_s), order = tws.filter(w => !(w.container && held(w.container))).concat(tws.filter(w => w.container && held(w.container)));
    for (const tw of order){
      const ready = s.occupied.filter(o => !held(o.container)).length ? s.occupied.filter(o => !held(o.container)) : s.occupied;
      if (tw.type === 'outbound' && !tw.container && s.occupied.length){ const old = ready.reduce((a, b) => b.in_yard_s > a.in_yard_s ? b : a); return {kind: 'load', source: old.slot, dest: tw.truck}; }
      if (tw.type === 'inbound' && tw.container && s.free_slots.length){ const bay = this.bays[tw.bay];
        const slot = s.free_slots.reduce((a, b) => Math.hypot(this.slots[b].x - bay.x, this.slots[b].z - bay.z) < Math.hypot(this.slots[a].x - bay.x, this.slots[a].z - bay.z) ? b : a);
        return {kind: 'unload', source: tw.truck, dest: slot}; } }
    return null; }
  ruleObstacle(a){
    if (a.kind === 'truck') return a.waited_s < 20 || a.detours >= 2 ? {action: 'wait', seconds: 5, why: '트럭 진입/출차 중 → 트럭 우선, 대기'} : {action: 'detour', why: '트럭이 비키지 않음 → 우회'};
    if (a.kind === 'worker' && a.horns >= 3 && a.waited_s >= 12) return {action: 'clear', why: `경적 ${a.horns}회에도 안 비킴 → 현장 관리자 호출(작업자 대피 유도)`};
    if (a.moving){ if (a.horns < 2 || (a.detour_failed && a.horns < 2 + a.detour_failed) || (a.creep && a.waited_s >= 10 * a.horns)) return {action: 'horn', why: '사람/차량 → 경적으로 비켜 달라고 알림'};
      if (a.creep) return {action: 'wait', seconds: 4, why: '좁은 진입 구간이라 우회 불가 → 대기'};
      if (a.detours >= 2) return a.waited_s >= 8 * (a.horns - 1) ? {action: 'horn', why: '우회해도 다시 경로에 들어옴 → 경적'} : {action: 'wait', seconds: 5, why: '우회해도 다시 경로에 들어옴 → 비킬 때까지 대기'};
      return {action: 'detour', why: `경적 ${a.horns}회에도 안 비킴 → 우회`}; }
    if (a.detours >= 2) return {action: 'clear', why: `고정 장애물, 우회 ${a.detours}회에도 다시 막힘 → 작업반 호출`};
    if (a.detour_extra_m != null && a.detour_extra_m < 30) return {action: 'detour', why: `고정 장애물, 우회가 짧음(+${Math.round(a.detour_extra_m)} m)`};
    return {action: 'clear', why: '고정 장애물, 우회 경로가 ' + (a.detour_extra_m == null ? '없음' : `김(+${Math.round(a.detour_extra_m)} m)`) + ' → 작업반 호출'}; }
  // ---- optional Ollama (browser -> local Ollama needs OLLAMA_ORIGINS to allow this page)
  async ai(what, info){
    if (this.controller !== 'ai' || this.aiFail >= 3) return null;
    const sys = what === 'task' ? 'You are the MAIN SERVER AI of a container yard (1 reach stacker, slots A1-A5/B1-B5, trucks at 3 bays). Decide the NEXT task with ONE tool call. user_requests first; inbound truck -> unload into a free slot near the truck; outbound (empty) truck -> load the container longest in the yard; serve the longest-waiting truck; else wait. One short Korean sentence first.'
      : 'You are the MAIN SERVER AI driving a reach stacker; its sensors stopped it for an object on the path. ONE tool call. Trucks have right of way: wait. People/vehicles: sound_horn first, detour if they still block; after 2 detours wait/horn; a person still blocking after 3 horns: request_clear (supervisor). Fixed objects: detour if detour_extra_m < 30 else request_clear. Never drive through. One short Korean sentence first.';
    const fn = (name, desc, props = {}, req = []) => ({type: 'function', function: {name, description: desc, parameters: {type: 'object', properties: props, required: req}}});
    const tools = what === 'task' ? [fn('start_task', 'kind unload(source=truck,dest=slot) / load(source=slot,dest=truck) / move(slot->slot)', {kind: {type: 'string'}, source: {type: 'string'}, dest: {type: 'string'}}, ['kind', 'source', 'dest']), fn('wait', 'nothing to do', {seconds: {type: 'number'}})]
      : [fn('sound_horn', 'horn'), fn('wait', 'wait', {seconds: {type: 'number'}}, ['seconds']), fn('detour', 're-plan around it'), fn('request_clear', 'crew / supervisor'), fn('stop_task', 'give up', {reason: {type: 'string'}}, ['reason'])];
    const msgs = [{role: 'system', content: sys}, {role: 'user', content: (what === 'task' ? 'Yard status: ' : 'Blocked: ') + JSON.stringify(info)}];
    for (let k = 0; k < 3; k++){
      let msg; try { const r = await fetch(this.aiUrl.replace(/\/$/, '') + '/api/chat', {method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({model: this.aiModel, messages: msgs, tools, stream: false, options: {temperature: 0}})}); msg = (await r.json()).message; this.aiFail = 0; }
      catch (e){ this.aiFail++; this.log('서버', 'Ollama 응답 없음' + (this.aiFail >= 3 ? ' — 규칙 자동조종으로 전환 (브라우저에서 쓰려면 OLLAMA_ORIGINS 설정 필요)' : '')); return null; }
      msgs.push(msg); if (msg.content) this.log('AI', msg.content.trim().slice(0, 300));
      const tc = (msg.tool_calls || [])[0]; if (!tc){ msgs.push({role: 'user', content: 'Answer with exactly one tool call.'}); continue; }
      let args = tc.function.arguments || {}; if (typeof args === 'string') try { args = JSON.parse(args); } catch (e){ args = {}; }
      const name = tc.function.name; this.log('도구', `${name}(${JSON.stringify(args)})`); let reason;
      if (what === 'task'){ if (name === 'wait'){ if (!this.ruleTask(this.status())){ this.stats.ai_decisions++; return null; } reason = 'there is work waiting: start a task'; }
        else if (name === 'start_task'){ const t = {kind: args.kind, source: String(args.source || ''), dest: String(args.dest || '')}; reason = this.checkTask(t);
          if (!reason){ if (this.requests.length) t.request = this.requests[0].id; this.stats.ai_decisions++; return t; } } else reason = 'unknown tool'; }
      else { const act = {sound_horn: 'horn', wait: 'wait', detour: 'detour', request_clear: 'clear', stop_task: 'stop'}[name]; reason = act ? this.checkAction(info, act) : 'unknown tool';
        if (!reason){ this.stats.ai_decisions++; return {action: act, seconds: args.seconds, why: 'AI'}; } }
      this.stats.guard_refusals++; this.log('안전장치', '거부: ' + reason); msgs.push({role: 'tool', content: JSON.stringify({refused: reason})}); }
    return null; }
  async chooseTask(){
    this.requests = this.requests.filter(r => { const t = this.taskFromRequest(r); let ok = !!t;
      if (ok && this.slots[r.dest]) ok = !this.slots[r.dest].holder && !this.slots[r.dest].reserved; else if (ok){ const tr = this.trucks[r.dest]; ok = tr.state === 'parked' && !tr.container && !tr.served; }
      if (!ok) this.log('서버', `우선 지시 ${r.id} 취소 (상황이 바뀜)`); return ok; });
    const s = this.status(); if (!s.trucks_waiting.length && !s.user_requests.length) return null;
    const a = await this.ai('task', s); if (a) return a; const t = this.ruleTask(s); if (t) this.stats.rule_decisions++; return t; }
  async control(){
    while (this.running){
      try { const task = await this.chooseTask(); if (!task){ await this.waitSim(3); continue; } await this.runTask(task); }
      catch (e){ this.log('서버', '오류: ' + e.message); console.error(e); this.task = null; await this.waitSim(3); } } }
  poseOf(ref){ if (this.slots[ref]){ const s = this.slots[ref]; return [s.x, s.z, s.yaw, 0]; } const b = this.bays[this.trucks[ref].bay]; return [b.x, b.z, 0, TRUCK_BED]; }
  async runTask(task){
    this.nTask++; task = Object.assign({}, task, {id: 'K' + this.nTask});
    task.container = task.kind === 'unload' ? this.trucks[task.source].container : this.slots[task.source].holder;
    if (this.slots[task.dest]) this.slots[task.dest].reserved = task.id; if (task.request) this.requests = this.requests.filter(r => r.id !== task.request); this.task = task;
    this.log('서버', `작업 ${task.id} 시작: ${task.container} ${task.source} → ${task.dest}${task.request ? ' (사용자 우선 지시 ' + task.request + ')' : ' · ' + {unload: '하역', load: '상차(반출)', move: '이동'}[task.kind]}`);
    let ok = false; try { ok = await this.doTask(task); } finally { if (this.slots[task.dest] && this.slots[task.dest].reserved === task.id) this.slots[task.dest].reserved = null; this.task = null; this.temp = []; this.alert = null; }
    if (ok){ if (task.kind === 'unload') this.stats.inbound++; else if (task.kind === 'load') this.stats.outbound++; if (task.request) this.stats.user++; this.log('서버', `작업 ${task.id} 완료 (${task.container} → ${task.dest})`); } }
  async doTask(task){
    const cid = task.container, [sx, sz, syaw, sy] = this.poseOf(task.source); task.src = [sx, sz, syaw, sy];
    const srcEx = task.kind === 'unload' ? [cid, task.source] : [cid];
    task.step = '컨테이너로 이동'; if (!await this.go(predockPose(sx, sz, syaw))) return false;
    task.step = '진입'; if (!await this.creep(dockPose(sx, sz, syaw), 1, srcEx)) return false;
    task.step = 'MAS-kit 정렬·결합'; this.st.h_target = sy + H + KIT_GAP; await this.waitUntil(() => Math.abs(this.st.h - this.st.h_target) < .005, 40);
    const box = this.containers[cid];
    if (box.autoFail >= 1){ // automatic alignment failed 3x on this box (same corner tolerance): a signalman guides it, no endless loop
      this.log('서버', `${cid} 자동 정렬 반복 실패 이력 → 신호수 호출, 수동 유도 결합 (약 ${MANUAL_S}초)`); task.step = '신호수 수동 유도';
      await this.waitSim(MANUAL_S); this.stats.manual_locks++; this.log('기계', `${cid} 신호수 유도로 트위스트록 잠금`); }
    else if (!await this.alignWithRetries(task)){ this.st.h_target = TRAVEL_H; box.autoFail = (box.autoFail || 0) + 1; box.holdUntil = this.t + HOLD_S;
      await this.creep(predockPose(sx, sz, syaw), -1, srcEx); this.log('서버', `${cid} 자동 정렬 3회 실패 → 다른 작업 먼저, 이 컨테이너는 신호수 수동 유도로 처리 예정`); return false; }
    task.step = '들어 올림'; const c = this.containers[cid];
    if (task.kind === 'unload'){ const t = this.trucks[task.source]; t.container = null; t.served = true; } else this.slots[task.source].holder = null;
    Object.assign(c, {where: 'stacker', slot: null, truck: null}); this.st.carry = cid; this.st.h_target = CARRY_LIFT + H;
    await this.waitUntil(() => Math.abs(this.st.h - this.st.h_target) < .005, 60);
    task.step = '후진으로 빠지기'; await this.creep(predockPose(sx, sz, syaw), -1, srcEx);
    const [dx, dz, dyaw, dy] = this.poseOf(task.dest); task.step = '목적지로 운반';
    while (!await this.go(predockPose(dx, dz, dyaw))){ if (!this.running) return false; this.log('서버', '운반 경로 없음 → 대기 후 재시도'); await this.waitSim(5); }
    task.step = '진입'; await this.creep(dockPose(dx, dz, dyaw), 1, [task.dest]);
    task.step = '내려놓기'; this.st.h_target = dy + H; await this.waitUntil(() => Math.abs(this.st.h - this.st.h_target) < .005, 60);
    Object.assign(c, {x: dx, z: dz, yaw: dyaw, y: dy, since: this.t});
    if (task.kind === 'load'){ const t = this.trucks[task.dest]; t.container = cid; t.served = true; Object.assign(c, {where: 'truck', truck: t.id}); }
    else { this.slots[task.dest].holder = cid; Object.assign(c, {where: 'slot', slot: task.dest}); }
    this.st.carry = null; this.st.h_target = TRAVEL_H; this.log('기계', `${cid} 트위스트록 해제 → ${task.dest} 에 내려놓음`);
    await this.waitUntil(() => this.st.h > TRAVEL_H - .5, 60);
    task.step = '후진으로 빠지기'; await this.creep(predockPose(dx, dz, dyaw), -1, [task.dest, cid]); return true; }
  creep(goal, dirn, ignore = []){ const p0 = [this.st.x, this.st.z, this.st.yaw]; return this.follow([[...p0, dirn]].concat(straight(p0, goal, dirn)), goal, ignore, true); }
  shrinkFrom(sh, pose){ const need = MARGIN + .45 - clearance(pose[0], pose[1], pose[2], [sh]); if (need <= 0) return sh;
    if (sh[0] === 'circle') return sh[3] - need > .1 ? ['circle', sh[1], sh[2], sh[3] - need] : null;
    return Math.min(sh[4], sh[5]) - need > .1 ? sh.slice(0, 4).concat([sh[4] - need, sh[5] - need]) : null; }
  carReserved(){ const c = this.car; if (!c || c.state === 'parked') return []; const out = [carShape(c.x, c.z, c.yaw)];
    if (c.state === 'drive' && c.v > .3 && !c.yielding) for (let d = 2; d <= 10; d += 2) out.push(carShape(...this.carPoseAt(c, d))); return out; }
  planTo(goal, extra = []){ const st = this.st, start = [st.x, st.z, st.yaw];
    let shapes = this.staticShapes(st.carry ? [st.carry] : []).concat(extra, this.carReserved().map(s => inflate(s, 1)));
    shapes = shapes.map(s => this.shrinkFrom(s, start)).filter(Boolean);
    if (Math.hypot(goal[0] - start[0], goal[1] - start[1]) < .7 && Math.abs(wrap(goal[2] - start[2])) < .17) return [[...start, 1], [...goal, 1]];
    return plan(new DistanceField(shapes), start, goal); }
  async go(goal){
    let path = null;
    for (let attempt = 0; attempt < 4; attempt++){
      path = this.planTo(goal); if (path) break; this.temp = [];
      const onGoal = this.debris.filter(d => d.known && d.clear_at == null && clearance(goal[0], goal[1], goal[2], [d.shape]) < SAFE_GAP + MARGIN);
      if (onGoal.length){ for (const d of onGoal){ d.clear_at = this.t + CREW_S; this.stats.clears++; } this.log('서버', `목표 지점에 ${onGoal.map(d => d.id).join(', ')} → 작업반 호출`);
        await this.waitUntil(() => !this.debris.some(d => onGoal.includes(d)), CREW_S + 30); continue; }
      const movers = this.dynamic().filter(([i, sh]) => i.moving && !i.zone && clearance(goal[0], goal[1], goal[2], [sh]) < 1);
      if (movers.length){ this.st.horn_t = this.t; this.stats.horns++; this.hornEffect(true); this.log('기계', `목표 지점에 ${movers.map(m => m[0].name).join(', ')} → 경적 ♪ 후 대기`); await this.waitSim(5); continue; }
      if (attempt === 1){ const {x, z, yaw} = this.st, bx = x - 4 * Math.sin(yaw), bz = z - 4 * Math.cos(yaw);
        if (clearance(bx, bz, yaw, this.staticShapes(this.st.carry ? [this.st.carry] : [])) > MARGIN){ this.log('서버', '경로 없음 → 4 m 후진 후 재계획'); await this.creep([bx, bz, yaw], -1); continue; } }
      if (attempt === 2){ const near = this.debris.filter(d => d.known && d.clear_at == null && Math.min(Math.hypot(d.x - goal[0], d.z - goal[1]), Math.hypot(d.x - this.st.x, d.z - this.st.z)) < 12);
        if (near.length){ for (const d of near){ d.clear_at = this.t + CREW_S; this.stats.clears++; } this.log('서버', `경로 없음 → 주변 ${near.map(d => d.id).join(', ')} 작업반 호출`); await this.waitSim(CREW_S + 1); continue; } }
      this.log('서버', '경로 계획 실패 (막힘) → 5초 대기 후 재계획'); await this.waitSim(5); }
    if (!path) return false;
    this.log('서버', `경로 계획: ${Math.round(pathLen(path))} m · 후진 전환 ${path.filter((p, i) => i && p[3] !== path[i - 1][3]).length}회`);
    return this.follow(path, goal); }
  async follow(path, goal, ignore = [], creep = false){
    this.setPath(path, ignore); const tries = {};
    while (this.running){
      await this.waitUntil(() => this.st.mode === 'arrived' || this.st.mode === 'blocked'); if (!this.running) return false;
      const st = this.st; if (st.mode === 'arrived'){ st.mode = 'idle'; st.path = null; this.alert = null; return true; }
      const [info, sh] = st.blocked_by, rec = tries[info.id] = tries[info.id] || {horns: 0, waited: 0, t0: this.t, detours: 0, detour_failed: 0};
      const alert = {object: info.name, id: info.id, kind: info.kind, moving: info.moving, distance_m: st.block_dist, horns: rec.horns, waited_s: Math.round(rec.waited * 10) / 10,
        stopped_s: Math.round((this.t - rec.t0) * 10) / 10, detours: rec.detours, detour_failed: rec.detour_failed, creep};
      this.alert = alert; if (!rec.waited && !rec.horns) this.log('기계', `장애물 감지: ${info.name} → ${alert.distance_m} m 앞에서 정지`);
      if (!info.moving && !creep){ const det = this.planTo(goal, [inflate(sh, SAFE_GAP + .3)]); alert.detour_extra_m = det ? Math.max(0, pathLen(det) - (st.cum[st.cum.length - 1] - st.s)) : null; }
      let dec = await this.ai('obstacle', alert); if (!dec){ dec = this.ruleObstacle(alert); this.stats.rule_decisions++; this.log('서버', '판단: ' + dec.why); }
      this.decision = Object.assign({}, dec, {t: this.t, object: info.name}); const act = dec.action;
      if (act === 'horn'){ st.horn_t = this.t; this.stats.horns++; this.hornEffect(); rec.horns++; this.log('기계', `경적 ♪ (${info.name})`); await this.waitSim(3); rec.waited += 3; }
      else if (act === 'wait'){ const sec = Math.max(1, Math.min(30, Number(dec.seconds) || 4)); this.stats.waits++; this.log('서버', `대기 ${sec}초`); await this.waitSim(sec); rec.waited += sec; }
      else if (act === 'clear' && info.kind === 'worker'){ this.stats.clears++; this.log('서버', `현장 관리자 호출: ${info.name} 대피 유도 (약 15초)`); await this.waitSim(15);
        const w = this.workers.find(x => x.id === info.id); if (w){ w.stubborn = false; if (!this.evade(w, this.stackerSweep())) Object.assign(w, {route: [WALK_NODES[nearestNode(w.x, w.z)]], state: 'evade', pause: 0, jay: false}); } }
      else if (act === 'clear' && info.kind === 'parked_car'){ this.car.until = this.t + 10; this.stats.clears++; this.log('서버', '주차 차량 운전자 호출: 이동 요청 (약 10초)'); await this.waitUntil(() => this.car.state !== 'parked', 40); }
      else if (act === 'clear'){ const d = this.debris.find(x => x.id === info.id); if (d && d.clear_at == null) d.clear_at = this.t + CREW_S; this.stats.clears++;
        this.log('서버', `작업반 호출: ${info.name} 제거 요청 (약 ${CREW_S}초)`); await this.waitUntil(() => !this.debris.some(x => x.id === info.id), CREW_S + 30); }
      else if (act === 'detour'){
        if (creep){ this.log('서버', '진입 구간은 우회 불가 → 대기'); await this.waitSim(4); rec.waited += 4; }
        else { const m = info.moving ? 2 : SAFE_GAP + .3, gap = clearance(st.x, st.z, st.yaw, [sh]); this.temp.push(inflate(sh, Math.max(0, Math.min(m, gap - MARGIN - .1)))); this.stats.detours++;
          const det = this.planTo(goal); if (!det){ rec.detour_failed++; this.temp.pop(); this.log('서버', '우회 경로 없음 → 대기'); await this.waitSim(4); rec.waited += 4; }
          else { rec.detours++; this.log('서버', `우회 경로 재계획: ${Math.round(pathLen(det))} m`); this.setPath(det, ignore); continue; } } }
      else if (act === 'stop'){ this.log('서버', '작업 중단: ' + (dec.why || '')); st.mode = 'idle'; return false; }
      if (st.mode === 'blocked') st.mode = 'driving';
    }
    return false; }
  // ---------------------------------------------------------------- alignment with case memory + retries
  async alignWithRetries(task){
    this.st.mode = 'aligning'; const tried = []; let reason = null;
    try {
      if (this.vary && !task.cond){ const r = this.rnd(), envs = Object.keys(ENVS);
        task.cond = {env: this.rnd() < .5 ? this.env : envs[Math.floor(this.rnd() * envs.length)], camFail: r < .08};
        this.log('현장', `현장 조건(시뮬 진실): 환경 ${ENVS[task.cond.env].name}${task.cond.camFail ? ' · 카메라 하나 틀어짐(편향)' : ''}`); }
      for (let attempt = 0; attempt < 3; attempt++){
        if (attempt && REAPPROACH.includes(reason)) await this.reapproach(task, reason);
        this.st.mode = 'aligning';
        const res = await this.alignOnce(task, attempt, reason, tried); if (res.plan) tried.push(res.plan);
        if (res.ok){ if (attempt){ this.stats.retry_success++; this.log('서버', `재시도 ${attempt + 1}회차 성공 (${res.plan || '-'})`); } return true; }
        reason = res.reason; this.stats.align_fail++; if (!this.running) return false;
        if (attempt < 2){ this.stats.retries++; this.log('서버', `정렬·결합 실패 (원인: ${REASON_KO[reason] || reason}) → 재시도 ${attempt + 2}/3${REAPPROACH.includes(reason) ? ' · 재접근' : ''}`); } }
      return false;
    } finally { this.st.mode = 'idle'; this.align = null; } }
  async reapproach(task, reason){
    const [sx, sz, syaw, sy] = task.src, ex = [task.container, task.source];
    this.log('기계', `재접근: 스프레더 상승 → 후진 ${PRE} m → 다시 진입 (${REASON_KO[reason] || reason})`);
    this.st.h_target = TRAVEL_H; await this.waitUntil(() => Math.abs(this.st.h - TRAVEL_H) < .005, 60);
    await this.creep(predockPose(sx, sz, syaw), -1, ex); await this.creep(dockPose(sx, sz, syaw), 1, ex);
    this.st.h_target = sy + H + KIT_GAP; await this.waitUntil(() => Math.abs(this.st.h - this.st.h_target) < .005, 60); }
  async alignOnce(task, attempt, prevReason, tried){
    const E = this.E, seed = Math.floor(this.rnd() * 4294967295) >>> 0 || 1, rnd = E.rng(seed), aux = E.rng((seed ^ 0x5A5A5A5A) >>> 0 || 7);
    const env = (task.cond && task.cond.env) || (this.machine === 'unstable' ? 'fog' : this.env), P = Object.assign({}, BASEP, PRESETS[this.hardware]);
    const tr = {tx: 0, ty: 0, th: 0, tol: [[0, 0], [0, 0], [0, 0], [0, 0]]};
    const r0 = P.R0 * Math.sqrt(rnd()), a0 = 2 * PI * rnd(); tr.tx = r0 * Math.cos(a0); tr.ty = r0 * Math.sin(a0); tr.th = P.theta0 * PI / 180 * rnd.n();
    const stc = []; for (let k = 0; k < 4; k++){ tr.tol[k] = [P.tolC * rnd.n(), P.tolC * rnd.n()]; const b = []; for (let h = 0; h < 5; h++) b.push(P.modelBias * rnd.n());
      stc.push({dz: P.gapSd * rnd.n(), pitch: P.tiltSd * rnd.n(), roll: P.tiltSd * rnd.n(), bias: b}); }
    const box = this.containers[task.container];
    if (box && box.tolZ) tr.tol = this.tolModel === 'iso' ? isoTol(box.isoZ, box.tolZ) : box.tolZ.map(([a, b]) => [P.tolC * a, P.tolC * b]);   // same box, same tolerance
    const camBias = (task.cond && task.cond.camFail) || this.machine === 'biased' ? [9 + aux(), -7] : [.5 * aux.n(), .5 * aux.n()], irBias = [aux.n(), aux.n()];
    const errs = () => CMM.map(([X, Y], q) => [tr.tx - tr.th * Y + tr.tol[q][0], tr.ty + tr.th * X + tr.tol[q][1]]);
    const worst = () => Math.max(...errs().map(v => Math.max(Math.abs(v[0]), Math.abs(v[1]))));
    const read = (kind, bias) => { const c = ENVS[env][kind]; if (aux() < c.drop) return null; let ex = c.sd * aux.n(), ey = c.sd * aux.n(); const et = c.sd / 3000 * aux.n();
      if (aux() < c.out) ex += (aux() < .5 ? -1 : 1) * (15 + 10 * aux()); return {x: tr.tx + bias[0] + ex, y: tr.ty + bias[1] + ey, th: tr.th + et, q: c.q}; };
    const pub = () => { this.align = {phase: 'sensing', truth_corner_errors_mm: errs(), truth_worst_mm: worst(), pose: {tx_mm: tr.tx, ty_mm: tr.ty, th_deg: tr.th * 180 / PI}}; };
    pub(); this.log('서버', `MAS-kit 키트 정렬 시작 (${task.container}${attempt ? ', ' + (attempt + 1) + '회차' : ''}) · 환경 ${ENVS[env].name}`);
    const hist = [[], [], [], []], meas = [0, 0, 0, 0, 0]; let round = 0, verify = false, holds = 0, resense = false, fine = false, planObj = null, feats = null, everDis = false;
    let K = P.K, iters = P.iters, useMachine = true, outcome = null, lowF = null;
    while (this.running){
      if (!verify && !resense) round++;
      const hw = useMachine && this.machine !== 'none', r = {cam: hw ? read('cam', camBias) : null, ir: hw ? read('ir', irBias) : null, kit: null};
      await this.waitSim(1.2);
      const Eg = errs(), est = [];
      for (let k = 0; k < 4; k++){ const f = E.frame(k, true), lx = f[0] * Eg[k][0], ly = f[1] * Eg[k][1]; E.measure(this.M, stc[k], lx, ly, rnd, P, meas);
        if (resense && hist[k].length) hist[k][hist[k].length - 1] = {m: meas.slice(), ox: 0, oy: 0}; else hist[k].push({m: meas.slice(), ox: 0, oy: 0});
        const es = hist[k].length >= 2 ? this.HIST.estimate(this.M, hist[k], P, () => E.estimate(this.M, meas, P)) : E.estimate(this.M, meas, P); est.push([f[0] * es.x, f[1] * es.y]); }
      const [kx, ky, kth] = rigid(est), resid = Math.max(...est.map(([x, y], k) => { const [X, Y] = CMM[k]; return Math.hypot(x - (kx - kth * Y), y - (ky + kth * X)); }));
      r.kit = {x: kx, y: ky, th: kth, q: Math.min(1, Math.max(.2, 1 - resid / 12))};
      let fu = fuse(r, fine); everDis = everDis || fu.disagree;
      if (!feats){ // first sensing: the situation the server knows -> plan from similar cases
        const mq = [r.cam, r.ir].filter(Boolean), machine = !mq.length ? 'none' : mq.every(x => x.q >= .7) ? 'valid' : 'unstable';
        const ew = Math.max(...est.map(v => Math.max(Math.abs(v[0]), Math.abs(v[1]))));
        feats = {hardware: this.gateKey(), env, machine, kit: r.kit.q >= .7 ? 'good' : r.kit.q >= .45 ? 'caution' : 'invalid', disagree: fu.disagree,
          dist: ew < 10 ? 'near' : ew < 20 ? 'mid' : 'far', attempt, prev: prevReason};
        if (this.memory){ planObj = this.memory.choose(feats, tried, prevReason);
          K = planObj.K; iters = planObj.max_rounds; useMachine = planObj.use_machine;
          this.planInfo = Object.assign({}, planObj, {task: task.id, container: task.container, attempt: attempt + 1, features: feats});
          this.log('서버', `사례 기반 계획: ${planObj.plan} — ${planObj.why}`);
          if (!useMachine){ r.cam = r.ir = null; fu = fuse(r, fine); } } }
      const rel = ['cam', 'ir', 'kit'].map(n => ({cam: '카메라', ir: '적외선', kit: '키트'}[n] + ' ' + Math.round(100 * fu.pct[n]) + '%')).join(' · ');
      this.log('키트', `${round}회차 센싱 · 의존도 ${rel}${fu.disagree ? ' — 불일치 센서 의존도 낮춤' : ''}`);
      if (!fu.ok){ holds++; if (holds > 2){ this.log('서버', '믿을 만한 센서가 없음 → 하강 금지, 안전 정지'); outcome = 'held'; break; }
        this.log('서버', '센서 신뢰도 부족 → 이동 없이 재측정'); resense = true; continue; }
      holds = 0; resense = false; if (!fine && Math.max(Math.abs(fu.fx), Math.abs(fu.fy)) <= 5){ fine = true; this.log('서버', '오차 5 mm 이내 → 정밀 단계: 키트 우선'); }
      const mv = [K * fu.fx, K * fu.fy, K * fu.fth];
      const predWorst = Math.max(...est.map(([x, y], k) => { const [X, Y] = CMM[k]; return Math.max(Math.abs(x - (mv[0] - mv[2] * Y)), Math.abs(y - (mv[1] + mv[2] * X))); }));
      const small = Math.max(Math.abs(fu.fx), Math.abs(fu.fy)) <= P.stopMM && Math.abs(fu.fth) * 3137 <= P.stopMM;
      let action = small && predWorst <= 4 ? 'lower' : verify ? (predWorst <= 4 ? 'lower' : 'stop') : round >= iters ? 'verify' : 'move';
      if (action === 'stop'){ this.log('서버', `최종 확인: 예상 최악 코너 ${predWorst.toFixed(2)} mm > 4 mm → 하강 금지, 안전 정지`); outcome = 'held'; break; }
      if (action === 'lower'){ lowF = [Math.max(...est.map(v => Math.max(Math.abs(v[0]), Math.abs(v[1])))), Math.max(...est.map(([x, y], k) => { const [X, Y] = CMM[k]; return Math.max(Math.abs(x - (kx - kth * Y)), Math.abs(y - (ky + kth * X))); }))];
        const g = this.memory ? this.memory.lowerP(this.gateKey(), lowF) : null;
        if (g && g.p < g.th){ this.log('서버', `하강 보류: 비슷한 과거 하강 ${g.n}건 중 결합 ${Math.round(100 * g.p)}% (< ${Math.round(100 * g.th)}%) → 재접근`); outcome = 'gate_hold'; this.stats.gate_holds = (this.stats.gate_holds || 0) + 1; break; } }
      this.log('서버', `보정 지시: X ${mv[0].toFixed(2)} / Y ${mv[1].toFixed(2)} mm / θ ${(mv[2] * 180 / PI).toFixed(4)}°`);
      tr.tx -= mv[0] + P.act * rnd.n(); tr.ty -= mv[1] + P.act * rnd.n(); tr.th -= mv[2];
      for (let k = 0; k < 4; k++){ const lf = E.frame(k, true), [X, Y] = CMM[k], deX = lf[0] * (-mv[0] + mv[2] * Y), deY = lf[1] * (-mv[1] - mv[2] * X);
        for (const o of hist[k]){ o.ox -= deX; o.oy -= deY; } }
      await this.waitSim(1); pub();
      if (action === 'lower'){ this.log('서버', '하강 조건 충족 → 하강'); outcome = captureOk(errs(), this.capture) ? 'success' : 'false_lower';
        this.log('기계', outcome === 'success' ? `결합 성공 (실제 최악 코너 ${worst().toFixed(2)} mm)` : `결합 실패: 실제 최악 코너 ${worst().toFixed(2)} mm > 5 mm`); this.align.phase = 'done'; await this.waitSim(1.5); break; }
      if (action === 'verify'){ verify = true; this.log('서버', `${iters}회 보정 완료 → 최종 확인 센싱`); } }
    if (!outcome) return {ok: false, reason: 'aborted', plan: planObj && planObj.plan};
    if (this.memory && lowF && (outcome === 'success' || outcome === 'false_lower')) this.memory.addLower(this.gateKey(), lowF, outcome === 'success');
    const reason = outcome === 'success' ? null : outcome === 'false_lower' ? 'false_lower' : outcome === 'gate_hold' ? 'gate_hold' : everDis ? 'disagree' : holds > 2 ? 'low_reliability' : feats && feats.dist === 'far' ? 'far' : 'not_converged';
    if (this.memory && feats){ this.memory.add(feats, planObj ? planObj.plan : 'STD', outcome, reason); this.stats.cases_added++; }
    return {ok: outcome === 'success', reason, plan: planObj && planObj.plan}; }
  // ---------------------------------------------------------------- state (same shape as the desktop host)
  state(){
    const st = this.st, r2 = v => Math.round(v * 100) / 100; let path = null;
    if (st.path && (st.mode === 'driving' || st.mode === 'blocked')){ const i0 = idxOf(st.cum, st.s); path = [];
      for (let i = i0; i < st.path.length; i += 3) path.push([r2(st.path[i][0]), r2(st.path[i][1]), st.path[i][3]]); const e = st.path[st.path.length - 1]; path.push([r2(e[0]), r2(e[1]), e[3]]); }
    const t = this.task;
    return {running: this.running, t: Math.round(this.t * 100) / 100, speed: this.speed, seed: this.seed, controller: this.controller, ai_model: this.aiModel, ai_ok: this.aiFail < 3,
      stacker: {x: st.x, z: st.z, yaw: st.yaw, v: r2(st.v), dir: st.dir, mode: st.mode, h: Math.round(st.h * 1000) / 1000, carry: st.carry, horn: this.t - st.horn_t < 1.5},
      path, containers: Object.values(this.containers).map(c => ({id: c.id, color: c.color, x: c.x, z: c.z, yaw: c.yaw, y: c.y, where: c.where, slot: c.slot, truck: c.truck})),
      slots: Object.values(this.slots).map(s => ({id: s.id, x: s.x, z: s.z, yaw: s.yaw, holder: s.holder, reserved: s.reserved})),
      trucks: Object.values(this.trucks).map(x => ({id: x.id, state: x.state, bay: x.bay, mode: x.mode, container: x.container, x: x.x, z: x.z, yaw: x.yaw, served: x.served})),
      bays: Object.values(this.bays).map(b => ({id: b.id, x: b.x, z: b.z, truck: b.truck})),
      workers: this.workers.map(w => ({id: w.id, x: w.x, z: w.z, yaw: w.yaw, state: w.state, jay: w.jay})),
      car: this.car ? {id: 'V1', x: this.car.x, z: this.car.z, yaw: this.car.yaw, state: this.car.state, v: r2(this.car.v), brake: this.car.brake} : null,
      debris: this.debris.map(d => ({id: d.id, kind: d.kind, x: d.x, z: d.z, yaw: d.yaw, known: d.known, clearing: d.clear_at != null})),
      alert: st.mode === 'blocked' || (this.alert && t) ? this.alert : null, decision: this.decision,
      task: t ? {id: t.id, kind: t.kind, source: t.source, dest: t.dest, container: t.container, step: t.step, request: t.request} : null,
      requests: this.requests.slice(), stats: Object.assign({}, this.stats), occupied: Object.values(this.slots).filter(s => s.holder).length, capacity: CAPACITY,
      align: this.align, memory: this.memory ? this.memory.summary() : null, plan: this.planInfo,
      walkways: WALK_EDGES.map(([a, b]) => [WALK_NODES[a], WALK_NODES[b]]), road: CAR_LOOP, crosswalks: [], parking: PARKING,
      metrics: Object.assign({}, this.metrics, {car_min_gap: this.car ? r2(this.car.min_gap) : null}), log: this.logLines.slice(-80)};
  }
}
root.MASKIT_YARD_SIM = {Yard, CaseMemory, ENVS, plan, DistanceField, straight, pathLen};
})(typeof window !== 'undefined' ? window : globalThis);
