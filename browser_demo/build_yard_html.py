"""Build the single-file browser version of the 3D AI yard (no Python server / EXE needed).

  python build_yard_html.py  ->  M-ALIGN_3D_AI야드_브라우저.html

Takes the desktop yard page (app/MALIGN_3D.html), the original Matrix5 engine + 21-case data + history
estimator (from M-ALIGN_3D_컨테이너옮기기.html, unmodified) and src/yard_sim.js (browser port of the yard
server), and replaces the /api/yard/* calls with the in-page simulation.
"""
import pathlib
import re

HERE = pathlib.Path(__file__).resolve().parent
APP = HERE.parent / 'app' / 'MALIGN_3D.html'
DEMO = HERE / 'M-ALIGN_3D_컨테이너옮기기.html'
SIM = HERE / 'src' / 'yard_sim.js'
OUT = HERE / 'M-ALIGN_3D_AI야드_브라우저.html'

LOCAL_API = r"""
// ---- in-page yard server (src/yard_sim.js) instead of the desktop program's /api/yard/*
const SIMLIB = window.MALIGN_YARD_SIM, MEM = new SIMLIB.CaseMemory('malign_yard_cases_v1');
let YARD = null, lastTick = performance.now();
setInterval(() => { const now = performance.now(); if (YARD) YARD.advance(Math.min(.5, (now - lastTick) / 1000)); lastTick = now; }, 40);
async function api(path, body){
  if (path === 'yard/state') return YARD ? YARD.state() : {running: false};
  if (path === 'yard/start'){ if (YARD) YARD.stop();
    YARD = new SIMLIB.Yard(Object.assign({}, body, {memory: MEM, ai_url: ($('aiurl') && $('aiurl').value.trim()) || 'http://127.0.0.1:11434'})).start(); return YARD.state(); }
  if (path === 'yard/stop'){ if (YARD) YARD.stop(); return {ok: true}; }
  if (path === 'yard/speed'){ if (YARD) YARD.speed = Number(body.speed) || 1; return {ok: true}; }
  if (path === 'yard/request'){ if (!YARD || !YARD.running) throw new Error('야드가 실행 중이 아닙니다'); return YARD.request(body.container, body.dest); }
  if (path === 'yard/memory/clear'){ MEM.clear(); return {ok: true}; }
  throw new Error('unknown ' + path);
}
window.__yard = () => YARD;
"""


def main():
    page = APP.read_text(encoding='utf-8')
    demo = DEMO.read_text(encoding='utf-8')
    # engine scripts: the three classic <script> blocks before the module in the stand-alone demo
    blocks = re.findall(r'<script>(/\* (?:ORIGINAL MATRIX5|HISTORY ESTIMATOR).*?)</script>', demo, re.S)
    assert len(blocks) == 3, len(blocks)
    engine = ''.join(f'<script>{b}</script>\n' for b in blocks) + '<script>window.MALIGN_DATA = DATA;</script>\n'
    sim = '<script>\n' + SIM.read_text(encoding='utf-8') + '\n</script>\n'

    page = page.replace('{"three":"/vendor/three/three.module.js","three/addons/":"/vendor/three/addons/"}',
                        '{"three":"https://cdn.jsdelivr.net/npm/three@0.180.0/build/three.module.js",'
                        '"three/addons/":"https://cdn.jsdelivr.net/npm/three@0.180.0/examples/jsm/"}')
    page = page.replace('<script type="module">', engine + sim + '<script type="module">', 1)
    # replace the HTTP api() with the local one
    page, n = re.subn(r"const TOKEN = window\.MALIGN_DESKTOP_TOKEN \|\| '';\nasync function api\(path, body\)\{.*?\n\}\n", '', page, count=1, flags=re.S)
    assert n == 1, 'api() not found'
    page = page.replace("const $ = id => document.getElementById(id);", "const $ = id => document.getElementById(id);\n" + LOCAL_API, 1)
    # desktop-only bits
    page = re.sub(r'\n <div style="font-size:11px;margin-top:8px"><a href="/3d-align".*?</div>', '', page, count=1)
    assert '/3d-align' not in page
    page = page.replace('<title>M-ALIGN 3D · AI 컨테이너 야드</title>', '<title>M-ALIGN 3D · AI 컨테이너 야드 (브라우저판)</title>')
    page = page.replace('<label for="model">AI 모델 (Ollama)</label><input id="model" value="qwen2.5:7b">',
                        '<label for="model">AI 모델 (Ollama)</label><input id="model" value="qwen2.5:7b">'
                        '<label for="aiurl">Ollama 주소 (OLLAMA_ORIGINS=* 필요)</label><input id="aiurl" value="http://127.0.0.1:11434">')
    page = page.replace('<option value="ai">Ollama AI (무료, 내 PC)</option><option value="autopilot">규칙 자동조종 (AI 없이)</option>',
                        '<option value="autopilot">규칙 자동조종 (AI 없이)</option><option value="ai">Ollama AI (무료, 내 PC)</option>')
    OUT.write_text(page, encoding='utf-8')
    print(OUT, len(page) // 1024, 'KB')


if __name__ == '__main__':
    main()
