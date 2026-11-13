# -*- coding: ascii -*-
# M-ALIGN v1.5.11 - 5-winding Matrix model for Maxwell 3D AC Magnetic (AEDT 2025 R2).
# IronPython 2.7 compatible (no f-strings). Run only through the 0x_*.py launchers.
#
# What this does
#   BUILD : copy the solved AFAST center-Xp model (geometry only, no .aedtresults),
#           add coils + coil terminals on the 4 passive U-cores, create Winding_2..5,
#           register a 5x5 Matrix ("Matrix5"), keep the AFAST adaptive setup,
#           validate, save as BASE. No solve.
#   CASES : for each case id in config "cases_to_run", copy BASE, move the container
#           group (dx,dy) or the spreader+sensor group (dz, pitch, roll), audit,
#           validate, optionally solve once, export 5x5 L/R/Z + per-pass L + .conv.
#
# What this never does
#   - create new container/casting CAD, Unite, delete existing objects
#   - change materials, mesh operations or the original Winding_1/CoilTerminal1
#   - close AEDT or projects (unless close_project_after_case=true in config)
#
# Arg formats cross-checked with pyaedt (main, 2026-11-10) and the 2025 R2-saved .aedt.
# Verified in AEDT 2025 R2 (user runs): build, 19-case preflight, solve, report export.
# Unattended run_all / resume / upload zips: verified with a mock API only.
import os, sys, csv, json, math, shutil, hashlib, datetime, traceback

VERSION = '1.5.12'
HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, 'config_v150.json'), 'r'))
if os.environ.get('MALIGN150_OUTPUT_DIR'):
    CFG['output_dir'] = os.environ['MALIGN150_OUTPUT_DIR']

PFX = 'MALIGN150_'
HEADS = ['Xp', 'Xm', 'Yp', 'Ym', 'Ref']
WINDING = {'Xp': 'Winding_1', 'Xm': 'Winding_2', 'Yp': 'Winding_3',
           'Ym': 'Winding_4', 'Ref': 'Winding_5'}
TERMINAL = {'Xp': 'CoilTerminal1', 'Xm': 'CoilTerminal2', 'Yp': 'CoilTerminal3',
            'Ym': 'CoilTerminal4', 'Ref': 'CoilTerminal5'}
SRC_CORE = 'U_Core'
SRC_COIL = 'Coil_Outer'
SRC_SHEET = 'Coil_Terminal_Sheet'
MATRIX = CFG['matrix_name']
SETUP = CFG['setup_name']


# ------------------------------------------------------------------ logging
DESKTOP = None  # set by build()/run_cases(); used to show progress in AEDT Message Manager


def ui(msg, level=0):
    # level 0=info 1=warning 2=error. Shows in AEDT Message Manager (Global).
    try:
        if DESKTOP is not None:
            DESKTOP.AddMessage('', '', level, 'MALIGN150 ' + str(msg)[:900])
    except Exception:
        pass


class Log(object):
    def __init__(self, path):
        self.path = path

    def __call__(self, event, detail=''):
        line = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + ' | ' + str(event)
        if detail != '':
            line += ' | ' + str(detail)
        try:
            f = open(self.path, 'a')
            f.write(line + '\n')
            f.close()
        except Exception:
            pass
        try:
            print('MALIGN150 | ' + line)
        except Exception:
            pass
        lvl = 2 if ('FAIL' in str(event) or 'ERROR' in str(event)) else 0
        ui(str(event) + (' | ' + str(detail)[:600] if detail != '' else ''), lvl)


def dump(path, value):
    f = open(path, 'w')
    json.dump(value, f, indent=2, sort_keys=True)
    f.close()


def sha256(path):
    h = hashlib.sha256()
    f = open(path, 'rb')
    while True:
        b = f.read(1024 * 1024)
        if not b:
            break
        h.update(b)
    f.close()
    return h.hexdigest()


def new_dir(root, label):
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    d = os.path.join(root, label + '_' + ts)
    k = 2
    while os.path.exists(d):
        d = os.path.join(root, label + '_' + ts + '_' + str(k))
        k += 1
    os.makedirs(d)
    return d


# ------------------------------------------------------------------ geometry helpers
def names(editor, group):
    try:
        return [str(x) for x in editor.GetObjectsInGroup(group)]
    except Exception:
        raise RuntimeError('GET_OBJECTS_FAILED ' + group + '\n' + traceback.format_exc())


def all_names(editor):
    return set(names(editor, 'Solids')) | set(names(editor, 'Sheets'))


def bbox(editor, n):
    b = [float(x) for x in editor.GetObjectBoundingBox(n)]
    if len(b) != 6:
        raise RuntimeError('BAD_BBOX ' + n + ' ' + repr(b))
    return b


def center(b):
    return [(b[i] + b[i + 3]) / 2.0 for i in range(3)]


def dims(b):
    return [b[i + 3] - b[i] for i in range(3)]


def maxdiff(a, b):
    return max([abs(a[i] - b[i]) for i in range(len(a))])


def move(editor, objs, dx, dy, dz):
    if not objs or abs(dx) + abs(dy) + abs(dz) < 1e-12:
        return
    editor.Move(['NAME:Selections', 'Selections:=', ','.join(objs),
                 'NewPartsModelFlag:=', 'Model'],
                ['NAME:TranslateParameters',
                 'TranslateVectorX:=', repr(float(dx)) + 'mm',
                 'TranslateVectorY:=', repr(float(dy)) + 'mm',
                 'TranslateVectorZ:=', repr(float(dz)) + 'mm'])


def rotate(editor, objs, axis, deg):
    if not objs or abs(deg) < 1e-12:
        return
    editor.Rotate(['NAME:Selections', 'Selections:=', ','.join(objs),
                   'NewPartsModelFlag:=', 'Model'],
                  ['NAME:RotateParameters', 'RotateAxis:=', axis,
                   'RotateAngle:=', repr(float(deg)) + 'deg'])


def rename(editor, old, new):
    editor.ChangeProperty(['NAME:AllTabs',
                           ['NAME:Geometry3DAttributeTab',
                            ['NAME:PropServers', old],
                            ['NAME:ChangedProps', ['NAME:Name', 'Value:=', new]]]])


def copy_translate(editor, src, dst, shift):
    before = all_names(editor)
    if dst in before:
        raise RuntimeError('NAME_ALREADY_PRESENT ' + dst)
    editor.Copy(['NAME:Selections', 'Selections:=', src])
    editor.Paste()
    fresh = list(all_names(editor) - before)
    if len(fresh) != 1:
        raise RuntimeError('PASTE_DID_NOT_CREATE_ONE_OBJECT ' + repr(fresh))
    rename(editor, fresh[0], dst)
    if dst not in all_names(editor):
        raise RuntimeError('RENAME_FAILED ' + fresh[0] + ' -> ' + dst)
    # Paste places the copy on top of the source; translate it to the target core.
    move(editor, [dst], shift[0], shift[1], shift[2])
    return dst


def duplicate_pair(editor, shift, coil_name, sheet_name, log):
    """Duplicate Coil_Outer + Coil_Terminal_Sheet by vector (same as GUI Duplicate > Along Line).
    Falls back to Copy/Paste+Move. Boundaries are NOT duplicated."""
    before_s = set(names(editor, 'Solids'))
    before_h = set(names(editor, 'Sheets'))
    try:
        editor.DuplicateAlongLine(
            ['NAME:Selections', 'Selections:=', SRC_COIL + ',' + SRC_SHEET,
             'NewPartsModelFlag:=', 'Model'],
            ['NAME:DuplicateToAlongLineParameters', 'CreateNewObjects:=', True,
             'XComponent:=', repr(float(shift[0])) + 'mm',
             'YComponent:=', repr(float(shift[1])) + 'mm',
             'ZComponent:=', repr(float(shift[2])) + 'mm',
             'NumClones:=', '2'],
            ['NAME:Options', 'DuplicateAssignments:=', False],
            ['CreateGroupsForNewObjects:=', False])
        new_s = list(set(names(editor, 'Solids')) - before_s)
        new_h = list(set(names(editor, 'Sheets')) - before_h)
        if len(new_s) != 1 or len(new_h) != 1:
            raise RuntimeError('DUPLICATE_ALONG_LINE_UNEXPECTED ' + repr((new_s, new_h)))
        rename(editor, new_s[0], coil_name)
        rename(editor, new_h[0], sheet_name)
        log('DUPLICATED_ALONG_LINE', coil_name + ' ' + sheet_name)
        return 'DuplicateAlongLine'
    except Exception:
        log('DUPLICATE_ALONG_LINE_FAILED_TRY_COPY_PASTE', traceback.format_exc()[-400:])
        # clean any partial result created by the failed call (only brand-new objects)
        junk = list((set(names(editor, 'Solids')) - before_s) | (set(names(editor, 'Sheets')) - before_h))
        if junk:
            editor.Delete(['NAME:Selections', 'Selections:=', ','.join(junk)])
        copy_translate(editor, SRC_COIL, coil_name, shift)
        copy_translate(editor, SRC_SHEET, sheet_name, shift)
        return 'CopyPaste'


def region_margins(editor):
    objs = [n for n in all_names(editor) if n != 'Region']
    reg = bbox(editor, 'Region')
    boxes = [bbox(editor, n) for n in objs]
    lo = [min([b[i] for b in boxes]) for i in range(3)]
    hi = [max([b[i + 3] for b in boxes]) for i in range(3)]
    m = [lo[i] - reg[i] for i in range(3)] + [reg[i + 3] - hi[i] for i in range(3)]
    return {'region_bbox_mm': reg, 'model_bbox_mm': lo + hi, 'margins_mm': m,
            'min_margin_mm': min(m)}


# ------------------------------------------------------------------ AEDT helpers
def open_design(oDesktop, path, log):
    log('OPEN_PROJECT_BEGIN', path)
    project = oDesktop.OpenProject(path)
    log('OPEN_PROJECT_RETURNED')
    if project is None:
        raise RuntimeError('OPEN_PROJECT_RETURNED_NONE ' + path)
    design = project.SetActiveDesign(CFG['design_name'])
    if design is None:
        raise RuntimeError('DESIGN_NOT_FOUND ' + CFG['design_name'])
    try:
        st = str(design.GetSolutionType())
        if 'AC' not in st and 'Eddy' not in st:
            raise RuntimeError('UNEXPECTED_SOLUTION_TYPE ' + st)
    except AttributeError:
        pass
    editor = design.SetActiveEditor('3D Modeler')
    if str(editor.GetModelUnits()).strip().lower() != 'mm':
        raise RuntimeError('MODEL_UNITS_NOT_MM')
    return project, design, editor


def validate(design, log):
    raw = str(design.ValidateDesign()).strip().lower()
    if raw not in ('true', '1'):
        raise RuntimeError('VALIDATE_DESIGN_FAILED ' + raw +
                           ' (see AEDT Message Manager for the first error)')
    log('VALIDATE_DESIGN_PASS')


def list_boundaries(module):
    # v1.5.5: windings/coil terminals are EXCITATIONS in Maxwell; GetBoundaries() can be
    # empty. Union of all listing methods; None (= cannot verify) if nothing is returned.
    found = []
    for meth in ('GetExcitations', 'GetBoundaries'):
        try:
            v = getattr(module, meth)()
            found += [str(x) for x in v]
        except Exception:
            pass
    for typ in ('Winding Group', 'Coil Terminal', 'Winding', 'Coil'):
        try:
            found += [str(x) for x in module.GetExcitationsOfType(typ)]
        except Exception:
            pass
    return sorted(set(found)) if found else None


def setup_args(params, solve_each_pass):
    return ['NAME:' + SETUP,
            'Enabled:=', True,
            ['NAME:MeshLink', 'ImportMesh:=', False],
            'MaximumPasses:=', int(params['MaximumPasses']),
            'MinimumPasses:=', int(params['MinimumPasses']),
            'MinimumConvergedPasses:=', int(params['MinimumConvergedPasses']),
            'PercentRefinement:=', int(params['PercentRefinement']),
            'SolveFieldOnly:=', False,
            'PercentError:=', float(params['PercentError']),
            'SolveMatrixAtLast:=', True,  # v1.5.6: AEDT "Solve matrix: After last pass" (false = only after converging)
            'UseNonLinearIterNum:=', False,
            'UseCacheFor:=', ['Pass'],
            'UseIterativeSolver:=', False,
            'RelativeResidual:=', 1e-05,
            'NonLinearResidual:=', 0.0001,
            'SmoothBHCurve:=', False,
            'Frequency:=', params['Frequency'],
            'HasSweepSetup:=', False,
            'UseHighOrderShapeFunc:=', False,
            'UseMuLink:=', False]


def edit_setup(design, params, log, label):
    mod = design.GetModule('AnalysisSetup')
    try:
        mod.EditSetup(SETUP, setup_args(params, bool(CFG['solve_matrix_each_pass'])))
        log('SETUP_EDITED', label + ' ' + json.dumps(params, sort_keys=True))
        return True
    except Exception:
        log('SETUP_EDIT_FAILED_KEEPING_EXISTING', label + ' ' + traceback.format_exc())
        return False


# ------------------------------------------------------------------ head mapping
def map_heads(editor):
    solids = names(editor, 'Solids')
    cores = sorted([n for n in solids if n == SRC_CORE or
                    (n.startswith(SRC_CORE) and n[len(SRC_CORE):].isdigit())])
    if len(cores) != 5:
        raise RuntimeError('EXPECTED_5_U_CORES ' + repr(cores))
    exp = CFG['head_xy_mm']
    tol = float(CFG['head_match_tol_mm'])
    out = {}
    for c in cores:
        cx, cy = center(bbox(editor, c))[:2]
        best = None
        bestd = 1e9
        for h in HEADS:
            d = math.hypot(cx - exp[h][0], cy - exp[h][1])
            if d < bestd:
                best, bestd = h, d
        if bestd > tol:
            raise RuntimeError('CORE_NOT_AT_EXPECTED_HEAD %s center=(%.3f,%.3f) nearest=%s d=%.3f'
                               % (c, cx, cy, best, bestd))
        if best in out:
            raise RuntimeError('TWO_CORES_AT_SAME_HEAD ' + best + ' ' + repr([out[best], c]))
        out[best] = c
    if out.get('Xp') != SRC_CORE:
        raise RuntimeError('ACTIVE_SOURCE_CORE_NOT_AT_XP ' + repr(out))
    return out


def object_groups(editor):
    solids = set(names(editor, 'Solids'))
    sheets = set(names(editor, 'Sheets'))
    steel = sorted([n for n in solids if n.startswith('TargetSteel_')])
    if 'Casting_Target' not in solids or not steel:
        raise RuntimeError('TARGET_CONTAINER_OBJECTS_MISSING')
    target = ['Casting_Target'] + steel
    sensor = sorted([n for n in (solids | sheets) if n not in target and n != 'Region'])
    return target, sensor


def head_gaps(editor, heads, casting_top):
    out = {}
    for h in HEADS:
        b = bbox(editor, heads[h])
        out[h] = {'core': heads[h], 'core_center_mm': center(b),
                  'pole_min_z_mm': b[2], 'gap_to_casting_top_mm': b[2] - casting_top}
    return out


# ------------------------------------------------------------------ BUILD
def build(oDesktop):
    global DESKTOP
    DESKTOP = oDesktop
    root = CFG['output_dir']
    ui('BUILD START v' + VERSION + ' output_dir=' + root)
    if not os.path.isdir(root):
        os.makedirs(root)
    run_dir = new_dir(root, 'BUILD')
    log = Log(os.path.join(run_dir, 'MALIGN150_build.log'))
    audit = {'version': VERSION, 'status': 'START', 'run_dir': run_dir, 'errors': [],
             'warnings': []}
    project = None
    try:
        src = os.path.join(HERE, CFG['source_aedt'])
        if not os.path.isfile(src):
            raise RuntimeError('SOURCE_AEDT_NOT_FOUND ' + src)
        h0 = sha256(src)
        audit['source_sha256'] = h0
        if h0 != CFG['source_aedt_sha256']:
            raise RuntimeError('SOURCE_SHA256_MISMATCH ' + h0)
        if os.path.isfile(os.path.join(root, 'BASE_BUILD_AUDIT.json')):
            raise RuntimeError('BASE_ALREADY_BUILT: a successful BASE is registered in '
                               'BASE_BUILD_AUDIT.json. Delete/rename that json to rebuild.')
        # v1.5.4: build directly at config "manual_base_aedt". An old unregistered file
        # there (e.g. from a failed attempt) is renamed aside, never deleted.
        base = str(CFG['manual_base_aedt'])
        bdir = os.path.dirname(base)
        if not os.path.isdir(bdir):
            os.makedirs(bdir)
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        pname = os.path.splitext(os.path.basename(base))[0]
        try:
            if pname in [str(x) for x in oDesktop.GetProjectList()]:
                oDesktop.CloseProject(pname)
                log('CLOSED_OLD_OPEN_PROJECT', pname)
        except Exception:
            log('COULD_NOT_CLOSE_OLD_PROJECT', pname)
        for old_path in (base, base + 'results', base + '.lock'):
            if os.path.exists(old_path):
                try:
                    os.rename(old_path, old_path + '.old_' + ts)
                    log('MOVED_OLD_ASIDE', old_path)
                except Exception:
                    raise RuntimeError('CLOSE THIS PROJECT IN AEDT FIRST, then re-run: ' + old_path)
        shutil.copy2(src, base)  # geometry + setup only; old fields are invalid after edits
        log('COPIED_SOURCE_TO_BASE', base)
        project, design, editor = open_design(oDesktop, base, log)

        objs = all_names(editor)
        for req in [SRC_CORE, SRC_COIL, SRC_SHEET, 'Region', 'Casting_Target']:
            if req not in objs:
                raise RuntimeError('MISSING_OBJECT ' + req)
        stale = [n for n in objs if n.startswith(PFX)]
        if stale:
            raise RuntimeError('SOURCE_ALREADY_HAS_V150_OBJECTS ' + repr(stale))
        heads = map_heads(editor)
        audit['head_to_core'] = heads
        log('HEADS_MAPPED', json.dumps(heads, sort_keys=True))

        bnd = design.GetModule('BoundarySetup')
        existing = list_boundaries(bnd)
        audit['boundaries_before'] = existing
        if existing is not None:
            for req in ['Winding_1', 'CoilTerminal1']:
                if req not in existing:
                    raise RuntimeError('ORIGINAL_EXCITATION_MISSING ' + req + ' ' + repr(existing))
            for h in HEADS[1:]:
                if WINDING[h] in existing or TERMINAL[h] in existing:
                    raise RuntimeError('EXCITATION_NAME_ALREADY_USED ' + WINDING[h])

        core0 = bbox(editor, SRC_CORE)
        coil0 = bbox(editor, SRC_COIL)
        sheet0 = bbox(editor, SRC_SHEET)
        if maxdiff(center(coil0)[:2], center(core0)[:2]) > 30.0:
            raise RuntimeError('SOURCE_COIL_NOT_ON_SOURCE_CORE')
        added = {}
        for h in HEADS[1:]:
            cb = bbox(editor, heads[h])
            if maxdiff(dims(cb), dims(core0)) > 0.05:
                raise RuntimeError('CORE_DIMENSIONS_OR_ORIENTATION_DIFFER ' + heads[h])
            shift = [center(cb)[i] - center(core0)[i] for i in range(3)]
            cn = PFX + 'Coil_' + h
            sn = PFX + 'Terminal_' + h
            method = duplicate_pair(editor, shift, cn, sn, log)
            for name, ref in [(cn, coil0), (sn, sheet0)]:
                want = [center(ref)[i] + shift[i] for i in range(3)]
                got = center(bbox(editor, name))
                if maxdiff(want, got) > 0.05:
                    raise RuntimeError('COPY_POSITION_WRONG %s want=%r got=%r' % (name, want, got))
                if maxdiff(dims(bbox(editor, name)), dims(ref)) > 0.05:
                    raise RuntimeError('COPY_SIZE_WRONG ' + name)
            added[h] = {'coil': cn, 'terminal': sn, 'core': heads[h], 'shift_mm': shift,
                        'method': method}
            log('ADDED_COIL_AND_TERMINAL', h + ' ' + cn + ' ' + sn)
        audit['added'] = added

        # Stranded coils: keep eddy effect OFF exactly like the original Coil_Outer.
        rec = ['NAME:EddyEffectVector']
        for h in HEADS[1:]:
            rec.append(['NAME:Data', 'Object Name:=', added[h]['coil'],
                        'Eddy Effect:=', False, 'Displacement Current:=', False])
        try:
            bnd.SetEddyEffect(['NAME:Eddy Effect Setting', rec])
            log('EDDY_OFF_FOR_NEW_COILS')
        except Exception:
            audit['warnings'].append('SET_EDDY_FAILED ' + traceback.format_exc())
            log('SET_EDDY_FAILED_CHECK_MANUALLY')

        for h in HEADS[1:]:
            bnd.AssignCoilTerminal(['NAME:' + TERMINAL[h],
                                    'Objects:=', [added[h]['terminal']],
                                    'Conductor number:=', str(CFG['conductor_number']),
                                    'Point out of terminal:=', False])
            bnd.AssignWindingGroup(['NAME:' + WINDING[h],
                                    'Type:=', 'Current', 'IsSolid:=', False,
                                    'Current:=', CFG['current'],
                                    'Resistance:=', '1ohm', 'Inductance:=', '0nH',
                                    'Voltage:=', '0mV', 'ParallelBranchesNum:=', '1',
                                    'Phase:=', '0deg'])
            bnd.AddWindingTerminals(WINDING[h], [TERMINAL[h]])
            log('WINDING_ASSIGNED', h + ' ' + WINDING[h] + ' <- ' + TERMINAL[h])
        after = list_boundaries(bnd)
        audit['boundaries_after'] = after
        if after is not None:
            missing = [x for h in HEADS for x in (WINDING[h], TERMINAL[h]) if x not in after]
            if missing:
                raise RuntimeError('EXCITATIONS_MISSING_AFTER_ASSIGN ' + repr(missing))

        par = design.GetModule('MaxwellParameterSetup')
        entries = [['NAME:MatrixEntry', 'Source:=', WINDING[h]] for h in HEADS]
        variants = [['NAME:' + MATRIX, ['NAME:MatrixEntry'] + entries],
                    ['NAME:' + MATRIX, ['NAME:MatrixEntry'] +
                     [e + ['NumberOfTurns:=', '1'] for e in entries]]]
        ok = False
        errs = []
        for v in variants:
            try:
                par.AssignMatrix(v)
                ok = True
                audit['matrix_args_used'] = repr(v)
                break
            except Exception:
                errs.append(traceback.format_exc())
        if not ok:
            raise RuntimeError('ASSIGN_MATRIX_FAILED\n' + '\n'.join(errs))
        log('MATRIX_ASSIGNED', MATRIX + ' = ' + repr([WINDING[h] for h in HEADS]))

        audit['setup_edit_ok'] = edit_setup(design, CFG['afast_setup'], log, 'AFAST')
        if not audit['setup_edit_ok']:
            audit['warnings'].append('Setup not edited: matrix is solved at last pass only; '
                                     'per-pass L report will be unavailable.')
        validate(design, log)
        project.Save()
        log('BASE_SAVED', base)
        audit['base_aedt'] = base
        audit['base_sha256_after_save'] = sha256(base)
        audit['region'] = region_margins(editor)
        target, sensor = object_groups(editor)
        audit['target_group'] = target
        audit['sensor_group'] = sensor
        audit['head_gaps'] = head_gaps(editor, heads, bbox(editor, 'Casting_Target')[5])
        audit['status'] = 'BUILD_PASS_NO_SOLVE'
        log('BUILD_PASS_NO_SOLVE')
    except Exception:
        audit['status'] = 'BUILD_FAILED'
        audit['errors'].append(traceback.format_exc())
        log('BUILD_FAILED', audit['errors'][-1])
    dump(os.path.join(run_dir, 'build_audit.json'), audit)
    if audit['status'] == 'BUILD_PASS_NO_SOLVE':
        dump(os.path.join(root, 'BASE_BUILD_AUDIT.json'), audit)
    log('END', audit['status'])
    return audit


# ------------------------------------------------------------------ CASES
def find_case(cid):
    for c in CFG['cases']:
        if c['id'] == cid:
            return c
    raise RuntimeError('UNKNOWN_CASE_ID ' + cid)


def apply_case(editor, case, heads, log):
    target, sensor = object_groups(editor)
    objs0 = all_names(editor)
    b_t0 = dict((n, bbox(editor, n)) for n in target)
    b_s0 = dict((n, bbox(editor, n)) for n in sensor)
    dx, dy, dz = float(case['dx']), float(case['dy']), float(case['dz'])
    pitch, roll = float(case['pitch_deg']), float(case['roll_deg'])
    top0 = b_t0['Casting_Target'][5]
    gap0 = bbox(editor, SRC_CORE)[2] - top0
    if abs(gap0 - float(CFG['reference_gap_mm'])) > 0.05:
        raise RuntimeError('BASE_GAP_NOT_20MM %.4f' % gap0)

    log('CASE_TRANSFORM_BEGIN', json.dumps(case, sort_keys=True))
    move(editor, target, dx, dy, 0.0)
    move(editor, sensor, 0.0, 0.0, dz)
    if abs(pitch) + abs(roll) > 0:
        z0 = bbox(editor, SRC_CORE)[2]  # pole face of the Xp core (after dz)
        move(editor, sensor, 0.0, 0.0, -z0)
        rotate(editor, sensor, 'Y', pitch)
        rotate(editor, sensor, 'X', roll)
        move(editor, sensor, 0.0, 0.0, z0)
    log('CASE_TRANSFORM_RETURNED')

    if all_names(editor) != objs0:
        raise RuntimeError('OBJECT_SET_CHANGED_DURING_TRANSFORM')
    for n in target:
        want = [b_t0[n][i] + [dx, dy, 0.0][i % 3] for i in range(6)]
        if maxdiff(want, bbox(editor, n)) > 0.001:
            raise RuntimeError('TARGET_NOT_MOVED_AS_REQUESTED ' + n)
    if abs(pitch) + abs(roll) < 1e-12:
        for n in sensor:
            want = [b_s0[n][i] + [0.0, 0.0, dz][i % 3] for i in range(6)]
            if maxdiff(want, bbox(editor, n)) > 0.001:
                raise RuntimeError('SENSOR_GROUP_NOT_MOVED_AS_REQUESTED ' + n)
    top = bbox(editor, 'Casting_Target')[5]
    gaps = head_gaps(editor, heads, top)
    for h in HEADS:
        if gaps[h]['gap_to_casting_top_mm'] <= 1.0:
            raise RuntimeError('HEAD_TOO_CLOSE_OR_INTERSECTING ' + h + ' ' + repr(gaps[h]))
    # Expected gap change of the Xp/Yp heads for rotations (small-angle check).
    if abs(pitch) > 0:
        d = gaps['Xp']['gap_to_casting_top_mm'] - gaps['Xm']['gap_to_casting_top_mm']
        expect = -2.0 * 55.0 * math.sin(math.radians(pitch))
        if abs(d - expect) > 0.3:
            raise RuntimeError('PITCH_SIGN_OR_PIVOT_UNEXPECTED Xp-Xm gap=%.3f expect=%.3f' % (d, expect))
    if abs(roll) > 0:
        d = gaps['Yp']['gap_to_casting_top_mm'] - gaps['Ym']['gap_to_casting_top_mm']
        expect = 2.0 * 55.0 * math.sin(math.radians(roll))
        if abs(d - expect) > 0.3:
            raise RuntimeError('ROLL_SIGN_OR_PIVOT_UNEXPECTED Yp-Ym gap=%.3f expect=%.3f' % (d, expect))
    reg = region_margins(editor)
    if reg['min_margin_mm'] < float(CFG['minimum_region_padding_mm']):
        raise RuntimeError('INSUFFICIENT_REGION_PADDING ' + repr(reg))
    return {'case': case, 'target_group': target, 'sensor_group_count': len(sensor),
            'casting_top_z_mm': top, 'head_gaps': gaps, 'region': reg,
            'error_definition': 'e = hole center - twistlock axis = (dx, dy) of container group',
            'warning': 'gap_to_casting_top is a bbox z-distance, not true surface clearance off-axis'}


def matrix_exprs(q):
    return [MATRIX + '.' + q + '(' + WINDING[a] + ',' + WINDING[b] + ')' for a in HEADS for b in HEADS]


def unique_exprs(q):
    out = []
    for i in range(5):
        for j in range(i, 5):
            out.append(MATRIX + '.' + q + '(' + WINDING[HEADS[i]] + ',' + WINDING[HEADS[j]] + ')')
    return out


def export_report(design, folder, fname, context, families, xcomp, exprs):
    try:
        return _export_report(design, folder, fname, context, families, xcomp, exprs)
    except Exception:
        return _export_report(design, folder, fname, context, families, xcomp, exprs)  # one retry


def _export_report(design, folder, fname, context, families, xcomp, exprs):
    rep = design.GetModule('ReportSetup')
    name = 'M150_' + fname.replace('.csv', '')
    path = os.path.join(folder, fname)
    rep.CreateReport(name, 'AC Magnetic', 'Data Table', context, [], families,
                     ['X Component:=', xcomp, 'Y Component:=', exprs])
    try:
        rep.ExportToFile(name, path)
    finally:
        try:
            rep.DeleteReports([name])
        except Exception:
            pass
    if not os.path.isfile(path):
        raise RuntimeError('EXPORT_MISSING ' + fname)
    f = open(path, 'r')
    rows = [r for r in csv.reader(f) if r]
    f.close()
    if len(rows) < 2 or len(rows[0]) < len(exprs) + 1:
        raise RuntimeError('EXPORT_COLUMNS_SHORT ' + fname + ' ' + repr(rows[:1]))
    return {'file': fname, 'rows': len(rows) - 1, 'columns': len(rows[0])}


def export_case(design, case_dir, res, log):
    for meth, fn in [('ExportConvergence', 'convergence.conv'), ('ExportProfile', 'profile.prof')]:
        try:
            getattr(design, meth)(SETUP, '', os.path.join(case_dir, fn), True)
            if os.path.isfile(os.path.join(case_dir, fn)):
                res['exported'].append(fn)
        except Exception:
            res['warnings'].append('EXPORT_' + meth + ' ' + traceback.format_exc())
    ctx = SETUP + ' : LastAdaptive'
    for q in ['L', 'R', 'Z']:
        try:
            info = export_report(design, case_dir, q + '_matrix.csv', ctx,
                                 ['Freq:=', ['All']], 'Freq', matrix_exprs(q))
            res['exported'].append(info['file'])
            log('EXPORTED', info['file'])
        except Exception:
            res['warnings'].append('EXPORT_' + q + ' ' + traceback.format_exc())
            log('EXPORT_FAILED', q)
    pctx = SETUP + ' : AdaptivePass'
    done = False
    # v1.5.6: matrix is computed after the last pass only -> no per-pass matrix data exists.
    for fam in ():
        try:
            info = export_report(design, case_dir, 'L_per_pass.csv', pctx, fam, 'Pass',
                                 unique_exprs('L'))
            res['exported'].append(info['file'])
            res['per_pass_rows'] = info['rows']
            log('EXPORTED', 'L_per_pass.csv rows=' + str(info['rows']))
            done = True
            break
        except Exception:
            res['warnings'].append('EXPORT_PER_PASS ' + traceback.format_exc())
    if not done:
        log('PER_PASS_MATRIX_NOT_AVAILABLE', 'matrix solved after last pass only (by design in v1.5.6)')
    if 'L_matrix.csv' not in res['exported']:
        raise RuntimeError('L_MATRIX_NOT_EXPORTED')
    f = open(os.path.join(case_dir, 'L_matrix.csv'), 'r')
    rows = [r for r in csv.reader(f) if r]
    f.close()
    empty = len([v for v in rows[1][1:] if not str(v).strip()])
    res['L_matrix_empty_cells'] = empty
    if empty:
        raise RuntimeError('L_MATRIX_EMPTY_CELLS %d/25 (matrix not computed)' % empty)
    log('L_MATRIX_VALUES_OK', '25/25')


def already_solved(root, cid):
    croot = os.path.join(root, 'cases')
    if not os.path.isdir(croot):
        return None
    for d in sorted(os.listdir(croot), reverse=True):
        if not d.startswith(cid + '_') or 'PREFLIGHT' in d:
            continue
        rp = os.path.join(croot, d, 'result.json')
        lp = os.path.join(croot, d, 'L_matrix.csv')
        try:
            r = json.load(open(rp, 'r'))
            if r.get('status') != 'SOLVED_PROVISIONAL' or r['case']['id'] != cid:
                continue
            f = open(lp, 'r')
            rows = [x for x in csv.reader(f) if x]
            f.close()
            vals = rows[1][1:]
            if len(vals) == 25 and all(str(v).strip() for v in vals):
                return os.path.join(croot, d)
        except Exception:
            continue
    return None


def run_cases(oDesktop, solve, case_ids=None):
    global DESKTOP
    DESKTOP = oDesktop
    root = CFG['output_dir']
    ids = list(case_ids) if case_ids is not None else list(CFG['cases_to_run'])
    for cid in ids:
        find_case(cid)  # fail fast on a typo before any solve
    ui('CASES START v' + VERSION + ' solve=' + str(solve) + ' n=' + str(len(ids)) + ' cases=' + repr(ids))
    ba = os.path.join(root, 'BASE_BUILD_AUDIT.json')
    if not os.path.isfile(ba):
        raise RuntimeError('BASE_NOT_BUILT: run 00_BUILD_MATRIX5_BASE.py first (needs BUILD_PASS)')
    build_audit = json.load(open(ba, 'r'))
    base = build_audit['base_aedt']
    if not os.path.isfile(base):
        raise RuntimeError('REGISTERED_BASE_MISSING ' + base)
    heads = build_audit['head_to_core']
    cases_root = os.path.join(root, 'cases')
    if not os.path.isdir(cases_root):
        os.makedirs(cases_root)
    results = []
    n_ok = 0
    t_batch = datetime.datetime.now()
    for k, cid in enumerate(ids):
        case = find_case(cid)
        t_case = datetime.datetime.now()
        if solve and CFG.get('resume_skip_solved', True):
            prev = already_solved(root, cid)
            if prev:
                ui('SKIP_ALREADY_SOLVED %s -> %s' % (cid, prev))
                results.append({'case': cid, 'status': 'SKIPPED_ALREADY_SOLVED', 'dir': prev})
                n_ok += 1
                continue
        case_dir = new_dir(cases_root, cid + ('' if solve else '_PREFLIGHT'))
        log = Log(os.path.join(case_dir, 'MALIGN150_case.log'))
        res = {'version': VERSION, 'case': case, 'solve_requested': bool(solve),
               'status': 'START', 'exported': [], 'warnings': [], 'errors': [],
               'base_sha256': sha256(base), 'case_dir': case_dir,
               'numeric_accuracy_proven': False}
        project = None
        try:
            if case.get('requires_blind_prediction') and solve:
                bp = os.path.join(root, 'BLIND_PREDICTION.json')
                if not os.path.isfile(bp):
                    raise RuntimeError('BLIND_PREDICTION_MISSING: run '
                                       'python malign150_post.py predict-blind first')
                res['blind_prediction_sha256'] = sha256(bp)
            dst = os.path.join(case_dir, 'M150_' + os.path.basename(case_dir) + '.aedt')
            shutil.copy2(base, dst)
            log('COPIED_BASE', dst)
            project, design, editor = open_design(oDesktop, dst, log)
            if map_heads(editor) != heads:
                raise RuntimeError('BASE_HEAD_MAPPING_CHANGED')
            res['geometry'] = apply_case(editor, case, heads, log)
            dump(os.path.join(case_dir, 'geometry_audit.json'), res['geometry'])
            params = CFG['strict_setup'] if case.get('setup') == 'strict' else CFG['afast_setup']
            if not edit_setup(design, params, log, str(case.get('setup', 'afast')).upper()):
                raise RuntimeError('SETUP_NOT_APPLIED (need SolveMatrixAtLast=true)')
            validate(design, log)
            project.Save()
            if not solve:
                res['status'] = 'PREFLIGHT_PASS_NO_SOLVE'
            else:
                log('SOLVE_BEGIN', cid)
                try:
                    design.Analyze(SETUP)
                    res['analyze_returned'] = True
                    log('SOLVE_RETURNED')
                except Exception:
                    res['analyze_returned'] = False
                    res['errors'].append('SOLVE_EXCEPTION ' + traceback.format_exc())
                    log('SOLVE_EXCEPTION_PARTIAL_EXPORT')
                try:
                    export_case(design, case_dir, res, log)
                except Exception:
                    res['analyze_returned'] = False
                    res['errors'].append('EXPORT_CHECK ' + traceback.format_exc())
                    log('EXPORT_CHECK_FAILED', res['errors'][-1][-300:])
                project.Save()
                res['status'] = ('SOLVED_PROVISIONAL' if res['analyze_returned']
                                 else 'SOLVE_FAILED_PARTIAL_EXPORT')
        except Exception:
            res['status'] = 'FAILED'
            res['errors'].append(traceback.format_exc())
            log('FAILED', res['errors'][-1])
        dump(os.path.join(case_dir, 'result.json'), res)
        log('END', res['status'])
        mins = (datetime.datetime.now() - t_case).seconds / 60.0
        res['case_minutes'] = mins
        dump(os.path.join(case_dir, 'result.json'), res)
        results.append({'case': cid, 'status': res['status'], 'dir': case_dir, 'minutes': round(mins, 1)})
        ok = res['status'] in ('SOLVED_PROVISIONAL', 'PREFLIGHT_PASS_NO_SOLVE')
        n_ok += 1 if ok else 0
        done = k + 1
        if done < len(ids):
            avg = (datetime.datetime.now() - t_batch).seconds / 60.0 / done
            eta = datetime.datetime.now() + datetime.timedelta(minutes=avg * (len(ids) - done))
            ui('PROGRESS %d/%d %s %s (%.1f min) ETA %s' % (done, len(ids), cid, res['status'], mins,
                                                           eta.strftime('%H:%M')), 0 if ok else 2)
        if project is not None and CFG.get('close_project_after_case'):
            try:
                oDesktop.CloseProject(project.GetName())
            except Exception:
                pass
        if not ok and n_ok == 0:
            ui('STOP: first case failed, nothing has worked yet in this batch -> fix before continuing', 2)
            break  # systemic problem: do not burn hours
        # otherwise continue with the next case (case-specific failure)
    dump(os.path.join(cases_root, 'last_batch_' + ('solve' if solve else 'preflight') + '.json'),
         results)
    return results


# ------------------------------------------------------------------ MANUAL BASE (v1.5.3)
def register_manual(oDesktop):
    """Register a BASE that the user built by hand in the AEDT GUI (README step A)."""
    global DESKTOP
    DESKTOP = oDesktop
    root = CFG['output_dir']
    if not os.path.isdir(root):
        os.makedirs(root)
    run_dir = new_dir(root, 'REGISTER')
    log = Log(os.path.join(run_dir, 'MALIGN150_register.log'))
    audit = {'version': VERSION, 'status': 'START', 'errors': [], 'warnings': []}
    try:
        path = str(CFG.get('manual_base_aedt', '')).strip()
        if not path or not os.path.isfile(path):
            raise RuntimeError('SET config "manual_base_aedt" to the full path of your saved 5-coil .aedt: ' + path)
        if os.path.isfile(os.path.join(root, 'BASE_BUILD_AUDIT.json')):
            raise RuntimeError('BASE_ALREADY_REGISTERED: rename output_dir/BASE_BUILD_AUDIT.json first')
        project, design, editor = open_design(oDesktop, path, log)
        heads = map_heads(editor)
        bnd = design.GetModule('BoundarySetup')
        b = list_boundaries(bnd)
        audit['boundaries'] = b
        if b is not None:
            miss = [WINDING[h] for h in HEADS if WINDING[h] not in b]
            if miss:
                raise RuntimeError('WINDINGS_MISSING ' + repr(miss) + ' found=' + repr(b))
        coils = [n for n in names(editor, 'Solids') if 'oil' in n]
        audit['coil_like_solids'] = coils
        if len(coils) < 5:
            raise RuntimeError('LESS_THAN_5_COIL_SOLIDS ' + repr(coils))
        validate(design, log)
        audit.update({'status': 'MANUAL_BASE_REGISTERED', 'base_aedt': path,
                      'base_sha256_after_save': sha256(path), 'head_to_core': heads,
                      'winding_map': WINDING, 'matrix_name_expected': MATRIX,
                      'note': 'Matrix5 existence is confirmed only when 02 exports L_matrix.csv'})
        dump(os.path.join(root, 'BASE_BUILD_AUDIT.json'), audit)
        log('MANUAL_BASE_REGISTERED', path)
    except Exception:
        audit['status'] = 'REGISTER_FAILED'
        audit['errors'].append(traceback.format_exc())
        log('REGISTER_FAILED', audit['errors'][-1])
    dump(os.path.join(run_dir, 'register_audit.json'), audit)
    return audit


# ------------------------------------------------------------------ RUN ALL (v1.5.9, unattended)
P5_NEG_X = ['P5_xm15', 'P5_xm30']  # v1.5.12: x is not mirror-symmetric (casting x -101.5..76.5)

MAIN16 = ['P1_center', 'P1_offaxis_30_30', 'P2_xp5', 'P2_xm5', 'P2_yp5', 'P2_ym5',
          'P3_diag_p3_p3', 'P3_diag_p3_m3', 'P4_pitch_p1', 'P4_roll_p1', 'P4_gap18', 'P4_gap22',
          'P5_xp15', 'P5_xp30', 'P5_yp15', 'P5_yp30']


def _post_module():
    pp = os.path.join(os.path.dirname(HERE), 'Python_Post')
    if pp not in sys.path:
        sys.path.insert(0, pp)
    import malign150_post as post
    return post


def _step(summary, name, fn):
    t0 = datetime.datetime.now()
    ui('=== STEP ' + name + ' BEGIN')
    try:
        r = fn()
        summary['steps'].append({'step': name, 'ok': True,
                                 'minutes': round((datetime.datetime.now() - t0).seconds / 60.0, 1)})
        ui('=== STEP ' + name + ' OK')
        return r
    except SystemExit as e:
        summary['steps'].append({'step': name, 'ok': False, 'error': 'SystemExit ' + str(e)})
        ui('=== STEP ' + name + ' STOPPED (continuing): ' + str(e), 1)
    except Exception:
        summary['steps'].append({'step': name, 'ok': False, 'error': traceback.format_exc()[-1500:]})
        ui('=== STEP ' + name + ' FAILED (continuing): ' + traceback.format_exc()[-500:], 2)
    return None


def check_output_dir():
    # v1.5.12 resumes inside the v1.5.11 folder (19 solved cases are reused, not re-solved).
    tag = CFG.get('output_dir_tag') or ('v' + VERSION.replace('.', '_'))
    if tag not in CFG['output_dir'].replace('\\', '/').split('/')[-1]:
        raise RuntimeError('OUTPUT_DIR_MUST_BE_VERSION_FOLDER (' + tag + '): ' + CFG['output_dir'])


def run_all(oDesktop):
    """One script for the whole day. Every step is isolated: a failing step never stops
    the following solves. Re-running it resumes (solved cases are skipped)."""
    global DESKTOP
    DESKTOP = oDesktop
    check_output_dir()
    root = CFG['output_dir']
    if not os.path.isdir(root):
        os.makedirs(root)
    summary = {'version': VERSION, 'started': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
               'steps': []}
    ui('RUN_ALL START v' + VERSION + ' output_dir=' + root)
    try:
        import System  # IronPython/.NET only
        free_gb = System.IO.DriveInfo(root[:2]).AvailableFreeSpace / 1024.0 ** 3
        summary['disk_free_gb_at_start'] = round(free_gb, 1)
        ui('DISK FREE %.1f GB on %s (need ~10 GB for 21 cases)' % (free_gb, root[:2]), 0 if free_gb > 10 else 1)
    except Exception:
        pass

    if not os.path.isfile(os.path.join(root, 'BASE_BUILD_AUDIT.json')):
        b = _step(summary, '0_BUILD_BASE', lambda: build(oDesktop))
        if not b or b.get('status') != 'BUILD_PASS_NO_SOLVE':
            ui('RUN_ALL STOP: base model could not be built', 2)
            dump(os.path.join(root, 'RUN_ALL_SUMMARY.json'), summary)
            return summary

    def solve(ids):
        r = run_cases(oDesktop, True, ids)
        bad = [x for x in r if x['status'] not in ('SOLVED_PROVISIONAL', 'SKIPPED_ALREADY_SOLVED')]
        summary.setdefault('cases', []).extend(r)
        if bad and len(bad) == len(r):
            raise RuntimeError('ALL CASES FAILED ' + repr(bad))
        return r

    main = _step(summary, '1_SOLVE_MAIN16', lambda: solve(MAIN16))
    if main is None:
        ui('RUN_ALL STOP: main batch did not run (see messages above)', 2)
        dump(os.path.join(root, 'RUN_ALL_SUMMARY.json'), summary)
        return summary
    _step(summary, '2_POST_COLLECT', lambda: _post_module().collect(root))
    bp = os.path.join(root, 'BLIND_PREDICTION.json')
    if not os.path.isfile(bp):
        _step(summary, '3_PREDICT_BLIND', lambda: _post_module().predict_blind(root))
    if os.path.isfile(bp):
        _step(summary, '4_SOLVE_BLIND', lambda: solve(['P3_blind_m2_p4']))
        _step(summary, '5_CHECK_BLIND', lambda: _post_module().check_blind(root))
    else:
        ui('BLIND SKIPPED: no prediction file (send cases folder for manual prediction)', 1)
    _step(summary, '5B_SOLVE_P5_NEG_X', lambda: solve(P5_NEG_X))
    _step(summary, '6_SOLVE_P6_GAP40', lambda: solve(['P6_gap40']))
    _step(summary, '7_SOLVE_P6_STRICT', lambda: solve(['P6_center_strict']))
    _step(summary, '8_POST_COLLECT_FINAL', lambda: _post_module().collect(root))
    summary['upload_packs'] = _step(summary, '9_MAKE_UPLOAD_ZIPS', lambda: make_upload_packs(root))
    summary['finished'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    dump(os.path.join(root, 'RUN_ALL_SUMMARY.json'), summary)
    ok = [x['case'] for x in summary.get('cases', []) if x['status'] in ('SOLVED_PROVISIONAL', 'SKIPPED_ALREADY_SOLVED')]
    bad = sorted(set(x['case'] for x in summary.get('cases', [])
                     if x['status'] not in ('SOLVED_PROVISIONAL', 'SKIPPED_ALREADY_SOLVED')) - set(ok))
    ui('RUN_ALL DONE  solved=%d  failed=%s  summary=%s' % (len(set(ok)), repr(bad),
       os.path.join(root, 'RUN_ALL_SUMMARY.json')))
    try:
        f = open(os.path.join(root, 'ALL_DONE.txt'), 'w')
        f.write('finished ' + summary['finished'] + '\n')
        f.write('solved ' + repr(sorted(set(ok))) + '\n')
        f.write('failed ' + repr(bad) + '\n')
        f.write('upload zips folder: ' + os.path.join(root, 'UPLOAD') + '\n')
        f.write('upload zips: ' + repr(summary.get('upload_packs')) + '\n')
        f.close()
    except Exception:
        pass
    try:
        make_upload_packs(root)  # rebuild so the final summary and ALL_DONE.txt are inside zip 1
    except Exception:
        ui('UPLOAD_ZIP_REBUILD_FAILED (zips from step 9 are still valid)', 1)
    return summary


# ------------------------------------------------------------------ UPLOAD PACKS (v1.5.11)
UPLOAD_FILES = ['result.json', 'geometry_audit.json', 'L_matrix.csv', 'R_matrix.csv', 'Z_matrix.csv',
                'convergence.conv', 'profile.prof', 'MALIGN150_case.log']
ROOT_FILES = ['RUN_ALL_SUMMARY.json', 'MATRIX5_CHECKS.json', 'matrix5_long.csv', 'H_matrix5_P2.json',
              'BLIND_PREDICTION.json', 'BLIND_CHECK.json', 'BASE_BUILD_AUDIT.json', 'ALL_DONE.txt']
UPLOAD_GROUPS = [('1_P1_P2', ('P1_', 'P2_')), ('2_P3_P4', ('P3_', 'P4_')), ('3_P5_P6', ('P5_', 'P6_'))]


def make_upload_packs(root=None, max_mb=None):
    """Small evidence only (no .aedt / .aedtresults). One zip per stage group, each <= max_mb.
    Output: <output_dir>/UPLOAD/MALIGN_UPLOAD_<group>[_partN].zip"""
    import zipfile
    root = root or CFG['output_dir']
    max_bytes = int(float(max_mb or CFG.get('upload_zip_max_mb', 20)) * 1024 * 1024)
    try:
        import zlib  # noqa
        mode = zipfile.ZIP_DEFLATED
    except Exception:
        mode = zipfile.ZIP_STORED
    up = os.path.join(root, 'UPLOAD')
    if not os.path.isdir(up):
        os.makedirs(up)
    for old in os.listdir(up):  # remove stale parts from a previous packing
        if old.startswith('MALIGN_UPLOAD_') and old.endswith('.zip'):
            try:
                os.remove(os.path.join(up, old))
            except Exception:
                pass
    croot = os.path.join(root, 'cases')
    dirs = sorted([d for d in os.listdir(croot) if os.path.isdir(os.path.join(croot, d))
                   and 'PREFLIGHT' not in d]) if os.path.isdir(croot) else []
    made = []
    for gname, prefixes in UPLOAD_GROUPS:
        items = []
        if gname.startswith('1_'):
            for fn in ROOT_FILES:
                fp = os.path.join(root, fn)
                if os.path.isfile(fp):
                    items.append((fp, 'root/' + fn))
        for d in dirs:
            if not d.startswith(prefixes):
                continue
            for fn in UPLOAD_FILES:
                fp = os.path.join(croot, d, fn)
                if os.path.isfile(fp):
                    items.append((fp, 'cases/' + d + '/' + fn))
        if not items:
            continue
        part, size, zf, zpath = 1, 0, None, None
        for fp, arc in items:
            fsz = os.path.getsize(fp)
            if zf is None or (size + fsz > max_bytes and size > 0):
                if zf is not None:
                    zf.close()
                    made.append(zpath)
                    part += 1
                zpath = os.path.join(up, 'MALIGN_UPLOAD_' + gname + ('' if part == 1 else '_part' + str(part)) + '.zip')
                if os.path.exists(zpath):
                    os.remove(zpath)
                zf = zipfile.ZipFile(zpath, 'w', mode)
                size = 0
            zf.write(fp, arc)
            size += fsz
        if zf is not None:
            zf.close()
            made.append(zpath)
    info = [(os.path.basename(z), round(os.path.getsize(z) / 1024.0 / 1024.0, 2)) for z in made]
    ui('UPLOAD_PACKS ' + repr(info) + ' -> ' + up)
    return info
