"""Python engine port == v3.5.4 JS engine, trial by trial.

Fixture rows were produced by the JS engine in node (tests/fixtures/make_js_reference.js).
Default run: grid 1,000 trials + history 150 trials (~20 s).
MALIGN_SLOW=1: full history 1,000 (586 successes) and target 300 (299), ~2 min.
If node is installed, a fresh JS run is also compared live.
"""
import json, os, pathlib, shutil, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import malign_engine as E  # noqa: E402

REF = json.loads((ROOT / 'tests' / 'fixtures' / 'js_reference_v354.json').read_text(encoding='utf-8'))
SLOW = os.environ.get('MALIGN_SLOW') == '1'
M = E.Model()


def compare(key, scenario, estimator, n, seed=2026):
    rows = REF[key][:n]
    P, rnd = E.settings(scenario, estimator), E.Rng(seed)
    ok = 0
    for i, (r_ok, r_worst, r_steps) in enumerate(rows):
        r = E.run_once(M, P, rnd)
        ok += r['ok']
        assert int(r['ok']) == r_ok and r['steps'] == r_steps, (key, i, r, rows[i])
        assert abs(r['worst'] - r_worst) < 1e-9, (key, i, r['worst'], r_worst)
    return ok


assert compare('base_base_2026', 'base', 'base', 1000) == 68
assert compare('base_history_2026', 'base', 'history', 1000 if SLOW else 150) == (586 if SLOW else sum(r[0] for r in REF['base_history_2026'][:150]))
if SLOW:
    assert compare('target_history_2026', 'target', 'history', 300) == 299

node = shutil.which('node')
if node:
    live = json.loads(subprocess.check_output([node, str(ROOT / 'tests' / 'fixtures' / 'make_js_reference.js'),
                                               'base', 'history', '40', '77'], timeout=120))
    P, rnd = E.settings('base', 'history'), E.Rng(77)
    for i, row in enumerate(live):
        r = E.run_once(M, P, rnd)
        assert int(r['ok']) == row[0] and r['steps'] == row[2] and abs(r['worst'] - row[1]) < 1e-9, (i, r, row)

# Device-side model size (separable form) — the figure quoted in the plan.
assert M.model_size() == 1620
print('PASS: Python engine matches v3.5.4 JS trial-by-trial (grid 68/1000%s)%s' %
      (', history 586/1000, target 299/300' if SLOW else ', history first 150', ', live node check' if node else ''), flush=True)
