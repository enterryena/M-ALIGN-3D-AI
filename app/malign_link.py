# -*- coding: utf-8 -*-
"""M-ALIGN kit link: store every correction round locally, then send it to the main server.

One kit = 4 correction units (one per container corner), each with its own unique
number. Every correction round produces ONE bundle that carries all 4 units'
5-head readings, the per-corner estimate and the commanded X/Y/rotation move.

Flow (the kit only measures and reports; it never commands the crane):

    IDLE --begin--> ALIGNING (round 1..max_rounds, each round -> 1 bundle)
    ALIGNING --converged--> HOLD (baseline = last readings)
    ALIGNING --not converged--> STOPPED (align_failed)
    HOLD --movement detected--> STOPPED (or IDLE+realign if on_movement='realign')
    STOPPED --main server 'realign'--> IDLE (realign requested) --begin--> ALIGNING ...

Storage is store-and-forward: a bundle is first committed to a local SQLite
outbox, then a background uploader POSTs pending bundles to the main server.
Network loss never drops data; unsent bundles stay 'pending' and are retried.
Python stdlib only. The source of readings (simulator now, real hardware later)
only has to call begin_cycle / record_round / finish_cycle / hold_reading.
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
STATES = ('IDLE', 'ALIGNING', 'HOLD', 'STOPPED')
SOURCES = ('simulation', 'hardware')
ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')

DEFAULT_CONFIG = {
    'kit_id': None,            # generated once on first run; edit to the real kit number
    'units': None,             # 4 unique unit numbers in corner order 1..4
    'server_url': '',          # e.g. http://10.0.0.5:8750/malign/v1/bundles ; '' = store only
    'api_key': '',             # sent as "Authorization: Bearer <api_key>" when set
    'max_rounds': 6,
    'batch_max': 50,
    'timeout_s': 5,
    'heartbeat_s': 10,
    'retry_max_s': 60,
    'movement_threshold_uH': 1.0,
    'movement_confirm': 2,
    'on_movement': 'stop',     # 'stop' = wait for main-server command, 'realign' = allow re-align at once
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
    """Load link_config.json; create it (never overwrite) with fresh unique numbers."""
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
    created = False
    if not cfg['kit_id']:
        cfg['kit_id'] = 'MALIGN-KIT-' + secrets.token_hex(4).upper()
        created = True
    if not cfg['units']:
        cfg['units'] = [cfg['kit_id'] + '-U' + str(i) for i in range(1, 5)]
        created = True
    validate_config(cfg)
    if created or not path.exists():
        tmp = path.with_suffix('.json.new')
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
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
    url = cfg['server_url']
    if url:
        p = urllib.parse.urlparse(url)
        if p.scheme not in ('http', 'https') or not p.netloc:
            raise ValueError('server_url 은 http:// 또는 https:// 주소여야 합니다')
    if not isinstance(cfg['max_rounds'], int) or not 1 <= cfg['max_rounds'] <= 20:
        raise ValueError('max_rounds 범위 오류 (1~20)')
    if not isinstance(cfg['batch_max'], int) or not 1 <= cfg['batch_max'] <= 500:
        raise ValueError('batch_max 범위 오류 (1~500)')
    for key in ('timeout_s', 'heartbeat_s', 'retry_max_s', 'movement_threshold_uH'):
        if _finite(cfg[key], key) <= 0:
            raise ValueError(key + ' 는 0보다 커야 합니다')
    if not isinstance(cfg['movement_confirm'], int) or not 1 <= cfg['movement_confirm'] <= 20:
        raise ValueError('movement_confirm 범위 오류 (1~20)')
    if cfg['on_movement'] not in ('stop', 'realign'):
        raise ValueError("on_movement 는 'stop' 또는 'realign'")
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
        self.lock = threading.RLock()
        self.uplink = {'last_error': None, 'last_attempt_at': None, 'last_ok_at': None}
        default = {'state': 'IDLE', 'session_id': None, 'cycle_no': 0, 'round_no': 0, 'source': None,
                   'last_readings': None, 'baseline': None, 'move_hits': 0, 'stop_reason': None,
                   'realign_requested': False, 'last_command': None}
        saved = outbox.get('kit_state') or {}
        self.st = dict(default, **{k: v for k, v in saved.items() if k in default})
        if self.st['state'] not in STATES:
            self.st['state'] = 'IDLE'

    # ---- helpers
    @property
    def kit_id(self):
        return self.cfg['kit_id']

    def _save(self):
        self.outbox.put('kit_state', self.st)

    def _emit(self, event, detail=None):
        msg = {'schema': SCHEMA_EVENT, 'kind': 'event',
               'msg_id': '%s|evt|%d|%s' % (self.kit_id, time.time_ns(), secrets.token_hex(3)),
               'kit_id': self.kit_id, 'session_id': self.st['session_id'], 'cycle_no': self.st['cycle_no'],
               'event': event, 'state': self.st['state'], 'created_at': utc_now(), 'detail': detail or {}}
        self.outbox.enqueue(msg)
        return msg

    def _commit(self):
        self._save()
        self.on_enqueue()

    def _check_session(self, session_id):
        if self.st['state'] != 'ALIGNING':
            raise LinkRefused('보정 진행 중이 아닙니다 (현재 상태: %s)' % self.st['state'])
        if session_id != self.st['session_id']:
            raise LinkRefused('세션 번호가 현재 보정 세션과 다릅니다')

    @staticmethod
    def _readings(units, need_est):
        if not isinstance(units, list) or len(units) != 4:
            raise ValueError('코너 4개(장치 4대)의 값이 모두 있어야 합니다')
        out = []
        for k, u in enumerate(units):
            if not isinstance(u, dict):
                raise ValueError('코너 %d 값 형식 오류' % (k + 1))
            s = u.get('sensor_uH')
            if not isinstance(s, list) or len(s) != 5:
                raise ValueError('코너 %d: 5헤드 측정값(sensor_uH 5개)이 필요합니다' % (k + 1))
            row = {'sensor_uH': [_finite(v, '코너 %d 센서' % (k + 1)) for v in s]}
            if need_est:
                e = u.get('est_mm')
                if not isinstance(e, list) or len(e) != 2:
                    raise ValueError('코너 %d: 추정 오차 est_mm [x,y] 가 필요합니다' % (k + 1))
                row['est_mm'] = [_finite(v, '코너 %d 추정' % (k + 1)) for v in e]
            out.append(row)
        return out

    # ---- public API (simulator UI or hardware adapter)
    def status(self):
        with self.lock:
            s = {k: self.st[k] for k in ('state', 'session_id', 'cycle_no', 'round_no', 'source',
                                         'stop_reason', 'realign_requested', 'last_command', 'move_hits')}
            s.update(kit_id=self.kit_id, units=list(self.cfg['units']), max_rounds=self.cfg['max_rounds'],
                     server_url=self.cfg['server_url'], server_configured=bool(self.cfg['server_url']),
                     outbox=self.outbox.counts(), uplink=dict(self.uplink))
            return s

    def begin_cycle(self, source='simulation', meta=None):
        if source not in SOURCES:
            raise ValueError('source 는 simulation 또는 hardware')
        with self.lock:
            st = self.st
            reason = 'new_cycle'
            if st['realign_requested']:
                reason = 'realign_requested'
            elif st['state'] == 'STOPPED':
                if self.cfg['server_url']:
                    raise LinkRefused('정지 상태(%s)입니다. 메인 서버의 재보정(realign) 명령을 기다리는 중입니다.'
                                      % st['stop_reason'])
                reason = 'local_restart_no_server'  # store-only mode: nobody could send the command
            if st['state'] == 'ALIGNING':
                self._emit('cycle_aborted', {'reason': 'superseded_by_new_cycle', 'rounds_sent': st['round_no']})
            st.update(state='ALIGNING', cycle_no=st['cycle_no'] + 1, round_no=0, source=source,
                      session_id='%s-%s-%s' % (self.kit_id, datetime.now().strftime('%Y%m%d%H%M%S'),
                                               secrets.token_hex(2)),
                      last_readings=None, baseline=None, move_hits=0, stop_reason=None, realign_requested=False)
            self._emit('cycle_start', {'source': source, 'reason': reason, 'max_rounds': self.cfg['max_rounds'],
                                       'units': [{'corner': i + 1, 'unit_id': u} for i, u in enumerate(self.cfg['units'])],
                                       'meta': meta if isinstance(meta, dict) else {}})
            self._commit()
            return self.status()

    def record_round(self, session_id, round_no, units, command, measured_at=None, extra=None):
        with self.lock:
            self._check_session(session_id)
            st = self.st
            if round_no != st['round_no'] + 1:
                raise ValueError('보정 회차 순서 오류 (기대 %d, 받음 %r)' % (st['round_no'] + 1, round_no))
            if round_no > self.cfg['max_rounds']:
                raise LinkRefused('최대 보정 횟수(%d회)를 넘었습니다' % self.cfg['max_rounds'])
            rows = self._readings(units, need_est=True)
            if not isinstance(command, dict):
                raise ValueError('command 형식 오류')
            cmd = {k: _finite(command.get(k), 'command.' + k) for k in ('x_mm', 'y_mm', 'theta_deg')}
            if measured_at is not None and (not isinstance(measured_at, str) or len(measured_at) > 64):
                raise ValueError('measured_at 형식 오류')
            if extra is not None and (not isinstance(extra, dict) or len(json.dumps(extra)) > 4000):
                raise ValueError('extra 는 4KB 이하 JSON 객체')
            now = utc_now()
            bundle = {
                'schema': SCHEMA_ROUND, 'kind': 'round',
                'msg_id': '%s|r%02d' % (session_id, round_no),
                'kit_id': self.kit_id, 'session_id': session_id, 'cycle_no': st['cycle_no'],
                'round_no': round_no, 'max_rounds': self.cfg['max_rounds'], 'source': st['source'],
                'measured_at': measured_at or now, 'created_at': now,
                'units': [{'corner': i + 1, 'unit_id': self.cfg['units'][i], 'heads': HEADS,
                           'sensor_uH': r['sensor_uH'], 'est_mm': {'x': r['est_mm'][0], 'y': r['est_mm'][1]}}
                          for i, r in enumerate(rows)],
                'command': cmd,
            }
            if extra:
                bundle['extra'] = extra
            self.outbox.enqueue(bundle)
            st['round_no'] = round_no
            st['last_readings'] = [r['sensor_uH'] for r in rows]
            self._commit()
            return {'msg_id': bundle['msg_id'], 'round_no': round_no}

    def finish_cycle(self, session_id, converged, extra=None):
        with self.lock:
            self._check_session(session_id)
            st = self.st
            if not isinstance(converged, bool):
                raise ValueError('converged 는 true/false')
            if st['round_no'] < 1:
                raise ValueError('보정 회차가 하나도 기록되지 않았습니다')
            detail = {'rounds': st['round_no'], 'converged': converged}
            if isinstance(extra, dict) and len(json.dumps(extra)) <= 4000:
                detail['extra'] = extra
            if converged:
                st.update(state='HOLD', baseline=st['last_readings'], move_hits=0)
                self._emit('aligned', detail)
            else:
                st.update(state='STOPPED', stop_reason='align_failed')
                self._emit('align_failed', detail)
            self._commit()
            return self.status()

    def abort_cycle(self, session_id, reason='aborted'):
        with self.lock:
            self._check_session(session_id)
            self._emit('cycle_aborted', {'reason': str(reason)[:200], 'rounds_sent': self.st['round_no']})
            self.st.update(state='IDLE')
            self._commit()
            return self.status()

    def hold_reading(self, units, measured_at=None):
        """Periodic reading while aligned. Detect movement vs the aligned baseline."""
        with self.lock:
            st = self.st
            if st['state'] != 'HOLD' or not st['baseline']:
                raise LinkRefused('정렬 완료(HOLD) 상태에서만 이동 감시를 합니다 (현재: %s)' % st['state'])
            rows = self._readings(units, need_est=False)
            deltas = [max(abs(a - b) for a, b in zip(r['sensor_uH'], base))
                      for r, base in zip(rows, st['baseline'])]
            worst = max(deltas)
            moved = False
            if worst > self.cfg['movement_threshold_uH']:
                st['move_hits'] += 1
            else:
                st['move_hits'] = 0
            if st['move_hits'] >= self.cfg['movement_confirm']:
                moved = True
                detail = {'max_delta_uH': worst, 'corner_delta_uH': deltas,
                          'threshold_uH': self.cfg['movement_threshold_uH'],
                          'readings_uH': [r['sensor_uH'] for r in rows],
                          'measured_at': measured_at if isinstance(measured_at, str) else utc_now(),
                          'action': self.cfg['on_movement']}
                if self.cfg['on_movement'] == 'realign':
                    st.update(state='IDLE', realign_requested=True, stop_reason=None)
                else:
                    st.update(state='STOPPED', stop_reason='movement_detected')
                self._emit('movement_detected', detail)
                self._commit()
            else:
                self._save()
            return {'moved': moved, 'max_delta_uH': worst, 'move_hits': st['move_hits'], 'state': st['state']}

    def apply_command(self, cmd):
        """Command from the main server: realign | stop | hold. Always acknowledged."""
        with self.lock:
            if not isinstance(cmd, dict):
                return
            cid = str(cmd.get('id', ''))[:80]
            ctype = cmd.get('type')
            if cid and cid == (self.st['last_command'] or {}).get('id'):
                return  # duplicate delivery
            ok, note = True, ''
            if ctype == 'realign':
                if self.st['state'] == 'ALIGNING':
                    ok, note = False, 'already_aligning'
                else:
                    self.st.update(state='IDLE', realign_requested=True, stop_reason=None)
            elif ctype == 'stop':
                if self.st['state'] == 'ALIGNING':
                    self._emit('cycle_aborted', {'reason': 'server_stop', 'rounds_sent': self.st['round_no']})
                self.st.update(state='STOPPED', stop_reason='server_stop', realign_requested=False)
            elif ctype == 'hold':
                note = 'no_op'
            else:
                ok, note = False, 'unknown_command'
            self.st['last_command'] = {'id': cid, 'type': ctype, 'received_at': utc_now(), 'ok': ok}
            self._emit('command_ack', {'command_id': cid, 'type': ctype, 'ok': ok, 'note': note})
            self._commit()


# --------------------------------------------------------------------------- uploader
class Uploader(threading.Thread):
    """Background sender: pending bundles -> main server, oldest first, in batches."""

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
                           'kit_state': self.link.status()['state'],
                           'messages': [p for _, p in batch]}, ensure_ascii=False, allow_nan=False).encode('utf-8')
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
                   'User-Agent': 'MALIGN-Kit/1', 'X-MALIGN-Kit': self.link.kit_id}
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
            for cmd in reply.get('commands', []) or []:
                self.link.apply_command(cmd)
            self.link.uplink.update(last_error=None, last_ok_at=utc_now())
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
            # idle: wait for a new bundle, or send a heartbeat (also fetches server commands)
            self.wake_event.wait(self.cfg['heartbeat_s'])
            self.wake_event.clear()


# --------------------------------------------------------------------------- factory
def open_link(folder, urlopen=None, start=True):
    cfg, cfg_path = load_config(folder)
    outbox = Outbox(pathlib.Path(folder) / 'outbox.sqlite3')
    link = KitLink(outbox, cfg)
    uploader = Uploader(outbox, link, cfg, urlopen=urlopen)
    link.on_enqueue = uploader.wake
    if start:
        uploader.start()
    return link, uploader, cfg_path
