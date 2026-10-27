"""Publish explicitly delivered workspace files; never scan unrelated folders."""
from __future__ import annotations
import hashlib
import json
import mimetypes
from pathlib import Path
import re
from urllib.parse import unquote,urlsplit

ALLOWED={'.png','.jpg','.jpeg','.webp','.gif','.avif','.pdf','.md','.txt','.csv','.json','.docx','.xlsx','.pptx','.mp4','.zip'}


def collect_artifacts(body, root):
    root=Path(root).resolve()
    declared=[]
    match=re.search(r'<hub-artifacts>(.*?)</hub-artifacts>',body,re.S)
    if match:
        values=json.loads(match.group(1))
        if not isinstance(values,list) or len(values)>12: raise ValueError('成果清单最多12项')
        declared=[(v['path'],True) for v in values if isinstance(v,dict) and isinstance(v.get('path'),str)]
        body=body[:match.start()]+body[match.end():]
    declared += [(m,False) for m in re.findall(r'\[[^\]]*\]\(([^\n)]+)\)',body)]
    found=[]; seen={}; aliases={}; total=0
    for reference,explicit in declared:
        value=unquote(reference.strip().strip('<>')).replace('\\','/')
        if re.match(r'^/[A-Za-z]:/',value): value=value[1:]
        if urlsplit(value).scheme and not re.match(r'^[A-Za-z]:/',value): continue
        candidate=Path(value)
        path=(candidate if candidate.is_absolute() else root/candidate).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() not in ALLOWED or any(p.startswith('.') for p in path.relative_to(root).parts) or path.name.casefold() in {'config.json','auth.json','credentials.json','state.json'}:
            if explicit: raise ValueError('成果必须是工作区内明确的交付文件，不能是私有配置')
            continue
        if not path.is_file():
            if explicit: raise ValueError('声明的成果文件不存在：'+path.name)
            continue
        # The manifest and Markdown often refer to the same file using
        # relative and absolute paths. Upload it once and normalize its links.
        if path in seen:
            aliases[reference]=seen[path]
            continue
        seen[path]=reference
        size=path.stat().st_size; total+=size
        if size>50*1024*1024 or total>100*1024*1024 or len(found)>=12: raise ValueError('成果超过本次上传数量或大小限制')
        found.append({'path':path,'reference':reference,'bytes':size})
    body=re.sub(r'(\[[^\]]*\]\()([^\n)]+)(\))',
                lambda m:m[1]+aliases.get(m[2],m[2])+m[3],body)
    return body.strip(),found


def publish_artifacts(client, slug, task_id, execution, root, body):
    body,files=collect_artifacts(body,root)
    for item in files:
        data=item['path'].read_bytes()
        key=hashlib.sha256(item['reference'].encode()+b'\0'+data).hexdigest()
        client.upload_task_artifact(slug,task_id,execution,key,item['reference'],item['path'].name,
            mimetypes.guess_type(item['path'].name)[0] or 'application/octet-stream',data)
    return body
