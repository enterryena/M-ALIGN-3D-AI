"""Service car vs trucks: trucks have right of way, the car keeps out of where a truck is or is about to be,
and a car that got there first drives on out. Regression: seed 9 used to lock the car and a leaving truck
(both waiting for each other) near the truck lane. No mutual wait > 40 s, no contact, trucks keep moving."""
import sys, time, math, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import yard_ops as O
for seed in (9, 3):
    y = O.YardOps(controller='autopilot', seed=seed, speed=60, aligner=lambda yd, task: yd.wait_sim(6))
    y.start(); t0 = time.time(); stuck = {}; dl = None
    while time.time() - t0 < 120 and y.t < 2000 and dl is None:
        time.sleep(0.2)
        with y.lock:
            c = y.car
            for t in y.trucks.values():
                if t['state'] in ('arriving', 'leaving') and c and c['v'] < .05 and math.hypot(c['x'] - t['x'], c['z'] - t['z']) < 20:
                    p = stuck.get(t['id'])
                    if p is None or abs(p[1] - t['s']) > .05: stuck[t['id']] = (y.t, t['s'])
                    elif y.t - p[0] > 40: dl = (seed, round(y.t), t['id'], round(t['x'], 1), round(t['z'], 1), round(c['x'], 1), round(c['z'], 1))
                else:
                    stuck.pop(t['id'], None)
    y.stop()
    assert dl is None, 'car and truck waiting for each other: %s' % (dl,)
    assert y.stats['contacts'] == 0 and y.stats['inbound'] + y.stats['outbound'] >= 4, (seed, y.stats)
print('PASS: car/truck — no mutual wait (seeds 9, 3), no contact, trucks keep moving', flush=True)
