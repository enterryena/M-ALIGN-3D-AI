# -*- coding: utf-8 -*-
"""Example M-ALIGN main server. Python stdlib only.

Reference for the real main server and a test partner for the kit. It follows
the job sequence:

  1. machine is in position          -> POST /malign/v1/machines/<machine_id>/ready {"kit_id": ...}
  2. server -> kit: start_job        (kit senses round 1, sends a 4-unit bundle)
  3. server applies the correction to the machine, confirms the move
  4. server -> kit: sense            (kit senses round n+1, sends a bundle)  ... about 6 times
  5. server -> kit: end_job          then the machine does the main action (lower + lock)

AUTO mode (default) does 2-5 by itself: it assumes the machine applies the kit's
correction as-is after --move-delay seconds, and ends the job after --rounds rounds.
With --manual, commands are only queued through the HTTP API (a real controller's job).

    python malign_main_server.py --port 8750 [--rounds 6] [--move-delay 1] [--manual] [--api-key KEY]

Kit side (link_config.json):  "server_url": "http://<this PC IP>:8750/malign/v1/bundles"

Endpoints
  POST /malign/v1/bundles                       kit upload -> {accepted, rejected, commands}
  POST /malign/v1/machines/<machine_id>/ready   {"kit_id": "..."}  machine ready -> start_job for that kit
  POST /malign/v1/kits/<kit_id>/commands        {"type": "start_job"|"sense"|"end_job"|"stop", ...}
  GET  /malign/v1/kits                          kits, last state, counts
  GET  /malign/v1/kits/<kit_id>/messages[?session_id=...]
  GET  /malign/v1/jobs                          jobs and their round counts / result
"""
import argparse
import json
import pathlib
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

KNOWN = {'MALIGN_round_bundle_v1': 'round', 'MALIGN_kit_event_v1': 'event'}
ID = r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}'
KIT_PATH = re.compile(r'^/malign/v1/kits/(' + ID + r')/(commands|messages)$')
READY_PATH = re.compile(r'^/malign/v1/machines/(' + ID + r')/ready$')
COMMANDS = ('start_job', 'sense', 'end_job', 'stop')
MAX_BODY = 10 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


class Store:
    def __init__(self, path, auto=True, rounds=6, move_delay=1.0):
        self.path = str(path)
        self.auto, self.rounds, self.move_delay = auto, rounds, move_delay
        self.lock = threading.RLock()
        with self.db() as con:
            con.execute('''CREATE TABLE IF NOT EXISTS messages (
                msg_id TEXT PRIMARY KEY, kit_id TEXT NOT NULL, session_id TEXT, kind TEXT NOT NULL,
                round_no INTEGER, event TEXT, received_at TEXT NOT NULL, payload TEXT NOT NULL)''')
            con.execute('CREATE INDEX IF NOT EXISTS idx_msg_kit ON messages(kit_id, session_id)')
            con.execute('''CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, kit_id TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL,
                not_before REAL NOT NULL DEFAULT 0, delivered_at TEXT)''')
            con.execute('CREATE TABLE IF NOT EXISTS kits (kit_id TEXT PRIMARY KEY, last_seen TEXT, last_state TEXT)')
            con.execute('''CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, kit_id TEXT NOT NULL, machine_id TEXT, status TEXT NOT NULL,
                rounds INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, ended_at TEXT)''')

    @contextmanager
    def db(self):
        con = sqlite3.connect(self.path, timeout=30)
        try:
            with con:
                yield con
        finally:
            con.close()

    # ---- commands
    def _queue(self, con, kit, cmd, delay=0.0):
        if cmd.get('type') not in COMMANDS:
            raise ValueError('type 은 ' + ' / '.join(COMMANDS))
        cmd = dict(cmd, id='cmd-' + datetime.now().strftime('%Y%m%d%H%M%S') + '-' + secrets.token_hex(3))
        con.execute('INSERT INTO commands VALUES (?,?,?,?,?,NULL)',
                    (cmd['id'], kit, json.dumps(cmd), now(), time.time() + delay))
        return cmd

    def _active_job(self, con, kit):
        row = con.execute("SELECT job_id FROM jobs WHERE kit_id=? AND status IN ('requested','running')", (kit,)).fetchone()
        return row[0] if row else None

    def _start_job(self, con, kit, machine_id):
        if self._active_job(con, kit):
            raise ValueError('이 키트는 이미 작업 중입니다')
        job = 'JOB-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(2)
        con.execute("INSERT INTO jobs VALUES (?,?,?, 'requested', 0, ?, NULL)", (job, kit, machine_id, now()))
        return self._queue(con, kit, {'type': 'start_job', 'job_id': job, 'machine_id': machine_id})

    def machine_ready(self, machine_id, kit):
        if not isinstance(kit, str) or not re.fullmatch(ID, kit):
            raise ValueError('kit_id 필요')
        with self.lock, self.db() as con:
            return self._start_job(con, kit, machine_id)

    def queue(self, kit, cmd):
        if not isinstance(cmd, dict):
            raise ValueError('JSON 객체 필요')
        with self.lock, self.db() as con:
            if cmd.get('type') == 'start_job':
                return self._start_job(con, kit, cmd.get('machine_id'))
            keep = {k: cmd[k] for k in ('type', 'session_id', 'round_no', 'result', 'reason') if k in cmd}
            return self._queue(con, kit, keep)

    # ---- kit upload
    def _react(self, con, kit, m, kind):
        sid = m.get('session_id')
        if kind == 'event':
            ev = m.get('event')
            if ev == 'sim_machine_ready' and self.auto:
                try:
                    self._start_job(con, kit, (m.get('detail') or {}).get('machine_id'))
                except ValueError:
                    pass
            elif ev == 'job_start':
                con.execute("UPDATE jobs SET status='running' WHERE job_id=?", (sid,))
                con.execute("INSERT OR IGNORE INTO jobs VALUES (?,?,?, 'running', 0, ?, NULL)",
                            (sid, kit, (m.get('detail') or {}).get('machine_id'), now()))
            elif ev in ('job_end', 'job_stopped', 'job_aborted'):
                con.execute('UPDATE jobs SET status=?, ended_at=? WHERE job_id=?', (ev[4:], now(), sid))
        elif kind == 'round':
            con.execute('UPDATE jobs SET rounds=MAX(rounds, ?) WHERE job_id=?', (m['round_no'], sid))
            if self.auto:
                # Here a real server would weigh m['correction'] against the machine's own data,
                # drive the machine, confirm the move — then ask for the next sensing.
                if m['round_no'] >= self.rounds:
                    self._queue(con, kit, {'type': 'end_job', 'session_id': sid, 'result': 'main_action'},
                                self.move_delay)
                else:
                    self._queue(con, kit, {'type': 'sense', 'session_id': sid, 'round_no': m['round_no'] + 1},
                                self.move_delay)

    def ingest(self, batch):
        if not isinstance(batch, dict) or batch.get('schema') != 'MALIGN_upload_batch_v1':
            raise ValueError('batch schema 오류')
        kit = batch.get('kit_id')
        msgs = batch.get('messages')
        if not isinstance(kit, str) or not isinstance(msgs, list):
            raise ValueError('kit_id / messages 형식 오류')
        accepted, rejected = [], []
        with self.lock, self.db() as con:
            for m in msgs:
                mid = m.get('msg_id') if isinstance(m, dict) else None
                try:
                    if not isinstance(mid, str) or not mid:
                        raise ValueError('msg_id 없음')
                    kind = KNOWN.get(m.get('schema'))
                    if not kind or m.get('kit_id') != kit:
                        raise ValueError('schema 또는 kit_id 불일치')
                    if kind == 'round':
                        units = m.get('units')
                        if not isinstance(units, list) or len(units) != 4 or \
                                len({u.get('unit_id') for u in units if isinstance(u, dict)}) != 4:
                            raise ValueError('장치 4대 묶음이 아님')
                        if not isinstance(m.get('round_no'), int):
                            raise ValueError('round_no 없음')
                    n0 = con.total_changes
                    con.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?)',
                                (mid, kit, m.get('session_id'), kind, m.get('round_no'), m.get('event'), now(),
                                 json.dumps(m, ensure_ascii=False)))
                    if con.total_changes > n0:  # react once; a resent duplicate is only acknowledged
                        self._react(con, kit, m, kind)
                    accepted.append(mid)
                except ValueError as exc:
                    if isinstance(mid, str):
                        rejected.append({'msg_id': mid, 'error': str(exc)})
            con.execute('INSERT OR REPLACE INTO kits VALUES (?,?,?)', (kit, now(), str(batch.get('kit_state'))))
            rows = con.execute('SELECT id, body FROM commands WHERE kit_id=? AND delivered_at IS NULL AND not_before<=? '
                               'ORDER BY created_at', (kit, time.time())).fetchall()
            con.executemany('UPDATE commands SET delivered_at=? WHERE id=?', [(now(), i) for i, _ in rows])
        return {'accepted': accepted, 'rejected': rejected, 'commands': [json.loads(b) for _, b in rows]}

    # ---- views
    def kits(self):
        with self.db() as con:
            out = []
            for kit, seen, state in con.execute('SELECT * FROM kits ORDER BY kit_id').fetchall():
                n = dict(con.execute('SELECT kind, COUNT(*) FROM messages WHERE kit_id=? GROUP BY kind', (kit,)).fetchall())
                out.append({'kit_id': kit, 'last_seen': seen, 'last_state': state,
                            'rounds': n.get('round', 0), 'events': n.get('event', 0)})
            return out

    def jobs(self):
        with self.db() as con:
            cols = ('job_id', 'kit_id', 'machine_id', 'status', 'rounds', 'created_at', 'ended_at')
            return [dict(zip(cols, r)) for r in con.execute('SELECT * FROM jobs ORDER BY created_at').fetchall()]

    def messages(self, kit, session_id=None):
        with self.db() as con:
            q, args = 'SELECT payload FROM messages WHERE kit_id=?', [kit]
            if session_id:
                q += ' AND session_id=?'
                args.append(session_id)
            return [json.loads(p) for (p,) in con.execute(q + ' ORDER BY rowid', args).fetchall()]


def make_handler(store, api_key):
    class H(BaseHTTPRequestHandler):
        server_version = 'MALIGN-Main/2'

        def log_message(self, fmt, *args):
            pass

        def _json(self, obj, status=200):
            raw = json.dumps(obj, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _auth(self):
            if api_key and self.headers.get('Authorization') != 'Bearer ' + api_key:
                self._json({'error': 'unauthorized'}, 401)
                return False
            return True

        def do_GET(self):
            if not self._auth():
                return
            u = urlparse(self.path)
            if u.path == '/malign/v1/kits':
                return self._json(store.kits())
            if u.path == '/malign/v1/jobs':
                return self._json(store.jobs())
            m = KIT_PATH.match(u.path)
            if m and m.group(2) == 'messages':
                return self._json(store.messages(m.group(1), parse_qs(u.query).get('session_id', [None])[0]))
            self._json({'error': 'not found'}, 404)

        def do_POST(self):
            if not self._auth():
                return
            try:
                n = int(self.headers.get('Content-Length', '-1'))
                if not 0 <= n <= MAX_BODY:
                    return self._json({'error': 'payload too large'}, 413)
                obj = json.loads(self.rfile.read(n).decode('utf-8') or '{}')
                path = urlparse(self.path).path
                if path == '/malign/v1/bundles':
                    return self._json(store.ingest(obj))
                m = READY_PATH.match(path)
                if m:
                    return self._json(store.machine_ready(m.group(1), obj.get('kit_id')))
                m = KIT_PATH.match(path)
                if m and m.group(2) == 'commands':
                    return self._json(store.queue(m.group(1), obj))
                self._json({'error': 'not found'}, 404)
            except (ValueError, UnicodeError, AttributeError) as exc:
                self._json({'error': str(exc)}, 400)
    return H


def main():
    ap = argparse.ArgumentParser(description='M-ALIGN example main server')
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8750)
    ap.add_argument('--db', default=str(pathlib.Path(__file__).with_name('main_server_v2.sqlite3')))
    ap.add_argument('--api-key', default='')
    ap.add_argument('--rounds', type=int, default=6, help='AUTO: rounds before end_job')
    ap.add_argument('--move-delay', type=float, default=1.0, help='AUTO: seconds the machine takes per correction')
    ap.add_argument('--manual', action='store_true', help='no automatic sense/end_job; use the commands API')
    a = ap.parse_args()
    store = Store(a.db, auto=not a.manual, rounds=a.rounds, move_delay=a.move_delay)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(store, a.api_key))
    print(json.dumps({'listening': '%s:%d' % (a.host, srv.server_port), 'db': a.db,
                      'mode': 'manual' if a.manual else 'auto', 'rounds': a.rounds}), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == '__main__':
    main()
