# -*- coding: ascii -*-
# MAS-kit v1.5.2 diagnostic. Changes NOTHING in any model. Tools > Run Script.
# Shows results in the AEDT Message Manager and writes MASKIT150_DIAG.txt
# next to this script AND in your user home folder.
import os, sys, json, traceback
lines = []
def say(m, lvl=0):
    lines.append(m)
    try:
        oDesktop.AddMessage('', '', lvl, 'MASKIT150 DIAG ' + m)
    except Exception:
        pass
try:
    say('python=' + sys.version.replace(chr(10), ' '))
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        say('script_dir=' + here)
    except NameError:
        here = os.getcwd()
        say('__file__ NOT DEFINED, cwd=' + here, 1)
    cfgp = os.path.join(here, 'config_v150.json')
    say('config_exists=' + str(os.path.isfile(cfgp)))
    cfg = json.load(open(cfgp, 'r'))
    say('version=' + str(cfg.get('version')) + ' output_dir=' + cfg['output_dir'])
    src = os.path.join(here, cfg['source_aedt'])
    say('source_aedt_exists=' + str(os.path.isfile(src)) + ' ' + src)
    say('core_exists=' + str(os.path.isfile(os.path.join(here, 'maskit150_core.py'))))
    od = cfg['output_dir']
    try:
        if not os.path.isdir(od):
            os.makedirs(od)
        t = os.path.join(od, '_write_test.txt'); open(t, 'w').write('ok'); os.remove(t)
        say('output_dir_writable=True')
        say('output_dir_listing=' + repr(sorted(os.listdir(od))[:30]))
    except Exception:
        say('output_dir_writable=False ' + traceback.format_exc()[-300:], 2)
    try:
        sys.path.insert(0, here)
        import maskit150_core as core
        say('core_import_ok version=' + core.VERSION)
    except Exception:
        say('core_import_FAILED ' + traceback.format_exc()[-600:], 2)
    try:
        say('open_projects=' + repr([str(x) for x in oDesktop.GetProjectList()]))
    except Exception:
        say('GetProjectList failed', 1)
except Exception:
    say('DIAG_EXCEPTION ' + traceback.format_exc()[-800:], 2)
for path in [os.path.join(locals().get('here', os.getcwd()), 'MASKIT150_DIAG.txt'),
             os.path.join(os.path.expanduser('~'), 'MASKIT150_DIAG.txt')]:
    try:
        f = open(path, 'w'); f.write('\n'.join(lines) + '\n'); f.close()
    except Exception:
        pass
