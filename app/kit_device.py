# -*- coding: utf-8 -*-
"""What ONE correction unit computes from its own 5-head reading (device-firmware logic).

Input : 5 calibrated readings (dL = L - L0, uH) of this unit's corner, head health, diagnostics.
Output: corner estimate (container frame), fitted gap change, reliability.
No 4-corner logic here — combining corners and choosing priorities is the main server's job.
Same arithmetic as the v3.5.4 grid estimator (maskit_engine.grid_estimate).
"""
import maskit_engine as E

DEVICE_SETTINGS = {'useHead': [1, 1, 1, 1, 1], 'searchMM': 36, 'fitGap': True}
SIM_DIAG = {'coil_temp_c': 25.0, 'supply_v': 24.0, 'fw_cal_version': 'SIM-FEM21-v1', 'error_code': 0}


def process_reading(M, corner_index, sensor_uH, head_ok=None, mirror=True):
    head_ok = [bool(v) for v in (head_ok or [True] * 5)]
    P = dict(DEVICE_SETTINGS, useHead=[1 if ok else 0 for ok in head_ok])
    if sum(P['useHead']) < 3:  # too few heads to locate anything
        return {'est_mm': None, 'gap_mm': None,
                'quality': {'rss_uH2': None, 'extrapolated': None, 'gap_out_of_range': None,
                            'head_ok': head_ok, 'grade': 'invalid'}}
    es = E.grid_estimate(M, sensor_uH, P)
    f = E.frame(corner_index, mirror)
    return {'est_mm': [f[0] * es['x'], f[1] * es['y']], 'gap_mm': es['dz'],
            'quality': E.corner_quality(M, sensor_uH, es, P, head_ok)}
