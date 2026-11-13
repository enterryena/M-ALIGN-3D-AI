"""Real simulator UI -> kit link -> example main server.

One 'run' click must deliver one 4-unit bundle per correction round, then the
cycle result; the movement test button must stop the kit until the main server
sends 'realign'. fetch() is bridged to the real local host (see test_ui_bridge).
"""
from pathlib import Path
import json, os, socket, subprocess, sys, tempfile, time, urllib.request, urllib.error
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
html = (ROOT / 'MALIGN_Desktop.html').read_text(encoding='utf-8')
UNITS = ['KIT-UI-U1', 'KIT-UI-U2', 'KIT-UI-U3', 'KIT-UI-U4']


def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close(); return port


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def post(url, data):
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def wait_for(fn, what, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError('timeout: ' + what)


with tempfile.TemporaryDirectory(prefix='malign_ui_link_') as tmp:
    tmp = Path(tmp)
    port = free_port()
    base = 'http://127.0.0.1:%d/malign/v1' % port
    (tmp / 'Link').mkdir()
    (tmp / 'Link' / 'link_config.json').write_text(json.dumps({
        'kit_id': 'KIT-UI', 'units': UNITS, 'server_url': base + '/bundles',
        'heartbeat_s': 0.3, 'retry_max_s': 0.5}), encoding='utf-8')
    srv = subprocess.Popen([sys.executable, str(ROOT / 'server_example' / 'malign_main_server.py'), '--host', '127.0.0.1',
                            '--port', str(port), '--db', str(tmp / 'main.sqlite3')], stdout=subprocess.PIPE, text=True)
    srv.stdout.readline()
    host = subprocess.Popen([sys.executable, str(ROOT / 'desktop_host.py'), '--headless', '--data-dir', str(tmp)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    info = json.loads(host.stdout.readline())
    try:
        def bridge(path, opts):
            req = urllib.request.Request(info['url'].rstrip('/') + path,
                                         data=opts['body'].encode() if opts.get('body') is not None else None,
                                         headers=opts.get('headers', {}), method=opts.get('method', 'GET'))
            try:
                with urllib.request.urlopen(req, timeout=60) as out:
                    return {'status': out.status, 'body': out.read().decode()}
            except urllib.error.HTTPError as err:
                return {'status': err.code, 'body': err.read().decode()}

        with sync_playwright() as play:
            browser = play.chromium.launch(executable_path=os.environ.get('MALIGN_CHROMIUM', '/usr/bin/chromium'),
                                           headless=True, args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1366, 'height': 900})
            errors, dialogs = [], []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.on('dialog', lambda d: (dialogs.append(d.message), d.dismiss()))
            page.expose_function('backendBridge', bridge)
            inj = ('<script>window.MALIGN_DESKTOP_TOKEN=%s;window.fetch=async function(u,o){const r=await window.backendBridge(u,o||{});'
                   'return new Response(r.body,{status:r.status,headers:{"content-type":"application/json"}});};</script>') % json.dumps(info['token'])
            page.set_content(html.replace('</head>', inj + '</head>', 1), wait_until='domcontentloaded')
            page.wait_for_function("document.getElementById('linkKit').textContent==='KIT-UI'", timeout=20000)
            page.wait_for_function("document.getElementById('bankStatus').textContent.includes('PC 영구 저장')", timeout=20000)

            # Find a seed whose run converges by the sensor criterion (ideal scenario usually does).
            page.locator('#autoSeed').uncheck()
            page.locator('#scenario').select_option('ideal')
            page.locator('#seed').fill('2026')
            page.locator('#run').click()
            page.wait_for_function("document.getElementById('verdict').textContent!=='진행 중' && !document.getElementById('run').disabled", timeout=60000)
            def link_status():
                req = urllib.request.Request(info['url'] + 'api/link/status', headers={'X-MALIGN-Token': info['token']})
                with urllib.request.urlopen(req, timeout=10) as r:
                    s = json.load(r)
                return s if s['state'] in ('HOLD', 'STOPPED') and s['outbox']['pending'] == 0 else None
            st = wait_for(link_status, 'cycle sent')
            msgs = get(base + '/kits/KIT-UI/messages?session_id=' + st['session_id'])
            rounds = [m for m in msgs if m['kind'] == 'round']
            events = [m['event'] for m in msgs if m['kind'] == 'event']
            assert 1 <= len(rounds) <= 6 and [m['round_no'] for m in rounds] == list(range(1, len(rounds) + 1))
            for m in rounds:
                assert [u['unit_id'] for u in m['units']] == UNITS and all(len(u['sensor_uH']) == 5 for u in m['units'])
                assert m['source'] == 'simulation' and set(m['command']) == {'x_mm', 'y_mm', 'theta_deg'}
                assert 'true' not in json.dumps(m).lower(), 'simulator truth must not be in round bundles'
            assert events[0] == 'cycle_start' and events[-1] in ('aligned', 'align_failed')
            assert st['state'] == ('HOLD' if events[-1] == 'aligned' else 'STOPPED')
            assert st['state'] == 'HOLD', 'ideal seed 2026 should converge; pick another seed if the model changed'

            # Movement after alignment -> STOPPED, reported to server.
            page.wait_for_function("!document.getElementById('linkMove').disabled", timeout=10000)
            page.locator('#linkMove').click()
            page.wait_for_function("document.getElementById('linkState').textContent==='정지'", timeout=15000)
            wait_for(lambda: any(m.get('event') == 'movement_detected' for m in get(base + '/kits/KIT-UI/messages')), 'movement event')

            # New run is refused until the main server says realign.
            page.locator('#run').click()
            page.wait_for_function("document.getElementById('phase').textContent==='메인 서버 명령 대기'", timeout=15000)
            assert dialogs and '재보정' in dialogs[-1]
            post(base + '/kits/KIT-UI/commands', {'type': 'realign'})
            page.wait_for_function("document.getElementById('linkStatus').textContent.includes('재보정 요청 수신')", timeout=15000)
            page.locator('#run').click()
            page.wait_for_function("document.getElementById('verdict').textContent!=='진행 중' && !document.getElementById('run').disabled", timeout=60000)
            wait_for(lambda: sum(1 for m in get(base + '/kits/KIT-UI/messages') if m.get('event') == 'cycle_start') == 2, 'second cycle')
            assert not errors, errors
            browser.close()
    finally:
        host.terminate(); host.wait(timeout=8)
        srv.terminate(); srv.wait(timeout=8)
print('PASS: UI run -> one 4-unit bundle per round on main server; movement -> STOPPED; server realign -> new cycle; no JS errors', flush=True)
