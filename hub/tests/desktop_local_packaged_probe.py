"""Exercise the frozen desktop with loopback fixtures and disposable profiles.

The fixture itself drives its own test page; it never reads a live Hub account.
"""
from __future__ import annotations
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_bridge.desktop_window import request_desktop_close, visible_gui_subprocess_kwargs

PAGE = r'''<!doctype html><html><head><meta charset="utf-8"><title>Hub packaged local continuity QA</title></head>
<body style="background:#182021;color:#e7efeb;font:17px Segoe UI;padding:40px">
<h1>Hub 本地恢复隔离测试</h1><p>仅连接本机测试服务，不访问个人账号。</p>
<main data-ws-shell data-user="qa-local-user">Persisted QA conversation</main><input name="_csrf" value="qa-fresh-token">
<script>
const wait=ms=>new Promise(r=>setTimeout(r,ms));
const state=()=>fetch('/qa/state',{cache:'no-store'}).then(r=>r.json());
async function report(name,pass,detail=''){await fetch('/qa/report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,pass,detail})});if(!pass)throw Error(name)}
async function until(fn){for(let n=0;n<200;n++){if(await fn())return;await wait(100)}throw Error('QA wait timed out')}
async function db(){return new Promise((ok,no)=>{const r=indexedDB.open('hub-packaged-proof',1);r.onupgradeneeded=()=>r.result.createObjectStore('files');r.onsuccess=()=>ok(r.result);r.onerror=()=>no(r.error)})}
async function fileStore(save){const d=await db();try{return await new Promise((ok,no)=>{const t=d.transaction('files',save?'readwrite':'readonly');const r=save?t.objectStore('files').put(new File(['exact persistent attachment bytes'],'附件.txt',{type:'text/plain'}),'draft'):t.objectStore('files').get('draft');let result;r.onsuccess=()=>result=r.result;t.oncomplete=()=>ok(result);t.onabort=()=>no(t.error)})}finally{d.close()}}
async function main(){
 await until(()=>!!window.pywebview?.api?.get_version);
 const s=await state();const api=window.pywebview.api;
 const asset=await fetch('/hub/static/app.css?v=20260820-bybc-video1');
 await report('bundled asset '+s.phase,asset.ok&&asset.headers.get('X-Hub-Desktop-Local')==='1'&&(await asset.text()).length>1000);
 if(s.phase==='online'){
  await report('frozen native dependencies',await api.get_version()==='1.2.0');
  const mismatch=await fetch('/hub/static/app.css?v=future-server-version');
  await report('future asset version uses server',mismatch.status===418);
  document.cookie='qa_cookie=kept; Max-Age=3600; Path=/hub; SameSite=Lax';await fileStore(true);
  await wait(800);await report('ready to disconnect',true);
  await until(async()=>(await state()).phase==='offline');location.reload();
 }else if(s.phase==='offline'||s.phase==='restart'){
  await report('offline document '+s.phase,document.documentElement.dataset.hubOffline==='true'&&location.pathname==='/hub/projects'&&document.querySelector('[name=_csrf]').value==='');
  const write=await fetch('/hub/projects/dispatch',{method:'POST'});
  await report('offline write fenced '+s.phase,write.status===503);
  const f=await fileStore(false);await report('attachment survives '+s.phase,f?.name==='附件.txt'&&await f.text()==='exact persistent attachment bytes');
  await report('cookie survives '+s.phase,document.cookie.includes('qa_cookie=kept'));
  if(s.phase==='offline'){await report('ready to restart',true);return}
  await report('ready to reconnect',true);
  await until(async()=>(await state()).phase==='recover');
  const fresh=await fetch(location.href,{cache:'no-store',headers:{'X-Hub-Online-Probe':'1'}});
  const body=await fresh.text();await report('fresh authenticated GET',fresh.ok&&body.includes('qa-fresh-token'));
  await until(async()=>(await api.workspace_online('qa-local-user')).online);
  const sent=await fetch('/hub/projects/dispatch',{method:'POST'});await report('write resumes after verification',sent.status===204);
  await report('offline marker cleared',!window.__HUB_DESKTOP_OFFLINE__&&!document.documentElement.dataset.hubOffline);
  await report('finished',true);
 }
}
main().catch(e=>fetch('/qa/report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'runtime failure',pass:false,detail:String(e)})}));
</script></body></html>'''


def run(exe: Path, output: Path):
    output.mkdir(parents=True, exist_ok=False)
    temporary = Path(tempfile.gettempdir()) / ('hub-local-package-qa-' + uuid.uuid4().hex)
    temporary.mkdir()
    report = {'passed': False, 'exe_sha256': hashlib.sha256(exe.read_bytes()).hexdigest(),
              'phase': 'online', 'checks': [], 'server_writes': 0, 'bundled_network_requests': 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/qa/state':
                body, mime, status = json.dumps({'phase': report['phase']}).encode(), 'application/json', 200
            elif report['phase'] in ('offline', 'restart'):
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            elif self.path.startswith('/hub/static/'):
                if 'future-server-version' not in self.path:
                    report['bundled_network_requests'] += 1
                body, mime, status = b'fixture network response', 'text/plain', 418
            else:
                body, mime, status = PAGE.encode(), 'text/html; charset=utf-8', 200
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path == '/qa/report':
                item = json.loads(self.rfile.read(min(10000, int(self.headers.get('Content-Length', '0')))))
                with lock:
                    report['checks'].append(item)
                    (output / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            else:
                report['server_writes'] += 1
            self.send_response(204)
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = temporary / 'config.json'
    config.write_text(json.dumps({'hub': f'http://127.0.0.1:{server.server_port}/hub', 'token': '', 'projects': {}}))
    environment = os.environ.copy()
    environment['APPDATA'] = str(temporary / 'Roaming')
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    def launch():
        return subprocess.Popen([str(exe.resolve()), '--config', str(config)], env=environment,
                                **visible_gui_subprocess_kwargs())
    process = launch()
    def until(name):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if any(not item['pass'] for item in report['checks']):
                raise AssertionError('Failed packaged browser check')
            if any(item['name'] == name and item['pass'] for item in report['checks']):
                return
            if process.poll() is not None:
                raise AssertionError('Frozen desktop exited unexpectedly')
            time.sleep(.2)
        raise AssertionError('Timed out: ' + name)
    try:
        until('ready to disconnect'); report['phase'] = 'offline'
        until('ready to restart')
        request_desktop_close(config); process.wait(timeout=20)
        report['phase'] = 'restart'; process = launch()
        until('ready to reconnect'); report['phase'] = 'recover'
        until('finished')
        assert report['server_writes'] == 1 and report['bundled_network_requests'] == 0
        report['passed'] = True
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        request_desktop_close(config)
        process.wait(timeout=20)
        server.shutdown(); server.server_close()
        report['temporary_profile'] = str(temporary)
        (output / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    raise SystemExit(run(arguments.exe, arguments.output))
