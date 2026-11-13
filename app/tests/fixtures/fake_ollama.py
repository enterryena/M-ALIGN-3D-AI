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
        raw = json.dumps({'message': reply}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


