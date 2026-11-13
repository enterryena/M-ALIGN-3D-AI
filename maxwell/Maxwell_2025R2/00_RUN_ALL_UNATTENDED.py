# -*- coding: ascii -*-
# M-ALIGN v1.5.11 launcher. AEDT 2025 R2: Tools > Run Script > select this file.
# Mode: RUN ALL unattended (16 cases -> post -> blind -> P6 -> final post). Re-run = resume.
import os, sys, traceback
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)
try:
    oDesktop.AddMessage('', '', 0, 'MALIGN150 launcher started: ' + _here)
except Exception:
    pass
try:
    import malign150_core as core
    try:
        reload(core)  # IronPython 2.7: pick up edited core on re-run in same session
    except NameError:
        pass
    if 'SOLVE' == 'BUILD':
        r = core.build(oDesktop)
        print('MALIGN150 RESULT ' + str(r.get('status')))
    else:
        r = core.run_all(oDesktop)
        print('MALIGN150 RUN_ALL FINISHED')
except Exception:
    err = traceback.format_exc()
    f = open(os.path.join(_here, 'MALIGN150_BOOT_ERROR.log'), 'a')
    f.write(err + '\n')
    f.close()
    print('MALIGN150_BOOT_ERROR ' + err)
    try:
        oDesktop.AddMessage('', '', 2, 'MALIGN150 BOOT ERROR: ' + err[-800:])
    except Exception:
        pass
    try:
        g = open(os.path.join(os.path.expanduser('~'), 'MALIGN150_BOOT_ERROR.log'), 'a')
        g.write(err + '\n')
        g.close()
    except Exception:
        pass
