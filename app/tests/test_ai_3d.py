"""AI main server end-to-end with a fake Ollama (scripted policy, no model needed):
the AI drives approach -> sensing -> corrections -> lowering through the tools; a premature
lowering is REFUSED by the safety guard and the AI is told why; the job ends locked.
Also: AI unreachable -> job aborted with a clear message."""
import json, pathlib, subprocess, sys, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests' / 'fixtures'))
from fake_ollama import FakeOllama  # noqa: E402


def call(url, data=None, h=None):
    h = dict(h or {})
    b = None
    if data is not None:
        h['Content-Type'] = 'application/json'
        b = json.dumps(data).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=b, headers=h, method='POST' if b else 'GET'), timeout=20) as r:
        return json.load(r)


def run_job(info, ctrl, seed=123, timeout=90):
    H = {'X-MALIGN-Token': info['token']}
    call(info['url'] + 'api/sim/start', {'hardware': 'target', 'machine': 'normal', 'seed': seed, 'controller': ctrl}, H)
    end = time.time() + timeout
    while time.time() < end:
        st = call(info['url'] + 'api/sim/state', None, H)
        if not st['active'] and st['outcome']:
            return st
        time.sleep(0.2)
    raise AssertionError('job did not finish')


fake = ThreadingHTTPServer(('127.0.0.1', 0), FakeOllama)
threading.Thread(target=fake.serve_forever, daemon=True).start()
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:  # Windows may hold the closed host's DB a moment longer
    tmp = pathlib.Path(tmp)
    (tmp / 'Link').mkdir()
    (tmp / 'Link' / 'link_config.json').write_text(json.dumps({
        'sim_move_delay_s': 0.05, 'ai_ollama_url': 'http://127.0.0.1:%d' % fake.server_port, 'ai_model': 'fake'}))
    h = subprocess.Popen([sys.executable, str(ROOT / 'desktop_host.py'), '--headless', '--data-dir', str(tmp)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    info = json.loads(h.stdout.readline())
    try:
        st = run_job(info, 'ai')
        assert st['outcome'] == 'success', (st['outcome'], st['log'][-5:])
        kinds = [e['kind'] for e in st['events']]
        assert kinds[:2] == ['approach_start', 'in_range'] and 'guard' in kinds and kinds[-3:] == ['lower', 'lock', 'raise']
        whos = [l['who'] for l in st['log']]
        assert 'AI' in whos and '안전장치' in whos and '키트' in whos
        assert any('거부' in l['text'] and '4.0 mm' in l['text'] for l in st['log'] if l['who'] == '안전장치')
        assert st['locked'] is True and st['truth_worst_mm'] <= 5
        assert set(st['pose']) == {'tx_mm', 'ty_mm', 'th_deg'} and len(st['tol_mm']) == 4
        # the same seed with the rule autopilot ends the same way
        st2 = run_job(info, 'autopilot')
        assert st2['outcome'] == 'success' and abs(st2['truth_worst_mm'] - st['truth_worst_mm']) < 1.0
        # 3D page and bundled three.js are served offline
        page = urllib.request.urlopen(info['url'] + '3d', timeout=10).read().decode('utf-8')
        assert 'MALIGN_DESKTOP_TOKEN' in page and 'importmap' in page
        js = urllib.request.urlopen(info['url'] + 'vendor/three/three.core.js', timeout=10).read()
        assert b"REVISION = '187'" in js
    finally:
        h.terminate(); h.wait(timeout=8)
    # Ollama unreachable -> aborted with a message
    (tmp / 'Link' / 'link_config.json').write_text(json.dumps({'ai_ollama_url': 'http://127.0.0.1:9', 'sim_move_delay_s': 0.05}))
    h = subprocess.Popen([sys.executable, str(ROOT / 'desktop_host.py'), '--headless', '--data-dir', str(tmp)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    info = json.loads(h.stdout.readline())
    try:
        st = run_job(info, 'ai', timeout=30)
        assert st['outcome'] == 'aborted' and any('Ollama 연결 실패' in l['text'] for l in st['log'])
    finally:
        h.terminate(); h.wait(timeout=8)
fake.shutdown()
print('PASS: AI main server via tools (fake Ollama) — approach, sensing, moves, guard refusal reported to AI, lock; '
      'autopilot same tools; 3D page + three.js served offline; unreachable AI aborts', flush=True)
