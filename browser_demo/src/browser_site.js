/* MAS-kit browser edition (v3.6): the virtual site and the kit link that the desktop version runs in Python,
   ported to JavaScript so the same screen works from a single HTML file without installing anything.

   Ported from app/ (operation for operation, same random streams, so the same seed gives the same job):
     reachstacker_sim.py  -> VirtualReachStacker      main_controller.py -> ControllerBrain
     kit_device.py        -> processReading            sim_site.py        -> Site (job sequence, log, frames)
     maskit_engine.World  -> World                     case_memory.lower_features -> lowerFeatures
   The kit link (maskit_link.py) is reduced to what the screen shows: there is no external main server in the
   browser, so every result stays in this browser (IndexedDB) and the built-in main server gives the orders. */
(function (root) {
  'use strict';
  var E = root.MASKIT, HIST = root.MASKIT_HISTORY;
  var CORNERS = E.CORNERS, frame = E.frame;

  // ---------------------------------------------------------------- settings (maskit_engine.settings)
  var PRESETS = {
    ideal: { modelBias: 0, noise: .1, gapSd: 0, tiltSd: 0, tolC: .7, act: .2 },
    target: { modelBias: .25, noise: .2, gapSd: .3, tiltSd: .05, tolC: .7, act: .2 },
    base: { modelBias: .5, noise: .3, gapSd: 1, tiltSd: .2, tolC: 3, act: .5 }
  };
  var BASE = { R0: 30, theta0: .1, K: .8, iters: 6, tolMM: 5, fitGap: true, mirror: true, useHead: [1, 1, 1, 1, 1], searchMM: 36, stopMM: 1 };
  function settings(hw, est, over) { return Object.assign({}, BASE, PRESETS[hw], { estimator: est }, over || {}); }
  function cp(a) { return a.map(function (v) { return v.slice(); }); }
  function deg(r) { return r * 180 / Math.PI; }

  // ---------------------------------------------------------------- kit side (kit_device.py + engine helpers)
  var FEM_RANGE_MM = 30, GAP_FIT_LIMIT = 3, RSS_CAUTION = 1.0;
  function gridEstimate(M, meas, P) {          // = maskit_engine.grid_estimate (JS estimate + fitted gap)
    var es = E.estimate(M, meas, P), w = P.useHead, g = M.gslope, h, gg = 0, gr = 0;
    var i = Math.round((es.x + M.R) / M.S), j = Math.round((es.y + M.R) / M.S), idx = i * M.N + j;
    for (h = 0; h < 5; h++) if (w[h]) { gg += g[h] * g[h]; gr += g[h] * (meas[h] - M.tab[h][idx]); }
    var dz = 0;
    if (P.fitGap && gg > 0) dz = Math.max(-3, Math.min(3, gr / gg));
    return { x: es.x, y: es.y, rss: es.rss, dz: dz };
  }
  function cornerQuality(es, headOk) {
    var extrap = Math.abs(es.x) > FEM_RANGE_MM || Math.abs(es.y) > FEM_RANGE_MM;
    var gapOut = Math.abs(es.dz) >= GAP_FIT_LIMIT - 1e-9;
    var bad = headOk.filter(function (v) { return !v; }).length, rss = es.rss, grade;
    if (bad >= 2 || !isFinite(rss)) grade = 'invalid';
    else if (extrap || gapOut || bad === 1 || rss > RSS_CAUTION) grade = 'caution';
    else grade = 'good';
    return { rss_uH2: rss, extrapolated: extrap, gap_out_of_range: gapOut, head_ok: headOk.slice(), grade: grade };
  }
  var DEVICE = { useHead: [1, 1, 1, 1, 1], searchMM: 36, fitGap: true };
  function processReading(M, k, uH, headOk) {
    headOk = (headOk || [true, true, true, true, true]).map(Boolean);
    var P = Object.assign({}, DEVICE, { useHead: headOk.map(function (v) { return v ? 1 : 0; }) });
    if (P.useHead.reduce(function (a, b) { return a + b; }, 0) < 3)
      return { est_mm: null, quality: { rss_uH2: null, extrapolated: null, gap_out_of_range: null, head_ok: headOk, grade: 'invalid' } };
    var es = gridEstimate(M, uH, P), f = frame(k, true);
    return { est_mm: { x: f[0] * es.x, y: f[1] * es.y }, gap_mm: es.dz, quality: cornerQuality(es, headOk) };
  }
  function kitUnits(M, readings) {
    return readings.map(function (r, k) {
      var res = processReading(M, k, r.sensor_uH, r.head_ok);
      return { sensor_uH: r.sensor_uH, est_mm: res.est_mm, quality: res.quality };
    });
  }

  // ---------------------------------------------------------------- truth (maskit_engine.World)
  function isoTol(zs, cast) {
    var clip = function (v, lo, hi) { return Math.max(lo, Math.min(hi, v)); };
    var dS = clip(-1 + 8 / 6 * zs[0], -5, 3), dP = clip(-1 + 8 / 6 * zs[1], -5, 3), sh = clip(13 / 3 * zs[2], -13, 13) / 1.87;
    return CORNERS.map(function (c, k) {
      var gx = c[0] > 0 ? 1 : -1, gy = c[1] > 0 ? 1 : -1;
      return [gx * dS / 2 + gy * sh / 2 + 0.5 * cast[k][0], gy * dP / 2 + 0.5 * cast[k][1]];
    });
  }
  function captureOk(errs, P) {
    var cap = P.capture || 'pm5';
    var cx = cap === 'head' || cap === 'float10' ? 10 : P.tolMM, cy = cap === 'head' ? 3.8 : cap === 'float10' ? 10 : P.tolMM;
    return errs.every(function (e) { return Math.abs(e[0]) <= cx && Math.abs(e[1]) <= cy; });
  }
  function World(P, rnd) {
    this.P = P; this.rnd = rnd;
    var r0 = P.R0 * Math.sqrt(rnd()), a0 = 2 * Math.PI * rnd();
    this.tx = r0 * Math.cos(a0); this.ty = r0 * Math.sin(a0); this.th = P.theta0 * Math.PI / 180 * rnd.n();
    this.tol = []; this.st = [];
    for (var k = 0; k < 4; k++) {
      this.tol.push([P.tolC * rnd.n(), P.tolC * rnd.n()]);
      var b = []; for (var h = 0; h < 5; h++) b.push(P.modelBias * rnd.n());
      var dz = P.gapSd * rnd.n(), pitch = P.tiltSd * rnd.n(), roll = P.tiltSd * rnd.n();
      this.st.push({ dz: dz, pitch: pitch, roll: roll, bias: b });
    }
    if (P.tolModel === 'iso') {
      var zs = [rnd.n(), rnd.n(), rnd.n()], cast = [];
      for (var q = 0; q < 4; q++) cast.push([rnd.n(), rnd.n()]);
      this.tol = isoTol(zs, cast);
    }
    this.extrap = false;
  }
  World.prototype.errs = function () {
    var out = [];
    for (var q = 0; q < 4; q++) {
      var X = CORNERS[q][0], Y = CORNERS[q][1];
      out.push([this.tx - this.th * Y + this.tol[q][0], this.ty + this.th * X + this.tol[q][1]]);
    }
    return out;
  };
  World.prototype.readCorner = function (M, k, Er) {
    Er = Er || this.errs();
    var f = frame(k, this.P.mirror), lx = f[0] * Er[k][0], ly = f[1] * Er[k][1];
    if (Math.abs(lx) > 30 || Math.abs(ly) > 30) this.extrap = true;
    var out = [0, 0, 0, 0, 0];
    return E.measure(M, this.st[k], lx, ly, this.rnd, this.P, out);
  };
  World.prototype.applyMove = function (sx, sy, dth) {
    this.tx -= sx + this.P.act * this.rnd.n();
    this.ty -= sy + this.P.act * this.rnd.n();
    this.th -= dth;
  };

  // ---------------------------------------------------------------- virtual reach stacker (reachstacker_sim.py)
  var MACHINE_MODES = {
    normal: { sxy: 1.5, sth: 0.01, bias: 1.0, status: 'valid' },
    unstable: { sxy: 6.0, sth: 0.05, bias: 1.0, status: 'unstable' },
    none: { sxy: null, sth: null, bias: 0.0, status: 'none' },
    biased: { sxy: 1.5, sth: 0.01, bias: 8.0, status: 'valid' }
  };
  var DRIFT_UH = [2.5, 0.0, -2.0, 0.0, 0.0];
  function VirtualReachStacker(M, P, worldRnd, auxRnd, machineMode, fault, faultCorner) {
    if (!MACHINE_MODES[machineMode]) throw new Error('machine_mode');
    if (['none', 'head_dead', 'drift'].indexOf(fault) < 0) throw new Error('fault');
    this.M = M; this.P = P; this.aux = auxRnd; this.mode = machineMode; this.fault = fault;
    this.faultCorner = faultCorner == null ? 1 : faultCorner;
    this.world = new World(P, worldRnd);
    var mm = MACHINE_MODES[machineMode];
    this.mbias = [mm.bias * auxRnd.n(), mm.bias * auxRnd.n()];
    this.lowered = this.locked = false;
  }
  VirtualReachStacker.prototype.cornerErrors = function () { return this.world.errs(); };
  VirtualReachStacker.prototype.worstMM = function () {
    return Math.max.apply(null, this.world.errs().map(function (v) { return Math.max(Math.abs(v[0]), Math.abs(v[1])); }));
  };
  VirtualReachStacker.prototype.approach = function () { return { in_range: true, range_mm: 30 }; };
  VirtualReachStacker.prototype.machineSensor = function () {
    var mm = MACHINE_MODES[this.mode];
    if (mm.sxy === null) return { status: 'none' };
    var w = this.world;
    return { status: mm.status, x_mm: w.tx + this.mbias[0] + mm.sxy * this.aux.n(),
             y_mm: w.ty + this.mbias[1] + mm.sxy * this.aux.n(), theta_rad: w.th + mm.sth * Math.PI / 180 * this.aux.n() };
  };
  VirtualReachStacker.prototype.move = function (x, y, th) { this.world.applyMove(x, y, th); };
  VirtualReachStacker.prototype.lower = function () { this.lowered = true; };
  VirtualReachStacker.prototype.lock = function () {
    this.locked = this.lowered && captureOk(this.world.errs(), this.P);
    return { locked: this.locked, worst_mm: this.worstMM() };
  };
  VirtualReachStacker.prototype.kitReadings = function () {
    var Er = this.world.errs(), out = [];
    for (var k = 0; k < 4; k++) {
      var r = this.world.readCorner(this.M, k, Er), ok = [true, true, true, true, true];
      if (k === this.faultCorner && this.fault === 'head_dead') { ok = [true, false, true, false, true]; r = [r[0], 0.0, r[2], 0.0, r[4]]; }
      else if (k === this.faultCorner && this.fault === 'drift') r = r.map(function (v, h) { return v + DRIFT_UH[h]; });
      out.push({ sensor_uH: r, head_ok: ok });
    }
    return out;
  };

  // ---------------------------------------------------------------- main-server brain (main_controller.py)
  var GRADE_W = { good: 1.0, caution: 0.7, invalid: 0.0 };
  var OUTLIER_MM = 12.0, KIT_RESID_OK_MM = 4.0, DISAGREE_MM = 6.0, LOWER_OK_MM = 4.0, STOP_MM = 1.0, CORNER_R_MM = 3137.0, MAX_HOLD = 2;
  var PRIORITY_W = { P1: [0.7, 0.3], P2: [0.2, 0.8], P3: [0.0, 1.0], P4: [0.5, 0.5] };
  var CASE_KO = { P1: '기존 센서 정상 → 기존 센서 위주, 키트로 다시 확인', P2: '기존 센서 불안정 → 키트 위주',
    P3: '기존 센서 정보 없음 → 키트만 사용', P4: '한쪽만으로는 부족 → 둘을 반반 섞음',
    P5: '둘 다 믿기 어려움 → 이동 보류', KIT_OVERRIDE: '기존 센서와 어긋남 → 앞뒤가 맞는 키트 값을 채택',
    MACHINE_OVERRIDE: '키트 값이 불확실 → 기존 센서 값을 채택', V354: '옛 방식(v3.5.4): 네 모서리 같은 비중' };
  function rigidFit(est) {
    var sx = 0, sy = 0, num = 0, den = 0, k;
    for (k = 0; k < 4; k++) { sx += est[k][0]; sy += est[k][1]; }
    sx /= 4; sy /= 4;
    for (k = 0; k < 4; k++) { var X2 = CORNERS[k][0], Y2 = CORNERS[k][1]; num += X2 * (est[k][1] - sy) - Y2 * (est[k][0] - sx); den += X2 * X2 + Y2 * Y2; }
    return [sx, sy, num / den];
  }
  function fitRigid(est, w) {
    var idx = [0, 1, 2, 3].filter(function (k) { return est[k] != null && w[k] > 0; });
    if (idx.length === 4 && idx.every(function (k) { return w[k] === w[idx[0]]; })) return rigidFit(est);
    if (!idx.length) return null;
    var W = 0, sx = 0, sy = 0, cx = 0, cy = 0, num = 0, den = 0;
    idx.forEach(function (k) { W += w[k]; });
    idx.forEach(function (k) { sx += w[k] * est[k][0]; }); sx /= W;
    idx.forEach(function (k) { sy += w[k] * est[k][1]; }); sy /= W;
    if (idx.length === 1) return [sx, sy, 0.0];
    idx.forEach(function (k) { cx += w[k] * CORNERS[k][0]; }); cx /= W;
    idx.forEach(function (k) { cy += w[k] * CORNERS[k][1]; }); cy /= W;
    idx.forEach(function (k) {
      var X = CORNERS[k][0] - cx, Y = CORNERS[k][1] - cy;
      num += w[k] * (X * (est[k][1] - sy) - Y * (est[k][0] - sx)); den += w[k] * (X * X + Y * Y);
    });
    var th = num / den;
    return [sx + th * cy, sy - th * cx, th];
  }
  function cornerModel(fit, k) { var X = CORNERS[k][0], Y = CORNERS[k][1]; return [fit[0] - fit[2] * Y, fit[1] + fit[2] * X]; }
  function residuals(est, fit, idx) {
    var out = {};
    idx.forEach(function (k) { var m = cornerModel(fit, k); out[k] = Math.hypot(est[k][0] - m[0], est[k][1] - m[1]); });
    return out;
  }
  function ControllerBrain(M, strategy, useMachine, interlock, maxRounds, K, estimator) {
    if (['equal', 'diagonal', 'weighted', 'v354'].indexOf(strategy) < 0) throw new Error('strategy');
    this.M = M; this.strategy = strategy;
    this.useMachine = useMachine && strategy !== 'v354'; this.interlock = interlock && strategy !== 'v354';
    this.maxRounds = maxRounds; this.K = K; this.estimator = estimator || 'history';
    this.P =Object.assign({}, BASE, { useHead: [1, 1, 1, 1, 1] });
    this.hist = [[], [], [], []]; this.rounds = 0; this.holds = 0; this.disagreeSeen = false; this.verifying = false; this.decisions = [];
  }
  ControllerBrain.prototype._cornerEstimates = function (units, resense) {
    var self = this, est = [], method = [], grade = [];
    units.forEach(function (u, k) {
      var f = frame(k, true), q = u.quality || {};
      grade.push(q.grade || 'good');
      var kit = u.est_mm;
      if (kit == null) { est.push(null); method.push('none'); return; }
      var kitLocal = { x: f[0] * kit.x, y: f[1] * kit.y }, es = kitLocal, m = 'kit_grid';
      if (self.estimator === 'history') {
        var entry = { m: u.sensor_uH.slice(), ox: 0, oy: 0 };
        if (resense && self.hist[k].length) self.hist[k][self.hist[k].length - 1] = entry; else self.hist[k].push(entry);
        if (self.hist[k].length >= 2) {
          es = HIST.estimate(self.M, self.hist[k], self.P, function () { return kitLocal; });
          m = ('historyUsed' in es) ? 'history' : 'kit_grid';
        }
      }
      est.push([f[0] * es.x, f[1] * es.y]); method.push(m);
    });
    return [est, method, grade];
  };
  ControllerBrain.prototype._combine = function (est, grade) {
    var s = this.strategy, w;
    if (s === 'equal' || s === 'v354') w = est.map(function (e) { return e != null ? 1.0 : 0.0; });
    else if (s === 'diagonal') w = [0, 1, 2, 3].map(function (k) { return (k === 0 || k === 2) && est[k] != null ? 1.0 : 0.0; });
    else w = [0, 1, 2, 3].map(function (k) { return est[k] != null ? (GRADE_W[grade[k]] || 0.0) : 0.0; });
    var fit = fitRigid(est, w), dropped = null;
    var idx = [0, 1, 2, 3].filter(function (k) { return w[k] > 0; });
    if (fit !== null && s === 'weighted' && idx.length >= 3) {
      var res = residuals(est, fit, idx), kBad = idx[0];
      idx.forEach(function (k) { if (res[k] > res[kBad]) kBad = k; });
      if (res[kBad] > OUTLIER_MM) {
        dropped = kBad; w[kBad] = 0.0; fit = fitRigid(est, w);
        idx = [0, 1, 2, 3].filter(function (k) { return w[k] > 0; });
      }
    }
    var resid = null;
    if (fit !== null && idx.length >= 3) { var rr = residuals(est, fit, idx); resid = Math.max.apply(null, idx.map(function (k) { return rr[k]; })); }
    var good = idx.filter(function (k) { return grade[k] === 'good'; }).length, status;
    if (fit === null) status = 'invalid';
    else if (idx.length >= 3 && good >= 3 && (resid === null || resid <= KIT_RESID_OK_MM)) status = 'good';
    else if (idx.length >= 2) status = 'caution';
    else status = 'invalid';
    if (fit !== null && idx.length === 1) status = 'caution';
    var key = idx.slice().sort().join(',');
    return { fit: fit, weights: w, dropped: dropped, used: idx, resid_mm: resid, status: status,
             rotation_ok: idx.length >= 3 || key === '0,2' || key === '1,3' };
  };
  ControllerBrain.prototype.decide = function (units, machine, resense) {
    if (!resense && !this.verifying) this.rounds += 1;
    var r = this._cornerEstimates(units, resense), est = r[0], method = r[1], grade = r[2];
    var kit = this._combine(est, grade);
    var mstat = this.useMachine ? ((machine || {}).status || 'none') : 'none';
    var mfit = (mstat === 'valid' || mstat === 'unstable') ? [machine.x_mm, machine.y_mm, machine.theta_rad] : null;
    var d = { round: this.rounds, resense: !!resense, verify: this.verifying, corner_est_mm: est, method: method, grade: grade,
              kit_status: kit.status, weights: kit.weights, dropped_corner: kit.dropped === null ? null : kit.dropped + 1,
              kit_resid_mm: kit.resid_mm, machine_status: mstat, machine_fit: mfit };
    var cse;
    if (this.strategy === 'v354') cse = 'V354';
    else if (kit.status === 'invalid' && mstat !== 'valid') cse = 'P5';
    else if (kit.status === 'invalid') cse = 'P1';
    else if (mstat === 'none') cse = 'P3';
    else if (mstat === 'unstable') cse = kit.status === 'good' ? 'P2' : 'P4';
    else cse = kit.status === 'good' ? 'P1' : 'P4';
    if (cse !== 'P5' && cse !== 'V354' && mfit !== null && kit.fit !== null) {
      var diff = Math.max(Math.abs(mfit[0] - kit.fit[0]), Math.abs(mfit[1] - kit.fit[1]));
      var limit = DISAGREE_MM + 0.3 * Math.max(Math.abs(kit.fit[0]), Math.abs(kit.fit[1]));
      d.disagree_mm = diff; d.disagree_limit_mm = limit;
      if (diff > limit) {
        if (!this.disagreeSeen) { this.disagreeSeen = true; return this._hold(d, 'disagree'); }
        if (kit.status === 'good') cse = 'KIT_OVERRIDE';
        else if (mstat === 'valid') cse = 'MACHINE_OVERRIDE';
        else return this._stop(d, 'sensors_disagree');
      }
    }
    d.case = cse; d.case_ko = CASE_KO[cse];
    if (cse === 'P5' || (kit.fit === null && mfit === null)) return this._hold(d, 'no_reliable_data');
    var wm, wk;
    if (cse === 'MACHINE_OVERRIDE') { wm = 1.0; wk = 0.0; }
    else if (cse === 'V354' || cse === 'P3' || cse === 'KIT_OVERRIDE' || mfit === null) { wm = 0.0; wk = 1.0; }
    else if (kit.fit === null) { wm = 1.0; wk = 0.0; }
    else { wm = PRIORITY_W[cse][0]; wk = PRIORITY_W[cse][1]; }
    var kf = kit.fit || [0, 0, 0], mf = mfit || [0, 0, 0];
    var fused = cse !== 'V354' ? [0, 1, 2].map(function (i) { return wm * mf[i] + wk * kf[i]; }) : kf;
    if (cse === 'P3' || cse === 'KIT_OVERRIDE' || wm === 0.0) fused = kf;
    else if (wk === 0.0) fused = mf;
    if (!kit.rotation_ok && wm === 0.0) fused = [fused[0], fused[1], 0.0];
    d.w_machine = wm; d.w_kit = wk; d.fused_mm = fused;
    this.holds = 0;
    var K = this.K, move = [K * fused[0], K * fused[1], K * fused[2]], self = this;
    var usable = [0, 1, 2, 3].filter(function (k) { return est[k] != null && (kit.weights[k] > 0 || self.strategy === 'v354' || self.strategy === 'equal'); });
    var pred = [0, 1, 2, 3].map(function (k) {
      if (est[k] == null) return null;
      var X = CORNERS[k][0], Y = CORNERS[k][1];
      return [est[k][0] - (move[0] - move[2] * Y), est[k][1] - (move[1] + move[2] * X)];
    });
    var predWorst = usable.length ? Math.max.apply(null, usable.map(function (k) { return Math.max(Math.abs(pred[k][0]), Math.abs(pred[k][1])); })) : Infinity;
    var converged = usable.length > 0 && usable.every(function (k) { return Math.abs(est[k][0]) <= STOP_MM && Math.abs(est[k][1]) <= STOP_MM; });
    var fusedSmall = Math.max(Math.abs(fused[0]), Math.abs(fused[1])) <= STOP_MM && Math.abs(fused[2]) * CORNER_R_MM <= STOP_MM;
    if (!converged && wm > 0 && fusedSmall && predWorst <= LOWER_OK_MM) converged = true;
    d.correction = { x_mm: move[0], y_mm: move[1], theta_rad: move[2], theta_deg: deg(move[2]) };
    d.predicted_worst_mm = predWorst; d.converged = converged;
    if (this.verifying) {
      this.verifying = false;
      var ok = converged || predWorst <= LOWER_OK_MM;
      d.action = ok ? 'lower_after_move' : 'stop'; d.reason = ok ? 'verified' : 'verify_failed';
      return this._record(d);
    }
    if (!this.interlock) { d.action = (converged || this.rounds >= this.maxRounds) ? 'lower_after_move' : 'move'; return this._record(d); }
    if (converged && kit.status !== 'invalid') d.action = 'lower_after_move';
    else if (this.rounds >= this.maxRounds) { d.action = 'move_then_verify'; this.verifying = true; }
    else d.action = 'move';
    return this._record(d);
  };
  ControllerBrain.prototype.applyHistoryOffsets = function (c) {
    if (this.estimator !== 'history') return;
    var cmdX = -c.x_mm, cmdY = -c.y_mm, cmdTh = -c.theta_rad;
    for (var k = 0; k < 4; k++) {
      var lf = frame(k, true), CX = CORNERS[k][0], CY = CORNERS[k][1];
      var deX = lf[0] * (cmdX - cmdTh * CY), deY = lf[1] * (cmdY + cmdTh * CX);
      this.hist[k].forEach(function (ob) { ob.ox -= deX; ob.oy -= deY; });
    }
  };
  ControllerBrain.prototype._hold = function (d, reason) {
    this.holds += 1;
    if (!('case' in d)) d.case = 'P5';
    if (!('case_ko' in d)) d.case_ko = CASE_KO[d.case] || '';
    if (this.holds > MAX_HOLD) return this._stop(d, reason);
    d.action = 'resense'; d.reason = reason; return this._record(d);
  };
  ControllerBrain.prototype._stop = function (d, reason) {
    if (!('case' in d)) d.case = 'P5';
    if (!('case_ko' in d)) d.case_ko = CASE_KO[d.case] || '';
    d.action = 'stop'; d.reason = reason; return this._record(d);
  };
  ControllerBrain.prototype._record = function (d) { this.decisions.push(d); return d; };

  // ---------------------------------------------------------------- one synchronous job (sim_job.run_job) - used by the tests
  function runJob(M, P, worldRnd, auxRnd, strategy, machineMode, fault, maxRounds) {
    var rs = new VirtualReachStacker(M, P, worldRnd, auxRnd, machineMode || 'none', fault || 'none');
    rs.approach();
    var w0 = rs.worstMM(), brain = new ControllerBrain(M, strategy || 'weighted', true, true, maxRounds || 6, P.K, P.estimator);
    var resense = false, outcome = null, sensings = 0;
    while (outcome === null) {
      var units = kitUnits(M, rs.kitReadings()), machine = rs.machineSensor();
      var d = brain.decide(units, machine, resense); sensings++;
      if (d.action === 'resense') { resense = true; continue; }
      resense = false;
      if (d.action === 'stop') { outcome = 'held'; break; }
      var c = d.correction;
      rs.move(c.x_mm, c.y_mm, c.theta_rad); brain.applyHistoryOffsets(c);
      if (d.action === 'lower_after_move') { rs.lower(); outcome = rs.lock().locked ? 'success' : 'false_lower'; }
    }
    return { outcome: outcome, ok: outcome === 'success', worst: rs.worstMM(), worst0: w0, rounds: brain.rounds, sensings: sensings };
  }

  // ---------------------------------------------------------------- live site (sim_site.py) + reduced kit link
  var ACT_KO = { disagree: '기존 센서와 키트 값이 어긋남', no_reliable_data: '믿을 만한 측정값 없음', sensors_disagree: '두 센서가 계속 어긋남',
                 verify_failed: '최종 확인에서 허용 범위를 넘음', verified: '최종 확인 통과' };
  var GRADE_KO = { good: '양호', caution: '주의', invalid: '무효' };
  var MSTAT_KO = { valid: '정상', unstable: '불안정', none: '없음' };
  function Site(getModel, opts) {
    this.getModel = getModel; this.job = null; this.approachMs = opts.approachMs || 1000; this.moveMs = opts.moveMs || 800;
    this.kit = { kit_id: 'KIT-BROWSER-1', state: 'IDLE', round_no: 0, session_id: null, sent: 0, last_job: null };
  }
  Site.prototype._now = function () { return Date.now() / 1000; };
  Site.prototype._log = function (who, text) { this.job.log.push({ t: this._now(), at: new Date().toISOString(), who: who, text: text }); };
  Site.prototype._frame = function (label) { this.job.frames.push({ t: this._now(), errors: cp(this.job.rs.cornerErrors()), label: label }); };
  Site.prototype._later = function (ms, fn) { var self = this, id = this.job.job_id; setTimeout(function () { if (self.job && self.job.job_id === id && !self.job.outcome) fn(); }, ms); };
  Site.prototype.start = function (p) {
    var M = this.getModel();
    if (this.job && !this.job.outcome) throw new Error('진행 중인 가상 작업이 있습니다');
    var hw = p.hardware || 'target', seed = p.seed == null ? ((Math.random() * 4294967296) >>> 0 || 1) : (p.seed >>> 0);
    var P = settings(hw, 'history', { tolModel: p.tol_model || 'orig', capture: p.capture || 'pm5' });
    var rs = new VirtualReachStacker(M, P, E.rng(seed), E.rng((seed ^ 0x5A5A5A5A) >>> 0), p.machine || 'normal', p.fault || 'none');
    var d = new Date(), pad = function (v) { return String(v).padStart(2, '0'); };
    var jobId = 'JOB-' + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + '-' + pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds()) + '-' + Math.floor(Math.random() * 65536).toString(16).padStart(4, '0');
    this.job = { job_id: jobId, seed: seed, P: P, rs: rs, phase: 'approach', outcome: null, frames: [], log: [], decision: null, machine_last: null,
                 settings: { hardware: hw, machine: p.machine, fault: p.fault, strategy: p.strategy, tol_model: P.tolModel, capture: P.capture },
                 brain: new ControllerBrain(M, p.strategy || 'weighted', true, true, 6, P.K), worst0: rs.worstMM() };
    this.kit.state = 'IDLE'; this.kit.session_id = jobId; this.kit.round_no = 0;
    this._frame('접근 시작');
    this._log('기계', '리치스태커: 멀리서 받은 컨테이너 대략 위치로 다가가는 중');
    var self = this;
    this._later(this.approachMs, function () { self._inRange(); });
    return this.state();
  };
  Site.prototype._inRange = function () {
    var j = this.job; j.rs.approach(); j.phase = 'sensing';
    this._frame('센서 측정 범위 도착 (±30 mm)');
    this._log('기계', '리치스태커: 자체 센서로 보니 키트가 잴 수 있는 범위(±30 mm) 안에 들어옴');
    this._log('서버', '메인 서버 → 키트: 작업 시작');
    this._sense(false, 1);
  };
  Site.prototype._sense = function (resense, roundNo) {
    var j = this.job, M = this.getModel();
    this.kit.state = 'SENSING'; this.kit.round_no = roundNo;
    var units = kitUnits(M, j.rs.kitReadings());
    this.kit.sent += 1; this.kit.state = 'WAITING';
    var machine = j.rs.machineSensor(); j.machine_last = machine;
    var d = j.brain.decide(units, machine, resense); j.decision = d;
    var tag = d.round + '차' + (resense ? ' 재측정' : (d.verify ? ' 최종 확인' : ''));
    this._log('키트', '키트 → 서버: ' + tag + ' 측정값 전송 (네 모서리 신뢰도 ' + d.grade.map(function (g) { return GRADE_KO[g] || g; }).join('/') + ')');
    this._log('서버', '서버 판단: ' + (d.case_ko || '') + ' · 기존 센서 ' + (MSTAT_KO[d.machine_status] || d.machine_status) + ' · 키트 ' + (GRADE_KO[d.kit_status] || d.kit_status) + (d.dropped_corner ? ' · 모서리' + d.dropped_corner + ' 값 제외' : ''));
    var self = this, act = d.action;
    if (act === 'resense') { this._log('서버', '서버 → 키트: 움직이지 않고 다시 측정 (' + (ACT_KO[d.reason] || d.reason) + ')'); this._later(200, function () { self._sense(true, roundNo); }); return; }
    if (act === 'stop') { this._finish('held'); this._log('서버', '서버: ' + (ACT_KO[d.reason] || d.reason) + ' → 내리지 않고 안전하게 멈춤'); return; }
    var c = d.correction;
    j.rs.move(c.x_mm, c.y_mm, c.theta_rad); j.brain.applyHistoryOffsets(c);
    this._frame(tag + ' 위치 보정');
    this._log('서버', '서버 → 리치스태커: 위치 보정 가로 ' + c.x_mm.toFixed(2) + ' / 세로 ' + c.y_mm.toFixed(2) + ' mm / 회전 ' + c.theta_deg.toFixed(4) + '° (기존 센서 ' + Math.round(100 * (d.w_machine || 0)) + '% · 키트 ' + Math.round(100 * (d.w_kit == null ? 1 : d.w_kit)) + '%)');
    if (act === 'lower_after_move') {
      j.rs.lower(); var locked = j.rs.lock().locked;
      this._frame('내림·잠금');
      this._log('서버', '서버 → 리치스태커: 내림 → 잠금 ' + (locked ? '성공' : '실패 (허용 범위 초과)') + ' → 들어 올림');
      this._finish(locked ? 'success' : 'false_lower'); return;
    }
    j.phase = act === 'move_then_verify' ? 'verify' : 'sensing';
    this._log('서버', '서버: 이동 끝 확인 → 키트에 ' + (act === 'move_then_verify' ? '최종 확인 측정' : '다음 측정') + ' 요청');
    this._later(this.moveMs, function () { self._sense(false, roundNo + 1); });
  };
  Site.prototype._finish = function (outcome) {
    var j = this.job; j.outcome = outcome; j.phase = outcome === 'success' ? 'done' : outcome === 'false_lower' ? 'failed' : outcome === 'aborted' ? 'aborted' : 'held';
    this.kit.state = 'IDLE';
    this.kit.last_job = { session_id: j.job_id, result: outcome === 'aborted' ? 'aborted' : outcome === 'held' ? 'stopped' : 'end', rounds: j.brain.rounds };
  };
  Site.prototype.abort = function (reason) { if (this.job && !this.job.outcome) { this._finish('aborted'); this._log('서버', '작업 중단 (' + reason + ')'); } };
  Site.prototype.state = function () {
    var j = this.job; if (!j) return { active: false };
    var d = j.decision;
    return { active: !j.outcome, job_id: j.job_id, phase: j.phase, outcome: j.outcome, seed: j.seed, settings: j.settings, worst0_mm: j.worst0,
             truth_worst_mm: j.rs.worstMM(), truth_corner_errors_mm: cp(j.rs.cornerErrors()), frames: j.frames.slice(), log: j.log.slice(-60),
             rounds: j.brain.rounds, machine_last: j.machine_last, decision: d ? Object.assign({}, d) : null, job_rs_tol: j.rs.world.tol };
  };
  Site.prototype.linkStatus = function () {
    var k = this.kit;
    return { kit_id: k.kit_id, state: k.state, round_no: k.round_no, session_id: k.session_id, pending: null,
             outbox: { pending: 0, sent: k.sent }, units: ['U1', 'U2', 'U3', 'U4'], server_configured: false, server_url: '',
             awaiting_start: false, last_job: k.last_job, uplink: { last_error: null }, browser: true };
  };

  // ---------------------------------------------------------------- lowering-gate data from the stored cases (desktop_host.gate_bank)
  function lowerFeatures(est) {
    if (!est || est.length !== 4 || est.some(function (e) { return e == null; })) return null;
    var f = rigidFit(est), sx = f[0], sy = f[1], th = f[2];
    var worst = Math.max.apply(null, est.map(function (e) { return Math.max(Math.abs(e[0]), Math.abs(e[1])); }));
    var resid = Math.max.apply(null, est.map(function (e, k) { var X = CORNERS[k][0], Y = CORNERS[k][1]; return Math.max(Math.abs(e[0] - (sx - th * Y)), Math.abs(e[1] - (sy + th * X))); }));
    return [Math.round(worst * 1e4) / 1e4, Math.round(resid * 1e4) / 1e4];
  }
  function gateBankFrom(records, q) {
    var F = [], Y = [];
    records.slice().sort(function (a, b) { return a.saved_order - b.saved_order; }).forEach(function (r) {
      if (r.scenario !== q.scenario || r.estimator !== q.estimator || String(r.radius_mm) !== String(q.radius)) return;
      if ((r.gate || {}).held) return;
      var rounds = (r.controller_observable || {}).rounds || [];
      var f = rounds.length ? lowerFeatures(rounds[rounds.length - 1].estimated_corner_errors_mm) : null;
      if (f) { F.push(f); Y.push(r.outcome === 'success' ? 1 : 0); }
    });
    return { F: F, Y: Y, n: F.length };
  }

  root.MASKIT_BROWSER_SITE = { Site: Site, runJob: runJob, settings: settings, kitUnits: kitUnits, ControllerBrain: ControllerBrain,
                               VirtualReachStacker: VirtualReachStacker, gateBankFrom: gateBankFrom, lowerFeatures: lowerFeatures };
  if (typeof module !== 'undefined') module.exports = root.MASKIT_BROWSER_SITE;
})(typeof window !== 'undefined' ? window : globalThis);
