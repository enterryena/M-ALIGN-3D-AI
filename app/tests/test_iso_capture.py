"""Real-spec options: ISO 668-based container corner tolerance and twistlock engagement (pm5 / head / float10).
- defaults leave the original engine untouched (same trials as before)
- iso mode: deviations inside the ISO limits; the same container keeps them on a re-approach
- capture criteria behave as specified; the virtual reach stacker's lock uses them."""
import sys, pathlib, random
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import malign_engine as E, malign_link
from reachstacker_sim import VirtualReachStacker
M = malign_link._load_model()
P0 = E.settings('base', 'history')
for s in range(1, 30):   # default = original
    a = E.run_once(M, P0, E.Rng(s)); b = E.run_once(M, E.settings('base', 'history', tolModel='orig', capture='pm5'), E.Rng(s))
    assert a == b
# iso deviations inside the limits
for s in range(200):
    r = random.Random(s); t = E.iso_tol([r.gauss(0, 3) for _ in range(3)], [[0, 0]] * 4)
    S = (t[1][0] - t[0][0]); P = (t[0][1] - t[3][1])          # corner-centre length / width change (mm)
    assert -5 - 1e-9 <= S <= 3 + 1e-9 and -5 - 1e-9 <= P <= 3 + 1e-9, (S, P)
    assert abs(t[0][0] - t[3][0]) <= 13 / 1.87 + 1e-9
# capture criteria
cap = lambda e, c: E.capture_ok(e, dict(P0, capture=c))
e = [[7, 1], [0, 0], [0, 0], [0, 0]]
assert not cap(e, 'pm5') and cap(e, 'head') and cap(e, 'float10')
e = [[1, 4.5], [0, 0], [0, 0], [0, 0]]
assert cap(e, 'pm5') and not cap(e, 'head') and cap(e, 'float10')
# lock uses the criterion; same container keeps its ISO deviations
ct = {'z': [[.3, -.2], [1, .1], [-.5, .4], [.2, .2]], 'iso': [1.5, -2.0, .8]}
Pi = E.settings('base', 'history', tolModel='iso', capture='float10')
a = VirtualReachStacker(M, Pi, E.Rng(5), E.Rng(6), container_tol=ct); b = VirtualReachStacker(M, Pi, E.Rng(77), E.Rng(78), container_tol=ct)
assert a.world.tol == b.world.tol == E.iso_tol(ct['iso'], ct['z'])
a.lower(); a.world.tx, a.world.ty, a.world.th = 0, 0, 0
assert a.lock()['locked'] == E.capture_ok(a.world.errs(), Pi)
# success rates in the expected order (base sensors, 300 trials)
rate = lambda **kw: sum(E.run_once(M, E.settings('base', 'history', **kw), E.Rng(s))['ok'] for s in range(1000, 1300)) / 300
r0, r1, r2 = rate(), rate(tolModel='iso'), rate(tolModel='iso', capture='float10')
assert r0 < r1 < r2 and r2 > .95, (r0, r1, r2)
print('PASS: ISO tolerance / capture — defaults unchanged; ISO limits; same box keeps deviations; lock uses the criterion; '
      'base sensors %.2f -> ISO %.2f -> ISO+floating %.2f' % (r0, r1, r2), flush=True)
