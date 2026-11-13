"""UI integration test without loopback Chromium navigation (blocked in sandbox).

The page receives the real bundled HTML with an injected token. Only fetch transport
is bridged to the actual local Python HTTP handler; computation and buttons execute
in Chromium. Thus tests UI + HTTP + SQLite while respecting network restrictions.
"""
from pathlib import Path
import json,subprocess,sys,tempfile,urllib.request,urllib.error
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
html=(ROOT/'MALIGN_Desktop.html').read_text(encoding='utf-8')
with tempfile.TemporaryDirectory(prefix='malign_browser_bridge_') as tmp:
 p=subprocess.Popen([sys.executable,str(ROOT/'desktop_host.py'),'--headless','--data-dir',tmp],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
 info=json.loads(p.stdout.readline())
 try:
  def bridge(path, opts):
   target=info['url'].rstrip('/')+path
   headers=opts.get('headers',{})
   method=opts.get('method','GET')
   body=opts.get('body')
   req=urllib.request.Request(target,data=body.encode('utf-8') if body is not None else None,headers=headers,method=method)
   try:
    with urllib.request.urlopen(req,timeout=60) as out:
     return {'ok':True,'status':out.status,'body':out.read().decode('utf-8')}
   except urllib.error.HTTPError as err:
    return {'ok':False,'status':err.code,'body':err.read().decode('utf-8')}
  with sync_playwright() as play:
   browser=play.chromium.launch(executable_path=__import__('os').environ.get('MALIGN_CHROMIUM','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
   page=browser.new_page(accept_downloads=True,viewport={'width':1366,'height':900})
   errors=[]
   page.on('pageerror',lambda err:errors.append(str(err)))
   page.expose_function('backendBridge',bridge)
   injection='''<script>window.MALIGN_DESKTOP_TOKEN=__TOKEN__;
window.fetch=async function(url, opts){
 const ret=await window.backendBridge(url,opts||{});
 return new Response(ret.body,{status:ret.status,headers:{'content-type':'application/json'}});
};</script>'''.replace('__TOKEN__',json.dumps(info['token']))
   active=html.replace('</head>',injection+'</head>',1)
   page.set_content(active,wait_until='domcontentloaded')
   page.wait_for_function("document.getElementById('bankStatus').textContent.includes('PC 영구 저장')",timeout=25000)
   assert str(Path(tmp)/'cases.sqlite3') in page.locator('#bankStatus').inner_text()
   page.locator('#autoSeed').uncheck()
   page.locator('#batchseed').fill('2026')
   page.locator('#scenario').select_option('base')
   page.locator('#estimator').select_option('base')
   page.locator('#count').select_option('100')
   page.locator('#batchRun').click()
   page.wait_for_function("document.getElementById('bankTotal').textContent === '100'",timeout=120000)
   assert page.locator('#batchSuccess').inner_text()=='6 / 100'
   assert page.locator('#bankSuccess').inner_text()=='6'
   assert page.locator('#bankFailure').inner_text()=='94'
   assert not errors,errors
   # Opening the app interface again in a fresh page must see the same DB.
   page2=browser.new_page(accept_downloads=True)
   page2.expose_function('backendBridge',bridge)
   page2.set_content(active,wait_until='domcontentloaded')
   page2.wait_for_function("document.getElementById('bankTotal').textContent === '100'",timeout=25000)
   assert page2.locator('#bankSuccess').inner_text()=='6'
   assert not errors,errors
   print('PASS: real JS UI + real SQLite backend, fixed seed 100: 6 successes / 94 failures; second UI retains 100 records; no JS errors',flush=True)
   browser.close()
 finally:
  p.terminate();p.wait(timeout=10)
