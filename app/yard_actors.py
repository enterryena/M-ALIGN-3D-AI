# -*- coding: utf-8 -*-
"""People and the service car in the virtual yard, behaving like a real site.

Workers
  * walk on the marked walkways only (perimeter + the corridor between rows A and B), between work spots
    (gate booth, site office, container inspection points) and work there for a while
  * keep clear of the reach stacker: before stepping toward its swept path (current footprint + the next
    25 m it will drive) they stop and wait at a safe distance (yield); someone already inside clears out faster
  * cross the service road only at crosswalks, after checking the car
  * rarely break the rules (short-cut across a working aisle, phone call standing still) - these are the
    situations the stacker's sensors and the main server have to deal with (horn / wait / detour)
Service car (V1)
  * drives the service road (rounded corners, no turning on the spot), speed limit 4 m/s, smooth accel/brake
  * keeps a following distance: 2 s of travel + 6 m to the stacker / trucks, 5 m to people, stops at
    crosswalks for pedestrians, gives way to the stacker crossing the road
  * parks only in the marked bay beside the south road; rarely stops on the road (delivery, hazard lights)
"""
import math

import yard_world as Y

ROAD_V = 4.0
CAR_ACC, CAR_DEC = 1.5, 3.0
CAR_GAP_MACHINE = 6.0
CAR_GAP_PERSON = 5.0
CAR_HEADWAY_S = 2.0
YIELD_M = 5.0           # a worker stops this far from the stacker's swept path (stacker's own zone: 3 m)
P_JAYWALK = 0.10        # trips taken as a short-cut across the working area
P_PHONE = 0.35          # a short-cutter stops for a phone call
P_PARK = 0.35           # laps that end in the parking bay
P_ROAD_STOP = 0.05      # legs with an illegal stop on the road


# --------------------------------------------------------------------------- shared helpers
def stacker_sweep(yard, ahead=25.0, step=1.0):
    """Footprint discs of the stacker now and along the next `ahead` metres of its path."""
    st = yard.st
    discs = list(Y.footprint(st['x'], st['z'], st['yaw']))
    if st['mode'] in ('driving', 'blocked') and st.get('path'):
        s, end = st['s'], st['cum'][-1]
        d = step
        while d <= ahead and s + d <= end:
            x, z, y = yard._interp(st['path'], st['cum'], s + d)
            discs.extend(Y.footprint(x, z, y))
            d += step
    return discs


def disc_gap(x, z, r, discs):
    return min((math.hypot(x - dx, z - dz) - dr - r for dx, dz, dr in discs), default=1e9)


# --------------------------------------------------------------------------- workers
def new_worker(yard, wid, node):
    x, z = Y.WALK_NODES[node]
    return {'id': wid, 'x': x, 'z': z, 'yaw': 0.0, 'route': [], 'v': 1.2, 'pause': yard.rnd.uniform(1, 6),
            'state': 'work', 'node': node, 'jay': False, 'stubborn': False, 'frozen': 0.0, 'yield_until': -1}


def _plan_trip(yard, w):
    rnd = yard.rnd
    here = w['node'] or Y.nearest_node(w['x'], w['z'])
    if rnd.random() < P_JAYWALK and yard.st['mode'] in ('driving', 'blocked') and yard.t > w['yield_until']:
        # short-cut: straight across the working area to the other side (rule breaking)
        p = yard._path_point_ahead(rnd.uniform(10, 16))
        if p:
            dst = rnd.choice([k for k in Y.WORK_SPOTS if k != here])
            w.update(route=[(p[0], p[1]), Y.WALK_NODES[dst]], node=dst, jay=True, v=1.3, state='jay',
                     stubborn=rnd.random() < P_PHONE)
            return
    dst = rnd.choice([k for k in Y.WORK_SPOTS if k != here])
    nodes = Y.walk_route(here, dst)
    route = [Y.WALK_NODES[n] for n in nodes]
    if math.hypot(route[0][0] - w['x'], route[0][1] - w['z']) < 0.3:
        route = route[1:]
    w.update(route=route, node=dst, jay=False, v=rnd.uniform(1.1, 1.4), state='walk', stubborn=False)
    w['cross'] = [Y.WALK_NODES[n] for n in nodes if n in Y.CROSSWALKS]


def _evade(yard, w, sweep):
    """Step back to the nearest walkway point well clear of the machine."""
    here = disc_gap(w['x'], w['z'], 0.4, sweep)
    if on_walk(w):
        # along the walkway, away from the machine: the end of the current walkway segment that is clearer,
        # then onward from there if that is still too close
        a, b = min(Y.WALK_EDGES, key=lambda e: Y.seg_dist(w['x'], w['z'], Y.WALK_NODES[e[0]], Y.WALK_NODES[e[1]]))
        def path_gap(k):            # the closest the walk to that end would come to the machine
            tx, tz = Y.WALK_NODES[k]
            return min(disc_gap(w['x'] + f * (tx - w['x']), w['z'] + f * (tz - w['z']), 0.4, sweep)
                       for f in (0.1, 0.25, 0.4, 0.55, 0.7, 0.85, 1.0))
        end = max((a, b), key=lambda k: (path_gap(k) >= here - 0.2, disc_gap(*Y.WALK_NODES[k], 0.4, sweep)))
        reach = sorted(Y.WALK_NODES, key=lambda k: -disc_gap(*Y.WALK_NODES[k], 0.4, sweep))
        pick = end
        if disc_gap(*Y.WALK_NODES[end], 0.4, sweep) < YIELD_M + 3:
            for k in reach:
                r = Y.walk_route(end, k)
                if all(disc_gap(*Y.WALK_NODES[n], 0.4, sweep) > here for n in r):
                    pick = k
                    break
        if disc_gap(*Y.WALK_NODES[end], 0.4, sweep) <= here + 0.3:
            return _step_away(yard, w, sweep, here)
        route = [Y.WALK_NODES[end]] + [Y.WALK_NODES[n] for n in Y.walk_route(end, pick)[1:]]
    else:            # off the walkway (short-cut): first straight away from the machine, then back to a walkway
        if _step_away(yard, w, sweep, here):
            n = Y.nearest_node(*w['route'][0])
            w['route'].append(Y.WALK_NODES[n])
            w['node'] = n
            return True
        nodes = sorted(Y.WALK_NODES, key=lambda k: math.hypot(Y.WALK_NODES[k][0] - w['x'], Y.WALK_NODES[k][1] - w['z']))
        pick = next((k for k in nodes if disc_gap(*Y.WALK_NODES[k], 0.4, sweep) > here + 0.5), None)
        if pick is None:
            return False
        route = [Y.WALK_NODES[pick]]
    w.update(route=route, node=pick, pause=0.0, state='evade', stubborn=False, jay=False, v=1.5,
             yield_until=yard.t + 60)
    return True


def _step_away(yard, w, sweep, here):
    """Trapped (machine on one side, containers on the other): walk straight to the clearest free spot nearby."""
    statics = yard._static_shapes(temp=False)
    best, best_gap = None, here + 0.5
    for k in range(16):
        a = k * math.pi / 8
        for r in (4.0, 7.0, 10.0):
            x, z = w['x'] + r * math.sin(a), w['z'] + r * math.cos(a)
            if any(Y.shape_dist(sh, x, z) < 0.8 for sh in statics):
                continue
            ok = all(disc_gap(w['x'] + f * (x - w['x']), w['z'] + f * (z - w['z']), 0.4, sweep) > 0.6 and
                     not any(Y.shape_dist(sh, w['x'] + f * (x - w['x']), w['z'] + f * (z - w['z'])) < 0.5 for sh in statics)
                     for f in (0.25, 0.5, 0.75, 1.0))
            g = disc_gap(x, z, 0.4, sweep)
            if ok and g > best_gap:
                best, best_gap = (x, z), g
    if best is None:
        return False
    w.update(route=[best], node=None, pause=0.0, state='evade', stubborn=False, jay=False, v=1.5, yield_until=yard.t + 60)
    return True


def on_walk(w):
    return Y.on_walkway(w['x'], w['z'])


def step_workers(yard, dt):
    sweep = stacker_sweep(yard) if yard.workers else []
    moving = yard.st['mode'] in ('driving', 'blocked')
    for w in yard.workers:
        # standing (working / waiting) too close to where the machine is going: move away first
        if moving and w['state'] in ('work', 'yield') and disc_gap(w['x'], w['z'], 0.4, sweep) < YIELD_M - 1.0:
            if _evade(yard, w, sweep):
                continue
        if w['pause'] > 0:
            w['pause'] -= dt
            if w['pause'] <= 0 and w['state'] == 'phone':
                w['state'], w['stubborn'] = 'jay', False
            continue
        if not w['route']:
            if w['state'] in ('walk', 'jay', 'yield', 'evade'):
                w['state'] = 'work'
                w['pause'] = yard.rnd.uniform(6, 20)        # working at the spot
                w['jay'] = False
                continue
            _plan_trip(yard, w)
            continue
        tx, tz = w['route'][0]
        d = math.hypot(tx - w['x'], tz - w['z'])
        if d < 0.25:
            w['x'], w['z'] = tx, tz
            w['route'].pop(0)
            if w['jay'] and w['stubborn'] and len(w['route']) == 1:   # reached the short-cut point: phone call
                w['state'], w['pause'] = 'phone', yard.rnd.uniform(20, 40)
            continue
        ux, uz = (tx - w['x']) / d, (tz - w['z']) / d
        nx, nz = w['x'] + ux * 1.5, w['z'] + uz * 1.5
        gap_now = disc_gap(w['x'], w['z'], 0.4, sweep)
        gap_next = disc_gap(nx, nz, 0.4, sweep)
        # rule 1: never walk into the machine; rule 2 (law-abiding): stop before the stacker's path
        if gap_next < 1.0 and gap_next < gap_now:
            w['frozen'] += dt
            if w['frozen'] > 3.0:            # it is in the way: step back to a clear walkway point
                w['frozen'] = 0.0
                _evade(yard, w, sweep)
            continue
        w['frozen'] = 0.0
        if not w['jay'] and w['state'] != 'evade' and moving and gap_next < YIELD_M and gap_next <= gap_now:
            w['state'] = 'yield'
            continue
        # crosswalk: look at the car before stepping on the road
        car = yard.car
        if car and not w['jay'] and any(math.hypot(cx - nx, cz - nz) < 3.0 for cx, cz in w.get('cross', ())):
            if car['v'] > 0.5 and math.hypot(car['x'] - nx, car['z'] - nz) < 14 and not car.get('yielding'):
                w['state'] = 'yield'
                continue
        if w['state'] != 'evade':
            w['state'] = 'jay' if w['jay'] else 'walk'
        v = w['v'] * (1.4 if gap_now < YIELD_M and moving else 1.0)   # inside the danger area: hurry out
        step = min(d, v * dt)
        w['yaw'] = math.atan2(ux, uz)
        w['x'] += ux * step
        w['z'] += uz * step


def horn_workers(yard, force=False):
    st = yard.st
    for w in yard.workers:
        if math.hypot(w['x'] - st['x'], w['z'] - st['z']) > (60 if force else 30):
            continue
        p_move = 0.35 if w['stubborn'] and w['state'] == 'phone' else 0.85
        if yard.rnd.random() < p_move:
            _evade(yard, w, stacker_sweep(yard))


# --------------------------------------------------------------------------- service car
def new_car(yard):
    path = Y.car_loop_path()
    cum = [0.0]
    for a, b in zip(path, path[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    loop_len = cum[-1] + math.hypot(path[0][0] - path[-1][0], path[0][1] - path[-1][1])
    x, z, yaw = path[0]
    return {'id': 'V1', 'x': x, 'z': z, 'yaw': yaw, 'v': 0.0, 'state': 'drive', 'loop': path, 'cum': cum,
            'L': loop_len, 's': 0.0, 'detour': None, 'until': -1, 'next_event': 30.0, 'yielding': False,
            'min_gap': 1e9, 'brake': False}


def _loop_pose(c, s):
    s %= c['L']
    path, cum = c['loop'], c['cum']
    if s >= cum[-1]:
        a, b, f = path[-1], path[0], (s - cum[-1]) / max(1e-9, c['L'] - cum[-1])
    else:
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


def car_is_static(c):
    """Parked in the bay: a plain obstacle for the stacker."""
    return c['state'] == 'parked'


def car_shape(x, z, yaw):
    return ('rect', x, z, yaw, 0.95, 2.2)


def _car_ahead_gap(yard, c, poses):
    """Distance along the car's own path to the first thing in its way: the stacker's reserved corridor,
    a truck, a person, an object."""
    st = yard.st
    sweep = stacker_sweep(yard, ahead=25.0)
    me0 = car_shape(c['x'], c['z'], c['yaw'])
    inside = any(Y.shape_dist(me0, dx, dz) < dr + 1.0 for dx, dz, dr in sweep)
    stacker = list(Y.footprint(st['x'], st['z'], st['yaw'])) if inside else sweep   # inside already: drive out
    for d, (x, z, y) in poses:
        me = car_shape(x, z, y)
        if any(Y.shape_dist(me, dx, dz) < dr + 1.0 for dx, dz, dr in stacker):
            return d, (3.0 if inside else CAR_GAP_MACHINE), 'stacker'
        for t in yard.trucks.values():
            if t['state'] != 'away':
                hw, hl, off = (1.3, 6.5, 1.5)
                if Y.shape_dist(('rect', t['x'] + off * math.sin(t['yaw']), t['z'] + off * math.cos(t['yaw']),
                                 t['yaw'], hw, hl), x, z) < 1.6:
                    return d, CAR_GAP_MACHINE, 'truck'
        for w in yard.workers:
            if Y.shape_dist(me, w['x'], w['z']) < 1.6:
                return d, CAR_GAP_PERSON, 'person'
        for o in yard.debris:
            if Y.shape_dist(o['shape'], x, z) < 1.4:
                return d, 3.0, 'object'
    return None, 0.0, None


def step_car(yard, dt):
    """Right-of-way rule of the yard: the reach stacker reserves the corridor it is about to drive (its footprint
    now + the next 25 m of its path). The car never enters a reserved corridor - it stops 6 m before it and waits.
    The stacker in turn plans around where the car is and is about to be (see YardOps._plan)."""
    c = yard.car
    if not c:
        return
    rnd = yard.rnd
    if c['state'] in ('parked', 'road_stop'):
        c['v'] = 0.0
        if yard.t < c['until']:
            return
        if c['state'] == 'parked':
            px, pz, py = Y.PARKING
            road_z = Y.CAR_LOOP[0][1]
            c['detour'] = Y.straight((px, pz, py), (px + 8.0, road_z, math.pi / 2), 1, 0.5)
            c['detour_s'], c['detour_cum'] = 0.0, None
            c['rejoin'] = _loop_s_near(c, px + 8.0, road_z)
        c['state'] = 'drive'
        c['next_event'] = yard.t + rnd.uniform(40, 90)
    # now and then: decide to park in the bay (legal) or stop on the road (illegal, rare)
    if c['detour'] is None and yard.t > c['next_event']:
        c['next_event'] = yard.t + rnd.uniform(40, 90)
        r = rnd.random()
        if r < P_PARK:
            c['want_park'] = True
        elif r < P_PARK + P_ROAD_STOP * 3:
            c['state'], c['until'], c['v'] = 'road_stop', yard.t + rnd.uniform(25, 45), 0.0
            yard.log('현장', '서비스 차량 V1 도로에 정차 (위반, 비상등)')
            return
    if c.get('want_park') and c['detour'] is None and abs(c['z'] - Y.CAR_LOOP[0][1]) < 0.6 \
            and Y.PARKING[0] - 14.0 < c['x'] < Y.PARKING[0] - 8.0 \
            and abs(c['yaw'] - math.pi / 2) < 0.2:
        px, pz, py = Y.PARKING
        c['detour'] = Y.straight((c['x'], c['z'], math.pi / 2), (px, pz, py), 1, 0.5)
        c['detour_s'], c['detour_cum'], c['park_after'], c['want_park'] = 0.0, None, True, False
    # look ahead along where the car will drive
    look = max(12.0, c['v'] * CAR_HEADWAY_S + CAR_GAP_MACHINE + 4.0)
    poses = []
    d = 1.0
    while d <= look:
        poses.append((d, _pose_at(c, d)))
        d += 1.0
    hit, gap, what = _car_ahead_gap(yard, c, poses)
    for cx, cz in (Y.WALK_NODES[k] for k in Y.CROSSWALKS):          # people at a crosswalk ahead
        for dd, (x, z, y) in poses:
            if math.hypot(x - cx, z - cz) < 2.5:
                if any(math.hypot(w['x'] - cx, w['z'] - cz) < 4.0 for w in yard.workers):
                    if hit is None or dd < hit:
                        hit, gap, what = dd, 4.0, 'crosswalk'
                break
    c['yielding'] = what in ('stacker', 'crosswalk')
    if what == 'object' and c['v'] < 0.1 and hit is not None and hit < 6:
        c['obj_wait'] = c.get('obj_wait', 0.0) + dt
        if c['obj_wait'] > 8.0:                      # the driver gets out and moves it off the road
            x, z, y = _pose_at(c, hit)
            for o in list(yard.debris):
                if Y.shape_dist(o['shape'], x, z) < 1.6:
                    yard.debris.remove(o)
                    yard.log('현장', '서비스 차량 운전자가 %s 치움' % o['id'])
            c['obj_wait'] = 0.0
    else:
        c['obj_wait'] = 0.0
    want = ROAD_V
    if hit is not None:
        room = max(0.0, hit - gap - c['v'] * 0.5)
        want = min(want, math.sqrt(2 * CAR_DEC * room))
    if c['v'] < want:
        c['v'] = min(want, c['v'] + CAR_ACC * dt)
    else:
        c['v'] = max(want, c['v'] - CAR_DEC * 1.5 * dt)
    c['brake'] = c['v'] < want - 0.1 or (hit is not None and c['v'] < 0.3)
    if c['v'] < 0.02:
        c['v'] = 0.0
        return
    _advance(c, c['v'] * dt)
    if c.pop('park_for', False):
        c['until'] = yard.t + rnd.uniform(20, 45)
        yard.log('현장', '서비스 차량 V1 주차 구역에 주차')
    if c['v'] > 0.5:                                   # actual clearance kept while moving (realism check)
        me = car_shape(c['x'], c['z'], c['yaw'])
        g = Y.footprint_clearance(yard.st['x'], yard.st['z'], yard.st['yaw'], [me])
        for w in yard.workers:
            g = min(g, Y.shape_dist(me, w['x'], w['z']) - 0.4)
        c['min_gap'] = min(c['min_gap'], g)


def car_reserved(yard, ahead=10.0):
    """Where the car is and will be in the next `ahead` metres (the stacker plans around this)."""
    c = yard.car
    if not c:
        return []
    out = [car_shape(c['x'], c['z'], c['yaw'])]
    if c['state'] == 'drive' and c['v'] > 0.3 and not c['yielding']:   # a car waiting for the stacker goes nowhere
        d = 2.0
        while d <= ahead:
            out.append(car_shape(*_pose_at(c, d)))
            d += 2.0
    return out


def _loop_s_near(c, x, z):
    best, bs = 1e9, 0.0
    for p, s in zip(c['loop'], c['cum']):
        d = math.hypot(p[0] - x, p[1] - z)
        if d < best:
            best, bs = d, s
    return bs


def _pose_at(c, ahead):
    if c['detour'] is not None:
        path = c['detour']
        cum = c.setdefault('detour_cum', None) or _cum(path)
        c['detour_cum'] = cum
        s = c['detour_s'] + ahead
        if s <= cum[-1]:
            return _interp(path, cum, s)
        s2 = (c.get('rejoin') or 0.0) + (s - cum[-1])
        if c.get('park_after'):
            return path[-1][:3]
        return _loop_pose(c, s2)
    return _loop_pose(c, c['s'] + ahead)


def _advance(c, ds):
    if c['detour'] is not None:
        cum = c.get('detour_cum') or _cum(c['detour'])
        c['detour_cum'] = cum
        c['detour_s'] += ds
        if c['detour_s'] >= cum[-1]:
            end = c['detour'][-1]
            c['x'], c['z'], c['yaw'] = end[:3]
            if c.get('park_after'):
                c['state'], c['v'] = 'parked', 0.0
                c['park_after'] = False
                c['detour'] = None
                c['detour_cum'] = None
                c['park_for'] = True
                return
            c['s'] = c.get('rejoin', 0.0)
            c['detour'] = None
            c['detour_cum'] = None
            return
        c['x'], c['z'], c['yaw'] = _interp(c['detour'], cum, c['detour_s'])
        return
    c['s'] = (c['s'] + ds) % c['L']
    c['x'], c['z'], c['yaw'] = _loop_pose(c, c['s'])


def _cum(path):
    cum = [0.0]
    for a, b in zip(path, path[1:]):
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    return cum


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


def horn_car(yard, force=False):
    c = yard.car
    st = yard.st
    if c and c['state'] == 'road_stop' and (force or math.hypot(c['x'] - st['x'], c['z'] - st['z']) < 35) \
            and yard.rnd.random() < 0.75:
        c['until'] = yard.t
        yard.log('현장', '서비스 차량 V1 경적 듣고 출발')
