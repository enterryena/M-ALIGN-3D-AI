# -*- coding: utf-8 -*-
"""Stage-3 comparison experiment. Writes JSON results (and prints a summary).

  python tools/compare_strategies.py [N] [out.json] [workers]

Set A  how to combine the 4 corners (kit only, machine sensor 'none'):
       v354 (no interlock, reference) / equal / diagonal (fixed reference pair 1-3) / weighted+outlier
       x hardware target, base
       x corner fault: none, head_dead or drift on corner 1 (a reference corner) or corner 2
Set B  priority with the machine's own sensor (weighted strategy):
       machine normal / unstable / none / biased x hardware target, base
Same random streams for every row (world seed 2026, aux seed 7): rows are paired.
"""
import json
import math
import pathlib
import sys
import time
from multiprocessing import Pool

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import malign_engine as E  # noqa: E402
import sim_job  # noqa: E402

_M = None


def wilson(k, n, z=1.96):
    if not n:
        return (0, 0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def run(cfg):
    global _M
    if _M is None:
        _M = E.Model()
    hw, strategy, machine, fault, fcorner, n, interlock, set_ = cfg
    P = E.settings(hw, 'history')
    w, a = E.Rng(2026), E.Rng(7)
    c = {'success': 0, 'false_lower': 0, 'held': 0}
    rounds = sens = res = 0
    t = time.time()
    for _ in range(n):
        r = sim_job.run_job(_M, P, w, a, strategy=strategy, machine_mode=machine, fault=fault,
                            interlock=interlock, fault_corner=fcorner)
        c[r['outcome']] += 1
        rounds += r['rounds']
        sens += r['sensings']
        res += r['resenses']
    lo, hi = wilson(c['success'], n)
    return {'set': set_, 'hardware': hw, 'strategy': strategy, 'machine': machine, 'fault': fault,
            'fault_corner': fcorner + 1 if fault != 'none' else None, 'n': n,
            'success': c['success'], 'false_lower': c['false_lower'], 'held': c['held'],
            'success_ci95': [lo, hi], 'avg_rounds': rounds / n, 'avg_sensings': sens / n,
            'resense_per_job': res / n, 'seconds': time.time() - t}


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    out = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / 'tools' / 'compare_results.json'
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    cfgs = []
    for hw in ('target', 'base'):
        for fault, fc in (('none', 0), ('head_dead', 0), ('drift', 0), ('head_dead', 1), ('drift', 1)):
            cfgs.append((hw, 'v354', 'none', fault, fc, n, False, 'A'))
            for strat in ('equal', 'diagonal', 'weighted'):
                cfgs.append((hw, strat, 'none', fault, fc, n, True, 'A'))
    for hw in ('target', 'base'):
        for machine in ('normal', 'unstable', 'none', 'biased'):
            cfgs.append((hw, 'weighted', machine, 'none', 0, n, True, 'B'))
    rows = []
    with Pool(workers) as pool:
        for r in pool.imap(run, cfgs):
            rows.append(r)
            print(r['set'], r['hardware'], r['fault'], r['fault_corner'], r['strategy'], r['machine'],
                  r['success'], r['false_lower'], r['held'], '%.0fs' % r['seconds'], flush=True)
    out.write_text(json.dumps({'n': n, 'world_seed': 2026, 'aux_seed': 7, 'rows': rows}, ensure_ascii=False, indent=1),
                   encoding='utf-8')
    print('written', out)


if __name__ == '__main__':
    main()
