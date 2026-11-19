"""Real simulator UI -> kit link -> main server -> virtual reach stacker (stage 3).

1 click = 1 job on the virtual site: approach -> in range -> start_job -> (sense -> bundle ->
server decision -> machine move) x N -> lower -> lock. The screen shows the server's
decisions and the machine's moves. Checked with the example AUTO server and with the
built-in virtual site. fetch() is bridged to the real local host.
"""
from pathlib import Path
import json, os, socket, subprocess, sys, tempfile, time, urllib.request, urllib.error
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
html = (ROOT / 'MASKIT_Desktop.html').read_text(encoding='utf-8')
CHROME = os.environ.get('MASKIT_CHROMIUM', '/usr/bin/chromium')


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
    inj = ('<script>window.MASKIT_DESKTOP_TOKEN=%s;window.fetch=async function(u,o){const r=await window.backendBridge(u,o||{});'
           'return new Response(r.body,{status:r.status,headers:{"content-type":"application/json"}});};</script>') % json.dumps(info['token'])
    page.set_content(html.replace('</head>', inj + '</head>', 1), wait_until='domcontentloaded')
    page.wait_for_function("document.getElementById('bankStatus').textContent.includes('PC 영구 저장')", timeout=20000)
    page.wait_for_function("document.getElementById('linkKit').textContent.length>3", timeout=20000)
    page.locator('#autoSeed').uncheck()
    page.locator('#seed').fill('123')
    page.locator('#scenario').select_option('target')
    page.locator('#simMachine').select_option('normal')
    page.locator('#simStrategy').select_option('weighted')
    page.locator('#run').click()
    page.wait_for_function("document.getElementById('phase').textContent.startsWith('9단계') && !document.getElementById('run').disabled",
                           timeout=90000)
    out = {'history': page.locator('#history').inner_text(), 'bankTotal': None, 'kit': page.locator('#linkKit').inner_text(),
           'verdict': page.locator('#verdict').inner_text(), 'decision': page.locator('#simDecision').inner_text()}
    page.wait_for_function("document.getElementById('bankTotal').textContent==='1'", timeout=15000)
    out['bankTotal'] = 1
    return host, info, browser, errors, out


with tempfile.TemporaryDirectory(prefix='maskit_ui_link_') as tmp, sync_playwright() as play:
    tmp = Path(tmp)
    # --- with the example AUTO main server
    port = free_port()
    base = 'http://127.0.0.1:%d/maskit/v1' % port
    (tmp / 'A' / 'Link').mkdir(parents=True)
    (tmp / 'A' / 'Link' / 'link_config.json').write_text(json.dumps({
        'kit_id': 'KIT-UI', 'units': ['UI-1', 'UI-2', 'UI-3', 'UI-4'], 'server_url': base + '/bundles',
        'heartbeat_s': 0.3, 'active_poll_s': 0.1, 'retry_max_s': 0.5}), encoding='utf-8')
    srv = subprocess.Popen([sys.executable, str(ROOT / 'server_example' / 'maskit_main_server.py'), '--host', '127.0.0.1',
                            '--port', str(port), '--db', str(tmp / 'main.sqlite3'), '--rounds', '6', '--move-delay', '0.3'],
                           stdout=subprocess.PIPE, text=True)
    srv.stdout.readline()
    host = browser = None
    try:
        host, info, browser, errors, out = run_ui(tmp / 'A', play)
        jobs = get(base + '/jobs')
        assert len(jobs) == 1 and jobs[0]['status'] == 'end', jobs
        dec = get(base + '/jobs/%s/decisions' % jobs[0]['job_id'])
        assert dec and dec[-1]['action'] == 'lower_after_move' and all('case' in d for d in dec)
        rounds = [m for m in get(base + '/kits/KIT-UI/messages?session_id=' + jobs[0]['job_id']) if m['kind'] == 'round']
        assert len(rounds) == len(dec) and [u['unit_id'] for u in rounds[0]['units']] == ['UI-1', 'UI-2', 'UI-3', 'UI-4']
        assert out['verdict'] == '성공' and out['history'].count('서버 판단:') == len(dec), out['history']
        assert '하강 → 결합 성공' in out['history'] and '보정 이동' in out['decision']
        first_history = out['history']
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
        assert out['kit'].startswith('MASKIT-KIT-') and out['verdict'] == '성공'
        assert out['history'].count('서버 판단:') == first_history.count('서버 판단:'), 'same seed -> same job on the built-in site'
        assert not errors, errors
    finally:
        if browser: browser.close()
        if host: host.terminate(); host.wait(timeout=8)

print('PASS: UI 1 job on the virtual site: server decisions + machine moves shown, lower/lock success; '
      'also stand-alone with built-in sim server; no JS errors', flush=True)
