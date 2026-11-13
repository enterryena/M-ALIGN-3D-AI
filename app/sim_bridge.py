# -*- coding: utf-8 -*-
"""Connects the desktop host's kit link to a virtual site (simulation source only).

No server_url : the virtual site (main server + reach stacker) runs inside this program.
server_url set: the virtual site runs in the example main server; the kit's simulated coil
                readers fetch readings from it (/sim/v1/..., simulation-only endpoints).
Either way the kit itself is unchanged: it only sees commands and sends bundles.
"""
import json
import threading
import urllib.parse
import urllib.request

from sim_site import SimSite, SimSensingSource


class LocalSim:
    def __init__(self, link, M):
        cfg = link.cfg
        self.link = link
        self.n = 0
        self.site = SimSite(M, self._send, link.kit_id, approach_s=1.0, move_s=float(cfg['sim_move_delay_s']),
                            max_rounds=int(cfg['sim_rounds']), ai_url=cfg['ai_ollama_url'], ai_model=cfg['ai_model'])
        link.observers.append(self._observe)
        self.source = SimSensingSource(link, self.site.kit_readings)

    def _send(self, cmd, delay):
        self.n += 1
        cmd = dict(cmd, id='local-%d' % self.n)
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


def attach(link, M, start=True):
    sim = RemoteSim(link) if link.cfg['server_url'] else LocalSim(link, M)
    if start:
        sim.source.start()
    return sim
