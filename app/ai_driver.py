# -*- coding: utf-8 -*-
"""AI main server: a local LLM (Ollama, free) runs the whole job through tools,
with a rule-based SAFETY GUARD between the AI and the machine.

Tools the AI can call (all go through the guard):
  approach_container()        drive to the container's rough position until inside the kit's range
  kit_sense(remeasure=False)  command the kit to sense once; returns 4 corners + reliability
  read_machine_sensor()       the reach stacker's own misalignment estimate
  suggest_correction()        the rule-based advisor's recommendation (priority case, move)
  move_spreader(x_mm, y_mm, theta_deg)   correction move; reduces the misalignment by that amount
  lower_and_lock()            lower the spreader, lock the twistlocks, lift
  stop_job(reason)            give up safely without lowering
Guard (refuses and tells the AI why):
  - no move before a fresh kit sensing (one move per sensing: "confirm the move, then sense again")
  - move limits |x|,|y| <= 40 mm, |theta| <= 0.5 deg
  - lower only with fresh kit data, kit not invalid, worst usable corner (measured, or predicted
    after the one move since sensing) <= 4 mm, and the machine sensor (if valid) not contradicting
If Ollama is not reachable, the job ends 'aborted' with that message (use the rule autopilot instead).
"""
import json
import math
import threading
import time
import urllib.error
import urllib.request

import malign_engine as E
from main_controller import ControllerBrain, LOWER_OK_MM, DISAGREE_MM

MOVE_LIMIT_MM = 40.0
MOVE_LIMIT_DEG = 0.5
MAX_TOOL_CALLS = 60
MAX_NUDGES = 3

SYSTEM = """You are the MAIN SERVER AI of M-ALIGN: you control a reach stacker that must lock its spreader's
4 twistlocks into a 20 ft container's corner castings, using a 4-unit magnetic alignment kit.
Units: millimetres and degrees. "Misalignment" = corner-casting hole position minus twistlock position.
move_spreader(x_mm, y_mm, theta_deg) REDUCES the misalignment by exactly that amount
(theta is a rotation about the spreader centre). The machine moves with a small actuator error.

Procedure (follow it):
1. approach_container()
2. kit_sense()  -> read the 4 corners and their reliability (good / caution / invalid)
3. suggest_correction() -> the rule advisor compares the kit with the machine's own sensor and
   proposes a move; using its numbers is usually right
4. move_spreader(...) once, then kit_sense() again to confirm (one move per sensing)
5. repeat 3-4, about 6 rounds at most, until every usable corner is within ~1 mm
6. lower_and_lock() to finish. If no reliable data, kit_sense(remeasure=true); if still hopeless, stop_job.
A safety guard may REFUSE a tool call and tell you why: read the reason and adapt; never insist.
Before each tool call write ONE short sentence in Korean saying what you do and why."""

TOOLS = [
    {'type': 'function', 'function': {'name': 'approach_container',
        'description': "Drive to the container's rough position (given from afar) until inside the kit's sensing range (+-30 mm).",
        'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
    {'type': 'function', 'function': {'name': 'kit_sense',
        'description': 'Command the alignment kit to sense once. remeasure=true measures again without any move.',
        'parameters': {'type': 'object', 'properties': {'remeasure': {'type': 'boolean'}}, 'required': []}}},
    {'type': 'function', 'function': {'name': 'read_machine_sensor',
        'description': "The reach stacker's own estimate of the misalignment (x_mm, y_mm, theta_deg) and its status.",
        'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
    {'type': 'function', 'function': {'name': 'suggest_correction',
        'description': "Rule advisor: combines the kit's corners and the machine sensor by reliability; proposes a move and the next action.",
        'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
    {'type': 'function', 'function': {'name': 'move_spreader',
        'description': 'Move the spreader to reduce the misalignment by (x_mm, y_mm, theta_deg).',
        'parameters': {'type': 'object', 'properties': {'x_mm': {'type': 'number'}, 'y_mm': {'type': 'number'},
                                                        'theta_deg': {'type': 'number'}},
                       'required': ['x_mm', 'y_mm', 'theta_deg']}}},
    {'type': 'function', 'function': {'name': 'lower_and_lock',
        'description': 'Lower the spreader onto the container, lock the 4 twistlocks and lift. Guarded.',
        'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
    {'type': 'function', 'function': {'name': 'stop_job',
        'description': 'Stop the job safely without lowering.',
        'parameters': {'type': 'object', 'properties': {'reason': {'type': 'string'}}, 'required': ['reason']}}},
]


class ToolBox:
    """Tools on one virtual-site job. The site object provides: rs (reach stacker), send(cmd, delay),
    wait_bundle(timeout), log(who, text), event(kind, **data), finish(outcome), job_id, timing."""

    def __init__(self, site, job):
        self.site, self.job = site, job
        self.rs = job['rs']
        self.advisor = ControllerBrain(site.M, 'weighted', True, True, max_rounds=6, K=job['P']['K'])
        self.kit_started = False
        self.last = None            # last kit sensing summary
        self.last_decision = None
        self.machine = None
        self.moves_since_sense = 0
        self.last_move = None
        self.refusals = 0

    # ---- helpers
    def _summary(self, msg):
        d = self.last_decision or {}
        corners = []
        for k, u in enumerate(msg['units']):
            e = u.get('est_mm')
            corners.append({'corner': k + 1, 'x_mm': None if e is None else round(e['x'], 2),
                            'y_mm': None if e is None else round(e['y'], 2), 'reliability': u['quality']['grade']})
        ref = msg.get('kit_reference') or {}
        return {'round': d.get('round'), 'measurement_id': msg.get('measurement_id'), 'corners': corners,
                'kit_overall': d.get('kit_status'),
                'kit_fit': None if not ref.get('available') else {
                    'x_mm': round(ref['fit_x_mm'], 2), 'y_mm': round(ref['fit_y_mm'], 2),
                    'theta_deg': round(ref['fit_theta_deg'], 4)},
                'worst_corner_mm': round(max((max(abs(c['x_mm']), abs(c['y_mm'])) for c in corners
                                              if c['x_mm'] is not None), default=float('nan')), 2)}

    # ---- tools
    def approach_container(self):
        if self.job['phase'] != 'approach':
            return {'refused': 'already at the container'}
        self.site.event('approach_start')
        time.sleep(self.site.approach_s)
        self.rs.approach()
        self.job['phase'] = 'ready'
        self.site.event('in_range')
        return {'in_range': True, 'sensing_range_mm': 30}

    def kit_sense(self, remeasure=False):
        if self.job['phase'] == 'approach':
            return {'refused': 'approach the container first (approach_container)'}
        if self.job['phase'] in ('lowered', 'done', 'held', 'failed', 'aborted'):
            return {'refused': 'job already finished'}
        sid = self.job['job_id']
        if not self.kit_started:
            self.kit_started = True
            cmd, resense = {'type': 'start_job', 'job_id': sid, 'machine_id': self.job['machine_id']}, False
        elif remeasure or self.moves_since_sense == 0:
            cmd, resense = {'type': 'resense', 'session_id': sid}, True
        else:
            cmd, resense = {'type': 'sense', 'session_id': sid}, False
        self.job['phase'] = 'sensing'
        self.site.event('sense', resense=resense)
        msg = self.site.request_bundle(cmd, job=self.job)
        if msg is None:
            return {'error': 'kit did not answer (timeout)'}
        self.machine = self.rs.machine_sensor()
        self.last_decision = self.site.advise(self.advisor, msg, self.machine, resense)
        hook = self.job.get('plan_hook')
        if hook is not None and cmd['type'] == 'start_job':
            # case memory: after the first sensing the server picks how to run the loop (gain, rounds, machine
            # sensor use) from similar past cases; the same bundle is re-decided with that plan.
            plan = hook(self.last_decision)
            if plan:
                self.job['plan'] = plan
                self.advisor = ControllerBrain(self.site.M, 'weighted', plan['use_machine'], True,
                                               max_rounds=plan['max_rounds'], K=plan['K'])
                self.last_decision = self.site.advise(self.advisor, msg, self.machine, resense)
                self.site.log('서버', '사례 기반 계획: %s — %s' % (plan['plan'], plan.get('why', '')))
        self.last = msg
        self.moves_since_sense = 0
        self.job['phase'] = 'ready'
        return self._summary(msg)

    def read_machine_sensor(self):
        m = self.machine or self.rs.machine_sensor()
        if m.get('status') == 'none':
            return {'status': 'none'}
        return {'status': m['status'], 'x_mm': round(m['x_mm'], 2), 'y_mm': round(m['y_mm'], 2),
                'theta_deg': round(math.degrees(m['theta_rad']), 4)}

    def suggest_correction(self):
        d = self.last_decision
        if not d:
            return {'refused': 'no kit sensing yet'}
        out = {'situation': d.get('case_ko'), 'advice': d.get('action'), 'reason': d.get('reason'),
               'machine_weight': d.get('w_machine'), 'kit_weight': d.get('w_kit')}
        if d.get('correction'):
            c = d['correction']
            out['move'] = {'x_mm': round(c['x_mm'], 3), 'y_mm': round(c['y_mm'], 3), 'theta_deg': round(c['theta_deg'], 5)}
            out['predicted_worst_corner_after_move_mm'] = round(d['predicted_worst_mm'], 2)
        return out

    def move_spreader(self, x_mm=0.0, y_mm=0.0, theta_deg=0.0):
        try:
            x, y, t = float(x_mm), float(y_mm), float(theta_deg)
        except (TypeError, ValueError):
            return self._refuse('move values must be numbers')
        if not all(math.isfinite(v) for v in (x, y, t)):
            return self._refuse('move values must be finite')
        if self.last is None:
            return self._refuse('no kit sensing yet: call kit_sense first')
        if self.moves_since_sense >= 1:
            return self._refuse('confirm the previous move first: call kit_sense before moving again')
        if abs(x) > MOVE_LIMIT_MM or abs(y) > MOVE_LIMIT_MM or abs(t) > MOVE_LIMIT_DEG:
            return self._refuse('move too large (limits %g mm, %g deg)' % (MOVE_LIMIT_MM, MOVE_LIMIT_DEG))
        self.site.event('move', x_mm=x, y_mm=y, theta_deg=t)
        time.sleep(self.site.move_s)
        th = math.radians(t)
        self.rs.move(x, y, th)
        corr = {'x_mm': x, 'y_mm': y, 'theta_rad': th}
        self.advisor.apply_history_offsets(corr)
        self.site.frame('보정 이동')
        self.moves_since_sense = 1
        self.last_move = (x, y, th)
        return {'done': True, 'note': 'move done; call kit_sense to confirm'}

    def _predicted_worst(self):
        d = self.last_decision or {}
        est = d.get('corner_est_mm') or []
        w = d.get('weights') or [1, 1, 1, 1]
        mv = self.last_move if self.moves_since_sense else (0.0, 0.0, 0.0)
        worst = None
        for k, e in enumerate(est):
            if e is None or w[k] <= 0:
                continue
            X, Y = E.CORNERS[k]
            px = e[0] - (mv[0] - mv[2] * Y)
            py = e[1] - (mv[1] + mv[2] * X)
            v = max(abs(px), abs(py))
            worst = v if worst is None else max(worst, v)
        return worst

    def lower_and_lock(self):
        d = self.last_decision
        if d is None:
            return self._refuse('no kit data: sense first')
        if self.moves_since_sense > 1:
            return self._refuse('position not confirmed: call kit_sense')
        if d.get('kit_status') == 'invalid':
            return self._refuse('kit data unreliable (invalid): remeasure or stop')
        worst = self._predicted_worst()
        if worst is None or worst > LOWER_OK_MM:
            return self._refuse('worst corner %.2f mm > %.1f mm allowed for lowering: keep correcting'
                                % (worst if worst is not None else float('nan'), LOWER_OK_MM))
        m = self.machine or {}
        ref = (self.last or {}).get('kit_reference') or {}
        if m.get('status') == 'valid' and ref.get('available') and d.get('case') != 'KIT_OVERRIDE':
            diff = max(abs(m['x_mm'] - ref['fit_x_mm']), abs(m['y_mm'] - ref['fit_y_mm']))   # both read before any move
            if diff > DISAGREE_MM + 0.3 * max(abs(ref['fit_x_mm']), abs(ref['fit_y_mm'])):
                return self._refuse('machine sensor and kit disagree by %.1f mm: remeasure first' % diff)
        return self.site.lower_and_lock()

    def stop_job(self, reason=''):
        return self.site.stop_job(str(reason)[:200] or 'ai_stop')

    def _refuse(self, reason):
        self.refusals += 1
        self.site.event('guard', reason=reason)
        self.site.log('안전장치', '거부: ' + reason)
        return {'refused': reason}

    def call(self, name, args):
        if self.site.job is not self.job or (self.job['outcome'] is not None and name != 'read_machine_sensor'):
            return {'refused': 'job finished or replaced', 'finished': True}   # a stale thread must not touch a newer job
        fn = {'approach_container': self.approach_container, 'kit_sense': self.kit_sense,
              'read_machine_sensor': self.read_machine_sensor, 'suggest_correction': self.suggest_correction,
              'move_spreader': self.move_spreader, 'lower_and_lock': self.lower_and_lock,
              'stop_job': self.stop_job}.get(name)
        if fn is None:
            return {'error': 'unknown tool ' + str(name)}
        args = args if isinstance(args, dict) else {}
        try:
            return fn(**{k: v for k, v in args.items() if k in fn.__code__.co_varnames})
        except Exception as exc:     # a broken tool call must not crash the job loop
            return {'error': '%s: %s' % (type(exc).__name__, exc)}


def ollama_chat(url, model, messages, timeout=120):
    body = json.dumps({'model': model, 'messages': messages, 'tools': TOOLS, 'stream': False,
                       'options': {'temperature': 0}}).encode('utf-8')
    req = urllib.request.Request(url.rstrip('/') + '/api/chat', data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))['message']


def run_ai(site, job, url, model):
    """The AI loop for one job (runs in its own thread)."""
    tb = ToolBox(site, job)
    s = job['settings']
    messages = [{'role': 'system', 'content': SYSTEM},
                {'role': 'user', 'content': 'New job %s. Machine sensor: %s. Align and lock the container.'
                                            % (job['job_id'], s['machine'])}]
    calls = nudges = 0
    while job['outcome'] is None:
        try:
            msg = ollama_chat(url, model, messages)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            site.log('서버', 'Ollama 연결 실패: %s — Ollama 실행 여부와 모델(%s)을 확인하세요' % (exc, model))
            site.abort('ai_unreachable')
            return
        messages.append(msg)
        if msg.get('content'):
            site.log('AI', msg['content'].strip()[:400])
        tcs = msg.get('tool_calls') or []
        if not tcs:
            nudges += 1
            if nudges > MAX_NUDGES:
                site.log('서버', 'AI가 작업을 끝내지 않아 안전 정지')
                site.stop_job('ai_no_progress')
                return
            messages.append({'role': 'user', 'content': 'Continue with the tools until lower_and_lock or stop_job.'})
            continue
        for tc in tcs:
            fn = tc.get('function', {})
            args = fn.get('arguments')
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls += 1
            site.log('도구', '%s(%s)' % (fn.get('name'), json.dumps(args or {}, ensure_ascii=False)))
            result = tb.call(fn.get('name'), args)
            messages.append({'role': 'tool', 'content': json.dumps(result, ensure_ascii=False)})
            if job['outcome'] is not None:
                break
            if calls >= MAX_TOOL_CALLS:
                site.log('서버', '도구 호출 한도 초과 → 안전 정지')
                site.stop_job('ai_too_many_calls')
                return


def run_autopilot(site, job):
    """Rule autopilot through the SAME tools and guard (works without Ollama)."""
    tb = ToolBox(site, job)

    def call(name, **args):
        site.log('도구', '%s(%s)' % (name, json.dumps(args, ensure_ascii=False)))
        return tb.call(name, args)
    call('approach_container')
    call('kit_sense')
    for _ in range(MAX_TOOL_CALLS):
        if job['outcome'] is not None:
            return
        sug = call('suggest_correction')
        adv = sug.get('advice')
        if adv == 'resense':
            call('kit_sense', remeasure=True)
            continue
        if adv == 'stop' or 'move' not in sug:
            call('stop_job', reason=sug.get('reason') or 'advisor_stop')
            return
        mv = sug['move']
        call('move_spreader', x_mm=mv['x_mm'], y_mm=mv['y_mm'], theta_deg=mv['theta_deg'])
        if adv == 'lower_after_move':
            r = call('lower_and_lock')
            if r.get('refused'):
                call('kit_sense')
                continue
            return
        call('kit_sense')
