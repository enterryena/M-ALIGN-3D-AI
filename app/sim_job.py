# -*- coding: utf-8 -*-
"""One complete simulated job, synchronously: virtual reach stacker + kit units + main-server brain.

Used by the comparison experiment and the tests. The live (threaded/HTTP) version in
sim_site.py runs the very same steps, one message at a time.

Outcomes:  success      lowered and the twistlocks locked (truth worst corner <= 5 mm)
           false_lower  lowered although a corner was > 5 mm (the dangerous case)
           held         the server refused to lower and stopped safely
"""
import kit_device
from main_controller import ControllerBrain
from reachstacker_sim import VirtualReachStacker


def kit_units(M, readings):
    units = []
    for k, r in enumerate(readings):
        res = kit_device.process_reading(M, k, r['sensor_uH'], r['head_ok'])
        units.append({'sensor_uH': r['sensor_uH'],
                      'est_mm': None if res['est_mm'] is None else {'x': res['est_mm'][0], 'y': res['est_mm'][1]},
                      'quality': res['quality']})
    return units


def run_job(M, P, world_rnd, aux_rnd, strategy='weighted', machine_mode='none', fault='none',
            max_rounds=6, interlock=True, use_machine=True, trace=False, fault_corner=1, brain_kw=None,
            plan_hook=None):
    """plan_hook(first_decision) -> plan dict (K, use_machine, max_rounds) or None: after the kit's first
    sensing the server may pick how to run the loop (case memory); the same bundle is then re-decided."""
    rs = VirtualReachStacker(M, P, world_rnd, aux_rnd, machine_mode, fault, fault_corner)
    rs.approach()
    w0 = rs.worst_mm()
    brain = ControllerBrain(M, strategy, use_machine, interlock, max_rounds, K=P['K'], estimator=P['estimator'])
    for k, v in (brain_kw or {}).items():
        setattr(brain, k, v)
    resense = False
    sensings = resenses = moves = 0
    frames = [rs.corner_errors()] if trace else None
    outcome = None
    while outcome is None:
        units = kit_units(M, rs.kit_readings())
        machine = rs.machine_sensor()
        d = brain.decide(units, machine, resense=resense)
        if plan_hook is not None and sensings == 0:
            plan = plan_hook(d)
            if plan:
                brain = ControllerBrain(M, strategy, plan['use_machine'], interlock, plan['max_rounds'], K=plan['K'],
                                        estimator=P['estimator'])
                d = brain.decide(units, machine, resense=resense)
        sensings += 1
        if d['action'] == 'resense':
            resense = True
            resenses += 1
            continue
        resense = False
        if d['action'] == 'stop':
            outcome = 'held'
            break
        c = d['correction']
        rs.move(c['x_mm'], c['y_mm'], c['theta_rad'])
        brain.apply_history_offsets(c)
        moves += 1
        if trace:
            frames.append(rs.corner_errors())
        if d['action'] == 'lower_after_move':
            rs.lower()
            outcome = 'success' if rs.lock()['locked'] else 'false_lower'
    return {'outcome': outcome, 'ok': outcome == 'success', 'worst': rs.worst_mm(), 'worst0': w0,
            'rounds': brain.rounds, 'sensings': sensings, 'resenses': resenses, 'moves': moves,
            'decisions': brain.decisions, 'frames': frames}
