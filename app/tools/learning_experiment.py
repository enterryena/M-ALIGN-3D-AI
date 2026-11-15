# -*- coding: utf-8 -*-
"""Does steering by similar past cases help? Same stream of jobs (mixed site conditions), two servers:
  fixed  : always the original plan (STD); a failed job is retried up to 2 more times with the same plan
  memory : case memory picks the plan after the first sensing, retries follow the failure reason
Each attempt is a new approach (new seed). Reports first-attempt success and success within 3 attempts,
early (first half) vs late (second half), plus false lowerings. Uses the synchronous job (sim_job).

usage: python tools/learning_experiment.py [jobs=400] [report.json]
"""
import json
import pathlib
import random
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import case_memory as CM  # noqa: E402
import malign_engine as E  # noqa: E402
import malign_link  # noqa: E402
import sim_job  # noqa: E402


def conditions(rnd):
    r = rnd.random()
    machine = 'normal' if r < .5 else 'unstable' if r < .75 else 'biased' if r < .9 else 'none'
    r = rnd.random()
    fault = 'none' if r < .85 else 'drift' if r < .95 else 'head_dead'
    return {'hardware': 'target' if rnd.random() < .8 else 'base', 'machine': machine, 'fault': fault}


def run(M, cond, seed, plan_hook):
    P = E.settings(cond['hardware'], 'history')
    return sim_job.run_job(M, P, E.Rng(seed), E.Rng(seed ^ 0x5A5A5A5A), 'weighted', cond['machine'], cond['fault'],
                           plan_hook=plan_hook)


def main(n=400, out=None):
    M = malign_link._load_model()
    rnd = random.Random(2026)
    stream = [(conditions(rnd), rnd.randrange(1, 1 << 30)) for _ in range(n)]
    mem = CM.CaseMemory()
    res = {'fixed': [], 'memory': []}
    plans_used = {}
    t0 = time.time()
    for i, (cond, seed) in enumerate(stream):
        # fixed server
        rec = {'first': False, 'within3': False, 'false_lower': 0, 'attempts': 0}
        for a in range(3):
            r = run(M, cond, seed * 7 + a, None)
            rec['attempts'] += 1
            rec['false_lower'] += r['outcome'] == 'false_lower'
            if r['ok']:
                rec['first'] = rec['first'] or a == 0
                rec['within3'] = True
                break
        res['fixed'].append(rec)
        # memory server
        rec = {'first': False, 'within3': False, 'false_lower': 0, 'attempts': 0}
        tried, reason = [], None
        for a in range(3):
            box = {}

            def hook(d, a=a, reason=reason, tried=tuple(tried)):
                f = CM.features_from(d, cond['hardware'], a, reason)
                p = mem.choose(f, tried, reason)
                box.update(f=f, p=p['plan'])
                return p
            r = run(M, cond, seed * 7 + a, hook)
            why = CM.failure_reason(r['outcome'], r['decisions'], box.get('f'))
            mem.add(box['f'], box['p'], r['outcome'], reason=why)
            plans_used[box['p']] = plans_used.get(box['p'], 0) + 1
            tried.append(box['p'])
            reason = why
            rec['attempts'] += 1
            rec['false_lower'] += r['outcome'] == 'false_lower'
            if r['ok']:
                rec['first'] = rec['first'] or a == 0
                rec['within3'] = True
                break
        res['memory'].append(rec)
    half = n // 2

    def summ(lst):
        return {'first_attempt': round(sum(r['first'] for r in lst) / len(lst), 3),
                'within_3': round(sum(r['within3'] for r in lst) / len(lst), 3),
                'mean_attempts': round(sum(r['attempts'] for r in lst) / len(lst), 2),
                'false_lower': sum(r['false_lower'] for r in lst)}
    report = {'jobs': n, 'seconds': round(time.time() - t0),
              'fixed': {'early': summ(res['fixed'][:half]), 'late': summ(res['fixed'][half:])},
              'memory': {'early': summ(res['memory'][:half]), 'late': summ(res['memory'][half:])},
              'plans_used': plans_used, 'memory_summary': mem.summary()}
    if out:
        pathlib.Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    return report


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    rep = main(n, sys.argv[2] if len(sys.argv) > 2 else None)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
