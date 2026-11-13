# -*- coding: utf-8 -*-
"""Virtual site for stage 3: container + reach stacker + its own sensors + the kit's coils.

Everything here is SIMULATION. The truth (where the twistlocks really are relative to
the corner castings) lives in malign_engine.World, built exactly like v3.5.4.

VirtualReachStacker
  approach(rough_target)   drive towards the container's rough position (given from afar)
                           and stop when the machine's OWN sensors say "inside the kit's
                           sensing range"; the remaining truth error is the v3.5.4 initial
                           error (R0 = 30 mm disk + rotation + corner tolerances)
  machine_sensor()         the machine's own estimate of the misalignment (x, y, theta),
                           per scenario: normal / unstable / none / biased
  move(x, y, theta_rad)    correction move commanded by the main server (actuator error)
  lower(), lock(), raise_() main action; lock succeeds only if every corner is within
                           the twistlock tolerance (5 mm, same as the v3.5.4 judgement)
Corner faults (kit side): 'none' | 'head_dead' (2 heads of one unit dead, detected) |
  'drift' (one unit's calibration drifted, NOT detected by the unit)

Separate random streams: the world uses the v3.5.4 stream (so results stay comparable
across strategies); machine sensor and faults use their own stream.
"""
import math

import malign_engine as E

LOCK_TOL_MM = 5.0
SENSING_RANGE_MM = 30.0     # Maxwell data cover +-30 mm; the kit is used inside this range
MACHINE_MODES = {
    # sigma_xy mm, sigma_theta deg, bias_sd mm (fixed per job), status reported by the machine
    'normal':   {'sxy': 1.5, 'sth': 0.01, 'bias': 1.0, 'status': 'valid'},
    'unstable': {'sxy': 6.0, 'sth': 0.05, 'bias': 1.0, 'status': 'unstable'},
    'none':     {'sxy': None, 'sth': None, 'bias': 0.0, 'status': 'none'},
    'biased':   {'sxy': 1.5, 'sth': 0.01, 'bias': 8.0, 'status': 'valid'},   # silent failure
}
FAULTS = ('none', 'head_dead', 'drift')
FAULT_CORNER = 1            # corner 2 (index 1)
DRIFT_UH = [2.5, 0.0, -2.0, 0.0, 0.0]


class VirtualReachStacker:
    def __init__(self, M, P, world_rnd, aux_rnd, machine_mode='none', fault='none', fault_corner=FAULT_CORNER):
        if machine_mode not in MACHINE_MODES:
            raise ValueError('machine_mode: ' + ', '.join(MACHINE_MODES))
        if fault not in FAULTS:
            raise ValueError('fault: ' + ', '.join(FAULTS))
        self.M, self.P, self.aux = M, P, aux_rnd
        self.machine_mode, self.fault, self.fault_corner = machine_mode, fault, int(fault_corner)
        self.world = E.World(P, world_rnd)
        mm = MACHINE_MODES[machine_mode]
        self.mbias = [mm['bias'] * aux_rnd.n(), mm['bias'] * aux_rnd.n()]
        self.phase = 'approach'
        self.lowered = self.locked = False
        self.log = []

    # ---- truth (for the simulator display and the final judgement only)
    def corner_errors(self):
        return self.world.errs()

    def worst_mm(self):
        return max(max(abs(v[0]), abs(v[1])) for v in self.world.errs())

    # ---- machine side
    def approach(self, rough_target=None):
        """Coarse approach with the machine's own means; ends inside the kit's sensing range."""
        self.phase = 'in_range'
        self.log.append(('approach', rough_target))
        return {'in_range': True, 'range_mm': SENSING_RANGE_MM}

    def machine_sensor(self):
        mm = MACHINE_MODES[self.machine_mode]
        if mm['sxy'] is None:
            return {'status': 'none'}
        w = self.world
        return {'status': mm['status'],
                'x_mm': w.tx + self.mbias[0] + mm['sxy'] * self.aux.n(),
                'y_mm': w.ty + self.mbias[1] + mm['sxy'] * self.aux.n(),
                'theta_rad': w.th + math.radians(mm['sth']) * self.aux.n()}

    def move(self, x_mm, y_mm, theta_rad):
        self.world.apply_move(x_mm, y_mm, theta_rad)
        self.log.append(('move', x_mm, y_mm, theta_rad))
        return {'done': True}

    def lower(self):
        self.lowered = True
        self.phase = 'lowered'
        return {'done': True}

    def lock(self):
        self.locked = self.lowered and self.worst_mm() <= LOCK_TOL_MM
        self.phase = 'locked' if self.locked else 'lock_failed'
        return {'locked': self.locked, 'worst_mm': self.worst_mm()}

    def raise_(self):
        self.phase = 'raised'
        return {'done': True}

    # ---- the kit's coils (what each unit's reader would output)
    def kit_readings(self):
        """4 units: {'sensor_uH': 5 values, 'head_ok': 5 flags}. Consumes the world stream like v3.5.4."""
        E_ = self.world.errs()
        out = []
        for k in range(4):
            r = self.world.read_corner(self.M, k, E_)
            ok = [True] * 5
            if k == self.fault_corner and self.fault == 'head_dead':
                ok = [True, False, True, False, True]
                r = [r[0], 0.0, r[2], 0.0, r[4]]
            elif k == self.fault_corner and self.fault == 'drift':
                r = [v + d for v, d in zip(r, DRIFT_UH)]
            out.append({'sensor_uH': r, 'head_ok': ok})
        return out
