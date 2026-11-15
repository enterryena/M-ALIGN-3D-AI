"""Kit = field device: 7 commands from the main server, 4 units x 23 items per sensing.

A) manual server: offline store + ordered resend; start_job -> sense / resense -> end_job;
   23 items per unit; measurement_id; sense_failed; self_test pass/fail -> FAULT ->
   fault_reset; unit fault aborts the job; watchdog -> FAULT; restart-safe.
B) auto server: virtual site (rule-based brain + virtual reach stacker) in the example server.
C) no server_url: built-in virtual site; same seed -> same result as B; bundles kept locally.
"""
import json, socket, subprocess, sys, tempfile, time, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import malign_engine as E  # noqa: E402

SERVER = ROOT / 'server_example' / 'malign_main_server.py'
UNITS = ['KIT-T01-U1', 'KIT-T01-U2', 'KIT-T01-U3', 'KIT-T01-U4']
M = E.Model()
DIAG = {'coil_temp_c': 26.5, 'supply_v': 23.9, 'fw_cal_version': 'TEST-1', 'error_code': 0}


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


class SimUnits:
    """Stands in for the 4 coil readers: readings of a simulated container corner set."""

    def __init__(self, seed=5):
        self.world = E.World(E.settings('target', 'history'), E.Rng(seed))

    def read(self):
        return [{'sensor_uH': self.world.read_corner(M, k), 'diag': DIAG} for k in range(4)]


def config(folder, kit, unit_ids, server_url, **kw):
    (folder / 'Link').mkdir(parents=True)
    (folder / 'Link' / 'link_config.json').write_text(json.dumps(dict({
        'kit_id': kit, 'units': unit_ids, 'server_url': server_url, 'heartbeat_s': 0.3, 'active_poll_s': 0.1,
        'retry_max_s': 0.5, 'timeout_s': 2}, **kw)), encoding='utf-8')


def pending(info, rnd=None, resense=None):
    s = host(info, 'link/status')
    p = s['pending']
    if p and p['type'] == 'sense' and (rnd is None or p['round_no'] == rnd) and (resense is None or p['resense'] == resense):
        return s
    return None


def state_is(info, state):
    return (lambda: (lambda s: s if s['state'] == state else None)(host(info, 'link/status')))


def run_sensing_source(info, units, timeout=40):
    done, end = 0, time.time() + timeout
    while time.time() < end:
        s = host(info, 'link/status')
        p = s['pending']
        if p and p['type'] == 'sense':
            host(info, 'link/round', {'session_id': s['session_id'], 'round_no': p['round_no'], 'units': units.read()})
            done = p['round_no']
        elif done and s['state'] == 'IDLE':
            return s, done
        time.sleep(0.05)
    raise AssertionError('job did not finish')


def count_items(u):
    ids = ['unit_id', 'corner', 'job_id', 'round_no', 'measurement_id', 'measured_at']
    n = sum(1 for k in ids if u.get(k) is not None)
    n += len(u['sensor_uH'])
    n += (2 if u['est_mm'] else 0) + (1 if u['gap_mm'] is not None else 0)
    n += sum(1 for k in ('rss_uH2', 'extrapolated', 'gap_out_of_range', 'head_ok', 'grade') if k in u['quality'])
    n += sum(1 for k in ('coil_temp_c', 'supply_v', 'fw_cal_version', 'error_code') if u['diag'].get(k) is not None)
    return n


with tempfile.TemporaryDirectory(prefix='malign_link_test_') as tmp:
    tmp = Path(tmp)
    # ------------------------------------------------------------------ A) manual server
    port = free_port()
    srv_url = 'http://127.0.0.1:%d/malign/v1' % port
    auth = {'Authorization': 'Bearer k1'}
    cmd_url = srv_url + '/kits/KIT-T01/commands'
    config(tmp / 'A', 'KIT-T01', UNITS, srv_url + '/bundles', api_key='k1', watchdog_s=4.0, sim_auto_sensing=False)
    units = SimUnits()
    h, info = start_host(tmp / 'A')
    server = None
    try:
        st = host(info, 'link/status')
        assert st['state'] == 'IDLE' and st['units'] == UNITS and st['server_configured'] and st['pending'] is None

        # Server down: stored, not lost; no duplicate "ready".
        host(info, 'link/sim_machine_ready', {'machine_id': 'RS-07'})
        assert host_err(info, 'link/sim_machine_ready', {'machine_id': 'RS-07'}) == 409
        wait_for(lambda: host(info, 'link/status')['uplink']['last_error'], 'send failure while server is down')
        assert host(info, 'link/status')['outbox']['pending'] == 1

        server = start_server(port, tmp / 'mainA.sqlite3', '--manual', '--api-key', 'k1')
        wait_for(lambda: host(info, 'link/status')['outbox']['pending'] == 0, 'resend after server up')
        assert host(info, 'link/status')['state'] == 'IDLE', 'manual server must not start by itself'

        # start_job -> round 1
        cmd = call(srv_url + '/machines/RS-07/ready', {'kit_id': 'KIT-T01'}, auth)
        st = wait_for(lambda: pending(info, 1, False), 'start_job -> sense round 1')
        sid = st['session_id']
        assert sid == cmd['job_id'] and st['state'] == 'SENSING'
        assert host_err(info, 'link/round', {'session_id': sid, 'round_no': 2, 'units': units.read()}) == 400
        assert host_err(info, 'link/round', {'session_id': sid, 'round_no': 1, 'units': units.read()[:3]}) == 400
        r1 = host(info, 'link/round', {'session_id': sid, 'round_no': 1, 'units': units.read()})
        assert r1['state'] == 'WAITING' and r1['measurement_id'] == 1 and len(r1['units']) == 4
        assert host_err(info, 'link/round', {'session_id': sid, 'round_no': 1, 'units': units.read()}) == 409

        # resense: same round, new measurement id
        call(cmd_url, {'type': 'resense', 'session_id': sid, 'round_no': 1}, auth)
        wait_for(lambda: pending(info, 1, True), 'resense round 1')
        assert host(info, 'link/round', {'session_id': sid, 'round_no': 1, 'units': units.read()})['measurement_id'] == 2

        # wrong-round sense refused; sense round 2 fails once, then succeeds
        call(cmd_url, {'type': 'sense', 'session_id': sid, 'round_no': 3}, auth)
        wait_for(lambda: (host(info, 'link/status')['last_command'] or {}).get('note', '').startswith('wrong_round'), 'refused sense')
        call(cmd_url, {'type': 'sense', 'session_id': sid, 'round_no': 2}, auth)
        wait_for(lambda: pending(info, 2, False), 'sense round 2')
        host(info, 'link/sense_failed', {'session_id': sid, 'round_no': 2, 'reason': 'unit 3 no signal'})
        assert host(info, 'link/status')['state'] == 'WAITING'
        call(cmd_url, {'type': 'sense', 'session_id': sid, 'round_no': 2}, auth)
        wait_for(lambda: pending(info, 2), 'sense round 2 again')
        assert host(info, 'link/round', {'session_id': sid, 'round_no': 2, 'units': units.read()})['measurement_id'] == 3

        call(cmd_url, {'type': 'end_job', 'session_id': sid, 'result': 'main_action'}, auth)
        wait_for(state_is(info, 'IDLE'), 'end_job')
        wait_for(lambda: host(info, 'link/status')['outbox']['pending'] == 0, 'all sent')
        msgs = call(srv_url + '/kits/KIT-T01/messages?session_id=' + sid, headers=auth)
        rounds = [m for m in msgs if m['kind'] == 'round']
        assert [(m['round_no'], m['measurement_id'], m['resense']) for m in rounds] == [(1, 1, False), (1, 2, True), (2, 3, False)]
        u = rounds[0]['units'][0]
        assert [x['unit_id'] for x in rounds[0]['units']] == UNITS
        assert count_items(u) == 23, (count_items(u), u)
        assert u['quality']['grade'] in ('good', 'caution', 'invalid') and u['diag']['fw_cal_version'] == 'TEST-1'
        assert rounds[0]['kit_reference']['available'] is True and 'fit_theta_deg' in rounds[0]['kit_reference']
        events = [m['event'] for m in msgs if m['kind'] == 'event' and m['event'] != 'command_ack']
        assert events == ['job_start', 'sense_failed', 'job_end'], events
        kit_row = call(srv_url + '/kits', headers=auth)[0]
        assert kit_row['heartbeat_seq'] > 3 and kit_row['fault_code'] == 0

        # self_test pass -> back to IDLE
        call(cmd_url, {'type': 'self_test'}, auth)
        st = wait_for(lambda: (lambda s: s if (s['pending'] or {}).get('type') == 'self_test' else None)(host(info, 'link/status')), 'self_test request')
        assert st['state'] == 'SELF_TEST'
        host(info, 'link/self_test_result', {'results': [{'ok': True}] * 4})
        assert host(info, 'link/status')['state'] == 'IDLE'
        # self_test fail -> FAULT; start refused until fault_reset
        call(cmd_url, {'type': 'self_test'}, auth)
        wait_for(state_is(info, 'SELF_TEST'), 'self_test 2')
        host(info, 'link/self_test_result', {'results': [{'ok': True}, {'ok': True}, {'ok': False, 'error_code': 20, 'detail': 'Ym open'}, {'ok': True}]})
        st = host(info, 'link/status')
        assert st['state'] == 'FAULT' and st['fault_code'] == 40 and 'Ym open' in st['fault_detail']
        call(srv_url + '/machines/RS-07/ready', {'kit_id': 'KIT-T01'}, auth)
        wait_for(lambda: (host(info, 'link/status')['last_command'] or {}).get('note') == 'busy:FAULT', 'start refused in FAULT')
        call(cmd_url, {'type': 'stop'}, auth)            # server cancels the refused job on its side
        call(cmd_url, {'type': 'fault_reset'}, auth)
        wait_for(state_is(info, 'IDLE'), 'fault_reset')

        # unit fault during a job -> job aborted + FAULT
        call(srv_url + '/machines/RS-07/ready', {'kit_id': 'KIT-T01'}, auth)
        st = wait_for(lambda: pending(info, 1), 'job 2')
        host(info, 'link/fault', {'code': 30, 'detail': 'unit 4 no response'})
        st = host(info, 'link/status')
        assert st['state'] == 'FAULT' and st['fault_code'] == 30 and st['last_job']['result'] == 'aborted'
        call(cmd_url, {'type': 'fault_reset'}, auth)
        wait_for(state_is(info, 'IDLE'), 'fault_reset 2')

        # watchdog: server disappears mid-job -> FAULT 50
        call(srv_url + '/machines/RS-07/ready', {'kit_id': 'KIT-T01'}, auth)
        st = wait_for(lambda: pending(info, 1), 'job 3')
        host(info, 'link/round', {'session_id': st['session_id'], 'round_no': 1, 'units': units.read()})
        wait_for(lambda: host(info, 'link/status')['outbox']['pending'] == 0, 'sent before server stop')
        stop(server)
        st = wait_for(state_is(info, 'FAULT'), 'watchdog', timeout=10)
        assert st['fault_code'] == 50 and st['last_job']['result'] == 'aborted'
        server = start_server(port, tmp / 'mainA.sqlite3', '--manual', '--api-key', 'k1')
        wait_for(lambda: host(info, 'link/status')['outbox']['pending'] == 0, 'fault events delivered after reconnect')
        ev = [m['event'] for m in call(srv_url + '/kits/KIT-T01/messages', headers=auth) if m['kind'] == 'event']
        assert 'fault' in ev and ev.count('job_aborted') == 2
        call(cmd_url, {'type': 'stop'}, auth)
        call(cmd_url, {'type': 'fault_reset'}, auth)
        wait_for(state_is(info, 'IDLE'), 'fault_reset 3')

        # restart mid-job: state survives
        cmd = call(srv_url + '/machines/RS-07/ready', {'kit_id': 'KIT-T01'}, auth)
        st = wait_for(lambda: pending(info, 1), 'job 4')
        host(info, 'link/round', {'session_id': st['session_id'], 'round_no': 1, 'units': units.read()})
        wait_for(lambda: host(info, 'link/status')['outbox']['pending'] == 0, 'sent before restart')
    finally:
        stop(h)
    h, info = start_host(tmp / 'A')
    try:
        st = host(info, 'link/status')
        assert st['state'] == 'WAITING' and st['round_no'] == 1 and st['measurement_id'] == 1 and st['session_id'] == cmd['job_id']
        call(cmd_url, {'type': 'stop', 'reason': 'operator'}, auth)
        wait_for(state_is(info, 'IDLE'), 'stop')
        assert host(info, 'link/status')['last_job']['result'] == 'stopped'
    finally:
        stop(h)
        stop(server)

    # ------------------------------------------------------------------ B) auto server: virtual site in the server
    port = free_port()
    srv_url = 'http://127.0.0.1:%d/malign/v1' % port
    server = start_server(port, tmp / 'mainB.sqlite3', '--rounds', '6', '--move-delay', '0.1')
    config(tmp / 'B', 'KIT-T02', ['B-1', 'B-2', 'B-3', 'B-4'], srv_url + '/bundles')
    h, info = start_host(tmp / 'B')
    try:
        st = host(info, 'sim/start', {'hardware': 'target', 'machine': 'normal', 'seed': 123})
        assert st['active'] and st['phase'] == 'approach'
        assert host_err(info, 'sim/start', {'seed': 1}) == 409, 'one virtual job at a time'
        st = wait_for(lambda: (lambda s: s if s.get('outcome') and not s['active'] else None)(host(info, 'sim/state')), 'virtual job', 40)
        assert st['outcome'] == 'success' and 1 <= st['rounds'] <= 7, st['outcome']
        wait_for(lambda: host(info, 'link/status')['state'] == 'IDLE' and host(info, 'link/status')['outbox']['pending'] == 0, 'kit idle')
        job = call(srv_url + '/jobs')[0]
        assert job['job_id'] == st['job_id'] and job['status'] == 'end'
        dec = call(srv_url + '/jobs/%s/decisions' % job['job_id'])
        assert dec and dec[0]['case'] in ('P1', 'P4') and dec[-1]['action'] == 'lower_after_move'
        rounds = [m for m in call(srv_url + '/kits/KIT-T02/messages?session_id=' + job['job_id']) if m['kind'] == 'round']
        assert len(rounds) == len(dec) and all(count_items(u) == 23 for u in rounds[0]['units'])
    finally:
        stop(h)
        stop(server)

    # ------------------------------------------------------------------ C) no server: built-in virtual site
    config(tmp / 'C', 'KIT-T03', ['C-1', 'C-2', 'C-3', 'C-4'], '', sim_move_delay_s=0.05)
    h, info = start_host(tmp / 'C')
    try:
        host(info, 'sim/start', {'hardware': 'target', 'machine': 'normal', 'seed': 123})
        st2 = wait_for(lambda: (lambda s: s if s.get('outcome') and not s['active'] else None)(host(info, 'sim/state')), 'local virtual job', 40)
        assert (st2['outcome'], st2['rounds'], round(st2['truth_worst_mm'], 9)) == (st['outcome'], st['rounds'], round(st['truth_worst_mm'], 9)), \
            'built-in and example server give the same result for the same seed'
        ls = wait_for(lambda: (lambda s: s if s['state'] == 'IDLE' and s['last_job'] else None)(host(info, 'link/status')), 'kit idle')
        assert ls['outbox']['pending'] > 0 and ls['outbox']['sent'] == 0, 'store-only mode keeps everything locally'
        assert ls['fault_code'] == 0, 'no watchdog without a server'
    finally:
        stop(h)

print('PASS: field-device kit — 7 commands, 23 items/unit, measurement ids, resense, self-test, FAULT/reset, '
      'watchdog, offline resend, restart-safe; server-side fit in auto mode; local sim', flush=True)
