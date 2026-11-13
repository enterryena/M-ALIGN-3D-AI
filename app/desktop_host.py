# -*- coding: utf-8 -*-
"""M-ALIGN Windows desktop host: bundled Matrix5 HTML + durable SQLite casebank.

Browser UI on a random 127.0.0.1 port, records in
%LOCALAPPDATA%\\MALIGN\\CaseBank\\cases.sqlite3. No camera. Python stdlib only.

v3.6 kit link: on the main server's command the 4-unit kit senses once; each
result (one bundle) is stored in
%LOCALAPPDATA%\\MALIGN\\Link\\outbox.sqlite3 and, ONLY if server_url is set in
Link\\link_config.json, sent to that main server (see malign_link.py).
"""
import argparse
import base64
import subprocess
import tempfile
from contextlib import contextmanager, closing
import json
import logging
import os
import pathlib
import secrets
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import malign_link

APP_TITLE = 'M-ALIGN Matrix5 — 영구 사례 누적 + 메인 서버 연동 (v3.6.0)'
APP_HTML = 'MALIGN_Desktop.html'
MAX_POST = 25 * 1024 * 1024


def resource_path(name):
    root = pathlib.Path(getattr(sys, '_MEIPASS', pathlib.Path(__file__).resolve().parent))
    return root / name


def archive_folder():
    """Version-independent, EXE-location-independent path, fixed for each Windows profile.

    Never silently redirect Windows storage to a different location: that can
    make a user's previous database appear lost after an update.
    """
    base = os.environ.get('LOCALAPPDATA')
    if sys.platform == 'win32' and not base:
        raise RuntimeError('Windows LOCALAPPDATA 경로를 확인할 수 없습니다. 기존 데이터가 다른 폴더에 저장되지 않도록 실행을 중단했습니다.')
    if not base:
        base = str(pathlib.Path.home() / '.local' / 'share')  # Linux test host only
    path = pathlib.Path(base) / 'MALIGN' / 'CaseBank'
    path.mkdir(parents=True, exist_ok=True)
    return path


class CaseBank:
    def __init__(self, file_path):
        self.file_path = pathlib.Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def conn(self):
        con = sqlite3.connect(str(self.file_path), timeout=30)
        con.execute('PRAGMA busy_timeout=30000')
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA synchronous=FULL')
        return con

    @contextmanager
    def connection(self):
        """Transactional SQLite context that ALWAYS closes the OS file handle.

        In sqlite3, ``with connection`` only commits/rolls back; it does
        not close the connection. Windows can reject file renames otherwise.
        """
        con = self.conn()
        try:
            with con:
                yield con
        finally:
            con.close()

    def _init(self):
        with self.connection() as con:
            con.execute('''CREATE TABLE IF NOT EXISTS cases (
                uid TEXT PRIMARY KEY,
                outcome TEXT NOT NULL CHECK (outcome IN ('success','failure')),
                saved_order REAL NOT NULL,
                saved_at TEXT NOT NULL,
                payload TEXT NOT NULL
            )''')
            con.execute('CREATE INDEX IF NOT EXISTS idx_cases_outcome ON cases(outcome)')
            con.execute('CREATE INDEX IF NOT EXISTS idx_cases_saved_order ON cases(saved_order)')
            con.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
            con.execute("INSERT OR IGNORE INTO meta (key,value) VALUES ('schema','MALIGN_v35_casebank_v1')")
        self.verify_integrity()

    def verify_integrity(self):
        with self.connection() as con:
            check = con.execute('PRAGMA quick_check').fetchone()
            if not check or check[0] != 'ok':
                raise RuntimeError('사례 데이터베이스 무결성 오류. 파일을 덮어쓰지 않고 중단합니다. 백업을 확인하세요.')
            version = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if not version or version[0] != 'MALIGN_v35_casebank_v1':
                raise RuntimeError('지원하지 않는 저장 형식입니다. 이전 기록을 변경하지 않고 중단합니다.')

    def daily_backup(self, retain=14):
        """Take one complete daily snapshot without clobbering any prior backup.

        A UNIQUE temporary file prevents collision with v3.5.1's potentially
        locked MALIGN_daily_YYYYMMDD.building.sqlite3. The temp file is
        renamed only AFTER both SQLite backup connections are closed.
        """
        if self.stats()['total'] == 0:
            return None
        backup_dir = self.file_path.parent / 'backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime('%Y%m%d')
        target = backup_dir / ('MALIGN_daily_' + today + '.sqlite3')
        if not target.exists():
            fd, unique_path = tempfile.mkstemp(
                prefix='MALIGN_daily_' + today + '.new_',
                suffix='.sqlite3', dir=str(backup_dir))
            os.close(fd)
            temporary = pathlib.Path(unique_path)
            try:
                self.backup(temporary)
                # On Windows, antivirus scanners can briefly hold a freshly
                # closed file. Retry only rename; NEVER move/delete the DB.
                for attempt in range(8):
                    try:
                        # Do not overwrite another process's completed backup.
                        temporary.rename(target)
                        break
                    except FileExistsError:
                        # Another instance finished first; its backup stays.
                        break
                    except PermissionError:
                        if target.exists():
                            break
                        if attempt == 7:
                            raise
                        time.sleep(0.125 * (attempt + 1))
            finally:
                # Clean up only the unique temporary file created by THIS
                # process. Never touch old locked .building files or real DB.
                if temporary.exists():
                    try:
                        temporary.unlink()
                    except PermissionError:
                        logging.warning('보조 백업 임시 파일 정리 지연: %s', temporary)
        daily = sorted(backup_dir.glob('MALIGN_daily_????????.sqlite3'))
        for obsolete in daily[:-retain]:
            try:
                obsolete.unlink()
            except OSError as exc:
                # Retention cleanup is optional: backups must NEVER prevent
                # launching the simulator or block access to actual records.
                logging.warning('오래된 백업 보존(정리 실패): %s (%s)', obsolete, exc)
        return target

    @staticmethod
    def _validate(r):
        if not isinstance(r, dict):
            raise ValueError('사례는 JSON 객체여야 합니다')
        uid = r.get('uid')
        outcome = r.get('outcome')
        if not isinstance(uid, str) or not 1 <= len(uid) <= 350:
            raise ValueError('사례 UID 형식 오류')
        if outcome not in ('success','failure'):
            raise ValueError('성공/실패 값 오류')
        saved_order = r.get('saved_order', time.time() * 1000)
        if not isinstance(saved_order, (int, float)) or isinstance(saved_order, bool):
            raise ValueError('저장 순번 오류')
        saved_at = r.get('saved_at', datetime.now(timezone.utc).isoformat())
        if not isinstance(saved_at, str) or len(saved_at) > 128:
            raise ValueError('저장 시각 오류')
        payload = json.dumps(r, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        if len(payload.encode('utf-8')) > 400_000:
            raise ValueError('개별 사례가 너무 큽니다')
        return (uid, outcome, float(saved_order), saved_at, payload)

    def add(self, records):
        if not isinstance(records, list) or len(records) > 2500:
            raise ValueError('사례 배열 크기 오류 (최대 2500건)')
        rows = [self._validate(r) for r in records]
        with self.connection() as con:
            n0 = con.total_changes
            con.executemany('INSERT OR IGNORE INTO cases(uid,outcome,saved_order,saved_at,payload) VALUES(?,?,?,?,?)', rows)
            return con.total_changes - n0

    def stats(self):
        with self.connection() as con:
            counters = dict(con.execute('SELECT outcome, COUNT(*) FROM cases GROUP BY outcome').fetchall())
            total = sum(counters.values())
            recent = []
            # Pull enough recent entries to find 3 successful cases in a typical batch.
            for (payload,) in con.execute("SELECT payload FROM cases WHERE outcome='success' ORDER BY saved_order DESC LIMIT 3"):
                r = json.loads(payload)
                recent.append({
                    'source_seed': r.get('source_seed'), 'trial_index': r.get('trial_index'),
                    'final_mm': r.get('final_worst_axis_mm'), 'scenario': r.get('scenario'),
                    'estimator': r.get('estimator'), 'saved_at': r.get('saved_at')
                })
            return {'total': total, 'success': counters.get('success', 0),
                    'failure': counters.get('failure', 0), 'recent': recent}

    def all(self):
        with self.connection() as con:
            return [json.loads(x[0]) for x in con.execute('SELECT payload FROM cases ORDER BY saved_order ASC')]

    def clear(self):
        with self.connection() as con:
            con.execute('DELETE FROM cases')

    def backup(self, output_path):
        # Online consistent SQLite backup. Does not interfere with saving records.
        target = pathlib.Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # sqlite3's context manager does NOT close its file handle. Explicit
        # closing is essential before renaming/copying a backup on Windows.
        with closing(self.conn()) as source:
            with closing(sqlite3.connect(str(target), timeout=30)) as dest:
                source.backup(dest)
                dest.commit()
        return str(target)


def make_handler(bank, token, link=None):
    class LocalHandler(BaseHTTPRequestHandler):
        server_version = 'MALIGN-Local/3.5'
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            logging.info('%s - %s', self.client_address[0], fmt % args)

        def _send(self, status, raw, mime):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            self.wfile.write(raw)

        def _json(self, value, status=200):
            self._send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'), 'application/json; charset=utf-8')

        def _authorized(self):
            host = self.headers.get('Host', '')
            if host not in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'):
                return False
            if self.headers.get('X-MALIGN-Token') != token:
                return False
            origin = self.headers.get('Origin')
            if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'):
                return False
            return True

        def do_GET(self):
            if self.client_address[0] not in ('127.0.0.1', '::1'):
                self._json({'error': 'loopback only'}, 403)
                return
            p = urlparse(self.path).path
            if p in ('/', '/index.html'):
                content = resource_path(APP_HTML).read_text(encoding='utf-8')
                inject = ('<script>window.MALIGN_DESKTOP_TOKEN=' + json.dumps(token) + ';</script>')
                content = content.replace('</head>', inject + '</head>', 1)
                self._send(200, content.encode('utf-8'), 'text/html; charset=utf-8')
                return
            if p == '/favicon.ico':
                self._send(204, b'', 'image/x-icon')
                return
            if not self._authorized():
                self._json({'error': 'unauthorized'}, 403)
                return
            if p == '/api/link/status':
                if link is None:
                    self._json({'error': 'link disabled'}, 404)
                else:
                    self._json(link.status())
            elif p == '/api/stats':
                self._json(bank.stats())
            elif p == '/api/all':
                self._json(bank.all())
            elif p == '/api/health':
                self._json({'ok': True, 'mode': 'desktop_disk', 'db': str(bank.file_path), 'count': bank.stats()['total'], 'schema': 'MALIGN_v35_casebank_v1'})
            else:
                self._json({'error': 'not found'}, 404)

        def do_POST(self):
            if not self._authorized():
                self._json({'error': 'unauthorized'}, 403)
                return
            if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                self._json({'error': 'requires application/json'}, 415)
                return
            try:
                length = int(self.headers.get('Content-Length', '-1'))
                if not 0 <= length <= MAX_POST:
                    self._json({'error': 'payload too large'}, 413)
                    return
                obj = json.loads(self.rfile.read(length).decode('utf-8'))
                if not isinstance(obj, dict):
                    raise ValueError('요청 JSON 객체 형식이 아닙니다')
                p = urlparse(self.path).path
                if p == '/api/add':
                    n = bank.add(obj.get('records'))
                    self._json({'added': n})
                elif p.startswith('/api/link/') and link is not None:
                    self._json(link_post(link, p[len('/api/link/'):], obj))
                elif p == '/api/clear':
                    if obj.get('confirm') != 'CLEAR':
                        raise ValueError('confirmation required')
                    # Backup before clearing, never overwrite an earlier backup.
                    name = 'MALIGN_preclear_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.sqlite3'
                    bank.backup(bank.file_path.parent / 'backups' / name)
                    bank.clear()
                    self._json({'ok': True})
                else:
                    self._json({'error': 'not found'}, 404)
            except malign_link.LinkRefused as exc:
                self._json({'error': str(exc), 'state': link.status()['state'] if link else None}, 409)
            except LookupError as exc:
                self._json({'error': str(exc)}, 404)
            except (ValueError, TypeError, UnicodeError, sqlite3.Error) as exc:
                self._json({'error': str(exc)}, 400)

    return LocalHandler


def link_post(link, action, obj):
    """Local kit-link API for the sensing source (simulator UI now, hardware reader later).

    The main server drives each job: poll GET /api/link/status and, when
    status['pending'] asks for a sensing, answer with /round (or /sense_failed).
    """
    if action == 'round':
        return link.record_round(obj.get('session_id'), obj.get('round_no'), obj.get('units'),
                                 obj.get('correction'), obj.get('measured_at'), obj.get('extra'))
    if action == 'sense_failed':
        return link.sense_failed(obj.get('session_id'), obj.get('round_no'), obj.get('reason', ''))
    if action == 'abort':
        return link.abort_local(obj.get('session_id'), obj.get('reason', 'local_abort'))
    if action == 'sim_machine_ready':
        return link.sim_machine_ready(obj.get('machine_id', 'SIM-MACHINE-1'))
    raise LookupError('not found')


def create_server(bank, link=None):
    token = secrets.token_urlsafe(32)
    srv = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(bank, token, link))
    srv.daemon_threads = True
    return srv, token


def start():
    ap = argparse.ArgumentParser(description=APP_TITLE)
    ap.add_argument('--headless', action='store_true', help='test mode; run local server until interrupted')
    ap.add_argument('--data-dir', default=None, help='test-only database path override')
    args = ap.parse_args()
    folder = pathlib.Path(args.data_dir) if args.data_dir else archive_folder()
    bank = CaseBank(folder / 'cases.sqlite3')
    backup_warning = None
    try:
        bank.daily_backup()
    except OSError as exc:
        # A non-critical backup failure must not make the app appear to lose
        # its case history. Never silently switch DBs or suppress the warning.
        backup_warning = f'{type(exc).__name__}: {exc}'
        logging.warning('일일 백업 실패. 실제 사례 DB는 그대로 사용: %s', backup_warning)
        if sys.stderr is not None:
            print('M-ALIGN WARNING: daily backup failed: ' + backup_warning, file=sys.stderr, flush=True)
    link_dir = folder / 'Link' if args.data_dir else folder.parent / 'Link'
    link, uploader, link_cfg_path = malign_link.open_link(link_dir)
    server, token = create_server(bank, link)
    port = server.server_port
    url = f'http://127.0.0.1:{port}/'
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    if args.headless:
        print(json.dumps({'url': url, 'token': token, 'database': str(bank.file_path),
                          'link_config': str(link_cfg_path), 'kit_id': link.kit_id}), flush=True)
        try:
            while True:
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            uploader.stop()
            server.shutdown()
            server.server_close()
        return
    import tkinter as tk
    from tkinter import ttk, messagebox
    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry('760x470')
    if backup_warning:
        root.after(500, lambda: messagebox.showwarning(
            'M-ALIGN 백업 경고',
            '기존 사례 데이터는 정상적으로 열렸지만 오늘의 백업 생성에 실패했습니다.\n'
            '사용 중 기록은 원래의 저장 위치에 계속 저장됩니다.\n'
            '저장 기록 백업 버튼으로 별도 백업을 확인하세요.\n\n'
            + backup_warning))
    root.minsize(670, 300)
    frame = ttk.Frame(root, padding=18)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='M-ALIGN | 센서 이력 + 누적 사례 데스크톱', font=('Malgun Gothic', 15, 'bold')).pack(anchor='w', pady=(0, 8))
    ttk.Label(frame, text='HTML 화면은 로컬 브라우저에서 열립니다. 이 창은 데이터 저장 서버입니다.\n창을 닫으면 서버는 종료되지만 저장된 성공·실패 기록은 남습니다.', justify='left').pack(anchor='w')
    ttk.Label(frame, text='영구 저장 위치: ' + str(bank.file_path), wraplength=650).pack(anchor='w', pady=(12, 4))
    stats = tk.StringVar(value='저장 건수 확인 중')
    ttk.Label(frame, textvariable=stats).pack(anchor='w', pady=(0, 4))
    link_text = tk.StringVar(value='키트 전송 상태 확인 중')
    ttk.Label(frame, textvariable=link_text, wraplength=650, justify='left').pack(anchor='w', pady=(0, 12))
    bar = ttk.Frame(frame)
    bar.pack(anchor='w')
    def update_stats():
        try:
            val = bank.stats()
            stats.set(f"누적 성공 {val['success']:,}건 / 실패 {val['failure']:,}건 / 전체 {val['total']:,}건")
        except Exception as exc:
            stats.set('저장소 확인 실패: ' + str(exc))
        try:
            ls = link.status()
            server_txt = ls['server_url'] or '미설정 (PC에만 보관, 내장 시뮬 서버가 지시)'
            err = ls['uplink']['last_error']
            link_text.set(f"키트 {ls['kit_id']} · 상태 {ls['state']} (회차 {ls['round_no']}) · 메인 서버 {server_txt}\n"
                          f"전송 대기 {ls['outbox']['pending']:,}건 / 전송 완료 {ls['outbox']['sent']:,}건"
                          + (f" · 최근 오류: {err}" if err else ''))
        except Exception as exc:
            link_text.set('키트 전송 상태 확인 실패: ' + str(exc))
        if root.winfo_exists():
            root.after(1600, update_stats)
    def open_browser():
        try:
            if not webbrowser.open(url):
                raise RuntimeError('기본 브라우저에서 열지 못했습니다.')
        except Exception as exc:
            messagebox.showwarning('브라우저 열기 실패', str(exc) + '\n주소 복사 버튼을 눌러 Chrome/Edge 주소창에 붙여넣으세요.')
    ttk.Button(bar, text='시뮬레이터 열기', command=open_browser).pack(side='left', padx=(0, 7))
    def copy_url():
        root.clipboard_clear()
        root.clipboard_append(url)
        root.update()
        messagebox.showinfo('주소 복사', '로컬 시뮬레이터 주소를 복사했습니다.\n' + url)
    ttk.Button(bar, text='주소 복사', command=copy_url).pack(side='left', padx=(0, 7))
    def open_dir():
        if sys.platform == 'win32':
            os.startfile(str(folder))
        else:
            messagebox.showinfo('저장 위치', str(folder))
    ttk.Button(bar, text='저장 폴더 열기', command=open_dir).pack(side='left', padx=(0, 7))
    def back_up():
        name = 'MALIGN_backup_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.sqlite3'
        try:
            output = bank.backup(folder / 'backups' / name)
            messagebox.showinfo('백업 완료', '기록 백업을 저장했습니다:\n' + output)
        except Exception as exc:
            messagebox.showerror('백업 실패', str(exc))
    ttk.Button(bar, text='저장 기록 백업', command=back_up).pack(side='left', padx=(0, 7))
    def open_link_config():
        # Server address/kit numbers are edited ONLY in this local file, never from the web page.
        if sys.platform == 'win32':
            os.startfile(str(link_cfg_path))
        else:
            messagebox.showinfo('전송 설정 파일', str(link_cfg_path))
        messagebox.showinfo('전송 설정', '설정 파일을 수정·저장한 뒤 프로그램을 다시 시작해야 적용됩니다.')
    ttk.Button(bar, text='전송 설정', command=open_link_config).pack(side='left', padx=(0, 7))
    # Full uninstall is deliberately separate from "clear cases": it removes ALL
    # app-owned persistent DBs, backups, and logs, then the bundled EXE.
    def remove_app():
        from tkinter import simpledialog
        if not messagebox.askyesno(
                'M-ALIGN 완전 제거',
                '이 Windows 계정의 M-ALIGN 누적 성공/실패 사례, DB, 모든 백업·로그·설정이 영구 삭제됩니다.\n'
                f"메인 서버로 아직 보내지 못한 보정 기록 {link.status()['outbox']['pending']:,}건도 함께 삭제됩니다.\n"
                '앱 실행 파일(EXE)도 함께 삭제합니다.\n\n'
                '한 번 삭제하면 복구하기 어려우며 기존 데이터의 자동 백업은 남기지 않습니다.\n'
                '정말로 완전히 제거할까요?', icon='warning'):
            return
        answer = simpledialog.askstring('최종 삭제 확인', '계속하려면 아래에 "완전삭제" 를 입력하세요:', parent=root)
        if answer != '완전삭제':
            messagebox.showinfo('제거 취소', '아무것도 삭제하지 않았습니다.')
            return
        if sys.platform != 'win32':
            messagebox.showerror('지원되지 않는 환경', '완전 제거 기능은 Windows 배포판에서만 지원합니다.')
            return
        exe_path = pathlib.Path(sys.executable).resolve() if getattr(sys, 'frozen', False) else None
        if exe_path and exe_path.name.lower() != 'malign_desktop.exe':
            messagebox.showerror('실행 경로 확인 실패', '배포 실행 파일명이 MALIGN_Desktop.exe가 아니어서 안전하게 제거할 수 없습니다.')
            return
        try:
            # Pass uninstall PowerShell from bundled resources in-memory;
            # no disposable script is left in TEMP or the install folder.
            script = resource_path('UNINSTALL_MALIGN.ps1').read_text(encoding='utf-8-sig')
            exe_b64 = base64.b64encode(str(exe_path or '').encode('utf-8')).decode('ascii')
            cmd = ('& {\n' + script + '\n} '
                   '-AppExePath ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(\'' + exe_b64 + '\'))) '
                   '-WaitPid ' + str(os.getpid()))
            encoded = base64.b64encode(cmd.encode('utf-16le')).decode('ascii')
            subprocess.Popen(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                              '-EncodedCommand', encoded],
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                             close_fds=True)
        except Exception as exc:
            messagebox.showerror('제거 준비 실패', '제거 도구를 시작할 수 없습니다. 데이터는 그대로 있습니다.\n' + str(exc))
            return
        # Do NOT perform the normal shutdown backup: uninstall must not
        # create another persistent copy after the user's deletion request.
        uploader.stop()
        server.shutdown()
        server.server_close()
        root.destroy()
    ttk.Button(frame, text='프로그램 + 모든 누적 기록 완전 제거', command=remove_app).pack(anchor='w', pady=(15, 0))
    def close():
        # Make a consistent snapshot of the latest committed cases when closing.
        # Never prevent normal shutdown if an optional backup failed without warning.
        try:
            if bank.stats()['total']:
                name = 'MALIGN_last_close_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.sqlite3'
                bank.backup(folder / 'backups' / name)
                recent = sorted((folder / 'backups').glob('MALIGN_last_close_*.sqlite3'))
                for old in recent[:-7]:
                    old.unlink()
        except Exception as exc:
            if not messagebox.askyesno('백업 실패', '종료 직전 백업을 만들지 못했습니다.\n' + str(exc) + '\n\n데이터베이스의 기존 기록은 유지됩니다. 종료할까요?'):
                return
        uploader.stop()
        server.shutdown()
        server.server_close()
        root.destroy()
    root.protocol('WM_DELETE_WINDOW', close)
    root.after(180, open_browser)
    root.after(400, update_stats)
    root.mainloop()


def report_fatal(exc):
    # On Windows --windowed EXEs, tracebacks have no visible console.
    import traceback
    message = f'{type(exc).__name__}: {exc}'
    try:
        path = archive_folder() / 'logs'
        path.mkdir(parents=True, exist_ok=True)
        logfile = path / 'startup_errors.log'
        with logfile.open('a', encoding='utf-8') as f:
            f.write('\n=== ' + datetime.now().isoformat() + ' ===\n')
            f.write(traceback.format_exc() + '\n')
    except Exception:
        logfile = None
    if sys.platform == 'win32' and '--headless' not in sys.argv:
        try:
            import ctypes
            detail = '\n오류 기록: ' + str(logfile) if logfile else ''
            ctypes.windll.user32.MessageBoxW(None, 'M-ALIGN 실행 실패. 기존 저장 데이터는 삭제하지 않았습니다.\n\n' + message + detail, 'M-ALIGN 시작 오류', 0x10)
            return
        except Exception:
            pass
    print('M-ALIGN STARTUP ERROR:', message, file=sys.stderr)


if __name__ == '__main__':
    try:
        start()
    except Exception as exc:
        report_fatal(exc)
        sys.exit(1)
