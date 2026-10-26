"""Credential-free model interface to a specific assigned maintenance task."""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path


def run_request(args):
    from .client import HubClient
    from .config import load_config
    from .hub_reply import select_runtime_config

    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,62}[a-z0-9]',args.project) or type(args.task_id) is not int or args.task_id<1:
        raise ValueError('项目或任务编号无效')
    if args.request_file.stat().st_size>16000:
        raise ValueError('维护请求最多16000字节')
    request=json.loads(args.request_file.read_text(encoding='utf-8-sig'))
    if not isinstance(request,dict) or request.get('action') not in {'list','get','update','diagnostics','status'}:
        raise ValueError('维护请求类型无效')
    config=select_runtime_config(load_config(args.config),args.binding_id)
    client=HubClient(config.hub,config.token,timeout_seconds=config.hub_request_timeout_seconds,
        get_retry_attempts=config.hub_get_retry_attempts,retry_backoff_seconds=config.hub_retry_backoff_seconds,source_ip=config.hub_source_ip)
    base=f'/api/projects/{args.project}/maintenance'
    context=client._json('GET',f'{base}/tasks/{args.task_id}')
    if request['action']=='list':
        return {'feedback':context['feedback'],'run':context['run']}
    if request['action']=='diagnostics':
        return {'diagnostics':context['diagnostics']}
    if request['action']=='status':
        return {'run':context['run']}
    feedback_id=request.get('feedback_id')
    allowed={item['id'] for item in context.get('feedback',[])}
    if type(feedback_id) is not int or feedback_id not in allowed:
        raise ValueError('只能处理本次维护任务明确关联的反馈')
    if request['action']=='get':
        return client._json('GET',f'{base}/feedback/{feedback_id}')
    status=request.get('status')
    if status not in {'new','triaged','in_progress','fixed','closed'}:
        raise ValueError('反馈状态无效')
    note=request.get('note','')
    evidence=request.get('verification_note',request.get('evidence',''))
    if not isinstance(note,str) or not isinstance(evidence,str) or len(note)>2000 or len(evidence)>4000:
        raise ValueError('维护说明或验证证据无效')
    if status=='fixed' and not evidence.strip():
        raise ValueError('修复需要具体的验证证据')
    if status=='closed' and not note.strip():
        raise ValueError('关闭反馈需要说明原因')
    execution = None
    if os.environ.get('CODEX_HUB_EXECUTION_TASK_ID') == str(args.task_id) and os.environ.get('CODEX_HUB_EXECUTION_PROJECT') == args.project:
        generation = os.environ.get('CODEX_HUB_EXECUTION_GENERATION','')
        run_key = os.environ.get('CODEX_HUB_EXECUTION_RUN_KEY','')
        if generation.isdigit() and len(generation)<12 and re.fullmatch(r'[A-Za-z0-9_.-]{16,100}',run_key):
            execution = {'generation':int(generation),'run_key':run_key}
    if execution is None:
        raise ValueError('反馈状态更新必须由当前维护执行进程发起')
    return client._json('POST',f'{base}/feedback/{feedback_id}/status',
                        {'status':status,'note':note,'verification_note':evidence,'task_id':args.task_id,'execution':execution})


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=os.environ.get('CODEX_BRIDGE_AGENT_CONFIG'))
    parser.add_argument('--binding-id')
    parser.add_argument('--project',required=True)
    parser.add_argument('--task-id',required=True,type=int)
    parser.add_argument('--request-file',required=True,type=Path)
    args=parser.parse_args(argv)
    try:
        if not args.config:
            raise ValueError('沿用当前智能体连接的配置路径')
        result=run_request(args)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    except Exception as exc:
        # Do not print request bodies, server errors, configuration or tokens.
        print(json.dumps({'error':f'维护请求未完成（{type(exc).__name__}），请检查请求字段、任务权限和连接状态。'},ensure_ascii=False))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
