"""Yard realism: people stay on walkways and keep clear of the machine, the service car keeps its distance.

Runs the yard (rule autopilot, stub alignment, fast clock) on 3 seeds and checks
  * no contact between the reach stacker and a person / vehicle
  * workers are on the marked walkways >= 90 % of the time (short-cuts are the rare exception)
  * law-abiding workers never come closer than 1.5 m to the stacker while it drives (they yield ~5 m back)
  * the service car, while moving, keeps >= 2 m from the stacker and from people
  * the yard keeps working (trucks served) - no standoffs between machine, car, trucks and people
"""
import pathlib, sys, time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yard_ops  # noqa: E402

rows = []
for seed in (3, 12, 30):
    y = yard_ops.YardOps(controller='autopilot', seed=seed, speed=40, aligner=lambda yd, task: yd.wait_sim(6)).start()
    t0 = time.time()
    while y.t < 1000 and time.time() - t0 < 300:      # wall clock: slow CI machines
        time.sleep(0.5)
    y.stop()
    time.sleep(0.3)
    m, s = y.state()['metrics'], y.stats
    walk = m['worker_on_walkway'] / max(1, m['worker_steps'])
    rows.append((seed, s['inbound'] + s['outbound'], s['contacts'], round(walk, 3), round(m['worker_min_gap_lawful'], 2), m['car_min_gap']))
    assert s['contacts'] == 0, (seed, s)
    assert walk >= 0.90, (seed, walk)
    assert m['worker_min_gap_lawful'] >= 1.5, (seed, m)
    assert m['car_min_gap'] >= 2.0, (seed, m)
    assert s['inbound'] + s['outbound'] >= 4, (seed, s)
print('PASS: yard realism (seed, tasks, contacts, on-walkway, lawful worker min gap m, car min gap m): %s' % rows, flush=True)
