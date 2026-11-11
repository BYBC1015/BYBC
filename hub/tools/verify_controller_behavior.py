"""Opt-in live Codex evaluation against an isolated, synthetic localhost Hub.

This is deliberately not part of pytest: --execute can consume model tokens.
No production Hub credentials, databases, tasks or artifacts are copied.
The production prompt, attachment preparation, CLI runner, helper and API are
used; worker execution is deliberately not started. Inspect evidence manually.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont
import uvicorn

from codex_hub.agents import AgentStore
from codex_hub.agent_roles import ROLES
from codex_hub.config import Settings
from codex_hub.db import connect_database, initialize_schema
from codex_hub.execution_leases import ExecutionStore
from codex_hub.main import create_app
from codex_hub.repository import Repository
from codex_bridge.agent_runtime import extract_reflection
from codex_bridge.agent_worker import controller_config
from codex_bridge.attachments import prepare_task_attachments
from codex_bridge.client import HubClient
from codex_bridge.codex_exec import CodexExecRunner
from codex_bridge.config import BridgeConfig, load_config, save_config
from codex_bridge.runner import BridgeRunner
from codex_bridge.task_pool import task_config
from codex_bridge.tool_locator import resolve_codex_cli_path


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def synthetic_image(path):
    """A wholly synthetic screenshot with a visual-only identifier and defect."""
    image = Image.new('RGB', (960, 540), '#20282a')
    draw = ImageDraw.Draw(image)
    font_path = Path('C:/Windows/Fonts/msyh.ttc')
    font = ImageFont.truetype(str(font_path), 28) if font_path.exists() else ImageFont.load_default()
    for i, (label, color) in enumerate([('重庆', '#746445'), ('上海', '#46666d'), ('广州', '#5c576c')]):
        x = i * 320
        draw.rectangle((x+6, 70, x+313, 465), fill=color)
        draw.text((x+110, 15), label, font=font, fill='white')
        for j in range(4):
            left = x+30+j*68
            if i == 0:
                draw.polygon([(left, 370), (left+53, 370), (left+80, 155+j*22), (left+27, 155+j*22)], fill='#b8c6c9')
            else:
                draw.rectangle((left, 170+j*22, left+50, 370), fill='#b8c6c9')
    draw.rectangle((24, 392, 290, 447), fill='#e9cb75')
    draw.text((40, 400), '山城花子  A7', font=font, fill='#23343a')
    draw.rectangle((130, 384, 287, 452), fill='#141d25')
    draw.text((25, 483), '测试画面 · 仅用于隔离验证', font=font, fill='white')
    image.save(path)


class QuietRunner(CodexExecRunner):
    def _run(self, *args, **kwargs):
        result = super()._run(*args, **kwargs)
        # Persist before the receipt wrapper, so a collector bug cannot erase a paid turn.
        result.output_path.with_suffix('.events.jsonl').write_text(result.stdout or '', encoding='utf-8')
        write_json(result.output_path.with_suffix('.process.json'), {
            'ok':result.ok, 'returncode':result.returncode, 'error':result.error, 'usage':result.token_usage})
        return result

    def _write_diagnostic(self, output_path, error, stderr):
        # Inherited MCP diagnostics may include private config. Never save them.
        output_path.with_suffix('.diagnostic.txt').write_text(error, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--evidence-dir', required=True, type=Path)
    parser.add_argument('--model', default='gpt-6-astra')
    parser.add_argument('--effort', choices=['medium', 'high'], default='medium')
    parser.add_argument('--scenarios', type=int, choices=[1, 2, 3, 4], default=3)
    parser.add_argument('--continue-run', type=Path, help='Resume this isolated fixture, never a production database')
    parser.add_argument('--start-scenario', type=int, choices=[1, 2, 3, 4], default=1)
    parser.add_argument('--timeout', type=int, default=150)
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    run_dir = args.continue_run.resolve() if args.continue_run else evidence / ('run-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    if args.continue_run and (evidence not in run_dir.parents or not (run_dir/'scenario-1-prompt.md').is_file()):
        raise ValueError('Only an existing synthetic run under the evidence directory may be continued')
    run_dir.mkdir(exist_ok=bool(args.continue_run))
    settings = Settings(data_dir=run_dir/'state', secret_key=uuid.uuid4().hex,
                        owner_password=uuid.uuid4().hex)
    settings.ensure_directories()
    db = connect_database(settings.database_path)
    initialize_schema(db)
    repo = Repository(db)
    slug = 'synthetic-controller-eval'
    store = AgentStore(db)
    if args.continue_run:
        device = dict(db.execute('SELECT * FROM devices WHERE label=?', ('Synthetic local evaluation only',)).fetchone())
        project = dict(db.execute('SELECT * FROM projects WHERE slug=?', (slug,)).fetchone())
        agents = {row['role_key']:dict(row) for row in db.execute('SELECT * FROM hub_agents WHERE project_id=?', (project['id'],))}
    else:
        device = repo.create_device('Synthetic local evaluation only')
        project = repo.create_project(slug=slug, name='隔离测试项目', created_by_device_id=device['id'])
        agents = {}
        for key, (name, scope) in ROLES.items():
            agent = store.create(project['id'], name, scope, kind='controller' if key == 'controller' else 'worker', device_id=device['id'])
            agents[key] = store.update(project['id'], agent['id'], job=scope, instructions=scope, device_id=device['id'], active=True, role_key=key)
    image = settings.files_dir/'synthetic-screenshot.png'
    if args.continue_run:
        attachment = dict(db.execute('SELECT * FROM attachments WHERE original_filename=?', (image.name,)).fetchone())
    else:
        synthetic_image(image)
        attachment = repo.create_attachment(project['id'], image.name, image.name, 'image/png', image.stat().st_size,
                                             hashlib.sha256(image.read_bytes()).hexdigest(), created_by_device_id=device['id'])
    app = create_app(settings)
    audit = []

    @app.middleware('http')
    async def observe(request, call_next):
        body = await request.body() if request.method == 'POST' else b''
        response = await call_next(request)
        payload = json.loads(body) if body and 'json' in request.headers.get('content-type', '') else None
        if isinstance(payload, dict):
            payload.pop('execution', None)  # execution fencing exists but isn't useful report content
        audit.append({'at': round(time.monotonic(), 3), 'method': request.method,
                      'path': request.url.path, 'status': response.status_code, 'payload': payload})
        return response

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False))
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic()+10
    while not server.started and time.monotonic() < deadline:
        time.sleep(.05)
    if not server.started:
        raise RuntimeError('Isolated fixture server did not start')
    port = sock.getsockname()[1]
    local = load_config(Path(os.environ['APPDATA'])/'CodexHubAgents/config.json')
    base = next(item for item in local.runtime_configs() if item.execution_backend == 'codex')
    cli = resolve_codex_cli_path(base.codex_cli_path)
    if cli is None:
        raise RuntimeError('Existing Codex CLI was not found')
    # Retain login/config without copying auth material into evidence.
    config = BridgeConfig(hub=f'http://127.0.0.1:{port}', token=repo.create_api_token(device['id'], 'synthetic-only').raw,
        prompt_dir=run_dir/'prompts', state_path=run_dir/'bridge-state.json',
        projects={slug:run_dir/'project'}, project_root=run_dir/'project', codex_cli_path=cli,
        codex_exec_output_dir=run_dir/'model-output', codex_exec_timeout_seconds=max(30, args.timeout),
        codex_exec_ignore_user_config=False, auto_execute_codex=True, open_codex=False,
        backend_options={key:value for key,value in base.backend_options.items() if key == 'windows_sandbox'})
    config_path = run_dir/'synthetic-connection.json'
    save_config(config, config_path)
    client = HubClient(config.hub, config.token)
    prior_env = {k:os.environ.get(k) for k in ('PYTHONPATH', 'CODEX_BRIDGE_AGENT_CONFIG')}
    os.environ['PYTHONPATH'] = str(ROOT)
    os.environ['CODEX_BRIDGE_AGENT_CONFIG'] = str(config_path)
    records = []
    room = (db.execute('SELECT conversation_id FROM tasks WHERE assigned_agent_id=? ORDER BY id DESC LIMIT 1',
                       (agents['controller']['id'],)).fetchone()[0] if args.continue_run else None)
    objectives = [
        '重庆这一幅是歪的，把它改成正向、地平线水平、建筑竖直的定帧，花子包装要看得清，上海和广州保持不变。只改我发的截图里这一块，先给我可看的图片候选。',
        '最终还要放回原短片，但原视频和工程我目前没传。你现在可以先做什么？能做的直接安排，不要把当前修改停下来。',
        '那就继续，重庆包装下面那条黑色遮挡也要去掉；其余要求沿用，别把上海广州改了。现在进行到哪一步了？',
        '沿用之前那项局部修改，交付时另附一张说明图标出修复区域；标注只放说明图，候选原图不要加字，上海广州仍保持原样。直接更新原安排，不要重复创建任务。简短告诉我当前进度。',
    ]
    try:
        for index, objective in enumerate(objectives[:args.scenarios], 1):
            if index < args.start_scenario:
                continue
            task = repo.create_task(project['id'], objective[:190], description=objective,
                                    assigned_agent_id=agents['controller']['id'])
            task = store.route(task['id'], {'action':'reuse' if room else 'new', 'conversation_id':room,
                                          'title':'三城市截图局部修改', 'reason':'延续当前截图修改'}, 'codex_cli')
            room = task['conversation_id']
            if index == 1:
                db.execute('INSERT INTO hub_task_attachments(task_id,attachment_id) VALUES (?,?)', (task['id'], attachment['id']))
                db.commit()
            task = repo.get_task(task['id'])
            task['assigned_agent_kind'] = 'controller'
            runtime = task_config(controller_config(config), task)
            workspace = runtime.projects[slug]
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace/'AGENTS.md').write_text(
                '# 隔离评估边界\n仅此目录和本轮 localhost Hub 是可操作对象。不要访问其他项目、生产 Hub、原始聊天、账号或外部素材。'
                '不要读取连接配置内容或输出凭据；使用已提供接口即可。'
                '不启动实际图片生成或执行者，只验证你作为总控的实际安排。不要生成假工具结果。\n', encoding='utf-8')
            prepared = prepare_task_attachments(client, runtime, slug, task['id'], workspace)
            bridge = BridgeRunner(runtime, client=client)
            prompt = client.get_task_prompt(slug, task['id']) + bridge._agent_tool_instructions(slug, task['id']) + prepared.prompt()
            prompt += '\n本轮仅需总控判断和必要派工，使用本轮给出的专用接口；无需生产图片，不读取源代码或用户其他目录。'
            prompt_path = run_dir/f'scenario-{index}-prompt.md'
            prompt_path.write_text(prompt, encoding='utf-8')
            lease_store = ExecutionStore(repo, lease_seconds=600)
            acquired = lease_store.acquire(task['id'], device['id'], uuid.uuid4().hex)['execution']
            execution = lease_store.start(task['id'], device['id'], acquired['generation'], acquired['run_key'])['execution']
            start_index = len(audit)
            if not args.execute:
                records.append({'scenario':index,'prepared':True,'executed':False,'task_id':task['id']})
                break
            progress = []
            begun = time.monotonic()
            def on_progress(kind, text):
                progress.append({'elapsed_seconds':round(time.monotonic()-begun, 3), 'kind':kind, 'text':text})
            result = QuietRunner(runtime).run(slug, workspace, prompt_path, prompt,
                execution_preferences={'model':args.model, 'reasoning_effort':args.effort},
                image_paths=prepared.images, execution_context={**execution, 'task_id':task['id'], 'project_slug':slug,
                    'receipt_path':str(run_dir/f'scenario-{index}-receipt.json'), 'execution_route':'chat',
                    'assigned_agent_kind':'controller', 'conversation_id':room,
                    'assigned_agent_id':agents['controller']['id'], 'device_id':device['id']},
                progress_callback=on_progress)
            elapsed = round(time.monotonic()-begun, 3)
            # All retained model output relates to synthetic test inputs. stderr is intentionally dropped.
            (run_dir/f'scenario-{index}-events.jsonl').write_text(result.stdout, encoding='utf-8')
            (run_dir/f'scenario-{index}-reply.md').write_text(result.last_message, encoding='utf-8')
            body, reflection, valid = extract_reflection(result.last_message, ok=result.ok)
            if result.ok:
                lease_store.complete(task['id'], device['id'], execution['generation'], execution['run_key'],
                    status=reflection.get('outcome', 'review'), summary=reflection.get('summary','')[:1000],
                    body=body, reflection=reflection, token_usage=result.token_usage)
            snapshot = [{key:t.get(key) for key in ('id','title','description','status','assigned_agent_id','conversation_id','execution_mode','execution_route')}
                        for t in repo.list_tasks(project['id'])]
            record = {'scenario':index,'objective':objective,'task_id':task['id'],'model':args.model,
                'reasoning_effort':args.effort,'elapsed_seconds':elapsed,'ok':result.ok,'returncode':result.returncode,
                'error':result.error, 'usage':result.token_usage,'image_inputs':len(prepared.images),
                'reflection_valid':valid,'progress':progress,'http_calls':audit[start_index:], 'tasks':snapshot,
                'task_attachments':{str(t['id']):repo.list_task_attachments(project['id'], t['id']) for t in snapshot}}
            records.append(record)
            write_json(run_dir/f'scenario-{index}-evidence.json', record)
            print(json.dumps({k:record[k] for k in ('scenario','ok','elapsed_seconds','usage','error')}, ensure_ascii=False), flush=True)
            if not result.ok:
                break
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        for key, value in prior_env.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
        db.close()
        # Ephemeral localhost credential is invalid after shutdown and omitted from sharable evidence.
        config_path.unlink(missing_ok=True)
        write_json(run_dir/f'http-audit-from-{args.start_scenario}.json', audit)
        write_json(run_dir/f'summary-from-{args.start_scenario}.json', {'executed':args.execute, 'model':args.model, 'effort':args.effort,
            'scenarios':records,'boundaries':['Synthetic database and attachments only',
            'Production CLI/prompt/attachment/helper/API; no actual production or worker generation',
            'No real video replacement, output image quality, frontend or device handoff validated',
            'No automatic conclusion from keywords: inspect actual tool calls and work scopes']})
    print(str(run_dir), flush=True)


if __name__ == '__main__':
    main()
