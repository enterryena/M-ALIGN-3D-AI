"""Kit link end-to-end: per-round 4-unit bundle -> local outbox -> main server.

Covers: store while the server is down, ordered resend after it comes up,
duplicate-safe delivery, round-order validation, HOLD movement detection,
STOPPED until the main server sends 'realign', and state kept across restart.
"""
import json, socket, subprocess, sys, tempfile, time, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / 'server_example' / 'malign_main_server.py'
UNITS = ['KIT-T01-U1', 'KIT-T01-U2', 'KIT-T01-U3', 'KIT-T01-U4']


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def call(url, data=None, headers=None, method=None):
    h = dict(headers or {})
    body = None
    if data is not None:
        h['Content-Type'] = 'application/json'
        body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers=h, method=method or ('POST' if body else 'GET'))
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.load(res)


def host(info, path, data=None):
    return call(info['url'] + 'api/' + path, data, {'X-MALIGN-Token': info['token']})


def host_err(info, path, data):
    try:
        host(info, path, data)
    except urllib.error.HTTPError as err:
        return err.code, json.load(err)
    raise AssertionError('expected HTTP error for ' + path)


def start_host(data_dir):
    p = subprocess.Popen([sys.executable, str(ROOT / 'desktop_host.py'), '--headless', '--data-dir', str(data_dir)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    line = p.stdout.readline().strip()
    if not line:
        raise RuntimeError('host failed: ' + p.stderr.read())
    return p, json.loads(line)


def start_server(port, db):
    p = subprocess.Popen([sys.executable, str(SERVER), '--host', '127.0.0.1', '--port', str(port), '--db', str(db),
                          '--api-key', 'k1'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    if not p.stdout.readline().strip():
        raise RuntimeError('server failed: ' + p.stderr.read())
    return p


def stop(p):
    p.terminate()
    p.wait(timeout=8)


def wait_for(fn, what, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.15)
    raise AssertionError('timeout waiting for ' + what)


def units(base, est=1.0):
    return [{'sensor_uH': [base + k + h * 0.5 for h in range(5)], 'est_mm': [est, -est]} for k in range(4)]


with tempfile.TemporaryDirectory(prefix='malign_link_test_') as tmp:
    tmp = Path(tmp)
    port = free_port()
    srv_url = 'http://127.0.0.1:%d/malign/v1' % port
    auth = {'Authorization': 'Bearer k1'}
    (tmp / 'Link').mkdir()
    (tmp / 'Link' / 'link_config.json').write_text(json.dumps({
        'kit_id': 'KIT-T01', 'units': UNITS, 'server_url': srv_url + '/bundles', 'api_key': 'k1',
        'heartbeat_s': 0.3, 'retry_max_s': 0.5, 'timeout_s': 2,
        'movement_threshold_uH': 1.0, 'movement_confirm': 2}), encoding='utf-8')

    h, info = start_host(tmp)
    server = None
    try:
        assert info['kit_id'] == 'KIT-T01'
        st = host(info, 'link/status')
        assert st['state'] == 'IDLE' and st['units'] == UNITS and st['server_configured']

        # 1) Main server is DOWN: rounds are still stored locally (pending).
        s1 = host(info, 'link/begin', {'source': 'simulation'})['session_id']
        for r in range(1, 4):
            assert host(info, 'link/round', {'session_id': s1, 'round_no': r, 'units': units(100 + r, 4 - r),
                                             'command': {'x_mm': 1, 'y_mm': -1, 'theta_deg': 0.001}})['round_no'] == r
        code, _ = host_err(info, 'link/round', {'session_id': s1, 'round_no': 3, 'units': units(1),
                                                'command': {'x_mm': 0, 'y_mm': 0, 'theta_deg': 0}})
        assert code == 400, 'repeated/out-of-order round must be refused'
        code, _ = host_err(info, 'link/round', {'session_id': s1, 'round_no': 4, 'units': units(1)[:3],
                                                'command': {'x_mm': 0, 'y_mm': 0, 'theta_deg': 0}})
        assert code == 400, 'a bundle must contain all 4 units'
        host(info, 'link/finish', {'session_id': s1, 'converged': True})
        st = wait_for(lambda: (lambda s: s if s['uplink']['last_error'] else None)(host(info, 'link/status')),
                      'send failure while server is down')
        assert st['state'] == 'HOLD' and st['outbox']['pending'] == 5 and st['outbox']['sent'] == 0  # start+3 rounds+aligned

        # 2) Server comes up: everything is delivered in order, nothing lost.
        server = start_server(port, tmp / 'main.sqlite3')
        wait_for(lambda: host(info, 'link/status')['outbox']['pending'] == 0, 'resend after server up')
        msgs = call(srv_url + '/kits/KIT-T01/messages?session_id=' + s1, headers=auth)
        rounds = [m for m in msgs if m['kind'] == 'round']
        assert [m['round_no'] for m in rounds] == [1, 2, 3]
        assert [u['unit_id'] for u in rounds[0]['units']] == UNITS
        assert rounds[0]['units'][0]['heads'] == ['Xp', 'Xm', 'Yp', 'Ym', 'Ref'] and len(rounds[0]['units'][3]['sensor_uH']) == 5
        assert [m['event'] for m in msgs if m['kind'] == 'event'] == ['cycle_start', 'aligned']

        # 3) HOLD: small change is not movement; big change twice = movement -> STOPPED.
        assert host(info, 'link/hold', {'units': units(103.2)})['moved'] is False
        assert host(info, 'link/hold', {'units': units(110)})['moved'] is False   # 1st hit only
        r = host(info, 'link/hold', {'units': units(110)})
        assert r['moved'] is True and r['state'] == 'STOPPED'
        code, body = host_err(info, 'link/begin', {'source': 'simulation'})
        assert code == 409 and body['state'] == 'STOPPED', 'must wait for main-server command'
        wait_for(lambda: any(m.get('event') == 'movement_detected'
                             for m in call(srv_url + '/kits/KIT-T01/messages', headers=auth)), 'movement event on server')

        # 4) Main server sends 'realign' -> kit may start a new cycle.
        cmd = call(srv_url + '/kits/KIT-T01/commands', {'type': 'realign'}, auth)
        wait_for(lambda: host(info, 'link/status')['realign_requested'], 'realign command delivery')
        s2 = host(info, 'link/begin', {'source': 'simulation'})
        assert s2['state'] == 'ALIGNING' and s2['cycle_no'] == 2 and s2['session_id'] != s1
        wait_for(lambda: any(m.get('event') == 'command_ack' and m['detail']['command_id'] == cmd['id']
                             for m in call(srv_url + '/kits/KIT-T01/messages', headers=auth)), 'command ack')
        host(info, 'link/round', {'session_id': s2['session_id'], 'round_no': 1, 'units': units(120),
                                  'command': {'x_mm': 0, 'y_mm': 0, 'theta_deg': 0}})
    finally:
        stop(h)

    # 5) Restart: state and stored bundles survive; the cycle can be finished.
    h, info = start_host(tmp)
    try:
        st = host(info, 'link/status')
        assert st['state'] == 'ALIGNING' and st['round_no'] == 1 and st['cycle_no'] == 2
        host(info, 'link/finish', {'session_id': st['session_id'], 'converged': False})
        wait_for(lambda: host(info, 'link/status')['outbox']['pending'] == 0, 'final send')
        kits = call(srv_url + '/kits', headers=auth)
        assert kits[0]['kit_id'] == 'KIT-T01' and kits[0]['rounds'] == 4
        assert host(info, 'link/status')['stop_reason'] == 'align_failed'
    finally:
        stop(h)
        if server:
            stop(server)

print('PASS: 4-unit round bundles stored offline, resent in order, movement->STOPPED, server realign, restart-safe', flush=True)
