# -*- coding: utf-8 -*-
"""Optional AI operator assistant for the M-ALIGN main server (free, local: Ollama).

The rule-based controller (main_controller.py) makes every alignment / lowering decision.
This assistant only turns an operator's words into a few WHITELISTED main-server API calls
and explains the state in plain Korean. It cannot command corrections, lowering or sensing.

Setup (free, offline):  install Ollama (https://ollama.com), then e.g.  ollama pull qwen2.5:7b
Run:   python tools/ai_operator.py --server http://127.0.0.1:8750 "KIT-0001로 가상 작업 하나 시작해줘"
       python tools/ai_operator.py --server http://127.0.0.1:8750          (interactive)

Allowed tools: list_jobs, kit_status, start_virtual_job (simulation), machine_ready,
               kit_command(type in stop / self_test / fault_reset)
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

SYSTEM = (
    'You are the operator assistant of the M-ALIGN main server (container corner alignment kit + reach stacker). '
    'Answer in Korean, briefly. Use ONLY the given tools to read state or act; never invent numbers or job ids. '
    'You must NOT decide corrections, lowering or sensing: the rule-based controller does that. '
    'If the operator asks for something outside your tools, say it is not allowed and why. '
    'Before stop or fault_reset, make sure the operator clearly asked for it.')

TOOLS = [
    {'type': 'function', 'function': {
        'name': 'list_jobs', 'description': 'List recent jobs with status and number of correction rounds.',
        'parameters': {'type': 'object', 'properties': {}, 'required': []}}},
    {'type': 'function', 'function': {
        'name': 'kit_status', 'description': "Kit's last state, heartbeat and fault code, plus the virtual site state.",
        'parameters': {'type': 'object', 'properties': {'kit_id': {'type': 'string'}}, 'required': ['kit_id']}}},
    {'type': 'function', 'function': {
        'name': 'start_virtual_job', 'description': 'SIMULATION: start one virtual job for a kit.',
        'parameters': {'type': 'object', 'properties': {
            'kit_id': {'type': 'string'},
            'hardware': {'type': 'string', 'enum': ['ideal', 'target', 'base']},
            'machine': {'type': 'string', 'enum': ['normal', 'unstable', 'none', 'biased']},
            'fault': {'type': 'string', 'enum': ['none', 'head_dead', 'drift']}}, 'required': ['kit_id']}}},
    {'type': 'function', 'function': {
        'name': 'machine_ready', 'description': 'A machine reports ready next to a container: start a job for its kit.',
        'parameters': {'type': 'object', 'properties': {'machine_id': {'type': 'string'}, 'kit_id': {'type': 'string'}},
                       'required': ['machine_id', 'kit_id']}}},
    {'type': 'function', 'function': {
        'name': 'kit_command', 'description': 'Send stop, self_test or fault_reset to a kit.',
        'parameters': {'type': 'object', 'properties': {
            'kit_id': {'type': 'string'}, 'type': {'type': 'string', 'enum': ['stop', 'self_test', 'fault_reset']}},
            'required': ['kit_id', 'type']}}},
]
ALLOWED_KIT_COMMANDS = ('stop', 'self_test', 'fault_reset')


class MainServer:
    def __init__(self, base, api_key=''):
        self.base = base.rstrip('/')
        self.h = {'Accept': 'application/json'}
        if api_key:
            self.h['Authorization'] = 'Bearer ' + api_key

    def call(self, path, data=None):
        h = dict(self.h)
        body = None
        if data is not None:
            h['Content-Type'] = 'application/json'
            body = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(self.base + path, data=body, headers=h, method='POST' if body is not None else 'GET')
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            return {'error': 'HTTP %d: %s' % (e.code, e.read().decode('utf-8', 'replace')[:200])}

    def run_tool(self, name, args):
        args = args if isinstance(args, dict) else {}
        kit = str(args.get('kit_id', ''))
        if name == 'list_jobs':
            return self.call('/malign/v1/jobs')[-10:]
        if name == 'kit_status':
            kits = [k for k in self.call('/malign/v1/kits') if isinstance(k, dict) and k.get('kit_id') == kit]
            site = self.call('/sim/v1/kits/%s/state' % kit)
            if isinstance(site, dict):
                site = {k: site.get(k) for k in ('active', 'job_id', 'phase', 'outcome', 'rounds', 'decision')}
            return {'kit': kits[0] if kits else None, 'virtual_site': site}
        if name == 'start_virtual_job':
            params = {k: args[k] for k in ('hardware', 'machine', 'fault') if k in args}
            st = self.call('/sim/v1/kits/%s/start' % kit, params)
            return {k: st.get(k) for k in ('job_id', 'phase', 'settings', 'error') if k in st}
        if name == 'machine_ready':
            return self.call('/malign/v1/machines/%s/ready' % args.get('machine_id', ''), {'kit_id': kit})
        if name == 'kit_command':
            if args.get('type') not in ALLOWED_KIT_COMMANDS:
                return {'error': 'not allowed: only ' + ', '.join(ALLOWED_KIT_COMMANDS)}
            return self.call('/malign/v1/kits/%s/commands' % kit, {'type': args['type']})
        return {'error': 'unknown tool'}


def ollama_chat(url, model, messages):
    body = json.dumps({'model': model, 'messages': messages, 'tools': TOOLS, 'stream': False,
                       'options': {'temperature': 0}}).encode('utf-8')
    req = urllib.request.Request(url.rstrip('/') + '/api/chat', data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode('utf-8'))['message']


def ask(server, text, ollama='http://127.0.0.1:11434', model='qwen2.5:7b', history=None, max_steps=5):
    """One operator request. Returns (answer_text, tool_log, history)."""
    messages = history or [{'role': 'system', 'content': SYSTEM}]
    messages.append({'role': 'user', 'content': text})
    log = []
    for _ in range(max_steps):
        msg = ollama_chat(ollama, model, messages)
        messages.append(msg)
        calls = msg.get('tool_calls') or []
        if not calls:
            return msg.get('content', ''), log, messages
        for c in calls:
            fn = c.get('function', {})
            args = fn.get('arguments')
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            result = server.run_tool(fn.get('name'), args)
            log.append({'tool': fn.get('name'), 'args': args, 'result': result})
            messages.append({'role': 'tool', 'content': json.dumps(result, ensure_ascii=False)[:4000]})
    return '(도구 호출이 너무 많아 멈췄습니다)', log, messages


def main():
    ap = argparse.ArgumentParser(description='M-ALIGN AI operator assistant (Ollama)')
    ap.add_argument('--server', default='http://127.0.0.1:8750')
    ap.add_argument('--api-key', default='')
    ap.add_argument('--ollama', default='http://127.0.0.1:11434')
    ap.add_argument('--model', default='qwen2.5:7b')
    ap.add_argument('text', nargs='*')
    a = ap.parse_args()
    server = MainServer(a.server, a.api_key)
    history = None
    try:
        if a.text:
            ans, log, _ = ask(server, ' '.join(a.text), a.ollama, a.model)
            for l in log:
                print('  [도구]', l['tool'], json.dumps(l['args'], ensure_ascii=False))
            print(ans)
            return
        print('M-ALIGN 운영 비서 (종료: 빈 줄)')
        while True:
            text = input('> ').strip()
            if not text:
                break
            ans, log, history = ask(server, text, a.ollama, a.model, history)
            for l in log:
                print('  [도구]', l['tool'], json.dumps(l['args'], ensure_ascii=False))
            print(ans)
    except urllib.error.URLError as e:
        print('Ollama 또는 메인 서버에 연결할 수 없습니다:', e, file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
