# -*- coding: utf-8 -*-
"""Connects the desktop host's kit link to a virtual site (simulation source only).

No server_url : the virtual site (main server + reach stacker) runs inside this program.
server_url set: the virtual site runs in the example main server; the kit's simulated coil
                readers fetch readings from it (/sim/v1/..., simulation-only endpoints).
Either way the kit itself is unchanged: it only sees commands and sends bundles.
"""
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request

from sim_site import SimSite, SimSensingSource


class LocalSim:
    def __init__(self, link, M):
        cfg = link.cfg
        self.link = link
        self.n = 0
        self.run_id = secrets.token_hex(3)
        self.site = SimSite(M, self._send, link.kit_id, approach_s=1.0, move_s=float(cfg['sim_move_delay_s']),
                            max_rounds=int(cfg['sim_rounds']), ai_url=cfg['ai_ollama_url'], ai_model=cfg['ai_model'])
        link.observers.append(self._observe)
        self.site.kit_max_rounds = int(link.cfg['max_rounds'])
        self.site.kit_max_resense = int(link.cfg['max_resense'])   # the kit's own cap: the server stops before it
        self.source = SimSensingSource(link, self.site.kit_readings)

    def _send(self, cmd, delay):
        self.n += 1
        cmd = dict(cmd, id='local-%s-%d' % (self.run_id, self.n))   # unique across restarts (the kit drops repeated ids)
        t = threading.Timer(max(0.0, float(delay)), self.link.apply_command, args=(cmd,))
        t.daemon = True
        t.start()

    def _observe(self, msg):
        if msg.get('event') == 'sim_machine_ready':     # older API: start with defaults
            t = threading.Timer(0, self._start_default)
            t.daemon = True
            t.start()
        else:
            self.site.on_message(msg)

    def _start_default(self):
        try:
            self.site.start()
        except Exception:
            pass

    def start(self, params):
        return self.site.start(**params)

    def state(self):
        return self.site.state()

    # ---- container yard (trucks, slots, obstacles) driven by the same main server
    yard = None
    memory = None

    def yard_start(self, p):
        import yard_ops
        if self.yard is not None:
            self.yard.stop()
            time.sleep(0.3)
        cfg = self.link.cfg
        ctrl = p.get('controller', 'autopilot')
        if ctrl not in ('ai', 'autopilot'):
            raise ValueError('controller: ai / autopilot')
        align = {k: p[k] for k in ('hardware', 'machine', 'fault', 'strategy', 'tol_model', 'capture') if k in p}
        import case_memory
        if self.memory is None:     # cases persist next to the kit's outbox (survive restarts)
            self.memory = case_memory.CaseMemory(self.link.outbox.file_path.parent / 'yard_cases.sqlite3')
        self.yard = yard_ops.YardOps(site=self.site, controller=ctrl, ai_url=cfg['ai_ollama_url'],
                                     ai_model=p.get('ai_model') or cfg['ai_model'], seed=p.get('seed'),
                                     speed=float(p.get('speed', 1.0)), people=bool(p.get('people', True)),
                                     debris=bool(p.get('debris', True)), align_opts=align,
                                     memory=self.memory, vary=bool(p.get('vary', False)),
                                     kit_state=lambda: self.link.status()['state']).start()
        return self.yard.state()

    def yard_stop(self):
        if self.yard is not None:
            self.yard.stop()
        return {'stopped': True}

    def yard_state(self):
        return self.yard.state() if self.yard is not None else {'running': False}

    def yard_request(self, container, dest):
        if self.yard is None or not self.yard.running:
            raise RuntimeError('야드 운영이 시작되지 않았습니다')
        return self.yard.request(str(container), str(dest))

    def yard_speed(self, speed):
        if self.yard is None:
            raise RuntimeError('야드 운영이 시작되지 않았습니다')
        self.yard.speed = max(0.25, min(8.0, float(speed)))
        return {'speed': self.yard.speed}


class RemoteSim:
    def __init__(self, link):
        self.link = link
        u = urllib.parse.urlparse(link.cfg['server_url'])
        self.base = '%s://%s/sim/v1/kits/%s' % (u.scheme, u.netloc, link.kit_id)
        self.headers = {'Accept': 'application/json'}
        if link.cfg['api_key']:
            self.headers['Authorization'] = 'Bearer ' + link.cfg['api_key']
        self.source = SimSensingSource(link, self.readings)

    def _call(self, path, data=None):
        h = dict(self.headers)
        body = None
        if data is not None:
            h['Content-Type'] = 'application/json'
            body = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(self.base + path, data=body, headers=h, method='POST' if body is not None else 'GET')
        with urllib.request.urlopen(req, timeout=float(self.link.cfg['timeout_s'])) as res:
            return json.loads(res.read().decode('utf-8'))

    def readings(self):
        return self._call('/readings')['units']

    def start(self, params):
        return self._call('/start', params)

    def state(self):
        return self._call('/state')

    def _no_yard(self, *a, **k):
        raise RuntimeError('야드 운영은 내장 메인 서버(server_url 비움)에서만 지원합니다')
    yard_start = yard_stop = yard_state = yard_request = yard_speed = _no_yard


def attach(link, M, start=True):
    sim = RemoteSim(link) if link.cfg['server_url'] else LocalSim(link, M)
    if start:
        sim.source.start()
    return sim
