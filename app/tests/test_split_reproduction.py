"""Stage-1 completion check: with the work split between kit and main server
(kit: own-corner grid estimate + reliability; server: history estimation with the
moves IT commanded + 4-corner rigid fit + gain), the v3.5.4 results are unchanged,
trial by trial. Default: grid 1,000 + history 150. MASKIT_SLOW=1: history 1,000 (586).
"""
import json, os, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'server_example'))
import maskit_engine as E  # noqa: E402
import maskit_server_logic as S  # noqa: E402

REF = json.loads((ROOT / 'tests' / 'fixtures' / 'js_reference_v354.json').read_text(encoding='utf-8'))
SLOW = os.environ.get('MASKIT_SLOW') == '1'
M = E.Model()


def check(key, scenario, estimator, n):
    P, rnd, ok = E.settings(scenario, estimator), E.Rng(2026), 0
    for i, (r_ok, r_worst, r_steps) in enumerate(REF[key][:n]):
        r = S.run_job_split(M, P, rnd)
        ok += r['ok']
        assert int(r['ok']) == r_ok and r['steps'] == r_steps and abs(r['worst'] - r_worst) < 1e-9, (key, i)
        if estimator == 'history' and len(r['decisions']) >= 2:
            assert r['decisions'][1]['method'] == ['history'] * 4
    return ok


assert check('base_base_2026', 'base', 'base', 1000) == 68
n = 1000 if SLOW else 150
got = check('base_history_2026', 'base', 'history', n)
assert got == (586 if SLOW else sum(r[0] for r in REF['base_history_2026'][:n]))
print('PASS: kit/server split reproduces v3.5.4 trial-by-trial (grid 68/1000, history %s)' %
      ('586/1000' if SLOW else 'first 150'), flush=True)
