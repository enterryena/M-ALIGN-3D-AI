# -*- coding: utf-8 -*-
"""MAS-kit kit link: the kit is a field device — it reports to the main server and acts on its commands.

One kit = 4 correction units (one per container corner), each with its own unique
number. The machine (e.g. reach stacker) and every decision about moving it belong to
the MAIN SERVER; the kit only senses and reports.

Job sequence:
  machine ready (machine -> server) -> start_job -> kit senses round 1 -> bundle
  -> server compares with the machine's sensors, decides and applies the correction,
     confirms the move -> sense (or resense: same round, no move) -> bundle ... (~6)
  -> end_job -> main action (lower + lock)

Commands (server -> kit), 7:  start_job, sense, resense, end_job, stop, fault_reset, self_test
Information (kit -> server), 5: command_ack, measurement bundle (4 units x 23 items),
                                state, heartbeat (every upload), error code

Kit states:
  IDLE --start_job--> SENSING --bundle--> WAITING --sense|resense--> SENSING ...
  WAITING|SENSING --end_job|stop--> IDLE
  IDLE|FAULT --self_test--> SELF_TEST --pass--> previous state | --fail--> FAULT
  any --fault (unit fault, self-test fail, watchdog)--> FAULT --fault_reset--> IDLE

Per unit (23 items): ids 6 (unit_id, corner, job, round, measurement_id, measured_at),
  readings 5 (dL uH vs calibration), estimate 3 (x, y, gap), reliability 5 (residual,
  extrapolated, gap out of range, head_ok flags, grade), diagnostics 4 (coil temp,
  supply voltage, fw/cal version, error code).
Each unit computes ONLY its own corner (kit_device.py). Combining the 4 corners,
priorities and the final correction are the main server's job; the kit's 4-corner
fit is sent as reference only.

Store-and-forward: every bundle/event is first committed to a local SQLite outbox,
then a background uploader POSTs pending ones in order and receives commands in the
reply. Watchdog: during a job, no successful server exchange for watchdog_s -> FAULT.
In simulation (source 'simulation') a virtual site (sim_bridge / sim_site) provides the
coil readings; with no server_url it also plays the main server and the reach stacker.
"""
import json
import logging
import math
import pathlib
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA_DB = 'MASKIT_link_outbox_v1'
SCHEMA_ROUND = 'MASKIT_round_bundle_v2'
SCHEMA_EVENT = 'MASKIT_kit_event_v1'
SCHEMA_BATCH = 'MASKIT_upload_batch_v2'
HEADS = ['Xp', 'Xm', 'Yp', 'Ym', 'Ref']
# Same corner order as the simulator's CORNERS array (twistlock centres, mm).
CORNER_POS_MM = [(-2926.5, 1129.5), (2926.5, 1129.5), (2926.5, -1129.5), (-2926.5, -1129.5)]
STATES = ('IDLE', 'SENSING', 'WAITING', 'SELF_TEST', 'FAULT')
JOB_STATES = ('SENSING', 'WAITING')
SOURCES = ('simulation', 'hardware')
COMMANDS = ('start_job', 'sense', 'resense', 'end_job', 'stop', 'fault_reset', 'self_test')
GRADES = ('good', 'caution', 'invalid')
ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
CONVERGED_MM = 1.0  # kit's own hint only; the main server decides when to finish
# Error codes (unit diag.error_code and kit fault_code)
ERRORS = {0: 'none', 10: 'sense_failed', 20: 'head_fault', 30: 'unit_no_response',
          40: 'self_test_failed', 50: 'watchdog_timeout', 90: 'other'}

DEFAULT_CONFIG = {
    'kit_id': None,            # generated once on first run; edit to the real kit number
    'units': None,             # 4 unique unit numbers in corner order 1..4
    'source': 'simulation',    # 'hardware' once the real sensor reader is attached
    'server_url': '',          # e.g. http://10.0.0.5:8750/maskit/v1/bundles ; '' = store only
    'api_key': '',             # sent as "Authorization: Bearer <api_key>" when set
    'max_rounds': 10,          # safety cap per job (the main server normally ends after ~6)
    'max_resense': 3,          # safety cap of re-measurements per round
    'batch_max': 50,
    'timeout_s': 5,
    'heartbeat_s': 2,          # upload/poll interval while idle (also the heartbeat)
    'active_poll_s': 0.5,      # upload/poll interval during a job
    'retry_max_s': 30,
    'watchdog_s': 10,          # during a job: no successful server exchange this long -> FAULT
    'sim_rounds': 6,           # built-in virtual site (no server_url): max correction rounds
    'sim_move_delay_s': 0.8,   # virtual site: time the machine takes for one correction move
    'sim_auto_sensing': True,  # simulation: the kit's coil readers answer from the virtual site
    'ai_ollama_url': 'http://127.0.0.1:11434',   # built-in virtual site: AI main server (Ollama, local, free)
    'ai_model': 'qwen2.5:7b',
}


class LinkRefused(Exception):
    """Request is valid JSON but not allowed in the current kit state (HTTP 409)."""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _finite(v, what):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(what + ' 값이 유한한 숫자가 아닙니다')
    return float(v)


# --------------------------------------------------------------------------- config
def load_config(folder):
    """Load link_config.json; create it (never overwrite user values) with fresh unique numbers."""
    folder = pathlib.Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'link_config.json'
    if path.exists():
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(raw, dict):
            raise ValueError('link_config.json 형식 오류 (JSON 객체 아님)')
    else:
        raw = {}
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
    created = not path.exists()
    if not cfg['kit_id']:
        cfg['kit_id'] = 'MASKIT-KIT-' + secrets.token_hex(4).upper()
        created = True
    if not cfg['units']:
        cfg['units'] = [cfg['kit_id'] + '-U' + str(i) for i in range(1, 5)]
        created = True
    validate_config(cfg)
    if created:
        keep = {k: v for k, v in raw.items() if k not in DEFAULT_CONFIG}  # never drop unknown user keys
        tmp = path.with_suffix('.json.new')
        tmp.write_text(json.dumps(dict(keep, **cfg), ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(path)
    return cfg, path


def validate_config(cfg):
    if not isinstance(cfg['kit_id'], str) or not ID_RE.match(cfg['kit_id']):
        raise ValueError('kit_id 형식 오류 (영문/숫자/._- 64자 이내)')
    units = cfg['units']
    if not isinstance(units, list) or len(units) != 4 or not all(isinstance(u, str) and ID_RE.match(u) for u in units):
        raise ValueError('units 는 코너 1~4 순서의 장치 번호 4개여야 합니다')
    if len(set(units)) != 4:
        raise ValueError('units 장치 번호 4개는 서로 달라야 합니다')
    if cfg['source'] not in SOURCES:
        raise ValueError("source 는 'simulation' 또는 'hardware'")
    url = cfg['server_url']
    if url:
        p = urllib.parse.urlparse(url)
        if p.scheme not in ('http', 'https') or not p.netloc:
            raise ValueError('server_url 은 http:// 또는 https:// 주소여야 합니다')
    for key, lo, hi in (('max_rounds', 1, 50), ('max_resense', 0, 20), ('batch_max', 1, 500), ('sim_rounds', 1, 50)):
        if not isinstance(cfg[key], int) or isinstance(cfg[key], bool) or not lo <= cfg[key] <= hi:
            raise ValueError('%s 범위 오류 (%d~%d)' % (key, lo, hi))
    if cfg['sim_rounds'] > cfg['max_rounds']:
        raise ValueError('sim_rounds 는 max_rounds 이하여야 합니다')
    for key in ('timeout_s', 'heartbeat_s', 'active_poll_s', 'retry_max_s', 'watchdog_s'):
        if _finite(cfg[key], key) <= 0:
            raise ValueError(key + ' 는 0보다 커야 합니다')
    for key in ('ai_ollama_url', 'ai_model'):
        if not isinstance(cfg[key], str) or not cfg[key] or len(cfg[key]) > 200:
            raise ValueError(key + ' 형식 오류')
    if not isinstance(cfg['sim_auto_sensing'], bool):
        raise ValueError('sim_auto_sensing 은 true/false')
    if _finite(cfg['sim_move_delay_s'], 'sim_move_delay_s') < 0:
        raise ValueError('sim_move_delay_s 는 0 이상')
    return cfg


# --------------------------------------------------------------------------- outbox
class Outbox:
    """Durable local store of every bundle/event, with send status."""

    def __init__(self, file_path):
        self.file_path = pathlib.Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            con.execute('''CREATE TABLE IF NOT EXISTS outbox (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_id TEXT UNIQUE NOT NULL,
                kind TEXT NOT NULL,
                session_id TEXT,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','rejected')),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                sent_at TEXT)''')
            con.execute('CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, seq)')
            con.execute('CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            con.execute("INSERT OR IGNORE INTO kv(key,value) VALUES ('schema', ?)", (json.dumps(SCHEMA_DB),))
            row = con.execute("SELECT value FROM kv WHERE key='schema'").fetchone()
            if json.loads(row[0]) != SCHEMA_DB:
                raise RuntimeError('지원하지 않는 전송 보관함 형식입니다. 기존 파일을 변경하지 않고 중단합니다.')

    @contextmanager
    def connection(self):
        # Always close the OS handle (Windows file-lock lesson from v3.5.3/3.5.4).
        con = sqlite3.connect(str(self.file_path), timeout=30)
        try:
            con.execute('PRAGMA busy_timeout=30000')
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('PRAGMA synchronous=FULL')
            with con:
                yield con
        finally:
            con.close()

    def enqueue(self, msg):
        payload = json.dumps(msg, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        with self.connection() as con:
            n0 = con.total_changes
            con.execute('INSERT OR IGNORE INTO outbox(msg_id,kind,session_id,created_at,payload) VALUES(?,?,?,?,?)',
                        (msg['msg_id'], msg['kind'], msg.get('session_id'), msg['created_at'], payload))
            return con.total_changes > n0

    def pending(self, limit):
        with self.connection() as con:
            rows = con.execute("SELECT msg_id, payload FROM outbox WHERE status='pending' ORDER BY seq LIMIT ?",
                               (int(limit),)).fetchall()
        return [(m, json.loads(p)) for m, p in rows]

    def mark_sent(self, msg_ids):
        if not msg_ids:
            return
        now = utc_now()
        with self.connection() as con:
            con.executemany("UPDATE outbox SET status='sent', sent_at=?, attempts=attempts+1, last_error=NULL "
                            "WHERE msg_id=? AND status='pending'", [(now, m) for m in msg_ids])

    def mark_rejected(self, items):
        with self.connection() as con:
            con.executemany("UPDATE outbox SET status='rejected', attempts=attempts+1, last_error=? "
                            "WHERE msg_id=? AND status='pending'", [(str(e)[:500], m) for m, e in items])

    def mark_failed(self, msg_ids, error):
        with self.connection() as con:
            con.executemany("UPDATE outbox SET attempts=attempts+1, last_error=? WHERE msg_id=? AND status='pending'",
                            [(str(error)[:500], m) for m in msg_ids])

    def counts(self):
        with self.connection() as con:
            c = dict(con.execute('SELECT status, COUNT(*) FROM outbox GROUP BY status').fetchall())
            last = con.execute("SELECT MAX(sent_at) FROM outbox WHERE status='sent'").fetchone()[0]
        return {'pending': c.get('pending', 0), 'sent': c.get('sent', 0),
                'rejected': c.get('rejected', 0), 'last_sent_at': last}

    def messages(self, session_id=None, limit=1000):
        with self.connection() as con:
            if session_id:
                rows = con.execute('SELECT payload, status FROM outbox WHERE session_id=? ORDER BY seq LIMIT ?',
                                   (session_id, int(limit))).fetchall()
            else:
                rows = con.execute('SELECT payload, status FROM outbox ORDER BY seq DESC LIMIT ?',
                                   (int(limit),)).fetchall()[::-1]
        return [dict(json.loads(p), _status=s) for p, s in rows]

    def get(self, key, default=None):
        with self.connection() as con:
            row = con.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.connection() as con:
            con.execute('INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)', (key, json.dumps(value)))






# --------------------------------------------------------------------------- kit state machine
class KitLink:
    def __init__(self, outbox, config, on_enqueue=None, processor=None):
        self.outbox = outbox
        self.cfg = config
        self.on_enqueue = on_enqueue or (lambda: None)
        self.processor = processor   # (corner_index, sensor_uH, head_ok) -> {est_mm, gap_mm, quality}
        self.observers = []          # callables(msg) — LocalMainSim listens here
        self.lock = threading.RLock()
        self.uplink = {'last_error': None, 'last_attempt_at': None, 'last_ok_at': None, 'heartbeat_seq': 0}
        self.last_contact = time.monotonic()
        default = {'state': 'IDLE', 'session_id': None, 'cycle_no': 0, 'round_no': 0, 'pending': None,
                   'measurement_id': 0, 'resense_count': 0, 'awaiting_start': False, 'fault_code': 0,
                   'fault_detail': None, 'pre_test_state': None, 'last_command': None, 'seen_commands': [],
                   'last_job': None, 'last_reference': None, 'last_self_test': None}
        saved = outbox.get('kit_state') or {}
        self.st = dict(default, **{k: v for k, v in saved.items() if k in default})
        if self.st['state'] not in STATES or (self.st['state'] in JOB_STATES and not self.st['session_id']):
            self.st.update(state='IDLE', session_id=None, pending=None)  # e.g. state saved by an older version
        if self.st['state'] == 'SELF_TEST':      # interrupted self-test: report it again
            self.st.update(state=self.st['pre_test_state'] or 'IDLE', pending=None)

    # ---- helpers
    @property
    def kit_id(self):
        return self.cfg['kit_id']

    def _save(self):
        self.outbox.put('kit_state', self.st)

    def _enqueue(self, msg):
        self.outbox.enqueue(msg)
        for fn in self.observers:
            try:
                fn(msg)
            except Exception:
                logging.exception('observer failed')
        return msg

    def _emit(self, event, detail=None):
        return self._enqueue({
            'schema': SCHEMA_EVENT, 'kind': 'event',
            'msg_id': '%s|evt|%d|%s' % (self.kit_id, time.time_ns(), secrets.token_hex(3)),
            'kit_id': self.kit_id, 'session_id': self.st['session_id'], 'cycle_no': self.st['cycle_no'],
            'event': event, 'state': self.st['state'], 'fault_code': self.st['fault_code'],
            'created_at': utc_now(), 'detail': detail or {}})

    def _commit(self):
        self._save()
        self.on_enqueue()

    def _end(self, result, detail):
        st = self.st
        st['last_job'] = {'session_id': st['session_id'], 'result': result, 'rounds': st['round_no'],
                          'measurements': st['measurement_id'], 'at': utc_now(), 'detail': detail}
        st.update(state='IDLE', pending=None)
        self._emit('job_' + result, dict(detail, rounds=st['round_no']))

    def _fault(self, code, detail):
        st = self.st
        if st['state'] in JOB_STATES:
            self._end('aborted', {'reason': 'fault', 'fault_code': code, 'by': 'kit'})
        st.update(state='FAULT', fault_code=int(code), fault_detail=str(detail)[:300], pending=None,
                  awaiting_start=False)
        self._emit('fault', {'fault_code': code, 'fault': ERRORS.get(code, 'other'), 'detail': str(detail)[:300]})

    def _check_job(self, session_id):
        if self.st['state'] not in JOB_STATES:
            raise LinkRefused('진행 중인 작업이 없습니다 (현재: %s)' % self.st['state'])
        if session_id != self.st['session_id']:
            raise LinkRefused('작업(세션) 번호가 현재 작업과 다릅니다')

    def mark_contact(self):
        self.last_contact = time.monotonic()

    # ---- read
    def status(self):
        with self.lock:
            st = self.st
            s = {k: st[k] for k in ('state', 'session_id', 'cycle_no', 'round_no', 'measurement_id', 'pending',
                                    'awaiting_start', 'fault_code', 'fault_detail', 'last_command', 'last_job',
                                    'last_reference', 'last_self_test')}
            s['fault'] = ERRORS.get(st['fault_code'], 'other')
            if st['pending']:
                s['pending'] = dict(st['pending'], session_id=st['session_id'])
            s.update(kit_id=self.kit_id, units=list(self.cfg['units']), source=self.cfg['source'],
                     max_rounds=self.cfg['max_rounds'], server_url=self.cfg['server_url'],
                     server_configured=bool(self.cfg['server_url']),
                     outbox=self.outbox.counts(), uplink=dict(self.uplink))
            return s

    def active(self):
        return self.st['state'] in JOB_STATES or self.st['awaiting_start'] or self.st['state'] == 'SELF_TEST'

    @staticmethod
    def _unit_input(k, u):
        if not isinstance(u, dict):
            raise ValueError('코너 %d 값 형식 오류' % (k + 1))
        s = u.get('sensor_uH')
        if not isinstance(s, list) or len(s) != 5:
            raise ValueError('코너 %d: 5헤드 측정값(sensor_uH 5개)이 필요합니다' % (k + 1))
        s = [_finite(v, '코너 %d 센서' % (k + 1)) for v in s]
        head_ok = u.get('head_ok', [True] * 5)
        if not isinstance(head_ok, list) or len(head_ok) != 5 or not all(isinstance(v, bool) for v in head_ok):
            raise ValueError('코너 %d: head_ok 는 true/false 5개' % (k + 1))
        d = u.get('diag') or {}
        if not isinstance(d, dict):
            raise ValueError('코너 %d: diag 형식 오류' % (k + 1))
        diag = {'coil_temp_c': _finite(d.get('coil_temp_c', float('nan')), '온도') if 'coil_temp_c' in d else None,
                'supply_v': _finite(d['supply_v'], '전압') if 'supply_v' in d else None,
                'fw_cal_version': str(d.get('fw_cal_version', ''))[:40] or None,
                'error_code': int(d.get('error_code', 0)) if not isinstance(d.get('error_code', 0), bool) else 0}
        return s, head_ok, diag

    def _reference(self, units):
        """Kit's own equal-weight 4-corner fit — REFERENCE ONLY (the server decides)."""
        est = [u['est_mm'] for u in units]
        if any(e is None for e in est):
            return {'available': False}
        sx = sum(e[0] for e in est) / 4
        sy = sum(e[1] for e in est) / 4
        num = den = 0.0
        for k in range(4):
            X, Y = CORNER_POS_MM[k]
            num += X * (est[k][1] - sy) - Y * (est[k][0] - sx)
            den += X * X + Y * Y
        worst = max(max(abs(e[0]), abs(e[1])) for e in est)
        return {'available': True, 'fit_x_mm': sx, 'fit_y_mm': sy, 'fit_theta_deg': math.degrees(num / den),
                'max_abs_est_mm': worst, 'converged_hint': worst <= CONVERGED_MM}

    # ---- sensing source (simulator now, hardware reader later)
    def record_round(self, session_id, round_no, units, measured_at=None, extra=None):
        """Result of ONE sensing requested by the main server -> one 4-unit bundle (23 items per unit)."""
        with self.lock:
            self._check_job(session_id)
            st = self.st
            p = st['pending']
            if st['state'] != 'SENSING' or not p or p['type'] != 'sense':
                raise LinkRefused('메인 서버의 센싱 명령을 기다리는 중입니다 (현재: %s)' % st['state'])
            if round_no != p['round_no']:
                raise ValueError('보정 회차 오류 (요청 %r, 받음 %r)' % (p['round_no'], round_no))
            if not isinstance(units, list) or len(units) != 4:
                raise ValueError('코너 4개(장치 4대)의 값이 모두 있어야 합니다')
            if measured_at is not None and (not isinstance(measured_at, str) or len(measured_at) > 64):
                raise ValueError('measured_at 형식 오류')
            if extra is not None and (not isinstance(extra, dict) or len(json.dumps(extra)) > 4000):
                raise ValueError('extra 는 4KB 이하 JSON 객체')
            inputs = [self._unit_input(k, u) for k, u in enumerate(units)]
            if self.processor is None:
                raise RuntimeError('장치 계산 모듈이 연결되지 않았습니다')
            now = utc_now()
            mid = st['measurement_id'] + 1
            out = []
            for k, (s, head_ok, diag) in enumerate(inputs):
                r = self.processor(k, s, head_ok)
                out.append({'corner': k + 1, 'unit_id': self.cfg['units'][k],
                            'job_id': session_id, 'round_no': round_no, 'measurement_id': mid,
                            'measured_at': measured_at or now,
                            'heads': HEADS, 'sensor_uH': s,
                            'est_mm': None if r['est_mm'] is None else {'x': r['est_mm'][0], 'y': r['est_mm'][1]},
                            'gap_mm': r['gap_mm'],
                            'quality': r['quality'],
                            'diag': diag,
                            '_est_list': r['est_mm']})
            ref = self._reference([{'est_mm': u.pop('_est_list')} for u in out])
            bundle = {
                'schema': SCHEMA_ROUND, 'kind': 'round',
                'msg_id': '%s|m%03d' % (session_id, mid),
                'kit_id': self.kit_id, 'session_id': session_id, 'cycle_no': st['cycle_no'],
                'round_no': round_no, 'measurement_id': mid, 'resense': bool(p.get('resense')),
                'source': self.cfg['source'], 'measured_at': measured_at or now, 'created_at': now,
                'units': out,
                'kit_reference': ref,
            }
            if extra:
                bundle['extra'] = extra
            st.update(state='WAITING', round_no=round_no, measurement_id=mid, pending=None, last_reference=ref)
            self._enqueue(bundle)
            self._commit()
            return {'msg_id': bundle['msg_id'], 'round_no': round_no, 'measurement_id': mid, 'state': 'WAITING',
                    'units': [{'est_mm': u['est_mm'], 'gap_mm': u['gap_mm'], 'grade': u['quality']['grade']} for u in out],
                    'kit_reference': ref}

    def sense_failed(self, session_id, round_no, reason):
        """Sensing could not be done this time (not a fault). The server may resend sense/resense."""
        with self.lock:
            self._check_job(session_id)
            p = self.st['pending']
            if self.st['state'] != 'SENSING' or not p or round_no != p.get('round_no'):
                raise LinkRefused('요청된 센싱 회차가 아닙니다')
            self.st.update(state='WAITING', pending=None)
            self._emit('sense_failed', {'round_no': round_no, 'error_code': 10, 'reason': str(reason)[:300]})
            self._commit()
            return self.status()

    def report_fault(self, code, detail=''):
        """Unit/hardware fault found by the sensing source (e.g. a dead coil). Kit -> FAULT."""
        with self.lock:
            if not isinstance(code, int) or isinstance(code, bool) or code not in ERRORS or code == 0:
                raise ValueError('오류 코드는 ' + ', '.join(str(c) for c in ERRORS if c))
            self._fault(code, detail)
            self._commit()
            return self.status()

    def record_self_test(self, results):
        """results: 4 x {'ok': bool, 'error_code': int, 'detail': str} in corner order."""
        with self.lock:
            st = self.st
            if st['state'] != 'SELF_TEST':
                raise LinkRefused('자가 점검 중이 아닙니다')
            if not isinstance(results, list) or len(results) != 4 or not all(isinstance(r, dict) for r in results):
                raise ValueError('장치 4대의 점검 결과가 필요합니다')
            rows = [{'corner': k + 1, 'unit_id': self.cfg['units'][k], 'ok': bool(r.get('ok')),
                     'error_code': int(r.get('error_code', 0) or 0), 'detail': str(r.get('detail', ''))[:200]}
                    for k, r in enumerate(results)]
            passed = all(r['ok'] for r in rows)
            st['last_self_test'] = {'at': utc_now(), 'passed': passed, 'units': rows}
            st.update(state=st['pre_test_state'] or 'IDLE', pending=None)
            self._emit('self_test_result', {'passed': passed, 'units': rows})
            if not passed:
                self._fault(40, ', '.join('코너%d:%s' % (r['corner'], r['detail'] or r['error_code'])
                                          for r in rows if not r['ok']))
            self._commit()
            return self.status()

    def abort_local(self, session_id, reason='local_abort'):
        """Kit-side abort (e.g. UI closed mid-job). Reported to the main server."""
        with self.lock:
            self._check_job(session_id)
            self._end('aborted', {'reason': str(reason)[:200], 'by': 'kit'})
            self._commit()
            return self.status()

    def watchdog_check(self):
        with self.lock:
            if (self.cfg['server_url'] and self.st['state'] in JOB_STATES
                    and time.monotonic() - self.last_contact > float(self.cfg['watchdog_s'])):
                self._fault(50, '메인 서버와 %s초 이상 통신 없음' % self.cfg['watchdog_s'])
                self._commit()
                return True
            return False

    def sim_machine_ready(self, machine_id='SIM-MACHINE-1'):
        """SIMULATION ONLY: stands in for 'machine -> main server: ready'. Real machines report directly."""
        with self.lock:
            if self.cfg['source'] != 'simulation':
                raise LinkRefused('하드웨어 모드에서는 기계가 직접 메인 서버에 준비 완료를 보냅니다')
            if self.st['state'] != 'IDLE':
                raise LinkRefused('작업을 시작할 수 없는 상태입니다 (현재: %s)' % self.st['state'])
            if self.st['awaiting_start']:
                raise LinkRefused('이미 준비 완료를 보냈고 메인 서버의 작업 지시를 기다리는 중입니다')
            if not isinstance(machine_id, str) or not ID_RE.match(machine_id):
                raise ValueError('machine_id 형식 오류')
            self.st['awaiting_start'] = True
            self._emit('sim_machine_ready', {'machine_id': machine_id, 'simulation': True})
            self._commit()
            return self.status()

    # ---- main server commands
    def apply_command(self, cmd):
        """The 7 commands. Every command is acknowledged (command_ack with ok/note)."""
        with self.lock:
            if not isinstance(cmd, dict):
                return
            st = self.st
            cid = str(cmd.get('id', ''))[:80]
            ctype = cmd.get('type')
            if cid and cid in st['seen_commands']:
                return  # duplicate delivery
            ok, note = True, ''
            sid = cmd.get('session_id')
            if ctype == 'start_job':
                if st['state'] != 'IDLE':
                    ok, note = False, 'busy:' + st['state']
                else:
                    job = cmd.get('job_id')
                    if not (isinstance(job, str) and ID_RE.match(job)):
                        job = '%s-%s-%s' % (self.kit_id, datetime.now().strftime('%Y%m%d%H%M%S'), secrets.token_hex(2))
                    st.update(state='SENSING', session_id=job, cycle_no=st['cycle_no'] + 1, round_no=0,
                              measurement_id=0, resense_count=0, awaiting_start=False, last_reference=None,
                              pending={'type': 'sense', 'round_no': 1, 'resense': False})
                    self.mark_contact()
                    self._emit('job_start', {'command_id': cid, 'machine_id': cmd.get('machine_id'),
                                             'units': [{'corner': i + 1, 'unit_id': u}
                                                       for i, u in enumerate(self.cfg['units'])]})
            elif ctype in ('sense', 'resense'):
                want = st['round_no'] + (1 if ctype == 'sense' else 0)
                if st['state'] != 'WAITING':
                    ok, note = False, 'not_waiting:' + st['state']
                elif sid is not None and sid != st['session_id']:
                    ok, note = False, 'wrong_session'
                elif cmd.get('round_no') is not None and cmd.get('round_no') != want:
                    ok, note = False, 'wrong_round(expected %d)' % want
                elif ctype == 'sense' and want > self.cfg['max_rounds']:
                    ok, note = False, 'max_rounds_reached'
                elif ctype == 'resense' and st['round_no'] < 1:
                    ok, note = False, 'nothing_to_resense'
                elif ctype == 'resense' and st['resense_count'] >= self.cfg['max_resense']:
                    ok, note = False, 'max_resense_reached'
                else:
                    if ctype == 'sense':
                        st['resense_count'] = 0
                    else:
                        st['resense_count'] += 1
                    st.update(state='SENSING', pending={'type': 'sense', 'round_no': want, 'resense': ctype == 'resense'})
            elif ctype == 'end_job':
                if st['state'] not in JOB_STATES:
                    ok, note = False, 'no_job'
                elif sid is not None and sid != st['session_id']:
                    ok, note = False, 'wrong_session'
                else:
                    self._end('end', {'command_id': cid, 'result': str(cmd.get('result', ''))[:100]})
            elif ctype == 'stop':
                st['awaiting_start'] = False
                if st['state'] in JOB_STATES:
                    self._end('stopped', {'command_id': cid, 'reason': str(cmd.get('reason', ''))[:200]})
                elif st['state'] == 'SELF_TEST':
                    st.update(state=st['pre_test_state'] or 'IDLE', pending=None)
                    note = 'self_test_cancelled'
                else:
                    note = 'no_job'
            elif ctype == 'fault_reset':
                if st['state'] == 'FAULT':
                    st.update(state='IDLE', fault_code=0, fault_detail=None)
                    self._emit('fault_reset', {'command_id': cid})
                else:
                    note = 'no_fault'
            elif ctype == 'self_test':
                if st['state'] not in ('IDLE', 'FAULT'):
                    ok, note = False, 'busy:' + st['state']
                else:
                    st.update(pre_test_state=st['state'], state='SELF_TEST', pending={'type': 'self_test'})
            else:
                ok, note = False, 'unknown_command'
            st['seen_commands'] = (st['seen_commands'] + [cid])[-50:] if cid else st['seen_commands']
            st['last_command'] = {'id': cid, 'type': ctype, 'received_at': utc_now(), 'ok': ok, 'note': note}
            self._emit('command_ack', {'command_id': cid, 'type': ctype, 'ok': ok, 'note': note,
                                       'round_no': st['round_no']})
            self._commit()


class Watchdog(threading.Thread):
    def __init__(self, link, period=0.25):
        super().__init__(daemon=True, name='MASKIT-watchdog')
        self.link, self.period = link, period
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.wait(self.period):
            try:
                self.link.watchdog_check()
            except Exception:
                logging.exception('watchdog check failed')

    def stop(self):
        self.stop_event.set()


# --------------------------------------------------------------------------- uploader
class Uploader(threading.Thread):
    """Background sender: pending messages -> main server, oldest first; commands come back in the reply."""

    def __init__(self, outbox, link, config, urlopen=None):
        super().__init__(daemon=True, name='MASKIT-uploader')
        self.outbox, self.link, self.cfg = outbox, link, config
        self.urlopen = urlopen or urllib.request.urlopen
        self.wake_event = threading.Event()
        self.stop_event = threading.Event()

    def wake(self):
        self.wake_event.set()

    def stop(self, timeout=5):
        self.stop_event.set()
        self.wake_event.set()
        if self.is_alive():
            self.join(timeout)

    def send_once(self):
        """One POST. Returns number of messages still pending afterwards. Raises on transport error."""
        url = self.cfg['server_url']
        if not url:
            return None
        batch = self.outbox.pending(self.cfg['batch_max'])
        ids = [m for m, _ in batch]
        self.link.uplink['heartbeat_seq'] += 1
        st = self.link.st
        body = json.dumps({'schema': SCHEMA_BATCH, 'kit_id': self.link.kit_id, 'sent_at': utc_now(),
                           'heartbeat_seq': self.link.uplink['heartbeat_seq'],
                           'kit_status': {'state': st['state'], 'session_id': st['session_id'],
                                          'round_no': st['round_no'], 'measurement_id': st['measurement_id'],
                                          'fault_code': st['fault_code']},
                           'messages': [p for _, p in batch]}, ensure_ascii=False, allow_nan=False).encode('utf-8')
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
                   'User-Agent': 'MASKIT-Kit/3', 'X-MASKIT-Kit': self.link.kit_id}
        if self.cfg['api_key']:
            headers['Authorization'] = 'Bearer ' + self.cfg['api_key']
        self.link.uplink['last_attempt_at'] = utc_now()
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method='POST')
            with self.urlopen(req, timeout=self.cfg['timeout_s']) as res:
                reply = json.loads(res.read().decode('utf-8') or '{}')
            if not isinstance(reply, dict):
                raise ValueError('메인 서버 응답이 JSON 객체가 아닙니다')
            accepted = reply.get('accepted', ids)
            rejected = reply.get('rejected', [])
            if not isinstance(accepted, list) or not isinstance(rejected, list):
                raise ValueError('메인 서버 응답 형식 오류 (accepted/rejected)')
            known = set(ids)
            self.outbox.mark_sent([m for m in accepted if m in known])
            self.outbox.mark_rejected([(r.get('msg_id'), r.get('error', 'rejected')) for r in rejected
                                       if isinstance(r, dict) and r.get('msg_id') in known])
            self.link.uplink.update(last_error=None, last_ok_at=utc_now())
            self.link.mark_contact()
            for cmd in reply.get('commands', []) or []:
                self.link.apply_command(cmd)
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError):
                msg = 'HTTP %d %s' % (exc.code, exc.reason)
            else:
                msg = '%s: %s' % (type(exc).__name__, exc)
            self.outbox.mark_failed(ids, msg)
            self.link.uplink['last_error'] = msg
            raise
        return self.outbox.counts()['pending']

    def run(self):
        backoff = 0.0
        while not self.stop_event.is_set():
            if not self.cfg['server_url']:
                self.wake_event.wait(self.cfg['heartbeat_s'])
                self.wake_event.clear()
                continue
            try:
                left = self.send_once()
                backoff = 0.0
                if left:
                    continue  # more to send: next batch immediately
            except Exception as exc:
                backoff = min(self.cfg['retry_max_s'], max(1.0, backoff * 2))
                logging.warning('메인 서버 전송 실패, %.0f초 뒤 재시도: %s', backoff, exc)
                self.stop_event.wait(backoff)
                continue
            # Nothing pending: wait for a new message, or poll the server for commands.
            self.wake_event.wait(self.cfg['active_poll_s'] if self.link.active() else self.cfg['heartbeat_s'])
            self.wake_event.clear()


# --------------------------------------------------------------------------- factory
def _load_model():
    import maskit_engine
    return maskit_engine.Model()


def default_processor(model):
    """Device-side per-corner computation (numpy + Maxwell model)."""
    import kit_device
    return lambda k, sensor_uH, head_ok: kit_device.process_reading(model, k, sensor_uH, head_ok)


class LinkRuntime:
    def __init__(self, link, uploader, watchdog, cfg_path, sim=None):
        self.link, self.uploader, self.watchdog, self.cfg_path, self.sim = link, uploader, watchdog, cfg_path, sim

    def stop(self):
        if self.sim:
            self.sim.source.stop()
        self.watchdog.stop()
        self.uploader.stop()


def open_link(folder, urlopen=None, start=True, processor=None, simulate=True):
    """simulate: when source is 'simulation', attach the virtual site (sim_bridge):
    built-in main server + reach stacker without server_url, or the example server's site with it."""
    cfg, cfg_path = load_config(folder)
    outbox = Outbox(pathlib.Path(folder) / 'outbox.sqlite3')
    model = _load_model()
    link = KitLink(outbox, cfg, processor=processor or default_processor(model))
    uploader = Uploader(outbox, link, cfg, urlopen=urlopen)
    link.on_enqueue = uploader.wake
    watchdog = Watchdog(link)
    sim = None
    if simulate and cfg['source'] == 'simulation' and cfg['sim_auto_sensing']:
        import sim_bridge
        sim = sim_bridge.attach(link, model, start=start)
    if start:
        uploader.start()
        watchdog.start()
    return LinkRuntime(link, uploader, watchdog, cfg_path, sim)
