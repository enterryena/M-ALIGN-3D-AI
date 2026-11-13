# -*- coding: utf-8 -*-
"""M-ALIGN kit link: the main server directs each sensing; the kit stores and sends every result.

One kit = 4 correction units (one per container corner), each with its own unique
number. The machine (e.g. reach stacker) and its movement belong to the MAIN SERVER;
the kit only senses and reports:

  1. machine moves into position and is ready         (machine -> main server)
  2. main server sends  start_job                    -> kit senses round 1
  3. kit sends round bundle: 4 units' 5-head readings + estimate + correction value
  4. main server applies the correction (weighing it against the machine's own data),
     confirms the move, then sends  sense            -> kit senses round n+1
  5. repeat (about 6 rounds)
  6. main server sends  end_job  and runs the main action (lower + lock)

Kit states:   IDLE --start_job--> SENSING --round sent--> WAITING --sense--> SENSING ...
              WAITING/SENSING --end_job | stop--> IDLE

Store-and-forward: every bundle/event is first committed to a local SQLite outbox,
then a background uploader POSTs pending ones to the main server in order and
receives the server's commands in the reply. Network loss never drops data.
The sensing source (simulator now, real hardware later) only has to watch
status()['pending'] and call record_round() / sense_failed().
With no server_url and source 'simulation', a built-in LocalMainSim plays the
main server's part so the simulator still runs stand-alone.
Python stdlib only.
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

SCHEMA_DB = 'MALIGN_link_outbox_v1'
SCHEMA_ROUND = 'MALIGN_round_bundle_v1'
SCHEMA_EVENT = 'MALIGN_kit_event_v1'
SCHEMA_BATCH = 'MALIGN_upload_batch_v1'
HEADS = ['Xp', 'Xm', 'Yp', 'Ym', 'Ref']
# Same corner order as the simulator's CORNERS array (twistlock centres, mm).
CORNER_POS_MM = [(-2926.5, 1129.5), (2926.5, 1129.5), (2926.5, -1129.5), (-2926.5, -1129.5)]
STATES = ('IDLE', 'SENSING', 'WAITING')
SOURCES = ('simulation', 'hardware')
COMMANDS = ('start_job', 'sense', 'end_job', 'stop')
ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
CONVERGED_MM = 1.0  # kit's own hint only; the main server decides when to finish

DEFAULT_CONFIG = {
    'kit_id': None,            # generated once on first run; edit to the real kit number
    'units': None,             # 4 unique unit numbers in corner order 1..4
    'source': 'simulation',    # 'hardware' once the real sensor reader is attached
    'server_url': '',          # e.g. http://10.0.0.5:8750/malign/v1/bundles ; '' = store only
    'api_key': '',             # sent as "Authorization: Bearer <api_key>" when set
    'max_rounds': 10,          # safety cap per job (the main server normally ends after ~6)
    'batch_max': 50,
    'timeout_s': 5,
    'heartbeat_s': 2,          # poll interval while idle (asks the server for start_job)
    'active_poll_s': 0.5,      # poll interval during a job (waiting for sense / end_job)
    'retry_max_s': 30,
    'sim_rounds': 6,           # LocalMainSim only (no server_url): rounds before end_job
    'sim_move_delay_s': 0.8,   # LocalMainSim only: time the "machine" takes to apply a correction
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
        cfg['kit_id'] = 'MALIGN-KIT-' + secrets.token_hex(4).upper()
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
    for key, lo, hi in (('max_rounds', 1, 50), ('batch_max', 1, 500), ('sim_rounds', 1, 50)):
        if not isinstance(cfg[key], int) or isinstance(cfg[key], bool) or not lo <= cfg[key] <= hi:
            raise ValueError('%s 범위 오류 (%d~%d)' % (key, lo, hi))
    if cfg['sim_rounds'] > cfg['max_rounds']:
        raise ValueError('sim_rounds 는 max_rounds 이하여야 합니다')
    for key in ('timeout_s', 'heartbeat_s', 'active_poll_s', 'retry_max_s'):
        if _finite(cfg[key], key) <= 0:
            raise ValueError(key + ' 는 0보다 커야 합니다')
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
    def __init__(self, outbox, config, on_enqueue=None):
        self.outbox = outbox
        self.cfg = config
        self.on_enqueue = on_enqueue or (lambda: None)
        self.observers = []          # callables(msg) — LocalMainSim listens here
        self.lock = threading.RLock()
        self.uplink = {'last_error': None, 'last_attempt_at': None, 'last_ok_at': None}
        default = {'state': 'IDLE', 'session_id': None, 'cycle_no': 0, 'round_no': 0, 'pending_round': None,
                   'awaiting_start': False, 'last_command': None, 'seen_commands': [], 'last_job': None,
                   'last_assessment': None}
        saved = outbox.get('kit_state') or {}
        self.st = dict(default, **{k: v for k, v in saved.items() if k in default})
        if self.st['state'] not in STATES:   # e.g. state saved by an older version
            self.st.update(state='IDLE', session_id=None, pending_round=None)

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
            'event': event, 'state': self.st['state'], 'created_at': utc_now(), 'detail': detail or {}})

    def _commit(self):
        self._save()
        self.on_enqueue()

    def _end(self, result, detail):
        st = self.st
        st['last_job'] = {'session_id': st['session_id'], 'result': result, 'rounds': st['round_no'],
                          'at': utc_now(), 'detail': detail}
        st.update(state='IDLE', pending_round=None)
        self._emit('job_' + result, dict(detail, rounds=st['round_no']))

    @staticmethod
    def _readings(units):
        if not isinstance(units, list) or len(units) != 4:
            raise ValueError('코너 4개(장치 4대)의 값이 모두 있어야 합니다')
        out = []
        for k, u in enumerate(units):
            if not isinstance(u, dict):
                raise ValueError('코너 %d 값 형식 오류' % (k + 1))
            s, e = u.get('sensor_uH'), u.get('est_mm')
            if not isinstance(s, list) or len(s) != 5:
                raise ValueError('코너 %d: 5헤드 측정값(sensor_uH 5개)이 필요합니다' % (k + 1))
            if not isinstance(e, list) or len(e) != 2:
                raise ValueError('코너 %d: 추정 오차 est_mm [x,y] 가 필요합니다' % (k + 1))
            out.append({'sensor_uH': [_finite(v, '코너 %d 센서' % (k + 1)) for v in s],
                        'est_mm': [_finite(v, '코너 %d 추정' % (k + 1)) for v in e]})
        return out

    def _check_job(self, session_id):
        if self.st['state'] == 'IDLE':
            raise LinkRefused('진행 중인 작업이 없습니다')
        if session_id != self.st['session_id']:
            raise LinkRefused('작업(세션) 번호가 현재 작업과 다릅니다')

    # ---- read
    def status(self):
        with self.lock:
            st = self.st
            s = {k: st[k] for k in ('state', 'session_id', 'cycle_no', 'round_no', 'awaiting_start',
                                    'last_command', 'last_job', 'last_assessment')}
            s['pending'] = ({'type': 'sense', 'session_id': st['session_id'], 'round_no': st['pending_round']}
                            if st['state'] == 'SENSING' else None)
            s.update(kit_id=self.kit_id, units=list(self.cfg['units']), source=self.cfg['source'],
                     max_rounds=self.cfg['max_rounds'], server_url=self.cfg['server_url'],
                     server_configured=bool(self.cfg['server_url']),
                     outbox=self.outbox.counts(), uplink=dict(self.uplink))
            return s

    def active(self):
        return self.st['state'] != 'IDLE' or self.st['awaiting_start']

    # ---- sensing source (simulator UI now, hardware reader later)
    def record_round(self, session_id, round_no, units, command, measured_at=None, extra=None):
        """Result of ONE sensing requested by the main server -> one 4-unit bundle."""
        with self.lock:
            self._check_job(session_id)
            st = self.st
            if st['state'] != 'SENSING':
                raise LinkRefused('메인 서버의 센싱 명령을 기다리는 중입니다 (현재: %s)' % st['state'])
            if round_no != st['pending_round']:
                raise ValueError('보정 회차 오류 (요청 %r, 받음 %r)' % (st['pending_round'], round_no))
            rows = self._readings(units)
            if not isinstance(command, dict):
                raise ValueError('command 형식 오류')
            cmd = {k: _finite(command.get(k), 'command.' + k) for k in ('x_mm', 'y_mm', 'theta_deg')}
            if measured_at is not None and (not isinstance(measured_at, str) or len(measured_at) > 64):
                raise ValueError('measured_at 형식 오류')
            if extra is not None and (not isinstance(extra, dict) or len(json.dumps(extra)) > 4000):
                raise ValueError('extra 는 4KB 이하 JSON 객체')
            worst = max(max(abs(r['est_mm'][0]), abs(r['est_mm'][1])) for r in rows)
            assess = {'max_est_mm': worst, 'within_mm': CONVERGED_MM, 'converged_hint': worst <= CONVERGED_MM}
            now = utc_now()
            bundle = {
                'schema': SCHEMA_ROUND, 'kind': 'round',
                'msg_id': '%s|r%02d' % (session_id, round_no),
                'kit_id': self.kit_id, 'session_id': session_id, 'cycle_no': st['cycle_no'],
                'round_no': round_no, 'source': self.cfg['source'],
                'measured_at': measured_at or now, 'created_at': now,
                'units': [{'corner': i + 1, 'unit_id': self.cfg['units'][i], 'heads': HEADS,
                           'sensor_uH': r['sensor_uH'], 'est_mm': {'x': r['est_mm'][0], 'y': r['est_mm'][1]}}
                          for i, r in enumerate(rows)],
                'correction': cmd,
                'kit_assessment': assess,
            }
            if extra:
                bundle['extra'] = extra
            st.update(state='WAITING', round_no=round_no, pending_round=None, last_assessment=assess)
            self._enqueue(bundle)
            self._commit()
            return {'msg_id': bundle['msg_id'], 'round_no': round_no, 'state': 'WAITING'}

    def sense_failed(self, session_id, round_no, reason):
        """Sensing could not be done; report it. The server may send 'sense' for the same round again."""
        with self.lock:
            self._check_job(session_id)
            if self.st['state'] != 'SENSING' or round_no != self.st['pending_round']:
                raise LinkRefused('요청된 센싱 회차가 아닙니다')
            self.st.update(state='WAITING', pending_round=None)
            self._emit('sense_failed', {'round_no': round_no, 'reason': str(reason)[:300]})
            self._commit()
            return self.status()

    def abort_local(self, session_id, reason='local_abort'):
        """Kit-side abort (e.g. UI closed mid-job). Reported to the main server."""
        with self.lock:
            self._check_job(session_id)
            self._end('aborted', {'reason': str(reason)[:200], 'by': 'kit'})
            self._commit()
            return self.status()

    def sim_machine_ready(self, machine_id='SIM-MACHINE-1'):
        """SIMULATION ONLY: stands in for step 1 (machine -> main server 'ready').

        The real machine reports to the main server itself; the kit never does this
        in hardware mode."""
        with self.lock:
            if self.cfg['source'] != 'simulation':
                raise LinkRefused('하드웨어 모드에서는 기계가 직접 메인 서버에 준비 완료를 보냅니다')
            if self.st['state'] != 'IDLE':
                raise LinkRefused('이미 작업이 진행 중입니다 (현재: %s)' % self.st['state'])
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
        """start_job | sense | end_job | stop. Every command is acknowledged (command_ack)."""
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
                              pending_round=1, awaiting_start=False, last_assessment=None)
                    self._emit('job_start', {'command_id': cid, 'machine_id': cmd.get('machine_id'),
                                             'units': [{'corner': i + 1, 'unit_id': u}
                                                       for i, u in enumerate(self.cfg['units'])]})
            elif ctype == 'sense':
                want = st['round_no'] + 1
                if st['state'] != 'WAITING':
                    ok, note = False, 'not_waiting:' + st['state']
                elif sid is not None and sid != st['session_id']:
                    ok, note = False, 'wrong_session'
                elif cmd.get('round_no') is not None and cmd.get('round_no') != want:
                    ok, note = False, 'wrong_round(expected %d)' % want
                elif want > self.cfg['max_rounds']:
                    ok, note = False, 'max_rounds_reached'
                else:
                    st.update(state='SENSING', pending_round=want)
            elif ctype == 'end_job':
                if st['state'] == 'IDLE':
                    ok, note = False, 'no_job'
                elif sid is not None and sid != st['session_id']:
                    ok, note = False, 'wrong_session'
                else:
                    self._end('end', {'command_id': cid, 'result': str(cmd.get('result', ''))[:100]})
            elif ctype == 'stop':
                st['awaiting_start'] = False
                if st['state'] != 'IDLE':
                    self._end('stopped', {'command_id': cid, 'reason': str(cmd.get('reason', ''))[:200]})
                else:
                    note = 'no_job'
            else:
                ok, note = False, 'unknown_command'
            st['seen_commands'] = (st['seen_commands'] + [cid])[-50:] if cid else st['seen_commands']
            st['last_command'] = {'id': cid, 'type': ctype, 'received_at': utc_now(), 'ok': ok, 'note': note}
            self._emit('command_ack', {'command_id': cid, 'type': ctype, 'ok': ok, 'note': note,
                                       'round_no': st['pending_round'] or st['round_no']})
            self._commit()


# --------------------------------------------------------------------------- built-in main server (simulation, no server_url)
class LocalMainSim:
    """Plays the main server when no server_url is set and the source is the simulator.

    machine ready -> start_job; each round -> (machine applies correction) -> sense,
    until sim_rounds, then end_job. Assumes the machine applies the kit's value as-is."""

    def __init__(self, link):
        self.link = link
        self.n = 0
        link.observers.append(self.on_message)

    def _later(self, cmd):
        self.n += 1
        cmd = dict(cmd, id='local-%d-%s' % (self.n, secrets.token_hex(2)))
        t = threading.Timer(float(self.link.cfg['sim_move_delay_s']), self.link.apply_command, args=(cmd,))
        t.daemon = True
        t.start()

    def on_message(self, msg):
        if msg.get('event') == 'sim_machine_ready':
            self._later({'type': 'start_job', 'machine_id': msg['detail'].get('machine_id')})
        elif msg.get('kind') == 'round':
            if msg['round_no'] >= self.link.cfg['sim_rounds']:
                self._later({'type': 'end_job', 'session_id': msg['session_id'], 'result': 'main_action'})
            else:
                self._later({'type': 'sense', 'session_id': msg['session_id'], 'round_no': msg['round_no'] + 1})


# --------------------------------------------------------------------------- uploader
class Uploader(threading.Thread):
    """Background sender: pending messages -> main server, oldest first; commands come back in the reply."""

    def __init__(self, outbox, link, config, urlopen=None):
        super().__init__(daemon=True, name='MALIGN-uploader')
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
        body = json.dumps({'schema': SCHEMA_BATCH, 'kit_id': self.link.kit_id, 'sent_at': utc_now(),
                           'kit_state': self.link.st['state'], 'session_id': self.link.st['session_id'],
                           'messages': [p for _, p in batch]}, ensure_ascii=False, allow_nan=False).encode('utf-8')
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
                   'User-Agent': 'MALIGN-Kit/2', 'X-MALIGN-Kit': self.link.kit_id}
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
def open_link(folder, urlopen=None, start=True):
    cfg, cfg_path = load_config(folder)
    outbox = Outbox(pathlib.Path(folder) / 'outbox.sqlite3')
    link = KitLink(outbox, cfg)
    uploader = Uploader(outbox, link, cfg, urlopen=urlopen)
    link.on_enqueue = uploader.wake
    if not cfg['server_url'] and cfg['source'] == 'simulation':
        LocalMainSim(link)
    if start:
        uploader.start()
    return link, uploader, cfg_path
