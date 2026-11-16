# -*- coding: utf-8 -*-
"""Live virtual site = rule-based main server + virtual reach stacker, talking to a real kit link.

Used two ways (same code):
  * inside the desktop host when no server_url is set (built-in main server), and
  * inside the example main server (server_example/malign_main_server.py, AUTO mode).
The kit is a separate component: SimSite sends it commands through `send(cmd, delay_s)`
and receives its bundles/events through `on_message(msg)`. In simulation the kit's coil
readers read the same virtual site (`kit_readings()`), so the screen shows exactly what
the main server decided and the machine did.

Job sequence (the team's 9 steps):
  approach (rough position from afar) -> in sensing range -> start_job -> bundle ->
  decide (priority, combine corners) -> machine moves -> sense ... -> lower -> lock -> raise -> end_job
"""
import math
import secrets
import threading
import time
from datetime import datetime, timezone

import malign_engine as E
import sim_job
from main_controller import ControllerBrain
from reachstacker_sim import VirtualReachStacker, MACHINE_MODES, FAULTS

STRATEGIES = ('weighted', 'equal', 'diagonal', 'v354')
CONTROLLERS = ('rule', 'autopilot', 'ai')   # rule: message-driven brain; autopilot / ai: tools + safety guard
HARDWARE = tuple(E.PRESETS)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


class SimSite:
    def __init__(self, M, send, kit_id, approach_s=1.0, move_s=0.8, max_rounds=6,
                 ai_url='http://127.0.0.1:11434', ai_model='qwen2.5:7b'):
        self.M, self.send, self.kit_id = M, send, kit_id
        self.approach_s, self.move_s, self.max_rounds = approach_s, move_s, max_rounds
        self.ai_url, self.ai_model = ai_url, ai_model
        self.lock = threading.RLock()
        self.job = None
        self.bundle_event = threading.Event()
        self.bundle = None

    # ---- control from the screen / API
    def start(self, hardware='target', machine='normal', fault='none', strategy='weighted', seed=None,
              machine_id='SIM-REACHSTACKER-1', controller='rule', ai_model=None, plan_hook=None, container_tol=None):
        if hardware not in HARDWARE or machine not in MACHINE_MODES or fault not in FAULTS or strategy not in STRATEGIES:
            raise ValueError('hardware/machine/fault/strategy 값 오류')
        if controller not in CONTROLLERS:
            raise ValueError('controller: ' + ', '.join(CONTROLLERS))
        with self.lock:
            if self.job and self.job['phase'] not in ('done', 'held', 'failed', 'aborted'):
                raise RuntimeError('진행 중인 가상 작업이 있습니다')
            seed = int(seed) & 0xFFFFFFFF if seed is not None else secrets.randbits(32) or 1
            P = E.settings(hardware, 'history')
            rs = VirtualReachStacker(self.M, P, E.Rng(seed), E.Rng(seed ^ 0x5A5A5A5A), machine, fault,
                                     container_tol=container_tol)
            job_id = 'JOB-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(2)
            self.job = {'job_id': job_id, 'machine_id': machine_id, 'phase': 'approach', 'seed': seed,
                        'settings': {'hardware': hardware, 'machine': machine, 'fault': fault, 'strategy': strategy,
                                     'container_tol': container_tol,
                                     'controller': controller, 'ai_model': ai_model or self.ai_model},
                        'events': [], 'stage': 'approach', 'locked': None,
                        'rs': rs, 'P': P,
                        'brain': ControllerBrain(self.M, strategy, True, True, self.max_rounds, K=P['K']),
                        'frames': [{'t': time.time(), 'errors': rs.corner_errors(), 'label': '접근 시작'}],
                        'log': [], 'outcome': None, 'last_decision': None, 'machine_last': None,
                        'resense_pending': False, 'started_at': _now(), 'worst0': rs.worst_mm(),
                        'plan_hook': plan_hook, 'plan': None,
                        'bundle_event': threading.Event(), 'bundle': None}
            if controller == 'rule':
                self._log('기계: 컨테이너 대략 위치(원거리 정보)로 접근 시작')
                self.event('approach_start')
                t = threading.Timer(self.approach_s, self._in_range, args=(job_id,))
            else:
                import ai_driver
                self.log('서버', '제어 주체: %s' % ('Ollama AI (%s)' % (ai_model or self.ai_model) if controller == 'ai'
                                                 else '규칙 자동조종 (같은 도구·안전장치)'))
                if controller == 'ai':
                    t = threading.Thread(target=ai_driver.run_ai, args=(self, self.job, self.ai_url, ai_model or self.ai_model))
                else:
                    t = threading.Thread(target=ai_driver.run_autopilot, args=(self, self.job))
            t.daemon = True
            t.start()
            return self.state()

    def _in_range(self, job_id):
        with self.lock:
            j = self.job
            if not j or j['job_id'] != job_id or j['phase'] != 'approach':
                return
            j['rs'].approach()
            j['phase'] = 'sensing'
            self.event('in_range')
            self._frame('센싱 범위 진입 (±30 mm)')
            self._log('기계: 자체 센서 기준 키트 센싱 범위(±30 mm) 진입 → 준비 완료 보고')
            self._log('메인 서버 → 키트: 작업 시작(start_job)')
            self.send({'type': 'start_job', 'job_id': job_id, 'machine_id': j['machine_id']}, 0)

    # ---- the kit's coil readers read the virtual site
    def kit_readings(self):
        with self.lock:
            j = self.job
            if not j or j['phase'] not in ('sensing', 'verify'):
                raise RuntimeError('센싱할 가상 작업이 없습니다')
            return j['rs'].kit_readings()

    # ---- messages from the kit
    def on_message(self, msg):
        with self.lock:
            j = self.job
            if not j or msg.get('session_id') != j['job_id']:
                return
            if msg.get('kind') == 'round' and j['settings']['controller'] != 'rule':
                j['bundle'] = msg            # per job: a stale tool thread of an older job can never take it
                j['bundle_event'].set()
            elif msg.get('kind') == 'round':
                self._on_bundle(j, msg)
            elif msg.get('event') in ('job_aborted', 'fault') and j['phase'] not in ('done', 'held', 'failed'):
                j['phase'] = 'aborted'
                j['outcome'] = 'aborted'
                self._log('키트 보고: %s → 작업 중단' % msg.get('event'))

    def _on_bundle(self, j, msg):
        brain, rs = j['brain'], j['rs']
        machine = rs.machine_sensor()
        j['machine_last'] = machine
        d = brain.decide(msg['units'], machine, resense=bool(msg.get('resense')))
        j['last_decision'] = d
        sid = j['job_id']
        tag = '%d차%s' % (d['round'], ' 재측정' if msg.get('resense') else (' 확인' if d.get('verify') else ''))
        self._log('키트 → 서버: %s 묶음 (측정 #%s, 신뢰도 %s)' % (tag, msg.get('measurement_id'), '/'.join(d['grade'])))
        self._log('서버 판단: %s · 기계 센서 %s · 키트 %s%s' % (
            d.get('case_ko', ''), d['machine_status'], d['kit_status'],
            ' · 코너%d 제외' % d['dropped_corner'] if d.get('dropped_corner') else ''))
        act = d['action']
        if act == 'resense':
            self._log('서버 → 키트: 재측정(이동 없음) — %s' % d.get('reason'))
            self.send({'type': 'resense', 'session_id': sid}, 0.2)
            return
        if act == 'stop':
            j['phase'] = 'held'
            j['outcome'] = 'held'
            self.event('stop', reason=d.get('reason'))
            self._log('서버: 신뢰할 수 있는 정렬 정보 없음(%s) → 하강 금지, 안전 정지' % d.get('reason'))
            self.send({'type': 'stop', 'reason': d.get('reason', 'held')}, 0)
            return
        c = d['correction']
        self.event('move', x_mm=c['x_mm'], y_mm=c['y_mm'], theta_deg=c['theta_deg'])
        rs.move(c['x_mm'], c['y_mm'], c['theta_rad'])
        brain.apply_history_offsets(c)
        self._frame('%s 보정 이동' % tag, move=c)
        self._log('서버 → 리치스태커: 보정 이동 X %.2f / Y %.2f mm / θ %.4f° (기계 %.0f%%·키트 %.0f%%)' % (
            c['x_mm'], c['y_mm'], c['theta_deg'], 100 * d.get('w_machine', 0), 100 * d.get('w_kit', 1)))
        if act == 'lower_after_move':
            self.event('lower')
            rs.lower()
            locked = rs.lock()['locked']
            j['locked'] = locked
            self.event('lock', locked=locked)
            self._frame('하강·결합')
            rs.raise_()
            self.event('raise', with_container=locked)
            j['outcome'] = 'success' if locked else 'false_lower'
            j['phase'] = 'done' if locked else 'failed'
            self._log('서버 → 리치스태커: 하강 → 결합 %s → 상승' % ('성공' if locked else '실패(허용 오차 초과)'))
            self.send({'type': 'end_job', 'session_id': sid, 'result': 'main_action'}, self.move_s)
            return
        self.event('sense')
        j['phase'] = 'verify' if act == 'move_then_verify' else 'sensing'
        self._log('서버: 이동 완료 확인 → 키트에 %s' % ('최종 확인 센싱' if act == 'move_then_verify' else '다음 센싱'))
        self.send({'type': 'sense', 'session_id': sid, 'round_no': msg['round_no'] + 1}, self.move_s)

    # ---- helpers / read
    def _log(self, text):
        who = '서버'
        for prefix, w in (('키트 → 서버', '키트'), ('기계', '기계'), ('서버 → 리치스태커', '서버'), ('키트 보고', '키트')):
            if text.startswith(prefix):
                who = w
        self.log(who, text)

    def log(self, who, text):
        if self.job is not None:
            self.job['log'].append({'t': time.time(), 'at': _now(), 'who': who, 'text': text})

    def event(self, kind, **data):
        if self.job is not None:
            self.job['events'].append(dict(data, t=time.time(), kind=kind))
            self.job['stage'] = kind

    def frame(self, label):
        with self.lock:
            self._frame(label)

    # ---- used by ai_driver.ToolBox (autopilot / AI)
    def request_bundle(self, cmd, timeout=15.0, job=None):
        j = job or self.job
        if j is not self.job or j['outcome'] is not None:
            return None                  # job finished / replaced: send nothing
        j['bundle_event'].clear()
        j['bundle'] = None
        tag = {'start_job': '작업 시작(start_job)', 'sense': '센싱(sense)', 'resense': '재측정(resense)'}[cmd['type']]
        self.log('서버', '서버 → 키트: ' + tag)
        self.send(cmd, 0)
        if not j['bundle_event'].wait(timeout) or self.job is not j:
            return None
        msg = j['bundle']
        self.log('키트', '키트 → 서버: 측정 #%s (신뢰도 %s)' % (msg.get('measurement_id'),
                                                       '/'.join(u['quality']['grade'] for u in msg['units'])))
        return msg

    def advise(self, brain, msg, machine, resense):
        with self.lock:
            d = brain.decide(msg['units'], machine, resense=resense)
            self.job['last_decision'] = d
            self.job['machine_last'] = machine
            self.job['brain'] = brain
            return d

    def lower_and_lock(self):
        j = self.job
        rs = j['rs']
        self.event('lower')
        time.sleep(self.move_s)
        rs.lower()
        locked = rs.lock()['locked']
        j['locked'] = locked
        self.event('lock', locked=locked)
        with self.lock:
            self._frame('하강·결합')
        time.sleep(self.move_s)
        rs.raise_()
        self.event('raise', with_container=locked)
        j['outcome'] = 'success' if locked else 'false_lower'
        j['phase'] = 'done' if locked else 'failed'
        self.log('서버', '하강 → 결합 %s → 상승' % ('성공' if locked else '실패(허용 오차 초과)'))
        self.send({'type': 'end_job', 'session_id': j['job_id'], 'result': 'main_action'}, 0)
        return {'locked': locked, 'worst_corner_mm': round(rs.worst_mm(), 2)}

    def stop_job(self, reason):
        j = self.job
        if j['outcome'] is None:
            j['outcome'] = 'held'
            j['phase'] = 'held'
            self.event('stop', reason=reason)
            self.log('서버', '안전 정지: %s (하강하지 않음)' % reason)
            if j['events'] and any(e['kind'] in ('sense', 'in_range') for e in j['events']):
                self.send({'type': 'stop', 'reason': reason}, 0)
        return {'stopped': True}

    def abort(self, reason):
        j = self.job
        if j and j['outcome'] is None:
            j['outcome'] = 'aborted'
            j['phase'] = 'aborted'
            self.event('abort', reason=reason)
            self.send({'type': 'stop', 'reason': reason}, 0)

    def _frame(self, label, move=None):
        self.job['frames'].append({'t': time.time(), 'errors': self.job['rs'].corner_errors(), 'label': label,
                                   'move': move})

    def state(self):
        with self.lock:
            j = self.job
            if not j:
                return {'active': False}
            d = j['last_decision'] or {}
            return {'active': j['phase'] not in ('done', 'held', 'failed', 'aborted'),
                    'job_id': j['job_id'], 'phase': j['phase'], 'outcome': j['outcome'], 'seed': j['seed'],
                    'settings': j['settings'], 'worst0_mm': j['worst0'],
                    'truth_worst_mm': j['rs'].worst_mm(), 'truth_corner_errors_mm': j['rs'].corner_errors(),
                    'frames': [{'t': f['t'], 'errors': f['errors'], 'label': f['label']} for f in j['frames']],
                    'log': j['log'][-60:],
                    'rounds': j['brain'].rounds,
                    'machine_last': j['machine_last'],
                    'decision': {k: d.get(k) for k in ('round', 'case', 'case_ko', 'kit_status', 'machine_status',
                                                       'grade', 'weights', 'dropped_corner', 'w_machine', 'w_kit',
                                                       'correction', 'predicted_worst_mm', 'action', 'reason',
                                                       'corner_est_mm')} if d else None,
                    'decisions': len(j['brain'].decisions),
                    'plan': None if not j.get('plan') else {k: j['plan'].get(k) for k in ('plan', 'ko', 'K', 'max_rounds', 'use_machine', 'similar', 'expected', 'why')},
                    'stage': j['stage'], 'locked': j['locked'],
                    'pose': {'tx_mm': j['rs'].world.tx, 'ty_mm': j['rs'].world.ty,
                             'th_deg': math.degrees(j['rs'].world.th)},
                    'tol_mm': j['rs'].world.tol, 'events': j['events'][-80:],
                    'approach_s': self.approach_s, 'move_s': self.move_s, 'now': time.time()}


class SimSensingSource(threading.Thread):
    """Simulated coil readers of the kit: answer the kit's pending requests from the virtual site."""

    def __init__(self, link, readings, period=0.05):
        super().__init__(daemon=True, name='MALIGN-sim-sensing')
        self.link, self.readings, self.period = link, readings, period
        self.stop_event = threading.Event()
        self.last_error = None

    def run(self):
        diag = {'coil_temp_c': 25.0, 'supply_v': 24.0, 'fw_cal_version': 'SIM-FEM21-v1', 'error_code': 0}
        while not self.stop_event.wait(self.period):
            try:
                s = self.link.status()
                p = s['pending']
                if not p:
                    continue
                if p['type'] == 'self_test':
                    self.link.record_self_test([{'ok': True, 'error_code': 0, 'detail': 'sim'}] * 4)
                elif p['type'] == 'sense':
                    units = [dict(r, diag=diag) for r in self.readings()]
                    self.link.record_round(s['session_id'], p['round_no'], units)
                self.last_error = None
            except Exception as exc:   # no virtual job yet / transient
                self.last_error = '%s: %s' % (type(exc).__name__, exc)

    def stop(self):
        self.stop_event.set()


def run_offline(M, **kw):
    """Convenience: one synchronous job (same logic) for quick checks."""
    P = E.settings(kw.get('hardware', 'target'), 'history')
    seed = kw.get('seed', 1)
    return sim_job.run_job(M, P, E.Rng(seed), E.Rng(seed ^ 0x5A5A5A5A), kw.get('strategy', 'weighted'),
                           kw.get('machine', 'normal'), kw.get('fault', 'none'))
