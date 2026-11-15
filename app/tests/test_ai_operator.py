"""AI operator plumbing with a fake Ollama (no model needed): tool calls reach the main
server, results go back to the model, and anything outside the whitelist is refused."""
import json, pathlib, socket, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import ai_operator as A  # noqa: E402

SCRIPT = []      # replies of the fake model, in order
SEEN = []        # requests the fake model received


class FakeOllama(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        SEEN.append(body)
        raw = json.dumps({'message': SCRIPT.pop(0)}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); p = s.getsockname()[1]; s.close(); return p


fake = ThreadingHTTPServer(('127.0.0.1', 0), FakeOllama)
threading.Thread(target=fake.serve_forever, daemon=True).start()
ollama = 'http://127.0.0.1:%d' % fake.server_port
port = free_port()
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    srv = subprocess.Popen([sys.executable, str(ROOT / 'server_example' / 'malign_main_server.py'), '--host', '127.0.0.1',
                            '--port', str(port), '--db', str(pathlib.Path(tmp) / 'm.sqlite3')], stdout=subprocess.PIPE, text=True)
    srv.stdout.readline()
    try:
        server = A.MainServer('http://127.0.0.1:%d' % port)
        # 1) a request that needs a tool
        SCRIPT[:] = [{'role': 'assistant', 'content': '', 'tool_calls': [{'function': {
                         'name': 'start_virtual_job', 'arguments': {'kit_id': 'KIT-AI', 'hardware': 'target'}}}]},
                     {'role': 'assistant', 'content': '가상 작업을 시작했습니다.'}]
        ans, log, hist = A.ask(server, 'KIT-AI로 가상 작업 시작해줘', ollama, 'fake')
        assert ans == '가상 작업을 시작했습니다.'
        assert log[0]['tool'] == 'start_virtual_job' and log[0]['result']['job_id'].startswith('JOB-')
        assert SEEN[0]['tools'] and SEEN[0]['messages'][0]['role'] == 'system'
        assert SEEN[1]['messages'][-1]['role'] == 'tool' and 'JOB-' in SEEN[1]['messages'][-1]['content']
        # 2) the model tries a command outside the whitelist -> refused, nothing queued
        SCRIPT[:] = [{'role': 'assistant', 'content': '', 'tool_calls': [{'function': {
                         'name': 'kit_command', 'arguments': json.dumps({'kit_id': 'KIT-AI', 'type': 'end_job'})}}]},
                     {'role': 'assistant', 'content': '그 명령은 허용되지 않습니다.'}]
        ans, log, _ = A.ask(server, '작업 끝내', ollama, 'fake', hist)
        assert 'not allowed' in log[0]['result']['error']
        # 3) unknown tool name
        assert A.MainServer('http://127.0.0.1:%d' % port).run_tool('lower_spreader', {}) == {'error': 'unknown tool'}
    finally:
        srv.terminate(); srv.wait(timeout=8)
fake.shutdown()
print('PASS: AI operator — whitelisted tool calls reach the main server; end_job/lowering not reachable', flush=True)
