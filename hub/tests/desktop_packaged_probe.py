"""Disposable HTTP fixture for validating the actual HubDesktop.exe bundle.

No device credentials or production browser storage are used. The save dialog is
confirmed manually in the isolated test window, then put 'finish' in command.txt.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_bridge.desktop_window import request_desktop_close, visible_gui_subprocess_kwargs


MAIN = '''<!doctype html><html lang="en"><meta charset="utf-8"><title>Packaged Hub QA</title>
<body style="background:#182021;color:#e7efeb;font:17px Segoe UI;padding:40px">
<h1>HubDesktop packaged QA</h1><p id="status">Starting isolated bundle checks...</p>
<input id="draft" style="width:450px" placeholder="Preserved chat draft"><input type="file" id="attachment">
<a id="download" href="/hub/download" download="hub-desktop-qa.txt">Download test result</a>
<script>
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function report(name,pass,detail=''){await fetch('/qa/report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,pass,detail})})}
async function run(){
 for(let n=0;n<200&&!window.pywebview?.api?.get_version;n++)await pause(200);
 const api=window.pywebview.api;const version=await api.get_version();
 await report('bundle API and Python dependencies',!!version,version);
 const projects=await api.list_projects();await report('configured project bridge',projects.length===1&&projects[0].id==='probe'&&projects[0].available);
 const state=await (await fetch('/qa/state')).json();
 if(state.restart){await report('cookie persisted across bundled process restart',document.cookie.includes('bundle_session=preserved'));document.querySelector('#status').textContent='Restart checks passed';return;}
 document.cookie='bundle_session=preserved; Max-Age=3600; Path=/hub; SameSite=Lax';
 document.querySelector('#draft').value='Keep this exact draft';const files=new DataTransfer();files.items.add(new File(['not a user file'],'probe.txt',{type:'text/plain'}));document.querySelector('#attachment').files=files.files;
 window.open('/hub/preview','_blank');
 for(let n=0;n<100;n++){let s=await (await fetch('/qa/state')).json();if(s.child)break;await pause(200)}
 await report('chat draft and File object survive child preview',document.querySelector('#draft').value==='Keep this exact draft'&&document.querySelector('#attachment').files[0]?.name==='probe.txt');
 document.querySelector('#status').textContent='Please save the test download in the isolated test folder';
 document.querySelector('#download').click();
 setInterval(async()=>{let s=await(await fetch('/qa/state')).json();if(s.saved){document.querySelector('#status').textContent='Download saved; chat remains active';await report('download did not replace chat with offline page',document.querySelector('#draft').value==='Keep this exact draft');}},1500);
}
run().catch(async e=>{document.querySelector('#status').textContent=String(e);await report('runtime error',false,String(e))});
</script></body></html>'''

CHILD = '''<!doctype html><html><meta charset="utf-8"><title>QA preview</title>
<body style="background:#182021;color:#e7efeb;font:20px Segoe UI;padding:40px"><h1>Isolated result preview</h1><p>The chat draft must remain in the main window.</p>
<script>(async()=>{for(let n=0;n<200&&!window.pywebview?.api?.get_version;n++)await new Promise(r=>setTimeout(r,200));let v=await window.pywebview.api.get_version();await fetch('/qa/report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:'packaged child shares login and native API',pass:!!v&&document.cookie.includes('bundle_session=preserved')})});})();</script></body></html>'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    state = args.state.resolve()
    state.mkdir(parents=True, exist_ok=True)
    project = state / 'project'
    project.mkdir(exist_ok=True)
    download = state / 'hub-desktop-qa.txt'
    results = {'exe_sha256': hashlib.sha256(args.exe.read_bytes()).hexdigest(), 'checks': [], 'child': False, 'restart': False}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/qa/state':
                with lock:
                    content = json.dumps({'child': results['child'], 'saved': download.is_file(), 'restart': results['restart']}).encode()
                mime = 'application/json'
            elif self.path == '/hub/download':
                content, mime = b'BYBC Hub packaged download probe\n', 'application/octet-stream'
            else:
                content, mime = (CHILD if self.path == '/hub/preview' else MAIN).encode(), 'text/html; charset=utf-8'
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Cache-Control', 'no-store')
            if self.path == '/hub/download':
                self.send_header('Content-Disposition', 'attachment; filename="hub-desktop-qa.txt"')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            data = json.loads(self.rfile.read(min(4096, int(self.headers.get('Content-Length', '0')))))
            with lock:
                if data not in results['checks']:
                    results['checks'].append(data)
                    print(json.dumps(data), flush=True)
                if data['name'] == 'packaged child shares login and native API':
                    results['child'] = True
                (state / 'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    hub = f'http://127.0.0.1:{server.server_port}/hub'
    config = state / 'config.json'
    config.write_text(json.dumps({'hub': hub, 'token': '', 'projects': {'probe': str(project)}}), encoding='utf-8')
    threading.Thread(target=server.serve_forever, daemon=True).start()
    environment = os.environ.copy()
    environment['APPDATA'] = str(state / 'appdata')
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    command = [str(args.exe.resolve()), '--config', str(config)]
    process = subprocess.Popen(command, env=environment, **visible_gui_subprocess_kwargs())
    print(json.dumps({'pid': process.pid, 'config': str(config), 'save_to': str(download)}), flush=True)
    deadline = time.monotonic() + 420
    command_file = state / 'command.txt'
    try:
        while time.monotonic() < deadline:
            requested = command_file.read_text().strip() if command_file.exists() else ''
            if requested == 'restart' and not results['restart']:
                request_desktop_close(config)
                process.wait(timeout=15)
                results['restart'] = True
                process = subprocess.Popen(command, env=environment, **visible_gui_subprocess_kwargs())
            if requested == 'finish':
                break
            if process.poll() is not None:
                raise RuntimeError(f'Bundled process exited with {process.returncode}')
            time.sleep(.25)
    finally:
        request_desktop_close(config)
        process.wait(timeout=15)
        server.shutdown()
        server.server_close()
        results['download_valid'] = download.is_file() and download.read_bytes() == b'BYBC Hub packaged download probe\n'
        (state / 'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
        print(json.dumps(results), flush=True)


if __name__ == '__main__':
    main()
