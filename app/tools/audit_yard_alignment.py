# -*- coding: utf-8 -*-
"""Audit: does the yard's pick-up follow the existing kit sensing / correction pipeline exactly?

Runs the real kit link (simulation source) + built-in main server + yard (rule autopilot), records every
alignment job the yard ran, then checks each one:
  1. kit bundles in the outbox for that job: 4 units x 23 items, measurement ids in order, one bundle per sensing
  2. order: every correction move comes after a fresh kit sensing (one move per sensing), rounds <= limit
  3. correction values: each move equals the rule advisor's suggestion computed from that bundle
  4. lowering: only when the predicted worst corner <= 4 mm; locked <=> true worst corner <= 5 mm
  5. replay: the same seed run as a stand-alone single job (no yard) gives the identical moves and result
Writes a JSON report (default: audit_yard_alignment.json next to this file).

usage: python tools/audit_yard_alignment.py [n_jobs] [report.json]
"""
import json
import math
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import malign_link  # noqa: E402
import yard_ops  # noqa: E402
from main_controller import LOWER_OK_MM  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
OUT = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / 'tools' / 'audit_yard_alignment.json'


def count_items(u):
    """Same count as tests/test_kit_link.py: identity 6 + 5 coil readings + estimate 3 + quality 5 + diagnostics 4."""
    n = sum(1 for k in ('unit_id', 'corner', 'job_id', 'round_no', 'measurement_id', 'measured_at') if u.get(k) is not None)
    n += len(u['sensor_uH'])
    n += (2 if u['est_mm'] else 0) + (1 if u['gap_mm'] is not None else 0)
    n += sum(1 for k in ('rss_uH2', 'extrapolated', 'gap_out_of_range', 'head_ok', 'grade') if k in u['quality'])
    n += sum(1 for k in ('coil_temp_c', 'supply_v', 'fw_cal_version', 'error_code') if u['diag'].get(k) is not None)
    return n


def snapshot(site):
    j = site.job
    brain = j['brain']
    return {'job_id': j['job_id'], 'seed': j['seed'], 'settings': dict(j['settings']), 'outcome': j['outcome'],
            'plan': j.get('plan'),
            'truth_worst_mm': j['rs'].worst_mm(), 'worst0_mm': j['worst0'],
            'moves': [(e['x_mm'], e['y_mm'], e['theta_deg']) for e in j['events'] if e['kind'] == 'move'],
            'kinds': [e['kind'] for e in j['events']],
            'decisions': [{'action': d.get('action'), 'correction': d.get('correction'),
                           'predicted_worst_mm': d.get('predicted_worst_mm')} for d in brain.decisions],
            'guard': [e.get('reason') for e in j['events'] if e['kind'] == 'guard']}


def wait_done(site, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        if site.job and site.job['outcome'] is not None and site.job['phase'] in ('done', 'held', 'failed', 'aborted'):
            return True
        time.sleep(0.05)
    return False


def main():
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / 'Link').mkdir()
    (tmp / 'Link' / 'link_config.json').write_text(json.dumps({'sim_move_delay_s': 0.05}))
    rt = malign_link.open_link(tmp / 'Link')
    sim = rt.sim
    site = sim.site
    jobs = []
    orig = yard_ops.YardOps._align_site

    def recorded(self, task, *args, **kw):
        ok = orig(self, task, *args, **kw)
        if site.job is not None:
            jobs.append(dict(snapshot(site), task=task['id'], container=task.get('container'), kind=task['kind']))
        return ok
    yard_ops.YardOps._align_site = recorded
    import case_memory
    y = yard_ops.YardOps(site=site, controller='autopilot', seed=31, speed=10, vary=True,
                         memory=case_memory.CaseMemory(tmp / 'Link' / 'yard_cases.sqlite3'),
                         kit_state=lambda: rt.link.status()['state']).start()
    sim.yard = y
    t0 = time.time()
    while len(jobs) < N and time.time() - t0 < 600:
        time.sleep(0.5)
    y.stop()
    yard_ops.YardOps._align_site = orig
    # a job cut off by stopping the yard is not a finished alignment
    cut = [j for j in jobs if j['outcome'] == 'aborted']
    jobs = [j for j in jobs if j['outcome'] != 'aborted']

    def kit_idle(timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if rt.link.status()['state'] == 'IDLE':
                return True
            time.sleep(0.1)
        return False
    kit_idle()

    # outbox rows per session
    import sqlite3
    con = sqlite3.connect(str(tmp / 'Link' / 'outbox.sqlite3'))
    rows = con.execute('SELECT session_id, kind, payload FROM outbox ORDER BY seq').fetchall()
    con.close()
    by_sid = {}
    for sid, kind, payload in rows:
        by_sid.setdefault(sid, []).append((kind, json.loads(payload)))

    report = {'jobs': [], 'cut_by_stop': len(cut), 'yard_stats': y.stats, 'sim_time_s': round(y.t), 'checks': {}}
    fails = []
    for jb in jobs:
        sid = jb['job_id']
        bundles = [p for k, p in by_sid.get(sid, []) if k == 'round' or p.get('kind') == 'round']
        items = sorted({count_items(u) for b in bundles for u in b.get('units', [])})
        mids = [b.get('measurement_id') for b in bundles]
        senses = jb['kinds'].count('sense')
        # 2. order: a move must follow a sense with no other move in between
        ordered, last = True, None
        for k in jb['kinds']:
            if k == 'move' and last != 'sense':
                ordered = False
            if k in ('sense', 'move'):
                last = k
        # 3. moves == advisor corrections (autopilot passes suggest_correction's rounded numbers)
        sugg = [d['correction'] for d in jb['decisions'] if d['correction']]
        match = len(sugg) >= len(jb['moves']) and all(
            abs(m[0] - round(c['x_mm'], 3)) < 1e-6 and abs(m[1] - round(c['y_mm'], 3)) < 1e-6 and
            abs(m[2] - round(c['theta_deg'], 5)) < 1e-9 for m, c in zip(jb['moves'], sugg))
        lowered = 'lower' in jb['kinds']
        locked_ok = (jb['outcome'] == 'success') == (jb['truth_worst_mm'] <= 5.0) if lowered else jb['outcome'] != 'success'
        last_pred = next((d['predicted_worst_mm'] for d in reversed(jb['decisions']) if d['predicted_worst_mm'] is not None), None)
        rec = {'job': sid, 'task': jb['task'], 'plan': (jb['plan'] or {}).get('plan'),
               'site': '%s/%s' % (jb['settings']['machine'], jb['settings']['fault']), 'container': jb['container'], 'kind': jb['kind'], 'seed': jb['seed'],
               'outcome': jb['outcome'], 'start_worst_mm': round(jb['worst0_mm'], 2), 'final_worst_mm': round(jb['truth_worst_mm'], 2),
               'kit_bundles': len(bundles), 'senses': senses, 'items_per_unit': items,
               'measurement_ids_increasing': mids == sorted(mids) and len(set(mids)) == len(mids),
               'moves': len(jb['moves']), 'move_after_sensing': ordered, 'moves_equal_advisor': match,
               'lowered': lowered, 'predicted_worst_at_lower_mm': last_pred, 'lock_matches_truth': locked_ok,
               'guard_refusals': len(jb['guard'])}
        ok = (len(bundles) == senses and items == [23] and rec['measurement_ids_increasing'] and ordered and match
              and locked_ok and (not lowered or (last_pred is not None and last_pred <= LOWER_OK_MM + 1e-9)))
        rec['pass'] = ok
        if not ok:
            fails.append(sid)
        report['jobs'].append(rec)

    # 5. replay the same seeds as stand-alone single jobs (the 3D-align / panel path)
    replay_same = 0
    for jb in jobs:
        params = {k: jb['settings'][k] for k in ('hardware', 'machine', 'fault', 'strategy')}   # incl. varied site conditions
        plan = jb['plan']          # the case memory's choice is part of the job: replay it
        kit_idle()
        site.start(seed=jb['seed'], controller='autopilot', machine_id='REPLAY',
                   plan_hook=(lambda d, plan=plan: plan) if plan else None, **params)
        wait_done(site)
        r = snapshot(site)
        same = (r['outcome'] == jb['outcome'] and abs(r['truth_worst_mm'] - jb['truth_worst_mm']) < 1e-9
                and r['moves'] == jb['moves'])
        replay_same += same
        if not same:
            fails.append('replay:' + jb['job_id'])
            report.setdefault('replay_diff', []).append({
                'job': jb['job_id'], 'yard': [jb['outcome'], round(jb['truth_worst_mm'], 3), len(jb['moves'])],
                'replay': [r['outcome'], round(r['truth_worst_mm'], 3), len(r['moves'])],
                'first_moves': [jb['moves'][:2], r['moves'][:2]], 'kit_state': rt.link.status()['state'],
                'replay_guard': r['guard'][:3], 'replay_kinds': r['kinds'][:12]})
            print('REPLAY DIFF', json.dumps(report['replay_diff'][-1], ensure_ascii=False))
    rt.stop()
    succ = sum(1 for j in report['jobs'] if j['outcome'] == 'success')
    report['checks'] = {'jobs': len(jobs), 'pass': len(jobs) - len([f for f in fails if not f.startswith('replay')]),
                        'replay_identical': replay_same, 'lock_success': succ, 'failures': fails}
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
    report['memory'] = y.memory.summary()
    print(json.dumps(report['checks'], ensure_ascii=False), 'memory', json.dumps(report['memory']))
    for j in report['jobs']:
        print('%-8s %-16s ' % (j['plan'], j['site']), end='')
        print('%-26s %-6s %-4s seed=%-11s %s start %5.1f mm -> final %4.2f mm | bundles %d/%d items %s moves %d '
              'advisor=%s order=%s lower@%s' % (j['job'], j['task'], j['kind'], j['seed'], j['outcome'], j['start_worst_mm'],
                                               j['final_worst_mm'], j['kit_bundles'], j['senses'], j['items_per_unit'], j['moves'],
                                               j['moves_equal_advisor'], j['move_after_sensing'],
                                               None if j['predicted_worst_at_lower_mm'] is None else round(j['predicted_worst_at_lower_mm'], 2)))
    return 0 if not fails and jobs else 1


if __name__ == '__main__':
    sys.exit(main())
