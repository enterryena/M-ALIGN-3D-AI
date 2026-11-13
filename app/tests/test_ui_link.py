"""Real simulator UI -> kit link -> main server, following the machine job sequence.

1 click = 1 job: machine ready -> start_job -> (sense -> bundle -> correction applied) x 6
-> end_job -> main action. Checked with the example AUTO main server, and with no
server (built-in LocalMainSim). fetch() is bridged to the real local host.
"""
from pathlib import Path
import json, os, socket, subprocess, sys, tempfile, time, urllib.request, urllib.error
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
html = (ROOT / 'MALIGN_Desktop.html').read_text(encoding='utf-8')
CHROME = os.environ.get('MALIGN_CHROMIUM', '/usr/bin/chromium')


def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close(); return port


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


def run_ui(data_dir, play):
    host = subprocess.Popen([sys.executable, str(ROOT / 'desktop_host.py'), '--headless', '--data-dir', str(data_dir)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    info = json.loads(host.stdout.readline())

    def bridge(path, opts):
        req = urllib.request.Request(info['url'].rstrip('/') + path,
                                     data=opts['body'].encode() if opts.get('body') is not None else None,
                                     headers=opts.get('headers', {}), method=opts.get('method', 'GET'))
        try:
            with urllib.request.urlopen(req, timeout=60) as out:
                return {'status': out.status, 'body': out.read().decode()}
        except urllib.error.HTTPError as err:
            return {'status': err.code, 'body': err.read().decode()}

    browser = play.chromium.launch(executable_path=CHROME, headless=True, args=['--no-sandbox'])
    page = browser.new_page(viewport={'width': 1366, 'height': 900})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('dialog', lambda d: d.dismiss())
    page.expose_function('backendBridge', bridge)
    inj = ('<script>window.MALIGN_DESKTOP_TOKEN=%s;window.fetch=async function(u,o){const r=await window.backendBridge(u,o||{});'
           'return new Response(r.body,{status:r.status,headers:{"content-type":"application/json"}});};</script>') % json.dumps(info['token'])
    page.set_content(html.replace('</head>', inj + '</head>', 1), wait_until='domcontentloaded')
    page.wait_for_function("document.getElementById('bankStatus').textContent.includes('PC 영구 저장')", timeout=20000)
    page.wait_for_function("document.getElementById('linkKit').textContent.length>3", timeout=20000)
    page.locator('#autoSeed').uncheck()
    page.locator('#seed').fill('2026')
    page.locator('#run').click()
    page.wait_for_function("document.getElementById('phase').textContent.startsWith('9단계') && !document.getElementById('run').disabled",
                           timeout=90000)
    out = {'history': page.locator('#history').inner_text(), 'bankTotal': None, 'kit': page.locator('#linkKit').inner_text()}
    page.wait_for_function("document.getElementById('bankTotal').textContent==='1'", timeout=15000)
    out['bankTotal'] = 1
    return host, info, browser, errors, out


with tempfile.TemporaryDirectory(prefix='malign_ui_link_') as tmp, sync_playwright() as play:
    tmp = Path(tmp)
    # --- with the example AUTO main server
    port = free_port()
    base = 'http://127.0.0.1:%d/malign/v1' % port
    (tmp / 'A' / 'Link').mkdir(parents=True)
    (tmp / 'A' / 'Link' / 'link_config.json').write_text(json.dumps({
        'kit_id': 'KIT-UI', 'units': ['UI-1', 'UI-2', 'UI-3', 'UI-4'], 'server_url': base + '/bundles',
        'heartbeat_s': 0.3, 'active_poll_s': 0.1, 'retry_max_s': 0.5}), encoding='utf-8')
    srv = subprocess.Popen([sys.executable, str(ROOT / 'server_example' / 'malign_main_server.py'), '--host', '127.0.0.1',
                            '--port', str(port), '--db', str(tmp / 'main.sqlite3'), '--rounds', '6', '--move-delay', '0.3'],
                           stdout=subprocess.PIPE, text=True)
    srv.stdout.readline()
    host = browser = None
    try:
        host, info, browser, errors, out = run_ui(tmp / 'A', play)
        jobs = get(base + '/jobs')
        assert len(jobs) == 1 and jobs[0]['status'] == 'end' and jobs[0]['rounds'] == 6, jobs
        msgs = get(base + '/kits/KIT-UI/messages?session_id=' + jobs[0]['job_id'])
        rounds = [m for m in msgs if m['kind'] == 'round']
        assert [m['round_no'] for m in rounds] == [1, 2, 3, 4, 5, 6]
        for m in rounds:
            assert [u['unit_id'] for u in m['units']] == ['UI-1', 'UI-2', 'UI-3', 'UI-4']
            assert all(len(u['sensor_uH']) == 5 for u in m['units']) and set(m['correction']) == {'x_mm', 'y_mm', 'theta_deg'}
            assert m['source'] == 'simulation' and 'evaluation' not in json.dumps(m)
        ev = [m['event'] for m in msgs if m['kind'] == 'event' and m['event'] != 'command_ack']
        assert ev[0] == 'job_start' and ev[-1] == 'job_end', ev
        assert out['history'].count('[센싱→메인 서버]') == 6
        assert not errors, errors
    finally:
        if browser: browser.close()
        if host: host.terminate(); host.wait(timeout=8)
        srv.terminate(); srv.wait(timeout=8)

    # --- no server configured: built-in LocalMainSim directs the job
    (tmp / 'B' / 'Link').mkdir(parents=True)
    (tmp / 'B' / 'Link' / 'link_config.json').write_text(json.dumps({'sim_move_delay_s': 0.2}), encoding='utf-8')
    host = browser = None
    try:
        host, info, browser, errors, out = run_ui(tmp / 'B', play)
        assert out['history'].count('[센싱→메인 서버]') == 6 and out['kit'].startswith('MALIGN-KIT-')
        assert not errors, errors
    finally:
        if browser: browser.close()
        if host: host.terminate(); host.wait(timeout=8)

print('PASS: UI 1 job = machine ready -> start_job -> 6 server-directed sensings (4-unit bundles) -> end_job; '
      'also stand-alone with built-in sim server; no JS errors', flush=True)
