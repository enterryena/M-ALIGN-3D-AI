"""Kit link end-to-end with the main server directing every sensing.

A) manual server (exact control): machine ready -> start_job -> round 1 -> sense -> ...
   -> end_job; offline storage + ordered resend; wrong round / missing unit refused;
   sense_failed + re-sense; state kept across restart; stop.
B) auto server: 6 rounds driven by the server, then end_job.
C) no server_url: built-in LocalMainSim drives the job; bundles kept locally.
"""
import json, socket, subprocess, sys, tempfile, time, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / 'server_example' / 'malign_main_server.py'
UNITS = ['KIT-T01-U1', 'KIT-T01-U2', 'KIT-T01-U3', 'KIT-T01-U4']
CORR = {'x_mm': 1.0, 'y_mm': -1.0, 'theta_deg': 0.001}


def free_port():
    s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close(); return port


def call(url, data=None, headers=None):
    h = dict(headers or {})
    body = None
    if data is not None:
        h['Content-Type'] = 'application/json'
        body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers=h, method='POST' if body is not None else 'GET')
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.load(res)


def host(info, path, data=None):
    return call(info['url'] + 'api/' + path, data, {'X-MALIGN-Token': info['token']})


def host_err(info, path, data):
    try:
        host(info, path, data)
    except urllib.error.HTTPError as err:
        return err.code
    raise AssertionError('expected HTTP error for ' + path)


def start_host(data_dir):
    p = subprocess.Popen([sys.executable, str(ROOT / 'desktop_host.py'), '--headless', '--data-dir', str(data_dir)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    line = p.stdout.readline().strip()
    if not line:
        raise RuntimeError('host failed: ' + p.stderr.read())
    return p, json.loads(line)


def start_server(port, db, *extra):
    p = subprocess.Popen([sys.executable, str(SERVER), '--host', '127.0.0.1', '--port', str(port), '--db', str(db), *extra],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    if not p.stdout.readline().strip():
        raise RuntimeError('server failed: ' + p.stderr.read())
    return p


def stop(p):
    p.terminate(); p.wait(timeout=8)


def wait_for(fn, what, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError('timeout waiting for ' + what)


def units(base, est=1.0):
    return [{'sensor_uH': [base + k + h * 0.5 for h in range(5)], 'est_mm': [est, -est]} for k in range(4)]


def config(folder, kit, unit_ids, server_url, **kw):
    (folder / 'Link').mkdir(parents=True)
    (folder / 'Link' / 'link_config.json').write_text(json.dumps(dict({
        'kit_id': kit, 'units': unit_ids, 'server_url': server_url, 'heartbeat_s': 0.3, 'active_poll_s': 0.1,
        'retry_max_s': 0.5, 'timeout_s': 2}, **kw)), encoding='utf-8')


def pending(info, rnd=None):
    s = host(info, 'link/status')
    p = s['pending']
    return s if p and (rnd is None or p['round_no'] == rnd) else None


def run_sensing_source(info, timeout=30):
    """Stands in for the sensing source: answer every sense request until the job ends."""
    done, end = 0, time.time() + timeout
    while time.time() < end:
        s = host(info, 'link/status')
        if s['pending']:
            n = s['pending']['round_no']
            host(info, 'link/round', {'session_id': s['pending']['session_id'], 'round_no': n,
                                      'units': units(100 + n, 6 - n), 'correction': CORR})
            done = n
        elif done and s['state'] == 'IDLE':
            return s, done
        time.sleep(0.05)
    raise AssertionError('job did not finish')


with tempfile.TemporaryDirectory(prefix='malign_link_test_') as tmp:
    tmp = Path(tmp)
    # ------------------------------------------------------------------ A) manual server
    port = free_port()
    srv_url = 'http://127.0.0.1:%d/malign/v1' % port
    auth = {'Authorization': 'Bearer k1'}
    config(tmp / 'A', 'KIT-T01', UNITS, srv_url + '/bundles', api_key='k1')
    h, info = start_host(tmp / 'A')
    server = None
    try:
        st = host(info, 'link/status')
        assert st['state'] == 'IDLE' and st['units'] == UNITS and st['server_configured'] and st['pending'] is None

        # Server down: the event is stored, not lost.
        host(info, 'link/sim_machine_ready', {'machine_id': 'RS-07'})
        assert host_err(info, 'link/sim_machine_ready', {'machine_id': 'RS-07'}) == 409  # no duplicate ready
        wait_for(lambda: host(info, 'link/status')['uplink']['last_error'], 'send failure while server is down')
        assert host(info, 'link/status')['outbox']['pending'] == 1

        server = start_server(port, tmp / 'mainA.sqlite3', '--manual', '--api-key', 'k1')
        wait_for(lambda: host(info, 'link/status')['outbox']['pending'] == 0, 'resend after server up')
        assert host(info, 'link/status')['state'] == 'IDLE', 'manual server must not start by itself'

        # 1-3) machine ready (machine -> main server) -> start_job -> kit asked for round 1
        cmd = call(srv_url + '/machines/RS-07/ready', {'kit_id': 'KIT-T01'}, auth)
        assert cmd['type'] == 'start_job'
        st = wait_for(lambda: pending(info, 1), 'start_job -> sense round 1')
        sid = st['session_id']
        assert sid == cmd['job_id'] and st['state'] == 'SENSING'

        # 4) one sensing -> one bundle; validation
        assert host_err(info, 'link/round', {'session_id': sid, 'round_no': 2, 'units': units(1), 'correction': CORR}) == 400
        assert host_err(info, 'link/round', {'session_id': sid, 'round_no': 1, 'units': units(1)[:3], 'correction': CORR}) == 400
        assert host(info, 'link/round', {'session_id': sid, 'round_no': 1, 'units': units(101, 4.0),
                                         'correction': CORR})['state'] == 'WAITING'
        assert host_err(info, 'link/round', {'session_id': sid, 'round_no': 1, 'units': units(1), 'correction': CORR}) == 409

        # 5-7) wrong-round sense is refused; correct one asks for round 2
        call(srv_url + '/kits/KIT-T01/commands', {'type': 'sense', 'session_id': sid, 'round_no': 3}, auth)
        wait_for(lambda: (host(info, 'link/status')['last_command'] or {}).get('note', '').startswith('wrong_round'), 'refused sense')
        assert host(info, 'link/status')['state'] == 'WAITING'
        call(srv_url + '/kits/KIT-T01/commands', {'type': 'sense', 'session_id': sid, 'round_no': 2}, auth)
        wait_for(lambda: pending(info, 2), 'sense round 2')
        host(info, 'link/sense_failed', {'session_id': sid, 'round_no': 2, 'reason': 'unit 3 no signal'})
        call(srv_url + '/kits/KIT-T01/commands', {'type': 'sense', 'session_id': sid, 'round_no': 2}, auth)
        wait_for(lambda: pending(info, 2), 'sense round 2 again')
        host(info, 'link/round', {'session_id': sid, 'round_no': 2, 'units': units(102, 0.5), 'correction': CORR})
        st = host(info, 'link/status')
        assert st['last_assessment']['converged_hint'] is True and st['last_assessment']['max_est_mm'] == 0.5

        # 9) end_job -> kit idle; server sees the whole job
        call(srv_url + '/kits/KIT-T01/commands', {'type': 'end_job', 'session_id': sid, 'result': 'main_action'}, auth)
        wait_for(lambda: host(info, 'link/status')['state'] == 'IDLE', 'end_job')
        wait_for(lambda: host(info, 'link/status')['outbox']['pending'] == 0, 'all sent')
        st = host(info, 'link/status')
        assert st['last_job']['result'] == 'end' and st['last_job']['rounds'] == 2
        msgs = call(srv_url + '/kits/KIT-T01/messages?session_id=' + sid, headers=auth)
        rounds = [m for m in msgs if m['kind'] == 'round']
        assert [m['round_no'] for m in rounds] == [1, 2]
        assert [u['unit_id'] for u in rounds[0]['units']] == UNITS and rounds[0]['units'][2]['heads'][4] == 'Ref'
        assert rounds[0]['correction'] == CORR and rounds[0]['kit_assessment']['converged_hint'] is False
        events = [m['event'] for m in msgs if m['kind'] == 'event' and m['event'] != 'command_ack']
        assert events == ['job_start', 'sense_failed', 'job_end'], events
        job = [j for j in call(srv_url + '/jobs', headers=auth) if j['job_id'] == sid][0]
        assert job['status'] == 'end' and job['rounds'] == 2 and job['machine_id'] == 'RS-07'

        # Restart mid-job: state survives; then main server stops the job.
        cmd = call(srv_url + '/machines/RS-07/ready', {'kit_id': 'KIT-T01'}, auth)
        st = wait_for(lambda: pending(info, 1), 'second job')
        host(info, 'link/round', {'session_id': st['session_id'], 'round_no': 1, 'units': units(110), 'correction': CORR})
        wait_for(lambda: host(info, 'link/status')['outbox']['pending'] == 0, 'sent before restart')
    finally:
        stop(h)
    h, info = start_host(tmp / 'A')
    try:
        st = host(info, 'link/status')
        assert st['state'] == 'WAITING' and st['round_no'] == 1 and st['session_id'] == cmd['job_id']
        call(srv_url + '/kits/KIT-T01/commands', {'type': 'stop', 'reason': 'operator'}, auth)
        wait_for(lambda: host(info, 'link/status')['state'] == 'IDLE', 'stop')
        assert host(info, 'link/status')['last_job']['result'] == 'stopped'
        wait_for(lambda: [j['status'] for j in call(srv_url + '/jobs', headers=auth)] == ['end', 'stopped'], 'server job status')
    finally:
        stop(h)
        stop(server)

    # ------------------------------------------------------------------ B) auto server: 6 rounds
    port = free_port()
    srv_url = 'http://127.0.0.1:%d/malign/v1' % port
    server = start_server(port, tmp / 'mainB.sqlite3', '--rounds', '6', '--move-delay', '0.2')
    config(tmp / 'B', 'KIT-T02', ['B-1', 'B-2', 'B-3', 'B-4'], srv_url + '/bundles')
    h, info = start_host(tmp / 'B')
    try:
        host(info, 'link/sim_machine_ready', {'machine_id': 'RS-01'})
        st, done = run_sensing_source(info)
        assert done == 6 and st['last_job']['result'] == 'end' and st['last_job']['rounds'] == 6
        wait_for(lambda: host(info, 'link/status')['outbox']['pending'] == 0, 'auto job sent')
        msgs = call(srv_url + '/kits/KIT-T02/messages?session_id=' + st['last_job']['session_id'])
        assert [m['round_no'] for m in msgs if m['kind'] == 'round'] == [1, 2, 3, 4, 5, 6]
        assert call(srv_url + '/jobs')[0]['status'] == 'end'
    finally:
        stop(h)
        stop(server)

    # ------------------------------------------------------------------ C) no server: LocalMainSim
    config(tmp / 'C', 'KIT-T03', ['C-1', 'C-2', 'C-3', 'C-4'], '', sim_rounds=3, sim_move_delay_s=0.05)
    h, info = start_host(tmp / 'C')
    try:
        host(info, 'link/sim_machine_ready', {})
        st, done = run_sensing_source(info)
        assert done == 3 and st['last_job']['result'] == 'end'
        assert st['outbox']['pending'] > 0 and st['outbox']['sent'] == 0, 'store-only mode keeps everything locally'
    finally:
        stop(h)

print('PASS: server-directed jobs (manual/auto/local), 4-unit bundle per sensing, offline store + resend, '
      'sense_failed/re-sense, restart-safe, stop', flush=True)
