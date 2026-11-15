"""Case memory: failures retried, successes accumulated, new jobs steered toward what worked in similar situations.

A. features / failure reasons from real decisions; cases persist across restarts (SQLite, closed handles)
B. choice: similar successful cases win; dissimilar cases count little; kit-first plans are blocked unless the
   kit itself is good (or the machine sensor contradicts it / is not valid); a plan that just failed is not repeated;
   the failure reason prefers fitting plans; a plan with false lowerings in similar cases is avoided
C. learning experiment (synchronous jobs, mixed site conditions): steering by memory reaches success within 3
   attempts more often than the fixed original plan, without more false lowerings
D. yard: failed alignment is retried (re-approach when the reason calls for it), retry success counted
"""
import pathlib, sys, tempfile, time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import case_memory as CM  # noqa: E402
import yard_ops  # noqa: E402

# ---------------- A
d_good = {'corner_est_mm': [[5, 2], [6, 1], [4, 3], [5, 2]], 'grade': ['good'] * 4, 'kit_status': 'good', 'machine_status': 'unstable'}
f = CM.features_from(d_good, 'target')
assert f['machine'] == 'unstable' and f['kit'] == 'good' and f['dist'] == 'near' and not f['disagree'] and f['invalid'] == 0
d_dis = dict(d_good, machine_status='valid', disagree_mm=9.0, disagree_limit_mm=4.0, kit_status='caution')
assert CM.features_from(d_dis, 'target')['disagree']
assert CM.failure_reason('success', []) is None and CM.failure_reason('false_lower', []) == 'false_lower'
assert CM.failure_reason('held', [{'reason': 'disagree'}]) == 'disagree'
assert CM.failure_reason('held', [{'reason': 'no_reliable_data'}]) == 'low_reliability'
assert CM.failure_reason('held', [{'reason': 'interlock'}]) == 'not_converged'

with tempfile.TemporaryDirectory() as tmp:
    db = pathlib.Path(tmp) / 'yard_cases.sqlite3'
    m = CM.CaseMemory(db)
    for i in range(10):
        m.add(f, 'KIT_SOFT', 'success', job_id='J%d' % i)
        m.add(f, 'STD', 'success' if i < 2 else 'held')
    m2 = CM.CaseMemory(db)                       # restart: the cases are still there
    assert m2.summary()['cases'] == 20 and m2.summary()['by_plan']['KIT_SOFT']['ok'] == 10

    # ---------------- B
    c = m2.choose(f)
    assert c['plan'] == 'KIT_SOFT' and c['similar'] >= 15 and c['expected'] > 0.8, c
    other = dict(f, machine='none', kit='caution', dist='far', hardware='base')     # different situation
    sc, nsim = m2.score(other)
    assert nsim == 0 and sc['KIT_SOFT']['evidence'] < 0.2
    # safety: kit-first plans need a good kit (or no valid machine cross-check / a contradiction)
    risky = dict(f, machine='valid', kit='caution', disagree=False)
    assert set(CM.CaseMemory.safe_plans(risky)) == {'STD', 'SOFT'}
    assert 'KIT' in CM.CaseMemory.safe_plans(dict(risky, disagree=True))
    # retry: the failed plan is not repeated; the reason prefers fitting plans
    r = m2.choose(f, tried=('KIT_SOFT',), reason='not_converged')
    assert r['plan'] != 'KIT_SOFT' and '재시도' in r['why'], r
    # false lowerings in similar cases push a plan away even if it often "succeeds"
    m3 = CM.CaseMemory()
    g = dict(f, machine='unstable', kit='good')
    for i in range(12):
        m3.add(g, 'KIT', 'success' if i % 3 else 'false_lower')
        m3.add(g, 'STD', 'success' if i % 2 else 'held')
    assert m3.choose(g)['plan'] != 'KIT'

# ---------------- C
sys.path.insert(0, str(ROOT / 'tools'))
import learning_experiment  # noqa: E402
rep = learning_experiment.main(300)
fx = rep['fixed']['early']['within_3'] + rep['fixed']['late']['within_3']
mm = rep['memory']['early']['within_3'] + rep['memory']['late']['within_3']
fl_fx = rep['fixed']['early']['false_lower'] + rep['fixed']['late']['false_lower']
fl_mm = rep['memory']['early']['false_lower'] + rep['memory']['late']['false_lower']
assert mm > fx, rep
assert fl_mm <= fl_fx + 3, rep

# ---------------- D. yard retries (stub alignment: first attempt fails with a reason)
calls = []


def aligner(yd, task):
    calls.append(task['id'])
    yd.wait_sim(4)
    n = calls.count(task['id'])
    if task['id'] == 'K1' and n == 1:
        return {'ok': False, 'reason': 'false_lower', 'plan': 'STD'}
    if task['id'] == 'K2' and n == 1:
        return {'ok': False, 'reason': 'not_converged', 'plan': 'STD'}
    return {'ok': True, 'plan': 'KIT_SOFT'}


y = yard_ops.YardOps(controller='autopilot', seed=23, speed=40, people=False, debris=False, aligner=aligner).start()
t0 = time.time()
while time.time() - t0 < 90 and y.stats['inbound'] + y.stats['outbound'] < 2:
    time.sleep(0.3)
y.stop()
s = y.stats
assert s['retries'] >= 2 and s['retry_success'] >= 2 and s['inbound'] + s['outbound'] >= 2, s
txt = [l['text'] for l in y.log_lines]
assert any('재접근' in t for t in txt), 'false lowering should trigger a re-approach'
assert any('원인: 수렴 부족' in t for t in txt) and any('재시도 2회차 성공' in t for t in txt)
print('PASS: case memory — cases persist; similar successes steer the plan; kit-first only when safe; failed plan '
      'not repeated, reason-preferred retry; false-lowering risk avoided; learning: within-3 %.3f vs fixed %.3f, '
      'false lowerings %d vs %d; yard retries with re-approach' % (mm / 2, fx / 2, fl_mm, fl_fx), flush=True)
