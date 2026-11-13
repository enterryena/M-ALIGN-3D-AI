# -*- coding: utf-8 -*-
"""Example M-ALIGN main server (receiver). Python stdlib only.

Receives the kit's per-round bundles, stores them (duplicate-safe by msg_id),
and hands queued commands (realign / stop / hold) back to the kit in the reply.
Use it to test the kit link, or as the reference for the real main server.

    python malign_main_server.py --port 8750 [--api-key SECRET] [--db main_server.sqlite3]

Kit side (link_config.json):  "server_url": "http://<this PC IP>:8750/malign/v1/bundles"

Endpoints
  POST /malign/v1/bundles                 kit upload (batch of rounds/events) -> {accepted, rejected, commands}
  POST /malign/v1/kits/<kit_id>/commands  {"type": "realign"|"stop"|"hold"} queue a command for a kit
  GET  /malign/v1/kits                    kits seen, last state, counts
  GET  /malign/v1/kits/<kit_id>/messages[?session_id=...]   stored rounds/events
"""
import argparse
import json
import pathlib
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

KNOWN = {'MALIGN_round_bundle_v1': 'round', 'MALIGN_kit_event_v1': 'event'}
KIT_PATH = re.compile(r'^/malign/v1/kits/([A-Za-z0-9][A-Za-z0-9._-]{0,63})/(commands|messages)$')
MAX_BODY = 10 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.Lock()
        with self.db() as con:
            con.execute('''CREATE TABLE IF NOT EXISTS messages (
                msg_id TEXT PRIMARY KEY, kit_id TEXT NOT NULL, session_id TEXT, kind TEXT NOT NULL,
                round_no INTEGER, event TEXT, received_at TEXT NOT NULL, payload TEXT NOT NULL)''')
            con.execute('CREATE INDEX IF NOT EXISTS idx_msg_kit ON messages(kit_id, session_id)')
            con.execute('''CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, kit_id TEXT NOT NULL, type TEXT NOT NULL, created_at TEXT NOT NULL,
                delivered_at TEXT)''')
            con.execute('''CREATE TABLE IF NOT EXISTS kits (
                kit_id TEXT PRIMARY KEY, last_seen TEXT, last_state TEXT)''')

    @contextmanager
    def db(self):
        con = sqlite3.connect(self.path, timeout=30)
        try:
            with con:
                yield con
        finally:
            con.close()

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
                    con.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?)',
                                (mid, kit, m.get('session_id'), kind, m.get('round_no'), m.get('event'), now(),
                                 json.dumps(m, ensure_ascii=False)))
                    accepted.append(mid)  # duplicates are accepted too (idempotent)
                except ValueError as exc:
                    if isinstance(mid, str):
                        rejected.append({'msg_id': mid, 'error': str(exc)})
            con.execute('INSERT OR REPLACE INTO kits VALUES (?,?,?)', (kit, now(), str(batch.get('kit_state'))))
            cmds = [{'id': i, 'type': t} for i, t in con.execute(
                'SELECT id, type FROM commands WHERE kit_id=? AND delivered_at IS NULL ORDER BY created_at', (kit,))]
            con.executemany('UPDATE commands SET delivered_at=? WHERE id=?', [(now(), c['id']) for c in cmds])
        return {'accepted': accepted, 'rejected': rejected, 'commands': cmds}

    def queue(self, kit, ctype):
        if ctype not in ('realign', 'stop', 'hold'):
            raise ValueError('type 은 realign / stop / hold')
        cid = 'cmd-' + datetime.now().strftime('%Y%m%d%H%M%S') + '-' + secrets.token_hex(3)
        with self.lock, self.db() as con:
            con.execute('INSERT INTO commands VALUES (?,?,?,?,NULL)', (cid, kit, ctype, now()))
        return {'id': cid, 'type': ctype}

    def kits(self):
        with self.db() as con:
            out = []
            for kit, seen, state in con.execute('SELECT * FROM kits ORDER BY kit_id'):
                n = dict(con.execute('SELECT kind, COUNT(*) FROM messages WHERE kit_id=? GROUP BY kind', (kit,)))
                out.append({'kit_id': kit, 'last_seen': seen, 'last_state': state,
                            'rounds': n.get('round', 0), 'events': n.get('event', 0)})
            return out

    def messages(self, kit, session_id=None):
        with self.db() as con:
            q, args = 'SELECT payload FROM messages WHERE kit_id=?', [kit]
            if session_id:
                q += ' AND session_id=?'
                args.append(session_id)
            return [json.loads(p) for (p,) in con.execute(q + ' ORDER BY received_at, msg_id', args)]


def make_handler(store, api_key):
    class H(BaseHTTPRequestHandler):
        server_version = 'MALIGN-Main/1'

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
            m = KIT_PATH.match(u.path)
            if m and m.group(2) == 'messages':
                sid = parse_qs(u.query).get('session_id', [None])[0]
                return self._json(store.messages(m.group(1), sid))
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
                m = KIT_PATH.match(path)
                if m and m.group(2) == 'commands':
                    return self._json(store.queue(m.group(1), obj.get('type')))
                self._json({'error': 'not found'}, 404)
            except (ValueError, UnicodeError, AttributeError) as exc:
                self._json({'error': str(exc)}, 400)
    return H


def main():
    ap = argparse.ArgumentParser(description='M-ALIGN example main server')
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8750)
    ap.add_argument('--db', default=str(pathlib.Path(__file__).with_name('main_server.sqlite3')))
    ap.add_argument('--api-key', default='')
    a = ap.parse_args()
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(Store(a.db), a.api_key))
    print(json.dumps({'listening': '%s:%d' % (a.host, srv.server_port), 'db': a.db}), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == '__main__':
    main()
