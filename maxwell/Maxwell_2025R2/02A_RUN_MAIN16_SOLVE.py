# -*- coding: ascii -*-
# MAS-kit v1.5.11 launcher. AEDT 2025 R2: Tools > Run Script > select this file.
# Mode: SOLVE fixed list ['P1_center', 'P1_offaxis_30_30', 'P2_xp5', 'P2_xm5', 'P2_yp5', 'P2_ym5', 'P3_diag_p3_p3', 'P3_diag_p3_m3', 'P4_pitch_p1', 'P4_roll_p1', 'P4_gap18', 'P4_gap22', 'P5_xp15', 'P5_xp30', 'P5_yp15', 'P5_yp30'] (config cases_to_run is ignored). See README_FIRST_KO.md.
import os, sys, traceback
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)
try:
    oDesktop.AddMessage('', '', 0, 'MASKIT150 launcher started: ' + _here)
except Exception:
    pass
try:
    import maskit150_core as core
    try:
        reload(core)  # IronPython 2.7: pick up edited core on re-run in same session
    except NameError:
        pass
    if 'SOLVE' == 'BUILD':
        r = core.build(oDesktop)
        print('MASKIT150 RESULT ' + str(r.get('status')))
    else:
        r = core.run_cases(oDesktop, True, ['P1_center', 'P1_offaxis_30_30', 'P2_xp5', 'P2_xm5', 'P2_yp5', 'P2_ym5', 'P3_diag_p3_p3', 'P3_diag_p3_m3', 'P4_pitch_p1', 'P4_roll_p1', 'P4_gap18', 'P4_gap22', 'P5_xp15', 'P5_xp30', 'P5_yp15', 'P5_yp30'])
        print('MASKIT150 RESULT ' + str(r))
except Exception:
    err = traceback.format_exc()
    f = open(os.path.join(_here, 'MASKIT150_BOOT_ERROR.log'), 'a')
    f.write(err + '\n')
    f.close()
    print('MASKIT150_BOOT_ERROR ' + err)
    try:
        oDesktop.AddMessage('', '', 2, 'MASKIT150 BOOT ERROR: ' + err[-800:])
    except Exception:
        pass
    try:
        g = open(os.path.join(os.path.expanduser('~'), 'MASKIT150_BOOT_ERROR.log'), 'a')
        g.write(err + '\n')
        g.close()
    except Exception:
        pass
