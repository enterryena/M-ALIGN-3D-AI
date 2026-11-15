# -*- coding: utf-8 -*-
"""Virtual container yard run by the main server (AI or rule autopilot).

What happens:
  * 3 trucks (T1-T3) take turns: an inbound truck brings a container -> the reach stacker unloads it
    into a free designated slot (A1-A5, B1-B5). When all 10 slots are full, the next truck comes
    empty (outbound) and the stacker loads the container that has been in the yard longest.
  * The user can click a container and a destination (free slot or empty truck at a bay): that
    request jumps the queue (it is the next task the server must start).
  * The reach stacker drives like a car (forward / reverse with steering, never on the spot). Its
    sensors see obstacles on the path ahead: fixed ones (cones, pallets) and moving ones (workers,
    a service car). It stops and the main server decides: horn, wait, detour (re-plan), or call a
    ground crew to clear a fixed object.
  * Picking a container up uses the M-ALIGN kit job (SimSite) for the mm-level alignment and lock.

A safety guard sits between the AI and the yard: it refuses tasks that break the rules (user request
first, slot must be free, ...) and actions that make no sense (horn at a cone, crew for a person),
and tells the AI why. If Ollama is unreachable the rule autopilot makes the decision (logged).
"""
import json
import math
import random
import threading
import time
import urllib.error
import urllib.request

import case_memory
import yard_world as Y

CAPACITY = len(Y.SLOTS)
TRUCK_IDS = ('T1', 'T2', 'T3')
COLORS = ('#d2601a', '#2a6fb0', '#2f8a4a', '#9b2d2d', '#7a7a7a', '#c8a024', '#5b3f8c', '#1f7a7a')
TRAVEL_H = Y.H + 2.6                  # spreader height while driving empty
CARRY_LIFT = 2.2                      # container bottom height while carrying
TRUCK_BED = 1.5                       # container bottom on a trailer
KIT_GAP = 0.020                       # kit coils sense 20 mm above the corner castings (Maxwell nominal gap)
HOIST_V = 0.45                        # spreader hoist / lower speed, m/s (reach stackers ~0.25-0.45 m/s)
SENSOR_RANGE = 16.0
SAFE_GAP = 0.6                        # footprint gap that counts as "path blocked"
V_FWD, V_REV, ACC, DEC = 3.0, 1.5, 0.8, 1.5
TRUCK_V = 5.0
CREW_S = 15.0
TRUCK_RECT = (1.3, 6.5, 1.5)          # half width, half length, forward offset of the rect centre


def _truck_shape(x, z, yaw):
    hw, hl, off = TRUCK_RECT
    return ('rect', x + off * math.sin(yaw), z + off * math.cos(yaw), yaw, hw, hl)


class YardOps:
    def __init__(self, site=None, controller='autopilot', ai_url='http://127.0.0.1:11434', ai_model='qwen2.5:7b',
                 seed=None, speed=1.0, people=True, debris=True, align_opts=None, aligner=None,
                 initial=7, dt=0.1, chat=None, memory=None, vary=False, kit_state=None):
        self.site = site
        self.controller = controller
        self.ai_url, self.ai_model = ai_url, ai_model
        self.seed = int(seed) if seed is not None else random.randrange(1, 1 << 30)
        self.rnd = random.Random(self.seed)
        self.speed = float(speed)
        self.people, self.debris_on = people, debris
        self.align_opts = dict(align_opts or {})
        self.aligner = aligner
        self.dt = dt
        self.chat = chat or self._ollama
        self.memory = memory
        self.vary = vary
        self.kit_state = kit_state      # callable -> kit link state; a new job waits for IDLE (stop of the last one applied)
        self.plan_info = None
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.running = False
        self.t = 0.0
        self.log_lines = []
        self.stats = {'inbound': 0, 'outbound': 0, 'user': 0, 'horns': 0, 'detours': 0, 'clears': 0, 'waits': 0,
                      'contacts': 0, 'align_fail': 0, 'guard_refusals': 0, 'ai_decisions': 0, 'rule_decisions': 0,
                      'retries': 0, 'retry_success': 0, 'cases_added': 0}
        self.ai_fail = 0
        self.n_cont = 0
        self.containers = {}
        self.slots = {sid: {'id': sid, 'x': x, 'z': z, 'yaw': yaw, 'holder': None, 'reserved': None}
                      for sid, x, z, yaw in Y.SLOTS}
        self.bays = {bid: {'id': bid, 'x': x, 'z': z, 'truck': None} for bid, x, z in Y.BAYS}
        self.trucks = {tid: {'id': tid, 'state': 'away', 'bay': None, 'mode': None, 'container': None,
                             'x': -90.0, 'z': Y.LANE_Z, 'yaw': math.pi / 2, 'path': None, 's': 0.0, 'cum': None,
                             'due': 2.0 + 18.0 * i, 'parked_t': None, 'served': False}
                       for i, tid in enumerate(TRUCK_IDS)}
        for sid in self.rnd.sample(list(self.slots), min(initial, CAPACITY)):
            s = self.slots[sid]
            c = self._new_container(s['x'], s['z'], s['yaw'], 0.0)
            c.update(where='slot', slot=sid, since=-self.rnd.uniform(60, 600))
            s['holder'] = c['id']
        self.debris = []
        self.temp = []
        self.workers = []
        self.car = None
        sx, sz, syaw = Y.HOME
        self.st = {'x': sx, 'z': sz, 'yaw': syaw}
        if people:
            for i in range(2):
                p = self._walk_point()
                self.workers.append({'id': 'W%d' % (i + 1), 'x': p[0], 'z': p[1], 'yaw': 0.0, 'target': None,
                                     'v': 1.2, 'pause': 0.0, 'yield_until': -1, 'stubborn': False, 'state': 'walk'})
            self.car_route = [(-40.0, -32.0), (40.0, -32.0), (40.0, 14.0), (-40.0, 14.0)]
            self.car = {'id': 'V1', 'x': -40.0, 'z': -32.0, 'yaw': math.pi / 2, 'leg': 1, 'v': 4.0,
                        'park_until': -1, 'next_park': 40.0, 'wait': 0.0, 'state': 'drive'}
        self.debris = []
        self.n_debris = 0
        self.next_debris = 35.0
        self.temp = []          # temporary planning obstacles (detours around moving objects)
        sx, sz, syaw = Y.HOME
        self.st = {'x': sx, 'z': sz, 'yaw': syaw, 'v': 0.0, 'dir': 1, 'mode': 'idle', 'path': None, 'cum': None,
                   's': 0.0, 'h': TRAVEL_H, 'h_target': TRAVEL_H, 'carry': None, 'horn_t': -9.0,
                   'pause': 0.0, 'blocked_by': None, 'block_dist': None}
        self.task = None
        self.requests = []
        self.n_req = 0
        self.n_task = 0
        self.alert = None
        self.align_state = None
        self.decision = None
        self.threads = []

    # ------------------------------------------------------------------ helpers
    def log(self, who, text):
        with self.lock:
            self.log_lines.append({'t': round(self.t, 1), 'at': time.time(), 'who': who, 'text': text})
            del self.log_lines[:-200]

    def _new_container(self, x, z, yaw, y0):
        self.n_cont += 1
        cid = 'C%02d' % self.n_cont
        c = {'id': cid, 'color': COLORS[(self.n_cont - 1) % len(COLORS)], 'x': x, 'z': z, 'yaw': yaw, 'y': y0,
             'where': None, 'slot': None, 'truck': None, 'since': self.t}
        self.containers[cid] = c
        return c

    def _static_shapes(self, exclude=(), temp=True):
        sh = [('circle', x, z, r) for x, z, r in Y.POLES] + [('rect',) + b for b in Y.BUILDINGS]
        for c in self.containers.values():
            if c['where'] == 'slot' and c['id'] not in exclude:
                sh.append(Y.container_rect(c['x'], c['z'], c['yaw']))
        for b in self.bays.values():
            tid = b['truck']
            if tid and tid not in exclude:
                sh.append(_truck_shape(b['x'], b['z'], math.pi / 2))
                sh.append(Y.container_rect(b['x'], b['z'], 0.0))
                ts = self.trucks[tid]['state']
                if ts in ('arriving', 'leaving'):     # the lane <-> bay swing area is the truck's while it moves
                    sh.append(self._swing_zone(b, ts))
        for d in self.debris:
            if d['known']:
                sh.append(self._inflate(d['shape'], SAFE_GAP))
        if temp:
            sh.extend(self.temp)
        return sh

    def _walk_point(self):
        for _ in range(200):
            x, z = self.rnd.uniform(-40, 40), self.rnd.uniform(-34, 26)
            if all(Y.shape_dist(s, x, z) > 2.0 for s in self._static_shapes(temp=False)) and \
                    Y.footprint_clearance(self.st['x'], self.st['z'], self.st['yaw'], [('circle', x, z, 0.4)]) > 3.0:
                return x, z
        return 0.0, 20.0

    def _dynamic(self):
        """Obstacles the stacker's sensors can run into (not in the planner's map unless detected)."""
        out = []
        for w in self.workers:
            out.append(({'id': w['id'], 'kind': 'worker', 'moving': True, 'name': '작업자 ' + w['id']},
                        ('circle', w['x'], w['z'], 0.4)))
        if self.car:
            c = self.car
            out.append(({'id': c['id'], 'kind': 'car', 'moving': True, 'name': '서비스 차량 ' + c['id']},
                        ('rect', c['x'], c['z'], c['yaw'], 0.95, 2.2)))
        for d in self.debris:
            out.append(({'id': d['id'], 'kind': d['kind'], 'moving': False, 'known': d['known'],
                         'name': {'cone': '라바콘', 'pallet': '팔레트'}[d['kind']] + ' ' + d['id']}, d['shape']))
        for t in self.trucks.values():
            if t['state'] in ('arriving', 'leaving'):
                info = {'id': t['id'], 'kind': 'truck', 'moving': True, 'name': '트럭 ' + t['id']}
                out.append((info, _truck_shape(t['x'], t['z'], t['yaw'])))
                zone = self._swing_zone(self.bays[t['bay']], t['state'])
                if Y.footprint_clearance(self.st['x'], self.st['z'], self.st['yaw'], [zone]) > 0:   # inside already: let it leave
                    out.append((dict(info, name='트럭 %s 진입·출차 구역' % t['id'], zone=True), zone))
        return out

    # ------------------------------------------------------------------ world step
    def step(self, dt):
        with self.lock:
            self.t += dt
            self._step_trucks(dt)
            self._step_workers(dt)
            self._step_car(dt)
            self._step_debris()
            self._step_stacker(dt)
            st = self.st
            st['h'] += max(-HOIST_V * dt, min(HOIST_V * dt, st['h_target'] - st['h']))
            self.cond.notify_all()

    # trucks ------------------------------------------------------------
    def _step_trucks(self, dt):
        for t in self.trucks.values():
            if t['state'] == 'away' and self.t >= t['due']:
                bay = next((b for b in self.bays.values() if b['truck'] is None), None)
                if bay is None:
                    t['due'] = self.t + 5
                    continue
                occupied = sum(1 for s in self.slots.values() if s['holder'])
                incoming = sum(1 for x in self.trucks.values() if x['state'] in ('arriving', 'parked') and x['mode'] == 'inbound')
                outgoing = sum(1 for x in self.trucks.values() if x['state'] in ('arriving', 'parked') and x['mode'] == 'outbound')
                full = occupied + incoming - outgoing >= CAPACITY
                t.update(state='arriving', bay=bay['id'], mode='outbound' if full else 'inbound', served=False)
                bay['truck'] = t['id']
                t['path'] = [(-90.0, Y.LANE_Z, math.pi / 2, 1), (bay['x'] - 18.0, Y.LANE_Z, math.pi / 2, 1)] + \
                    Y.straight((bay['x'] - 18.0, Y.LANE_Z, math.pi / 2), (bay['x'], bay['z'], math.pi / 2), 1, 1.0)
                self._set_truck_path(t)
                if t['mode'] == 'inbound':
                    c = self._new_container(t['x'], t['z'], 0.0, TRUCK_BED)
                    c.update(where='truck', truck=t['id'])
                    t['container'] = c['id']
                    self.log('트럭', '%s 입고 트럭 도착 중 (%s 적재) → %s' % (t['id'], c['id'], bay['id']))
                else:
                    t['container'] = None
                    self.log('트럭', '%s 반출용 빈 트럭 도착 중 → %s (야드 %d/%d칸 참)' % (t['id'], bay['id'], occupied, CAPACITY))
            elif t['state'] in ('arriving', 'leaving'):
                self._move_truck(t, dt)
            elif t['state'] == 'parked' and t['served']:
                gap = Y.footprint_clearance(self.st['x'], self.st['z'], self.st['yaw'], [_truck_shape(t['x'], t['z'], t['yaw'])])
                if gap > 1.5 and self.st['mode'] != 'aligning':
                    bay = self.bays[t['bay']]
                    t['state'] = 'leaving'
                    t['path'] = Y.straight((bay['x'], bay['z'], math.pi / 2), (bay['x'] + 18.0, Y.LANE_Z, math.pi / 2), 1, 1.0) + \
                        [(95.0, Y.LANE_Z, math.pi / 2, 1)]
                    t['path'].insert(0, (t['x'], t['z'], t['yaw'], 1))
                    self._set_truck_path(t)
                    self.log('트럭', '%s 출발 (%s)' % (t['id'], '컨테이너 %s 싣고 반출' % t['container'] if t['container'] else '빈 차'))

    @staticmethod
    def _swing_zone(bay, state):
        dx = -5.5 if state == 'arriving' else 5.5      # bay + the lane swing, with a margin below the bay front
        return ('rect', bay['x'] + dx, 33.25, 0.0, 14.5, 4.25)

    def _set_truck_path(self, t):
        cum = [0.0]
        for a, b in zip(t['path'], t['path'][1:]):
            cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        t['cum'], t['s'] = cum, 0.0
        t['x'], t['z'], t['yaw'] = t['path'][0][:3]

    def _move_truck(self, t, dt):
        # trucks wait in the lane until the swing area is clear, then do not stop for the stacker (emergency only)
        bay = self.bays[t['bay']]
        in_lane = t['state'] == 'arriving' and t['x'] < bay['x'] - 18.5
        if in_lane and t['x'] > bay['x'] - 30:
            if Y.footprint_clearance(self.st['x'], self.st['z'], self.st['yaw'], [self._swing_zone(bay, 'arriving')]) < 0:
                return
        if t['state'] == 'leaving' and t['s'] < 1.0:
            if Y.footprint_clearance(self.st['x'], self.st['z'], self.st['yaw'], [self._swing_zone(bay, 'leaving')]) < 0:
                return
        nx, nz, ny = self._interp(t['path'], t['cum'], t['s'] + 3.0)
        if Y.footprint_clearance(self.st['x'], self.st['z'], self.st['yaw'], [_truck_shape(nx, nz, ny)]) < 0.3:
            return
        fx, fz = math.sin(t['yaw']), math.cos(t['yaw'])
        for o in self.trucks.values():
            if o is not t and o['state'] in ('arriving', 'leaving'):
                ahead = (o['x'] - t['x']) * fx + (o['z'] - t['z']) * fz
                lateral = abs((o['x'] - t['x']) * fz - (o['z'] - t['z']) * fx)
                if 0 < ahead < 17 and lateral < 3.0:
                    return
        t['s'] = min(t['cum'][-1], t['s'] + TRUCK_V * dt)
        t['x'], t['z'], t['yaw'] = self._interp(t['path'], t['cum'], t['s'])
        cid = t['container']
        if cid and self.containers[cid]['where'] == 'truck':
            c = self.containers[cid]
            c.update(x=t['x'], z=t['z'], yaw=t['yaw'] - math.pi / 2, y=TRUCK_BED)
        if t['s'] >= t['cum'][-1] - 1e-6:
            if t['state'] == 'arriving':
                t['state'] = 'parked'
                t['parked_t'] = self.t
                t['yaw'] = math.pi / 2
                self.log('트럭', '%s %s 정차 완료 (%s)' % (t['id'], t['bay'], '하역 대기' if t['mode'] == 'inbound' else '상차 대기'))
            else:
                if cid:
                    self.containers[cid]['where'] = 'gone'
                    self.containers.pop(cid, None)
                self.bays[t['bay']]['truck'] = None
                t.update(state='away', bay=None, container=None, due=self.t + self.rnd.uniform(15, 35), served=False)

    @staticmethod
    def _interp(path, cum, s):
        if s <= 0:
            return path[0][:3]
        if s >= cum[-1]:
            return path[-1][:3]
        lo, hi = 0, len(cum) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if cum[mid] <= s:
                lo = mid
            else:
                hi = mid
        a, b = path[lo], path[hi]
        f = (s - cum[lo]) / max(1e-9, cum[hi] - cum[lo])
        dy = (b[2] - a[2] + math.pi) % (2 * math.pi) - math.pi
        return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + dy * f

    # people / car ------------------------------------------------------
    def _path_point_ahead(self, ahead):
        st = self.st
        if st['mode'] != 'driving' or not st['path'] or st['cum'][-1] - st['s'] < ahead + 2:
            return None
        return self._interp(st['path'], st['cum'], st['s'] + ahead)

    def _step_workers(self, dt):
        for w in self.workers:
            if w['pause'] > 0:
                w['pause'] -= dt
                if w['pause'] <= 0 and w['state'] == 'phone':
                    w['state'] = 'walk'
                    w['stubborn'] = False
                continue
            if w['target'] is None:
                p = self._path_point_ahead(self.rnd.uniform(11, 15)) if self.t > w['yield_until'] and self.rnd.random() < 0.35 else None
                if p:
                    w['target'], w['cross'] = (p[0], p[1]), True
                else:
                    w['target'], w['cross'] = self._walk_point(), False
                w['v'] = 1.2
            tx, tz = w['target']
            d = math.hypot(tx - w['x'], tz - w['z'])
            # people do not walk into the machine's body
            nxt = ('circle', w['x'] + (tx - w['x']) / max(d, 1e-6) * .8, w['z'] + (tz - w['z']) / max(d, 1e-6) * .8, .4)
            gap_next = Y.footprint_clearance(self.st['x'], self.st['z'], self.st['yaw'], [nxt])
            if gap_next < 1.0 and gap_next < Y.footprint_clearance(self.st['x'], self.st['z'], self.st['yaw'],
                                                                  [('circle', w['x'], w['z'], .4)]):
                w['frozen'] = w.get('frozen', 0.0) + dt
                if w['frozen'] > 3.0:          # the machine is in the way: walk somewhere else
                    w['target'], w['cross'], w['frozen'] = self._walk_point(), False, 0.0
                continue
            w['frozen'] = 0.0
            if d < 0.3:
                w['target'] = None
                if w.get('cross') and self.rnd.random() < 0.55:
                    w['state'], w['stubborn'], w['pause'] = 'phone', True, self.rnd.uniform(25, 45)
                else:
                    w['pause'] = self.rnd.uniform(2, 6)
                continue
            step = min(d, w['v'] * dt)
            w['yaw'] = math.atan2(tx - w['x'], tz - w['z'])
            w['x'] += (tx - w['x']) / d * step
            w['z'] += (tz - w['z']) / d * step

    def _step_car(self, dt):
        c = self.car
        if not c:
            return
        if self.t < c['park_until']:
            return
        if c['state'] == 'park':
            c['state'] = 'drive'
            c['next_park'] = self.t + self.rnd.uniform(30, 70)
        if self.t >= c['next_park']:
            c['state'] = 'park'
            c['park_until'] = self.t + self.rnd.uniform(25, 50)
            return
        tx, tz = self.car_route[c['leg']]
        d = math.hypot(tx - c['x'], tz - c['z'])
        if d < 0.5:
            c['leg'] = (c['leg'] + 1) % len(self.car_route)
            return
        yaw = math.atan2(tx - c['x'], tz - c['z'])
        ahead = ('rect', c['x'] + 4 * math.sin(yaw), c['z'] + 4 * math.cos(yaw), yaw, 1.2, 3.0)
        if Y.footprint_clearance(self.st['x'], self.st['z'], self.st['yaw'], [ahead]) < 0.8:
            c['wait'] += dt
            if c['wait'] > 10:       # give way: turn back (3-point turn shortened)
                c['leg'] = (c['leg'] - 1) % len(self.car_route)
                c['wait'] = 0.0
            return
        c['wait'] = 0.0
        c['yaw'] = yaw
        step = min(d, c['v'] * dt)
        c['x'] += (tx - c['x']) / d * step
        c['z'] += (tz - c['z']) / d * step

    def _step_debris(self):
        for d in list(self.debris):
            if d['clear_at'] is not None and self.t >= d['clear_at']:
                self.debris.remove(d)
                self.log('현장', '작업반이 %s 치움' % d['id'])
        if not self.debris_on or self.t < self.next_debris or len(self.debris) >= 3:
            return
        self.next_debris = self.t + self.rnd.uniform(55, 100)
        p = self._path_point_ahead(self.rnd.uniform(20, 26))
        if p is None:
            x, z = self._walk_point()
        else:
            x, z = p[0], p[1]
        if Y.footprint_clearance(self.st['x'], self.st['z'], self.st['yaw'], [('circle', x, z, 1.0)]) < 3:
            return
        self.n_debris += 1
        kind = self.rnd.choice(('cone', 'pallet'))
        yaw = self.rnd.uniform(0, math.pi)
        shape = ('circle', x, z, 0.45) if kind == 'cone' else ('rect', x, z, yaw, 0.6, 0.5)
        self.debris.append({'id': 'O%d' % self.n_debris, 'kind': kind, 'x': x, 'z': z, 'yaw': yaw, 'shape': shape,
                            'known': False, 'clear_at': None})

    # stacker -------------------------------------------------------------
    def _detect(self):
        st = self.st
        if not st['path']:
            return None, None
        dyn = [(info, sh) for info, sh in self._dynamic()
               if Y.shape_dist(sh, st['x'], st['z']) < SENSOR_RANGE]
        if not dyn:
            return None, None
        s0, end = st['s'], st['cum'][-1]
        s = s0
        while s <= min(end, s0 + SENSOR_RANGE):
            x, z, y = self._interp(st['path'], st['cum'], s)
            for info, sh in dyn:
                if info['id'] in st.get('ignore', ()):
                    continue
                if Y.footprint_clearance(x, z, y, [sh]) < (0.0 if info.get('zone') else 0.3 if info.get('known') else SAFE_GAP):
                    return (info, sh), s - s0
            s += 0.7
        return None, None

    def _step_stacker(self, dt):
        st = self.st
        # safety: count contacts (should stay 0)
        for info, sh in self._dynamic():
            if info['kind'] != 'truck' and Y.footprint_clearance(st['x'], st['z'], st['yaw'], [sh]) < 0:
                if st.get('contact') != info['id']:
                    st['contact'] = info['id']
                    self.stats['contacts'] += 1
                    self.log('안전장치', '접촉 경고: %s (리치스태커 %s)' % (info['name'], st['mode']))
                break
        else:
            st['contact'] = None
        if st['mode'] != 'driving':
            return
        if st['pause'] > 0:
            st['pause'] -= dt
            return
        hit, dist = self._detect()
        path, cum = st['path'], st['cum']
        # next stop: direction change or the end
        i = max(0, min(len(cum) - 1, self._index(cum, st['s'])))
        d0 = path[min(i + 1, len(path) - 1)][3] or 1
        j = i + 1
        while j < len(path) and (path[j][3] or d0) == d0:
            j += 1
        stop_s = cum[min(j, len(cum)) - 1] if j < len(path) else cum[-1]
        room = stop_s - st['s']
        if hit:
            info, sh = hit
            if not info['moving']:
                for d in self.debris:
                    if d['id'] == info['id'] and not d['known']:
                        d['known'] = True
            room = min(room, max(0.0, dist - 1.0))
        vmax = V_FWD if d0 > 0 else V_REV
        v_allow = min(vmax, math.sqrt(2 * DEC * max(0.0, room)))
        if st['v'] < v_allow:
            st['v'] = min(v_allow, st['v'] + ACC * dt)
        else:
            st['v'] = max(v_allow, st['v'] - (DEC if not hit or dist > 3 else 4.0) * dt)
        st['dir'] = d0
        if hit and (st['v'] < 0.05 or room < 0.05):
            st['v'] = 0.0
            st['mode'] = 'blocked'
            st['blocked_by'] = hit
            st['block_dist'] = round(dist, 1)
            return
        st['s'] = min(stop_s, st['s'] + st['v'] * dt)
        st['x'], st['z'], st['yaw'] = self._interp(path, cum, st['s'])
        if st['s'] >= stop_s - 1e-6:
            st['v'] = 0.0
            if stop_s >= cum[-1] - 1e-6:
                st['mode'] = 'arrived'
                st['x'], st['z'], st['yaw'] = path[-1][:3]
            else:
                st['pause'] = 0.8          # gear change forward <-> reverse
                st['s'] += 1e-3

    @staticmethod
    def _index(cum, s):
        lo, hi = 0, len(cum) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if cum[mid] <= s:
                lo = mid
            else:
                hi = mid
        return lo

    def set_path(self, path, ignore=()):
        with self.lock:
            cum = [0.0]
            for a, b in zip(path, path[1:]):
                cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
            st = self.st
            st.update(path=path, cum=cum, s=0.0, mode='driving', v=0.0, blocked_by=None, ignore=set(ignore))

    # ------------------------------------------------------------------ waiting (sim time)
    def wait_sim(self, seconds):
        with self.cond:
            end = self.t + seconds
            while self.running and self.t < end:
                self.cond.wait(0.5)
        return self.running

    def wait_until(self, pred, timeout=None):
        with self.cond:
            end = None if timeout is None else self.t + timeout
            while self.running and not pred():
                if end is not None and self.t >= end:
                    return False
                self.cond.wait(0.5)
        return self.running

    # ------------------------------------------------------------------ run
    def start(self):
        self.running = True
        self.log('서버', '야드 운영 시작 · 제어: %s · 컨테이너 %d개 / 지정 구역 %d칸 · 트럭 3대 순환' % (
            'Ollama AI (%s)' % self.ai_model if self.controller == 'ai' else '규칙 자동조종',
            sum(1 for s in self.slots.values() if s['holder']), CAPACITY))
        for fn, name in ((self._world_loop, 'yard-world'), (self._control_loop, 'yard-control')):
            th = threading.Thread(target=fn, name=name, daemon=True)
            th.start()
            self.threads.append(th)
        return self

    def stop(self):
        self.running = False
        with self.cond:
            self.cond.notify_all()
        if self.site is not None:
            try:
                if self.site.job and self.site.job.get('outcome') is None:
                    self.site.abort('yard_stopped')
            except Exception:
                pass

    def _world_loop(self):
        while self.running:
            t0 = time.time()
            self.step(self.dt)
            sl = self.dt / max(self.speed, 1e-6) - (time.time() - t0)
            if sl > 0:
                time.sleep(sl)

    def _control_loop(self):
        while self.running:
            try:
                task = self._choose_task()
                if not task:
                    self.wait_sim(3)
                    continue
                self._run_task(task)
            except Exception as exc:       # keep the yard alive, report
                self.log('서버', '오류: %s: %s' % (type(exc).__name__, exc))
                self.task = None
                self.wait_sim(3)

    # ------------------------------------------------------------------ status for the brain
    def status(self):
        with self.lock:
            occ = [{'slot': s['id'], 'container': s['holder'], 'in_yard_s': round(self.t - self.containers[s['holder']]['since'])}
                   for s in self.slots.values() if s['holder']]
            free = [s['id'] for s in self.slots.values() if not s['holder'] and not s['reserved']]
            trucks = [{'truck': t['id'], 'bay': t['bay'], 'type': t['mode'], 'container': t['container'],
                       'waiting_s': round(self.t - t['parked_t'])}
                      for t in self.trucks.values() if t['state'] == 'parked' and not t['served']]
            arriving = [{'truck': t['id'], 'type': t['mode']} for t in self.trucks.values() if t['state'] == 'arriving']
            return {'time_s': round(self.t), 'capacity': CAPACITY, 'occupied': occ, 'free_slots': free,
                    'trucks_waiting': trucks, 'trucks_arriving': arriving,
                    'user_requests': [{'id': r['id'], 'container': r['container'], 'to': r['dest']} for r in self.requests],
                    'stacker': {'x': round(self.st['x'], 1), 'z': round(self.st['z'], 1), 'carrying': self.st['carry']}}

    # ------------------------------------------------------------------ user requests
    def request(self, container, dest):
        with self.lock:
            c = self.containers.get(container)
            if not c or c['where'] not in ('slot', 'truck'):
                raise ValueError('옮길 수 없는 컨테이너: %s' % container)
            if self.task and self.task.get('container') == container:
                raise ValueError('%s 는 지금 작업 중입니다' % container)
            if dest in self.slots:
                s = self.slots[dest]
                if s['holder'] or s['reserved']:
                    raise ValueError('%s 는 비어 있지 않습니다' % dest)
            elif dest in self.trucks:
                t = self.trucks[dest]
                if t['state'] != 'parked' or t['container'] or t['served'] or c['where'] != 'slot':
                    raise ValueError('%s 에는 실을 수 없습니다 (정차한 빈 트럭만)' % dest)
            else:
                raise ValueError('목적지 오류: %s' % dest)
            if any(r['container'] == container for r in self.requests):
                self.requests = [r for r in self.requests if r['container'] != container]
            self.n_req += 1
            r = {'id': 'R%d' % self.n_req, 'container': container, 'dest': dest, 't': self.t}
            self.requests.append(r)
            self.log('사용자', '우선 지시 %s: %s → %s' % (r['id'], container, dest))
            return r

    def _task_from_request(self, r):
        c = self.containers.get(r['container'])
        if not c or c['where'] not in ('slot', 'truck'):
            return None
        src = c['slot'] if c['where'] == 'slot' else c['truck']
        kind = 'unload' if c['where'] == 'truck' else ('load' if r['dest'] in self.trucks else 'move')
        return {'kind': kind, 'source': src, 'dest': r['dest'], 'request': r['id']}

    # ------------------------------------------------------------------ guard
    def check_task(self, task):
        """None if allowed, else the reason (Korean/English mix kept short for the AI)."""
        kind, src, dst = task.get('kind'), task.get('source'), task.get('dest')
        if self.requests:
            r = self.requests[0]
            want = self._task_from_request(r)
            if want and (want['source'], want['dest']) != (src, dst):
                return 'user request %s (%s -> %s) has priority: start it first' % (r['id'], r['container'], r['dest'])
        if kind == 'unload':
            t = self.trucks.get(src)
            if not t or t['state'] != 'parked' or t['served'] or not t['container']:
                return '%s is not a parked truck with a container' % src
        elif kind in ('load', 'move'):
            s = self.slots.get(src)
            if not s or not s['holder']:
                return 'slot %s holds no container' % src
        else:
            return 'kind must be unload / load / move'
        if kind == 'load':
            t = self.trucks.get(dst)
            if not t or t['state'] != 'parked' or t['container'] or t['served']:
                return '%s is not a parked empty truck' % dst
        else:
            s = self.slots.get(dst)
            if not s:
                return 'destination must be a slot A1-B5'
            if s['holder'] or s['reserved']:
                return 'slot %s is not free' % dst
        return None

    def check_action(self, alert, action):
        moving = alert['moving']
        if action == 'horn' and not moving:
            return 'horn does nothing for a fixed object: detour or request_clear'
        if action == 'clear' and moving:
            return 'the ground crew clears fixed objects only: horn, wait or detour'
        if action not in ('horn', 'wait', 'detour', 'clear', 'stop'):
            return 'unknown action'
        return None

    # ------------------------------------------------------------------ brains
    def _rule_task(self, s):
        if self.requests:
            return self._task_from_request(self.requests[0])
        waiting = sorted(s['trucks_waiting'], key=lambda x: -x['waiting_s'])
        for tw in waiting:
            if tw['type'] == 'outbound' and not tw['container'] and s['occupied']:
                oldest = max(s['occupied'], key=lambda o: o['in_yard_s'])
                return {'kind': 'load', 'source': oldest['slot'], 'dest': tw['truck']}
            if tw['type'] == 'inbound' and tw['container'] and s['free_slots']:
                bay = self.bays[tw['bay']]
                slot = min(s['free_slots'], key=lambda k: math.hypot(self.slots[k]['x'] - bay['x'], self.slots[k]['z'] - bay['z']))
                return {'kind': 'unload', 'source': tw['truck'], 'dest': slot}
        return None

    def _rule_obstacle(self, a):
        if a['kind'] == 'truck':
            if a['waited_s'] < 20 or a.get('detours', 0) >= 2:
                return {'action': 'wait', 'seconds': 5, 'why': '트럭 진입/출차 중 → 트럭 우선, 대기'}
            return {'action': 'detour', 'why': '트럭이 비키지 않음 → 우회'}
        if a['moving']:
            if a['horns'] < 2 or (a.get('detour_failed') and a['horns'] < 2 + a['detour_failed']) \
                    or (a.get('creep') and a['waited_s'] >= 10 * a['horns']):
                return {'action': 'horn', 'why': '사람/차량 → 경적으로 비켜 달라고 알림'}
            if a.get('creep'):
                return {'action': 'wait', 'seconds': 4, 'why': '좁은 진입 구간이라 우회 불가 → 대기'}
            if a.get('detours', 0) >= 2:      # it keeps stepping into the new path: stop chasing, wait and warn
                if a['waited_s'] >= 8 * (a['horns'] - 1):
                    return {'action': 'horn', 'why': '우회해도 다시 경로에 들어옴 → 경적'}
                return {'action': 'wait', 'seconds': 5, 'why': '우회해도 다시 경로에 들어옴 → 비킬 때까지 대기'}
            return {'action': 'detour', 'why': '경적 %d회에도 안 비킴 → 우회' % a['horns']}
        if a.get('detour_extra_m') is not None and a['detour_extra_m'] < 30:
            return {'action': 'detour', 'why': '고정 장애물, 우회가 짧음(+%.0f m)' % a['detour_extra_m']}
        return {'action': 'clear', 'why': '고정 장애물, 우회 경로가 %s → 작업반 호출' % (
            '없음' if a.get('detour_extra_m') is None else '김(+%.0f m)' % a['detour_extra_m'])}

    def _purge_requests(self):
        with self.lock:
            keep = []
            for r in self.requests:
                ok = self._task_from_request(r) is not None
                d = r['dest']
                if ok and d in self.slots:
                    ok = not self.slots[d]['holder'] and not self.slots[d]['reserved']
                elif ok:
                    t = self.trucks[d]
                    ok = t['state'] == 'parked' and not t['container'] and not t['served']
                if ok:
                    keep.append(r)
                else:
                    self.log('서버', '우선 지시 %s 취소 (상황이 바뀜: %s → %s 불가)' % (r['id'], r['container'], d))
            self.requests = keep

    def _choose_task(self):
        self._purge_requests()
        s = self.status()
        if not s['trucks_waiting'] and not s['user_requests']:
            return None
        if self.controller == 'ai' and self.ai_fail < 3:
            task = self._ai_decide('task', s)
            if task is not None:
                return task
        task = self._rule_task(s)
        if task:
            self.stats['rule_decisions'] += 1
            if self.controller == 'ai':
                self.log('서버', '규칙 자동조종이 대신 결정')
        return task

    # ---- Ollama
    SYSTEM_TASK = """You are the MAIN SERVER AI of a container yard with one reach stacker, 10 designated slots
(A1-A5, B1-B5) and trucks arriving at 3 bays. Decide the NEXT task with ONE tool call.
Rules: user_requests come first (start the first one exactly). An inbound truck brings a container:
unload it into a free slot (prefer one close to the truck). An outbound truck is empty: load the container that
has been in the yard the longest (largest in_yard_s). Serve the truck that has waited longest. If nothing to do, wait.
Before the tool call write one short Korean sentence explaining the choice."""
    SYSTEM_OBST = """You are the MAIN SERVER AI driving a reach stacker in a container yard. Its sensors stopped it:
an object blocks the planned path. Choose ONE action with ONE tool call.
Trucks (kind=truck) have right of way: wait for them (5 s at a time); detour only if they do not clear.
People and vehicles (moving=true): sound_horn first and they usually step aside; if they still block after
horns, detour; if they block again after 2 detours (detours >= 2), wait and sound the horn instead of detouring again. Fixed objects (cone, pallet): detour if detour_extra_m is short (< 30 m), otherwise request_clear
(a ground crew removes it in ~15 s). Never drive through. Write one short Korean sentence first."""
    TOOLS_TASK = [
        {'type': 'function', 'function': {'name': 'start_task', 'description': 'Start a task. kind: unload (source = truck id, dest = slot), load (source = slot, dest = truck id), move (source = slot, dest = slot).',
         'parameters': {'type': 'object', 'properties': {'kind': {'type': 'string', 'enum': ['unload', 'load', 'move']},
                        'source': {'type': 'string'}, 'dest': {'type': 'string'}}, 'required': ['kind', 'source', 'dest']}}},
        {'type': 'function', 'function': {'name': 'wait', 'description': 'Nothing to do now.',
         'parameters': {'type': 'object', 'properties': {'seconds': {'type': 'number'}}, 'required': []}}}]
    TOOLS_OBST = [
        {'type': 'function', 'function': {'name': 'sound_horn', 'description': 'Sound the horn so people / vehicles move away, then wait a few seconds.',
         'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
        {'type': 'function', 'function': {'name': 'wait', 'description': 'Wait in place.',
         'parameters': {'type': 'object', 'properties': {'seconds': {'type': 'number'}}, 'required': ['seconds']}}},
        {'type': 'function', 'function': {'name': 'detour', 'description': 'Re-plan the path around the object (forward/reverse with steering).',
         'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
        {'type': 'function', 'function': {'name': 'request_clear', 'description': 'Call the ground crew to remove a FIXED object (~15 s).',
         'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
        {'type': 'function', 'function': {'name': 'stop_task', 'description': 'Give up this task safely.',
         'parameters': {'type': 'object', 'properties': {'reason': {'type': 'string'}}, 'required': ['reason']}}}]

    def _ollama(self, messages, tools):
        body = json.dumps({'model': self.ai_model, 'messages': messages, 'tools': tools, 'stream': False,
                           'options': {'temperature': 0}}).encode('utf-8')
        req = urllib.request.Request(self.ai_url.rstrip('/') + '/api/chat', data=body,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode('utf-8'))['message']

    def _ai_decide(self, what, info):
        """One decision through the AI with the guard; None -> caller uses the rule autopilot."""
        if what == 'task':
            msgs = [{'role': 'system', 'content': self.SYSTEM_TASK},
                    {'role': 'user', 'content': 'Yard status: ' + json.dumps(info, ensure_ascii=False)}]
            tools = self.TOOLS_TASK
        else:
            msgs = [{'role': 'system', 'content': self.SYSTEM_OBST},
                    {'role': 'user', 'content': 'Blocked: ' + json.dumps(info, ensure_ascii=False)}]
            tools = self.TOOLS_OBST
        for attempt in range(3):
            try:
                msg = self.chat(msgs, tools)
                self.ai_fail = 0
            except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
                self.ai_fail += 1
                self.log('서버', 'Ollama 응답 없음(%s)%s' % (exc, ' — 이후 규칙 자동조종으로 전환' if self.ai_fail >= 3 else ''))
                return None
            msgs.append(msg)
            if msg.get('content'):
                self.log('AI', msg['content'].strip()[:300])
            tcs = msg.get('tool_calls') or []
            if not tcs:
                msgs.append({'role': 'user', 'content': 'Answer with exactly one tool call.'})
                continue
            fn = tcs[0].get('function', {})
            args = fn.get('arguments') or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            name = fn.get('name')
            self.log('도구', '%s(%s)' % (name, json.dumps(args, ensure_ascii=False)))
            if what == 'task':
                if name == 'wait':
                    if self._rule_task(self.status()) is None:
                        self.stats['ai_decisions'] += 1
                        return None
                    reason = 'there is work waiting (truck or user request): start a task'
                elif name == 'start_task':
                    task = {'kind': args.get('kind'), 'source': str(args.get('source', '')), 'dest': str(args.get('dest', ''))}
                    reason = self.check_task(task)
                    if reason is None:
                        if self.requests:
                            task['request'] = self.requests[0]['id']
                        self.stats['ai_decisions'] += 1
                        return task
                else:
                    reason = 'unknown tool'
            else:
                act = {'sound_horn': 'horn', 'wait': 'wait', 'detour': 'detour', 'request_clear': 'clear',
                       'stop_task': 'stop'}.get(name)
                reason = self.check_action(info, act) if act else 'unknown tool'
                if reason is None:
                    self.stats['ai_decisions'] += 1
                    return {'action': act, 'seconds': args.get('seconds'), 'why': 'AI'}
            self.stats['guard_refusals'] += 1
            self.log('안전장치', '거부: ' + reason)
            msgs.append({'role': 'tool', 'content': json.dumps({'refused': reason})})
        return None

    # ------------------------------------------------------------------ task execution
    def _pose_of(self, ref):
        if ref in self.slots:
            s = self.slots[ref]
            return s['x'], s['z'], s['yaw'], 0.0
        t = self.trucks[ref]
        b = self.bays[t['bay']]
        return b['x'], b['z'], 0.0, TRUCK_BED

    def _run_task(self, task):
        with self.lock:
            self.n_task += 1
            task = dict(task, id='K%d' % self.n_task)
            if task['kind'] == 'unload':
                cid = self.trucks[task['source']]['container']
            else:
                cid = self.slots[task['source']]['holder']
            task['container'] = cid
            if task['dest'] in self.slots:
                self.slots[task['dest']]['reserved'] = task['id']
            if task.get('request'):
                self.requests = [r for r in self.requests if r['id'] != task['request']]
            self.task = task
        name = {'unload': '하역', 'load': '상차(반출)', 'move': '이동'}[task['kind']]
        self.log('서버', '작업 %s 시작: %s %s → %s%s' % (task['id'], cid, task['source'], task['dest'],
                                                   ' (사용자 우선 지시 %s)' % task['request'] if task.get('request') else ' · ' + name))
        ok = False
        try:
            ok = self._do_task(task)
        finally:
            with self.lock:
                if task['dest'] in self.slots and self.slots[task['dest']]['reserved'] == task['id']:
                    self.slots[task['dest']]['reserved'] = None
                self.task = None
                self.temp = []
                self.alert = None
        if ok:
            with self.lock:
                if task['kind'] == 'unload':
                    self.stats['inbound'] += 1
                elif task['kind'] == 'load':
                    self.stats['outbound'] += 1
                if task.get('request'):
                    self.stats['user'] += 1
            self.log('서버', '작업 %s 완료 (%s → %s)' % (task['id'], cid, task['dest']))

    def _do_task(self, task):
        cid = task['container']
        sx, sz, syaw, sy = self._pose_of(task['source'])
        task['src'] = (sx, sz, syaw, sy)
        src_ex = (cid, task['source']) if task['kind'] == 'unload' else (cid,)
        # 1 drive to the container (line-up point), roll in
        self.task['step'] = '컨테이너로 이동'
        if not self._go(Y.predock_pose(sx, sz, syaw)):
            return False
        self.task['step'] = '진입'
        if not self._creep(Y.dock_pose(sx, sz, syaw), 1, ignore=src_ex):
            return False
        # 2 kit alignment + lock
        self.task['step'] = 'M-ALIGN 정렬·결합'
        with self.lock:
            self.st['h_target'] = sy + Y.H + KIT_GAP
        self.wait_until(lambda: abs(self.st['h'] - self.st['h_target']) < 0.005, 40)
        if not self._align(task):
            with self.lock:
                self.st['h_target'] = TRAVEL_H
            self._creep(Y.predock_pose(sx, sz, syaw), -1, ignore=src_ex)
            self.log('서버', '%s 결합 실패 → 작업 보류 (다음에 다시 시도)' % cid)
            return False
        # 3 lift
        self.task['step'] = '들어 올림'
        with self.lock:
            c = self.containers[cid]
            if task['kind'] == 'unload':
                t = self.trucks[task['source']]
                t['container'] = None
                t['served'] = True
            else:
                self.slots[task['source']]['holder'] = None
            c.update(where='stacker', slot=None, truck=None)
            self.st['carry'] = cid
            self.st['h_target'] = CARRY_LIFT + Y.H
        self.wait_until(lambda: abs(self.st['h'] - self.st['h_target']) < 0.005, 60)
        self.task['step'] = '후진으로 빠지기'
        self._creep(Y.predock_pose(sx, sz, syaw), -1, ignore=src_ex)
        # 4 drive to the destination
        dx, dz, dyaw, dy = self._pose_of(task['dest'])
        self.task['step'] = '목적지로 운반'
        while not self._go(Y.predock_pose(dx, dz, dyaw)):
            if not self.running:
                return False
            self.log('서버', '운반 경로 없음 → 대기 후 재시도')
            self.wait_sim(5)
        self.task['step'] = '진입'
        self._creep(Y.dock_pose(dx, dz, dyaw), 1, ignore=(task['dest'],))
        self.task['step'] = '내려놓기'
        with self.lock:
            self.st['h_target'] = dy + Y.H
        self.wait_until(lambda: abs(self.st['h'] - self.st['h_target']) < 0.005, 60)
        with self.lock:
            c = self.containers[cid]
            c.update(x=dx, z=dz, yaw=dyaw, y=dy, since=self.t)
            if task['kind'] == 'load':
                t = self.trucks[task['dest']]
                t['container'] = cid
                t['served'] = True
                c.update(where='truck', truck=t['id'])
            else:
                self.slots[task['dest']]['holder'] = cid
                c.update(where='slot', slot=task['dest'])
            self.st['carry'] = None
            self.st['h_target'] = TRAVEL_H
        self.log('기계', '%s 트위스트록 해제 → %s 에 내려놓음' % (cid, task['dest']))
        self.wait_until(lambda: self.st['h'] > TRAVEL_H - 0.5, 60)
        self.task['step'] = '후진으로 빠지기'
        self._creep(Y.predock_pose(dx, dz, dyaw), -1, ignore=(task['dest'], cid))
        return True

    def _creep(self, goal, dirn, ignore=()):
        with self.lock:
            p0 = (self.st['x'], self.st['z'], self.st['yaw'])
        path = [(p0[0], p0[1], p0[2], dirn)] + Y.straight(p0, goal, dirn)
        return self._follow(path, goal, ignore=ignore, creep=True)

    def _plan(self, goal, extra_temp=()):
        with self.lock:
            start = (self.st['x'], self.st['z'], self.st['yaw'])
            exclude = (self.st['carry'],) if self.st['carry'] else ()
            shapes = self._static_shapes(exclude) + list(extra_temp)
            # a zone the stacker already stands in (e.g. a truck was dispatched meanwhile) must not trap it
            shapes = [self._shrink_from(sh, start) for sh in shapes]
            shapes = [sh for sh in shapes if sh is not None]
        df = Y.DistanceField(shapes)
        if math.hypot(goal[0] - start[0], goal[1] - start[1]) < 0.7 and abs(Y.wrap(goal[2] - start[2])) < 0.17:
            return [(start[0], start[1], start[2], 1), (goal[0], goal[1], goal[2], 1)]
        return Y.plan(df, start, goal)

    def _go(self, goal):
        for attempt in range(4):
            path = self._plan(goal)
            if path:
                break
            with self.lock:
                self.temp = []
                on_goal = [d for d in self.debris if d['known'] and d['clear_at'] is None and
                           Y.footprint_clearance(goal[0], goal[1], goal[2], [d['shape']]) < SAFE_GAP + Y.MARGIN]
                for d in on_goal:
                    d['clear_at'] = self.t + CREW_S
                    self.stats['clears'] += 1
            if on_goal:
                self.log('서버', '목표 지점에 %s → 작업반 호출 (우회로는 도달 불가)' % ', '.join(d['id'] for d in on_goal))
                ids = {d['id'] for d in on_goal}
                self.wait_until(lambda: not any(d['id'] in ids for d in self.debris), CREW_S + 30)
                continue
            with self.lock:
                movers = [info for info, sh in self._dynamic()
                          if info['moving'] and not info.get('zone') and Y.footprint_clearance(goal[0], goal[1], goal[2], [sh]) < 1.0]
                if movers:
                    self.st['horn_t'] = self.t
                    self.stats['horns'] += 1
                    self._horn_effect(force=True)
            if movers:
                self.log('기계', '목표 지점에 %s → 경적 ♪ 후 대기' % ', '.join(m['name'] for m in movers))
                self.wait_sim(5)
                continue
            self.log('서버', '경로 계획 실패 (막힘) → 5초 대기 후 재계획')
            self.wait_sim(5)
        else:
            return False
        self.log('서버', '경로 계획: %.0f m · 후진 전환 %d회' % (Y.path_length(path),
                                                       sum(1 for a, b in zip(path, path[1:]) if a[3] != b[3])))
        return self._follow(path, goal)

    def _follow(self, path, goal, ignore=(), creep=False):
        self.set_path(path, ignore)
        tries = {}
        while self.running:
            self.wait_until(lambda: self.st['mode'] in ('arrived', 'blocked'))
            if not self.running:
                return False
            with self.lock:
                st = self.st
                if st['mode'] == 'arrived':
                    st['mode'] = 'idle'
                    st['path'] = None
                    self.alert = None
                    return True
                info, sh = st['blocked_by']
                rec = tries.setdefault(info['id'], {'horns': 0, 'waited': 0.0, 't0': self.t})
                alert = {'object': info['name'], 'id': info['id'], 'kind': info['kind'], 'moving': info['moving'],
                         'distance_m': st['block_dist'], 'horns_so_far': rec['horns'], 'horns': rec['horns'],
                         'waited_s': round(rec['waited'], 1), 'stopped_s': round(self.t - rec['t0'], 1),
                         'detour_failed': rec.get('detour_failed', 0), 'detours': rec.get('detours', 0), 'creep': creep}
                self.alert = alert
            if alert['waited_s'] == 0 and alert['horns'] == 0:
                self.log('기계', '장애물 감지: %s → %.1f m 앞에서 정지' % (info['name'], alert['distance_m']))
            # fixed object: price the detour for the decision
            if not info['moving'] and not creep:
                with self.lock:
                    infl = self._safe_inflate(sh, SAFE_GAP + 0.3)
                det = self._plan(goal, [infl])
                alert['detour_extra_m'] = None if det is None else round(
                    max(0.0, Y.path_length(det) - (self.st['cum'][-1] - self.st['s'])), 1)
            dec = self._decide_obstacle(alert)
            act = dec['action']
            with self.lock:
                self.decision = dict(dec, t=self.t, object=info['name'])
            if act == 'horn':
                with self.lock:
                    self.st['horn_t'] = self.t
                    self.stats['horns'] += 1
                    self._horn_effect()
                rec['horns'] += 1
                self.log('기계', '경적 ♪ (%s)' % info['name'])
                self.wait_sim(3)
                rec['waited'] += 3
            elif act == 'wait':
                sec = max(1.0, min(30.0, float(dec.get('seconds') or 4)))
                self.stats['waits'] += 1
                self.log('서버', '대기 %.0f초' % sec)
                self.wait_sim(sec)
                rec['waited'] += sec
            elif act == 'clear':
                with self.lock:
                    for d in self.debris:
                        if d['id'] == info['id'] and d['clear_at'] is None:
                            d['clear_at'] = self.t + CREW_S
                    self.stats['clears'] += 1
                self.log('서버', '작업반 호출: %s 제거 요청 (약 %d초)' % (info['name'], CREW_S))
                self.wait_until(lambda: all(d['id'] != info['id'] for d in self.debris), CREW_S + 30)
            elif act == 'detour':
                if creep:
                    self.log('서버', '진입 구간은 우회 불가 → 대기')
                    self.wait_sim(4)
                    rec['waited'] += 4
                else:
                    with self.lock:
                        self.temp.append(self._safe_inflate(sh, 2.0 if info['moving'] else SAFE_GAP + 0.3))
                        self.stats['detours'] += 1
                    det = self._plan(goal)
                    if det is None:
                        rec['detour_failed'] = rec.get('detour_failed', 0) + 1
                        self.log('서버', '우회 경로 없음 → 대기')
                        with self.lock:
                            self.temp.pop()
                        self.wait_sim(4)
                        rec['waited'] += 4
                    else:
                        rec['detours'] = rec.get('detours', 0) + 1
                        self.log('서버', '우회 경로 재계획: %.0f m' % Y.path_length(det))
                        self.set_path(det, ignore)
                        continue
            elif act == 'stop':
                self.log('서버', '작업 중단: %s' % dec.get('why', ''))
                with self.lock:
                    self.st['mode'] = 'idle'
                return False
            with self.lock:
                if self.st['mode'] == 'blocked':
                    self.st['mode'] = 'driving'
        return False

    @staticmethod
    def _shrink_from(sh, pose):
        """Shrink a planning shape the stacker's footprint already overlaps (stale zone, inflated object) so a
        path can start; drop it if it would vanish."""
        need = Y.MARGIN + 0.45 - Y.footprint_clearance(pose[0], pose[1], pose[2], [sh])   # + grid error
        if need <= 0:
            return sh
        if sh[0] == 'circle':
            return ('circle', sh[1], sh[2], sh[3] - need) if sh[3] - need > 0.1 else None
        if min(sh[4], sh[5]) - need > 0.1:
            return sh[:4] + (sh[4] - need, sh[5] - need)
        return None

    def _safe_inflate(self, sh, m):
        """Inflate an obstacle for planning, but never into the stacker's current footprint (or no path could start)."""
        gap = Y.footprint_clearance(self.st['x'], self.st['z'], self.st['yaw'], [sh])
        return self._inflate(sh, max(0.0, min(m, gap - Y.MARGIN - 0.1)))

    @staticmethod
    def _inflate(sh, m):
        if sh[0] == 'circle':
            return ('circle', sh[1], sh[2], sh[3] + m)
        return sh[:4] + (sh[4] + m, sh[5] + m)

    def _horn_effect(self, force=False):
        st = self.st
        for w in self.workers:
            if math.hypot(w['x'] - st['x'], w['z'] - st['z']) > (60 if force else 30):
                continue
            p_move = 0.35 if w['stubborn'] else 0.75
            if self.rnd.random() < p_move:
                # step aside: perpendicular to the stacker heading, away from its path
                fx, fz = math.sin(st['yaw']), math.cos(st['yaw'])
                side = 1 if (w['x'] - st['x']) * fz - (w['z'] - st['z']) * fx >= 0 else -1
                w.update(target=(w['x'] + side * fz * 6.0, w['z'] - side * fx * 6.0), v=1.6, pause=0.0,
                         state='walk', stubborn=False, cross=False, yield_until=self.t + 40)
        c = self.car
        if c and c['state'] == 'park' and (force or math.hypot(c['x'] - st['x'], c['z'] - st['z']) < 35) and self.rnd.random() < 0.7:
            c['park_until'] = self.t
            c['next_park'] = self.t + 60

    def _decide_obstacle(self, alert):
        if self.controller == 'ai' and self.ai_fail < 3:
            d = self._ai_decide('obstacle', alert)
            if d is not None:
                return d
        d = self._rule_obstacle(alert)
        self.stats['rule_decisions'] += 1
        self.log('서버', '판단: %s' % d['why'])
        return d

    # ------------------------------------------------------------------ kit alignment
    def _align(self, task):
        """Kit alignment with retries. Each attempt: the case memory picks the plan from similar past cases;
        after a failure the reason narrows the next plan (and may call for a re-approach); every attempt is stored."""
        with self.lock:
            self.st['mode'] = 'aligning'
        tried, reason = [], None
        try:
            for attempt in range(3):
                if attempt and reason in case_memory.REAPPROACH:
                    self._reapproach(task, reason)
                    with self.lock:
                        self.st['mode'] = 'aligning'
                if self.aligner is not None:      # tests: stub alignment (bool, or dict with ok/reason/plan)
                    ok = self.aligner(self, task)
                    res = dict(ok) if isinstance(ok, dict) else {'ok': ok, 'reason': None if ok else 'not_converged'}
                    res.setdefault('plan', None)
                else:
                    res = self._align_site(task, attempt, reason, tried)
                if res['plan']:
                    tried.append(res['plan'])
                if res['ok']:
                    if attempt:
                        self.stats['retry_success'] += 1
                        self.log('서버', '재시도 %d회차 성공 (%s)' % (attempt + 1, res['plan'] or '-'))
                    return True
                reason = res['reason']
                self.stats['align_fail'] += 1
                if not self.running:
                    return False
                if attempt < 2:
                    self.stats['retries'] += 1
                    self.log('서버', '정렬·결합 실패 (원인: %s) → 재시도 %d/3%s' % (
                        case_memory.REASON_KO.get(reason, reason), attempt + 2,
                        ' · 재접근' if reason in case_memory.REAPPROACH else ''))
            return False
        finally:
            with self.lock:
                self.st['mode'] = 'idle'
                self.align_state = None

    def _reapproach(self, task, reason):
        """Back out to the line-up point and roll in again: a new approach gives the kit a new start."""
        sx, sz, syaw, sy = task['src']
        self.log('기계', '재접근: 스프레더 상승 → 후진 %.0f m → 다시 진입 (%s)' % (Y.PRE, case_memory.REASON_KO.get(reason, reason)))
        with self.lock:
            self.st['h_target'] = TRAVEL_H
        self.wait_until(lambda: abs(self.st['h'] - TRAVEL_H) < 0.005, 60)
        src_ex = (task['container'], task['source'])
        self._creep(Y.predock_pose(sx, sz, syaw), -1, ignore=src_ex)
        self._creep(Y.dock_pose(sx, sz, syaw), 1, ignore=src_ex)
        with self.lock:
            self.st['h_target'] = sy + Y.H + KIT_GAP
        self.wait_until(lambda: abs(self.st['h'] - self.st['h_target']) < 0.005, 60)

    def _align_site(self, task, attempt, reason=None, tried=()):
        site = self.site
        if site is None:
            return {'ok': True, 'reason': None, 'plan': None}
        ctrl = 'ai' if (self.controller == 'ai' and self.ai_fail < 3) else 'autopilot'
        hardware = self.align_opts.get('hardware', 'target')
        box = {}

        def plan_hook(first_decision):
            f = case_memory.features_from(first_decision, hardware, attempt, reason)
            box['features'] = f
            if self.memory is None:
                return None
            p = self.memory.choose(f, tried, reason)
            box['plan'] = p['plan']
            with self.lock:
                self.plan_info = dict(p, task=task['id'], container=task.get('container'), attempt=attempt + 1, features=f)
            return p
        params = dict(self.align_opts, controller=ctrl, seed=self.rnd.randrange(1, 1 << 31),
                      machine_id='YARD-RS-1', plan_hook=plan_hook)
        if self.vary:
            # site conditions change job to job (weather, sensor health); they persist through the retries of a task
            if 'cond' not in task:
                r = self.rnd.random()
                machine = 'normal' if r < .5 else 'unstable' if r < .75 else 'biased' if r < .9 else 'none'
                r = self.rnd.random()
                task['cond'] = {'machine': machine, 'fault': 'none' if r < .85 else 'drift' if r < .95 else 'head_dead'}
                self.log('현장', '현장 조건(시뮬 진실, 서버는 모름): 기존 센서 %s · 키트 %s' % (
                    {'normal': '정상', 'unstable': '불안정(악천후)', 'biased': '편향(고장)', 'none': '꺼짐'}[machine],
                    {'none': '정상', 'drift': '코너2 교정 틀어짐', 'head_dead': '코너2 헤드 고장'}[task['cond']['fault']]))
            params.update(task['cond'])
        if ctrl == 'ai':
            params['ai_model'] = self.ai_model
        if self.kit_state is not None:
            end = time.time() + 10
            while time.time() < end and self.kit_state() != 'IDLE':
                time.sleep(0.05)
        self.log('서버', 'M-ALIGN 키트 정렬 시작 (%s%s)' % (cid_of(task), ', %d회차' % (attempt + 1) if attempt else ''))
        try:
            site.start(**params)
        except RuntimeError:
            site.abort('yard_restart')
            time.sleep(0.2)
            site.start(**params)
        last_log = 0
        outcome = None
        while self.running:
            s = site.state()
            with self.lock:
                self.align_state = s
            logs = s.get('log') or []
            for e in logs:
                if e['t'] > last_log:
                    last_log = e['t']
                    self.log(e['who'], e['text'])
            if not s.get('active'):
                if s.get('outcome') == 'aborted' and ctrl == 'ai':
                    self.ai_fail = 3
                outcome = s.get('outcome')
                break
            time.sleep(0.15)
        else:
            site.abort('yard_stopped')
            return {'ok': False, 'reason': 'aborted', 'plan': box.get('plan')}
        j = site.job
        decisions = j['brain'].decisions if j else []
        why = case_memory.failure_reason(outcome, decisions, box.get('features'))
        if self.memory is not None and box.get('features') and outcome != 'aborted':
            self.memory.add(box['features'], box.get('plan') or case_memory.DEFAULT, outcome, job_id=j['job_id'],
                            task=task['id'], rounds=j['brain'].rounds,
                            moves=sum(1 for e in j['events'] if e['kind'] == 'move'), reason=why,
                            sim_truth={'machine': j['settings']['machine'], 'fault': j['settings']['fault'],
                                       'start_worst_mm': round(j['worst0'], 2), 'final_worst_mm': round(j['rs'].worst_mm(), 2)})
            self.stats['cases_added'] += 1
        return {'ok': outcome == 'success', 'reason': why, 'plan': box.get('plan')}

    # ------------------------------------------------------------------ state for the 3D view
    def state(self):
        with self.lock:
            st = self.st
            path = None
            if st['path'] and st['mode'] in ('driving', 'blocked'):
                i0 = self._index(st['cum'], st['s'])
                path = [[round(p[0], 2), round(p[1], 2), p[3]] for p in st['path'][i0::3]] + \
                       [[round(st['path'][-1][0], 2), round(st['path'][-1][1], 2), st['path'][-1][3]]]
            al = None
            if self.align_state:
                a = self.align_state
                al = {k: a.get(k) for k in ('phase', 'stage', 'outcome', 'truth_corner_errors_mm', 'truth_worst_mm',
                                            'decision', 'rounds', 'pose', 'settings')}
            return {
                'running': self.running, 't': round(self.t, 2), 'speed': self.speed, 'seed': self.seed,
                'controller': self.controller, 'ai_model': self.ai_model, 'ai_ok': self.ai_fail < 3,
                'stacker': {'x': st['x'], 'z': st['z'], 'yaw': st['yaw'], 'v': round(st['v'], 2), 'dir': st['dir'],
                            'mode': st['mode'], 'h': round(st['h'], 3), 'carry': st['carry'],
                            'horn': self.t - st['horn_t'] < 1.5},
                'path': path,
                'containers': [{k: c[k] for k in ('id', 'color', 'x', 'z', 'yaw', 'y', 'where', 'slot', 'truck')}
                               for c in self.containers.values()],
                'slots': [{'id': s['id'], 'x': s['x'], 'z': s['z'], 'yaw': s['yaw'], 'holder': s['holder'],
                           'reserved': s['reserved']} for s in self.slots.values()],
                'trucks': [{k: t[k] for k in ('id', 'state', 'bay', 'mode', 'container', 'x', 'z', 'yaw', 'served')}
                           for t in self.trucks.values()],
                'bays': [{'id': b['id'], 'x': b['x'], 'z': b['z'], 'truck': b['truck']} for b in self.bays.values()],
                'workers': [{'id': w['id'], 'x': w['x'], 'z': w['z'], 'yaw': w['yaw'], 'state': w['state']} for w in self.workers],
                'car': None if not self.car else {k: self.car[k] for k in ('id', 'x', 'z', 'yaw', 'state')},
                'debris': [{'id': d['id'], 'kind': d['kind'], 'x': d['x'], 'z': d['z'], 'yaw': d['yaw'],
                            'known': d['known'], 'clearing': d['clear_at'] is not None} for d in self.debris],
                'alert': self.alert if st['mode'] == 'blocked' or (self.alert and self.task) else None,
                'decision': self.decision,
                'task': None if not self.task else {k: self.task.get(k) for k in ('id', 'kind', 'source', 'dest', 'container', 'step', 'request')},
                'requests': list(self.requests),
                'stats': dict(self.stats),
                'occupied': sum(1 for s in self.slots.values() if s['holder']),
                'capacity': CAPACITY,
                'align': al,
                'memory': self.memory.summary() if self.memory is not None else None,
                'plan': None if not self.plan_info else {k: self.plan_info.get(k) for k in (
                    'plan', 'ko', 'why', 'similar', 'expected', 'scores', 'task', 'container', 'attempt', 'features')},
                'log': self.log_lines[-80:],
            }


def cid_of(task):
    return task.get('container') or '?'
