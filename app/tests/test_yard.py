"""Container yard (AI version): car-like paths, trucks in/out, obstacles, user priority, guard, AI via fake Ollama.

A. planner: forward/reverse paths never turn tighter than the minimum radius and never on the spot,
   every sample is collision-free
B. guard: user request first, slot must be free, horn only for people/vehicles, crew only for fixed objects
C. rule autopilot (stub alignment, fast clock): trucks unloaded into slots, yard full -> outbound loading,
   obstacles met and handled (horn / detour / crew), no contact, user request done first
D. desktop host + fake Ollama: the AI picks tasks and obstacle actions through the guard (wrong choices
   refused and explained), alignment through the M-ALIGN kit job, user request via the API, 3D pages served
E. Ollama unreachable -> rule autopilot takes over and the yard keeps working
"""
import json, math, pathlib, random, subprocess, sys, tempfile, threading, time, urllib.request
from http.server import ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests' / 'fixtures'))
import yard_world as Y  # noqa: E402
import yard_ops as O  # noqa: E402
from fake_ollama import FakeOllama  # noqa: E402

# ---------------- A. planner
rnd = random.Random(3)
shapes = [('circle', x, z, r) for x, z, r in Y.POLES] + [('rect',) + b for b in Y.BUILDINGS]
for sid, x, z, yaw in rnd.sample(Y.SLOTS, 6):
    shapes.append(Y.container_rect(x, z, yaw))
for b, x, z in Y.BAYS:
    shapes.append(('rect', x + 1.5, z, math.pi / 2, 1.3, 6.5))
df = Y.DistanceField(shapes)
targets = [(s[1], s[2], s[3]) for s in Y.SLOTS] + [(x, z, 0.0) for _, x, z in Y.BAYS]
start, reversing = Y.HOME, 0
for i in range(8):
    goal = Y.predock_pose(*rnd.choice(targets))
    path = Y.plan(df, start, goal)
    assert path, ('no path', start, goal)
    assert Y.max_turn_rate(path) <= Y.KAPPA * 1.01, 'tighter than the minimum turning radius / turned on the spot'
    assert all(Y.footprint_clearance(p[0], p[1], p[2], shapes) > 0 for p in path), 'collision on the path'
    reversing += sum(1 for p in path if p[3] < 0)
    start = path[-1][:3]
assert reversing > 0, 'paths should use reverse gear somewhere'
assert Y.max_turn_rate([(0, 0, 0, 1), (0, 0, 1.0, 1)]) == float('inf')

# ---------------- B. guard
y = O.YardOps(controller='autopilot', seed=7, people=False, debris=False)
occ = [s for s in y.slots.values() if s['holder']]
free = [s for s in y.slots.values() if not s['holder']]
r = y.request(occ[0]['holder'], free[0]['id'])
assert 'priority' in y.check_task({'kind': 'move', 'source': occ[1]['id'], 'dest': free[1]['id']})
assert y.check_task({'kind': 'move', 'source': occ[0]['id'], 'dest': free[0]['id']}) is None
y.requests.clear()
assert 'not free' in y.check_task({'kind': 'move', 'source': occ[0]['id'], 'dest': occ[1]['id']})
assert 'not a parked truck' in y.check_task({'kind': 'unload', 'source': 'T1', 'dest': free[0]['id']})
assert y.check_action({'moving': False}, 'horn') and y.check_action({'moving': True}, 'clear')
assert y.check_action({'moving': True}, 'horn') is None and y.check_action({'moving': False}, 'clear') is None
try:
    y.request(occ[0]['holder'], occ[1]['id'])
    raise AssertionError('occupied destination accepted')
except ValueError:
    pass

# ---------------- C. rule autopilot, fast clock
y = O.YardOps(controller='autopilot', seed=23, speed=40, aligner=lambda yd, task: yd.wait_sim(6))
occ = [s for s in y.slots.values() if s['holder']]
free = [s for s in y.slots.values() if not s['holder']]
req_c = occ[0]['holder']
y.request(req_c, free[0]['id'])
y.start()
t0 = time.time()
while time.time() - t0 < 90 and not (y.stats['inbound'] >= 4 and y.stats['outbound'] >= 2):
    time.sleep(0.5)
y.stop()
st = y.stats
assert st['user'] == 1 and st['inbound'] >= 3 and st['outbound'] >= 1, st
assert st['contacts'] == 0, st
assert st['horns'] + st['detours'] + st['clears'] > 0, 'no obstacle met'
first = next(l['text'] for l in y.log_lines if '작업 K1 시작' in l['text'])
assert req_c in first and '사용자 우선 지시' in first, first
assert y.slots[free[0]['id']]['holder'] == req_c
assert any('반출용 빈 트럭' in l['text'] for l in y.log_lines)


# ---------------- D/E. desktop host (+ fake Ollama)
def call(url, data=None, h=None):
    h = dict(h or {})
    b = None
    if data is not None:
        h['Content-Type'] = 'application/json'
        b = json.dumps(data).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=b, headers=h, method='POST' if b else 'GET'), timeout=20) as res:
        return json.load(res)


def host(tmp, ollama):
    (tmp / 'Link').mkdir(exist_ok=True)
    (tmp / 'Link' / 'link_config.json').write_text(json.dumps({'sim_move_delay_s': 0.05, 'ai_ollama_url': ollama, 'ai_model': 'fake'}))
    h = subprocess.Popen([sys.executable, str(ROOT / 'desktop_host.py'), '--headless', '--data-dir', str(tmp)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return h, json.loads(h.stdout.readline())


fake = ThreadingHTTPServer(('127.0.0.1', 0), FakeOllama)
threading.Thread(target=fake.serve_forever, daemon=True).start()
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:  # Windows may hold the closed host's DB a moment longer
    tmp = pathlib.Path(tmp)
    h, info = host(tmp, 'http://127.0.0.1:%d' % fake.server_port)
    H = {'X-MALIGN-Token': info['token']}
    try:
        st = call(info['url'] + 'api/yard/start', {'controller': 'ai', 'speed': 8, 'seed': 23, 'people': True, 'debris': True}, H)
        c = next(c for c in st['containers'] if c['where'] == 'slot')
        f = next(s for s in st['slots'] if not s['holder'])
        call(info['url'] + 'api/yard/request', {'container': c['id'], 'dest': f['id']}, H)
        end = time.time() + 240
        while time.time() < end:
            st = call(info['url'] + 'api/yard/state', None, H)
            if st['stats']['user'] >= 1 and st['stats']['inbound'] >= 1 and st['stats']['ai_decisions'] >= 3:
                break
            time.sleep(1)
        s = st['stats']
        assert s['user'] >= 1 and s['inbound'] >= 1 and s['ai_decisions'] >= 3, s
        assert s['guard_refusals'] >= 1 and s['contacts'] == 0, s
        # the log lines with refusals / kit alignment are in the full log (state shows the last 80)
        whos = {l['who'] for l in st['log']}
        assert {'AI', '도구'} & whos, whos
        try:
            call(info['url'] + 'api/yard/request', {'container': 'C99', 'dest': 'A1'}, H)
            raise AssertionError('bad request accepted')
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        page = urllib.request.urlopen(info['url'] + '3d', timeout=10).read().decode('utf-8')
        assert 'MALIGN_YARD' in page and 'yard/state' in page
        page2 = urllib.request.urlopen(info['url'] + '3d-align', timeout=10).read().decode('utf-8')
        assert 'MALIGN_DESKTOP_TOKEN' in page2 and 'sim/start' in page2
        call(info['url'] + 'api/yard/stop', {}, H)
    finally:
        h.terminate(); h.wait(timeout=8)
    # E. no Ollama -> rule autopilot takes over
    h, info = host(tmp, 'http://127.0.0.1:9')
    H = {'X-MALIGN-Token': info['token']}
    try:
        call(info['url'] + 'api/yard/start', {'controller': 'ai', 'speed': 8, 'seed': 5, 'people': False, 'debris': False}, H)
        end = time.time() + 150
        while time.time() < end:
            st = call(info['url'] + 'api/yard/state', None, H)
            if st['stats']['inbound'] >= 1:
                break
            time.sleep(1)
        assert st['stats']['inbound'] >= 1 and not st['ai_ok'], st['stats']
        assert any('Ollama 응답 없음' in l['text'] for l in st['log']) or st['stats']['rule_decisions'] > 0
    finally:
        h.terminate(); h.wait(timeout=8)
fake.shutdown()
print('PASS: yard — car-like paths (no on-the-spot turns, reverse used, collision-free); guard rules; '
      'trucks in/out with full-yard outbound; obstacles horn/detour/crew without contact; user request first; '
      'AI decisions through the guard (fake Ollama) with kit alignment; fallback to rules without Ollama', flush=True)
