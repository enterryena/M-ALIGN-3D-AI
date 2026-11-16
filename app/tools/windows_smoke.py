# -*- coding: utf-8 -*-
"""Smoke test of the desktop program (python script or the built EXE): yard runs, 3D page loads.

usage:
  python tools/windows_smoke.py                       # runs desktop_host.py with this Python
  python tools/windows_smoke.py --exe dist/MALIGN_Desktop.exe --shot yard.png
Checks: host starts headless, /3d and bundled three.js are served, the AI yard (rule autopilot) unloads /
loads trucks with M-ALIGN alignment and no contact; with --shot also opens /3d in Chromium (Playwright),
presses start, takes a screenshot and fails on page errors.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]


def call(url, data=None, h=None):
    h = dict(h or {})
    b = None
    if data is not None:
        h['Content-Type'] = 'application/json'
        b = json.dumps(data).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=b, headers=h, method='POST' if b else 'GET'), timeout=30) as r:
        return json.load(r)


def kill(proc):
    if os.name == 'nt':    # one-file EXE: the bootloader starts a child process, kill the tree
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exe')
    ap.add_argument('--shot')
    ap.add_argument('--tasks', type=int, default=2)
    ap.add_argument('--timeout', type=float, default=400)
    ap.add_argument('--stacks', help='write periodic thread dumps of the host here (hang diagnosis)')
    a = ap.parse_args()
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / 'Link').mkdir()
    (tmp / 'Link' / 'link_config.json').write_text(json.dumps({'sim_move_delay_s': 0.1, 'ai_ollama_url': 'http://127.0.0.1:9'}))
    info_file = tmp / 'info.json'
    cmd = ([a.exe] if a.exe else [sys.executable, str(ROOT / 'desktop_host.py')]) + \
        ['--headless', '--data-dir', str(tmp), '--info-file', str(info_file)]
    env = dict(os.environ, MALIGN_DEBUG_STACKS=str(pathlib.Path(a.stacks or (tmp / 'stacks.txt')).resolve()))
    host_log = open(tmp / 'host_output.txt', 'w', encoding='utf-8', errors='replace')
    proc = subprocess.Popen(cmd, stdout=host_log, stderr=subprocess.STDOUT, env=env)
    def host_tail():
        host_log.flush()
        txt = (tmp / 'host_output.txt').read_text(encoding='utf-8', errors='replace')
        return 'host exit code %s, last output:\n%s' % (proc.poll(), txt[-4000:])
    try:
        end = time.time() + 120
        while not info_file.exists() and time.time() < end:
            time.sleep(0.3)
        assert info_file.exists(), 'host did not start'
        time.sleep(0.3)
        info = json.loads(info_file.read_text(encoding='utf-8'))
        H = {'X-MALIGN-Token': info['token']}
        page = urllib.request.urlopen(info['url'] + '3d', timeout=20).read().decode('utf-8')
        assert 'MALIGN_YARD' in page, '3D yard page missing'
        js = urllib.request.urlopen(info['url'] + 'vendor/three/three.core.js', timeout=20).read()
        assert b"REVISION = '187'" in js, 'bundled three.js missing'
        print('host ok:', info['url'], flush=True)
        errors = []
        if a.shot:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            exe = os.environ.get('MALIGN_CHROMIUM')
            b = pw.chromium.launch(executable_path=exe if exe and os.path.exists(exe) else None,
                                   args=['--no-sandbox', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
            pg = b.new_page(viewport={'width': 1500, 'height': 900})
            pg.on('pageerror', lambda e: errors.append(str(e)))
            pg.goto(info['url'] + '3d')
            pg.wait_for_function('window.MALIGN_YARD !== undefined', timeout=60000)
            time.sleep(1)
            pg.select_option('#controller', 'autopilot')
            pg.select_option('#speed', '8')
            pg.fill('#seed', '12')
            pg.click('#start')
            t1 = time.time()
            while time.time() - t1 < 30 and not call(info['url'] + 'api/yard/state', None, H).get('running'):
                time.sleep(0.5)
            if not call(info['url'] + 'api/yard/state', None, H).get('running'):
                raise AssertionError('start button did not start the yard: ' + pg.inner_text('#sub'))
        else:
            call(info['url'] + 'api/yard/start', {'controller': 'autopilot', 'speed': 8, 'seed': 12}, H)
        end = time.time() + a.timeout
        st = None
        shot_done = False
        last_print = 0
        while time.time() < end:
            t_req = time.time()
            st = call(info['url'] + 'api/yard/state', None, H)
            s = st['stats']
            if time.time() - last_print > 15:
                last_print = time.time()
                tk = st.get('task') or {}
                print('  t=%6.1f  %-9s %-14s in %d out %d  (state %.2fs)' % (st['t'], st['stacker']['mode'], tk.get('step', '-'),
                      s['inbound'], s['outbound'], time.time() - t_req), flush=True)
            if a.shot and not shot_done and st['stacker']['mode'] == 'driving' and st.get('path'):
                pg.screenshot(path=a.shot)
                shot_done = True
                b.close()       # free the CPU (software WebGL) for the rest of the run
                pw.stop()
            if s['inbound'] + s['outbound'] >= a.tasks and (shot_done or not a.shot):
                break
            time.sleep(1)
        s = st['stats']
        print('yard:', json.dumps(s, ensure_ascii=False), 'sim', st['t'], 's', flush=True)
        if a.shot:
            if not shot_done:
                pg.screenshot(path=a.shot)
                b.close()
                pw.stop()
            assert not errors, errors
        assert s['inbound'] + s['outbound'] >= a.tasks, 'yard made no progress'
        assert s['contacts'] == 0, 'contact with a person/vehicle'
        assert s['align_fail'] == 0 or s['inbound'] + s['outbound'] > 0
        print('PASS: windows smoke' + (' (EXE)' if a.exe else ''), flush=True)
    except BaseException:
        print(host_tail(), flush=True)      # why the host stopped answering (crash output / exit code)
        raise
    finally:
        kill(proc)


if __name__ == '__main__':
    main()
