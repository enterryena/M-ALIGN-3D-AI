"""Scripted stand-in for Ollama /api/chat (tests and screenshots only)."""
import json
from http.server import BaseHTTPRequestHandler

CALLS = []


def tool(name, **args):
    return {'role': 'assistant', 'content': '%s 실행' % name, 'tool_calls': [{'function': {'name': name, 'arguments': args}}]}


class FakeOllama(BaseHTTPRequestHandler):
    """Policy: approach, sense, try to lower too early (guard must refuse), then follow the advisor."""
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        msgs = body['messages']
        assert body['tools'] and msgs[0]['role'] == 'system'
        tnames = {t['function']['name'] for t in body['tools']}
        if 'start_task' in tnames or 'sound_horn' in tnames:
            return self._send(yard_policy(msgs, 'start_task' in tnames))
        names = [tc['function']['name'] for m in msgs if m['role'] == 'assistant' for tc in (m.get('tool_calls') or [])]
        last = json.loads(msgs[-1]['content']) if msgs[-1]['role'] == 'tool' else None
        prev = names[-1] if names else None
        if prev is None:
            reply = tool('approach_container')
        elif prev == 'approach_container':
            reply = tool('kit_sense')
        elif prev == 'kit_sense' and names.count('lower_and_lock') == 0:
            reply = tool('lower_and_lock')                      # premature on purpose
        elif prev == 'kit_sense':
            reply = tool('suggest_correction')
        elif prev == 'lower_and_lock' and last.get('refused'):
            reply = tool('suggest_correction')
        elif prev == 'suggest_correction':
            adv = last.get('advice')
            if adv == 'resense':
                reply = tool('kit_sense', remeasure=True)
            elif adv == 'stop' or 'move' not in last:
                reply = tool('stop_job', reason='advisor says stop')
            else:
                self.server.lower_next = adv == 'lower_after_move'
                reply = tool('move_spreader', **last['move'])
        elif prev == 'move_spreader':
            reply = tool('lower_and_lock') if getattr(self.server, 'lower_next', False) else tool('kit_sense')
        else:
            reply = {'role': 'assistant', 'content': '작업을 마쳤습니다.'}
        CALLS.append(prev)
        self._send(reply)

    def _send(self, reply):
        raw = json.dumps({'message': reply}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)




YARD = {'wrong_task': 0, 'wrong_obstacle': 0}


def yard_policy(msgs, task_mode):
    """Yard decisions: the first decision of each kind is deliberately wrong (the guard must refuse it
    and say why); after a refusal the policy follows the yard rules."""
    info = json.loads(msgs[1]['content'].split(': ', 1)[1])
    refused = any(m['role'] == 'tool' and 'refused' in m['content'] for m in msgs)
    if task_mode:
        if not refused and YARD['wrong_task'] == 0:
            YARD['wrong_task'] += 1
            occ = info['occupied'][0]['slot'] if info['occupied'] else 'A1'
            src = info['trucks_waiting'][0]['truck'] if info['trucks_waiting'] else 'T1'
            return tool('start_task', kind='unload', source=src, dest=occ)           # occupied slot: refused
        if info['user_requests']:
            r = info['user_requests'][0]
            occ = {o['container']: o['slot'] for o in info['occupied']}
            onto = {t['container']: t['truck'] for t in info['trucks_waiting'] if t['container']}
            if r['container'] in onto:
                return tool('start_task', kind='unload', source=onto[r['container']], dest=r['to'])
            kind = 'load' if r['to'].startswith('T') else 'move'
            return tool('start_task', kind=kind, source=occ.get(r['container'], '?'), dest=r['to'])
        for t in sorted(info['trucks_waiting'], key=lambda x: -x['waiting_s']):
            if t['type'] == 'outbound' and info['occupied']:
                old = max(info['occupied'], key=lambda o: o['in_yard_s'])
                return tool('start_task', kind='load', source=old['slot'], dest=t['truck'])
            if t['type'] == 'inbound' and info['free_slots']:
                return tool('start_task', kind='unload', source=t['truck'], dest=info['free_slots'][0])
        return tool('wait', seconds=3)
    if not refused and YARD['wrong_obstacle'] == 0:
        YARD['wrong_obstacle'] += 1
        return tool('request_clear') if info['moving'] else tool('sound_horn')      # both refused by the guard
    if info['kind'] == 'truck':
        return tool('wait', seconds=5)
    if info['moving']:
        if info.get('detours', 0) >= 2:
            return tool('wait', seconds=5)
        return tool('sound_horn') if info['horns'] < 2 else tool('detour')
    extra = info.get('detour_extra_m')
    return tool('detour') if extra is not None and extra < 30 else tool('request_clear')
