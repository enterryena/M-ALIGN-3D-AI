# -*- coding: ascii -*-
# M-ALIGN v1.5.11: run the post-processing inside AEDT (no separate Python needed).
# Tools > Run Script. Reads/writes only output_dir. Does not touch any model.
import os, sys, json, traceback
_here = os.path.dirname(os.path.abspath(__file__))
_post = os.path.join(os.path.dirname(_here), 'Python_Post')
for p in (_here, _post):
    if p not in sys.path:
        sys.path.insert(0, p)
def _msg(m, lvl=0):
    try:
        oDesktop.AddMessage('', '', lvl, 'MALIGN150 POST ' + str(m)[:900])
    except Exception:
        print(m)
try:
    cfg = json.load(open(os.path.join(_here, 'config_v150.json'), 'r'))
    import malign150_post as post
    try:
        reload(post)
    except NameError:
        pass
    out = cfg['output_dir']
    _msg('check_blind START ' + out)
    try:
        r = post.check_blind(out)
    except SystemExit as e:
        _msg('check_blind STOPPED: ' + str(e), 2)
        r = None
    if 'check_blind' == 'collect' and r:
        ck = r.get('checks', {})
        for k in ('P1_center_vs_AFAST', 'P1_offaxis_diff_vs_AFAST', 'P1_baseline_asymmetry_uH'):
            if k in ck:
                _msg(k + ' ' + json.dumps(ck[k], default=str)[:700])
        if 'P2_sensitivity' in ck:
            p2 = ck['P2_sensitivity']
            _msg('P2 verdict=%s usable_x=%s usable_y=%s cond=%.2f' % (p2['verdict'], p2['usable_heads_x'], p2['usable_heads_y'], p2['condition']))
        _msg('full report: ' + os.path.join(out, 'MATRIX5_CHECKS.json'))
    _msg('check_blind END')
except Exception:
    _msg('POST ERROR ' + traceback.format_exc()[-800:], 2)
