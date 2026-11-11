"""Publish declared deliverables without letting one bad file hide good results."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import stat
from urllib.parse import unquote, urlsplit

ALLOWED = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif', '.pdf', '.md',
           '.txt', '.csv', '.json', '.docx', '.xlsx', '.pptx', '.mp4', '.zip'}
MAX_FILES = 12
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
_PRIVATE_NAMES = {'config.json', 'auth.json', 'credentials.json', 'state.json'}
_LINK = re.compile(r'(\[[^\]]*\]\()([^\n)]+)(\))')
_MANIFEST = re.compile(r'<hub-artifacts>(.*?)</hub-artifacts>', re.S)


@dataclass(frozen=True)
class ArtifactIssue:
    code: str
    name: str
    message: str

    def as_dict(self):
        return {'code': self.code, 'name': self.name, 'message': self.message}


class ArtifactValidationError(ValueError):
    """A permanent local validation failure, with no raw private paths."""
    def __init__(self, issues):
        self.issues = tuple(issues)
        super().__init__('；'.join(f'{item.name}：{item.message}' for item in self.issues))


@dataclass
class ArtifactPlan:
    body: str
    files: list[dict]
    issues: list[ArtifactIssue]
    roots: tuple[Path, ...] = field(repr=False)


@dataclass
class ArtifactPublication:
    body: str
    issues: list[ArtifactIssue]
    published_count: int

    def issue_summary(self):
        if not self.issues:
            return ''
        return '以下文件尚未发布：\n' + '\n'.join(
            f'- {item.name}：{item.message}' for item in self.issues)


def _name(path):
    return re.sub(r'[\x00-\x1f\x7f<>\[\]`]', '_', Path(path).name)[:100] or '成果文件'


def _stamp(info):
    # On Windows Python may report creation time for stat().st_ctime_ns but
    # change time for fstat().st_ctime_ns. They cannot be compared to each other.
    # File identity, size and mtime remain comparable across both APIs.
    stamp = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    return stamp if os.name == 'nt' else stamp + (info.st_ctime_ns,)


def _relative_root(path, roots):
    return next((root for root in roots if path.is_relative_to(root)), None)


def _reference_path(reference, root):
    value = unquote(reference.strip().strip('<>')).replace('\\', '/')
    if re.match(r'^/[A-Za-z]:/', value):
        value = value[1:]
    if not value or '\x00' in value or '\n' in value or '\r' in value:
        raise ArtifactValidationError([ArtifactIssue('invalid_path', '成果文件', '文件引用无效')])
    if urlsplit(value).scheme and not re.match(r'^[A-Za-z]:/', value):
        return None
    candidate = Path(value)
    return candidate if candidate.is_absolute() else root / candidate


def _inspect(candidate, roots):
    """Resolve only a declared path; private and escaping paths are never read."""
    path = candidate.resolve()
    root = _relative_root(path, roots)
    if root is None:
        raise ArtifactValidationError([ArtifactIssue('outside_root', '工作区外文件',
            '不在本任务明确允许的交付目录内，未读取或上传')])
    lexical_root = _relative_root(candidate.absolute(), roots)
    relative_parts = path.relative_to(root).parts
    if lexical_root is not None:
        relative_parts += candidate.absolute().relative_to(lexical_root).parts
    if (path.name.casefold() in _PRIVATE_NAMES or candidate.name.casefold() in _PRIVATE_NAMES
            or any(part.startswith('.') and part not in {'.', '..'} for part in relative_parts)):
        raise ArtifactValidationError([ArtifactIssue('private_path', '受保护文件',
            '私有配置或隐藏目录不能作为成果发布')])
    name = _name(path)
    if path.suffix.lower() not in ALLOWED:
        hint = '请将可公开的测试证据整理为 Markdown，或检查内容后打包为 ZIP' if path.suffix.lower() in {'.xml', '.log'} else '请导出为支持的交付格式'
        raise ArtifactValidationError([ArtifactIssue('unsupported_type', name, '不支持此文件格式；' + hint)])
    try:
        info = path.stat()
    except FileNotFoundError:
        raise ArtifactValidationError([ArtifactIssue('missing_file', name, '文件不存在，未上传')]) from None
    if not stat.S_ISREG(info.st_mode):
        raise ArtifactValidationError([ArtifactIssue('not_file', name, '需要明确的普通文件，不能上传目录')])
    if info.st_size > MAX_FILE_BYTES:
        raise ArtifactValidationError([ArtifactIssue('file_too_large', name, '超过单文件 50 MB 限制')])
    return path, info


def _append_issue(issues, issue):
    if issue not in issues:
        issues.append(issue)


def preflight_artifacts(body, root, *, allowed_roots=()):
    """Return independent valid files plus explicit, actionable rejections.

    Extra roots must be caller-authorized task directories, never directories
    inferred from an untrusted manifest or the whole Downloads directory.
    """
    root = Path(root).resolve()
    roots = tuple(dict.fromkeys([root, *(Path(item).resolve() for item in allowed_roots)]))
    if not isinstance(body, str):
        raise ArtifactValidationError([ArtifactIssue('invalid_body', '成果说明', '需要文本内容')])
    issues, declared = [], []
    matches = list(_MANIFEST.finditer(body))
    if len(matches) > 1:
        issues.append(ArtifactIssue('multiple_manifests', '成果清单', '只能包含一份成果清单；其余清单未处理'))
    if matches:
        try:
            values = json.loads(matches[0][1])
        except (ValueError, RecursionError):
            values = None
        if not isinstance(values, list):
            issues.append(ArtifactIssue('invalid_manifest', '成果清单', '需要有效的 JSON 文件列表'))
        else:
            if len(values) > MAX_FILES:
                issues.append(ArtifactIssue('manifest_limit', '成果清单', '最多声明 12 个文件；多余声明未处理'))
            for value in values[:MAX_FILES]:
                if not isinstance(value, dict) or not isinstance(value.get('path'), str) or not value['path'].strip():
                    _append_issue(issues, ArtifactIssue('invalid_manifest_item', '成果清单', '每项需要非空 path 文件引用'))
                else:
                    declared.append((value['path'], True))
    clean_body = _MANIFEST.sub('', body)
    if '<hub-artifacts>' in clean_body or '</hub-artifacts>' in clean_body:
        issues.append(ArtifactIssue('invalid_manifest', '成果清单', '成果清单标记不完整'))
        clean_body = re.sub(r'<hub-artifacts>.*\Z', '', clean_body, flags=re.S).replace('</hub-artifacts>', '')
    explicit_references = {reference for reference, _ in declared}
    declared += [(match[2], False) for match in _LINK.finditer(clean_body)]
    files, seen, aliases, invalid_references = [], {}, {}, set()
    invalid_paths = set()
    total = 0
    for reference, explicit in declared:
        detected_file = False
        candidate = None
        try:
            candidate = _reference_path(reference, root)
            if candidate is None:
                if explicit:
                    _append_issue(issues, ArtifactIssue('remote_reference', '远程文件引用', '成果清单需要本任务已有的本地文件'))
                continue
            path, info = _inspect(candidate, roots)
            detected_file = True
            if path in seen:
                aliases[reference] = seen[path]
                continue
            if len(files) >= MAX_FILES or total + info.st_size > MAX_TOTAL_BYTES:
                raise ArtifactValidationError([ArtifactIssue('delivery_limit', _name(path),
                    '超过本次最多 12 个文件或合计 100 MB 的限制')])
            total += info.st_size
            seen[path] = reference
            files.append({'path': path, 'reference': reference, 'bytes': info.st_size,
                          '_candidate': candidate, '_stamp': _stamp(info)})
        except ArtifactValidationError as exc:
            invalid_alias = False
            if candidate is not None:
                try:
                    invalid_path = candidate.resolve()
                    invalid_alias = invalid_path in invalid_paths
                    if explicit:
                        invalid_paths.add(invalid_path)
                except (OSError, ValueError, RuntimeError):
                    pass
            if explicit or detected_file or invalid_alias or reference in explicit_references:
                invalid_references.add(reference)
            if explicit or detected_file:
                for issue in exc.issues:
                    _append_issue(issues, issue)
        except (OSError, ValueError, RuntimeError):
            if explicit or reference in explicit_references:
                invalid_references.add(reference)
            if explicit:
                _append_issue(issues, ArtifactIssue('unreadable_file', '成果文件', '文件暂时无法读取，请检查后补交'))
    def normalize_link(match):
        if match[2] in invalid_references:
            return match[1][:-1] + '（未发布）'
        return match[1] + aliases.get(match[2], match[2]) + match[3]
    clean_body = _LINK.sub(normalize_link, clean_body)
    return ArtifactPlan(clean_body.strip(), files, issues, roots)


def collect_artifacts(body, root, *, allowed_roots=()):
    """Compatibility API: strict validation remains all-or-nothing."""
    plan = preflight_artifacts(body, root, allowed_roots=allowed_roots)
    if plan.issues:
        raise ArtifactValidationError(plan.issues)
    return plan.body, plan.files


def _opened_path(handle, fallback):
    """Check the actual opened file, including a parent changed to a junction."""
    if os.name == 'nt':
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        get_path = kernel.GetFinalPathNameByHandleW
        get_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        get_path.restype = wintypes.DWORD
        size = get_path(msvcrt.get_osfhandle(handle.fileno()), None, 0, 0)
        if not size:
            raise OSError('Unable to verify opened file')
        buffer = ctypes.create_unicode_buffer(size + 1)
        written = get_path(msvcrt.get_osfhandle(handle.fileno()), buffer, size + 1, 0)
        if not written or written > size:
            raise OSError('Unable to verify opened file')
        value = buffer.value
        if value.startswith('\\\\?\\UNC\\'):
            value = '\\\\' + value[8:]
        elif value.startswith('\\\\?\\'):
            value = value[4:]
        return Path(value).resolve()
    fd_path = Path('/proc/self/fd') / str(handle.fileno())
    return fd_path.resolve(strict=True) if fd_path.exists() else fallback.resolve(strict=True)


def _read_verified(item, roots):
    name = _name(item['path'])
    try:
        path, info = _inspect(item['_candidate'], roots)
        if path != item['path'] or _stamp(info) != item['_stamp']:
            raise ArtifactValidationError([ArtifactIssue('changed_file', name, '文件在预检后发生变化，请重新核验后补交')])
        with path.open('rb') as handle:
            actual = _opened_path(handle, path)
            if actual != path or _relative_root(actual, roots) is None:
                raise ArtifactValidationError([ArtifactIssue('changed_file', '成果文件', '文件位置在预检后变化，未读取或上传')])
            before = os.fstat(handle.fileno())
            if _stamp(before) != item['_stamp']:
                raise ArtifactValidationError([ArtifactIssue('changed_file', name, '文件在预检后发生变化，请重新核验后补交')])
            data = handle.read(MAX_FILE_BYTES + 1)
            after = os.fstat(handle.fileno())
        if len(data) != item['bytes'] or len(data) > MAX_FILE_BYTES or _stamp(after) != item['_stamp']:
            raise ArtifactValidationError([ArtifactIssue('changed_file', name, '读取时文件发生变化，请重新核验后补交')])
        final_path, final_info = _inspect(item['_candidate'], roots)
        if final_path != path or _stamp(final_info) != item['_stamp']:
            raise ArtifactValidationError([ArtifactIssue('changed_file', name, '读取时文件发生变化，请重新核验后补交')])
        return data
    except ArtifactValidationError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise ArtifactValidationError([ArtifactIssue('unreadable_file', name, '文件暂时无法读取，请检查后补交')]) from None


def _upload(client, slug, task_id, execution, item, data):
    key = hashlib.sha256(item['reference'].encode() + b'\0' + data).hexdigest()
    return client.upload_task_artifact(slug, task_id, execution, key, item['reference'],
        item['path'].name, mimetypes.guess_type(item['path'].name)[0] or 'application/octet-stream', data)


def publish_artifacts(client, slug, task_id, execution, root, body, *, allowed_roots=()):
    """Compatibility API; callers wanting partial delivery opt in explicitly."""
    plan = preflight_artifacts(body, root, allowed_roots=allowed_roots)
    if plan.issues:
        raise ArtifactValidationError(plan.issues)
    for item in plan.files:
        _upload(client, slug, task_id, execution, item, _read_verified(item, plan.roots))
    return plan.body


def publish_artifacts_partial(client, slug, task_id, execution, root, body, *, allowed_roots=()):
    """Publish valid files; propagate unknown outcomes and stale/denied fences.

    The caller retains its receipt and the same upload keys on network errors.
    Only explicit permanent validation statuses skip an individual file.
    """
    plan = preflight_artifacts(body, root, allowed_roots=allowed_roots)
    issues, uploaded, rejected = list(plan.issues), 0, set()
    for item in plan.files:
        try:
            data = _read_verified(item, plan.roots)
            _upload(client, slug, task_id, execution, item, data)
            uploaded += 1
        except ArtifactValidationError as exc:
            rejected.add(item['reference'])
            for issue in exc.issues:
                _append_issue(issues, issue)
        except Exception as exc:
            status = getattr(exc, 'status_code', None)
            if status not in {413, 415, 422}:
                raise
            rejected.add(item['reference'])
            message = {413: '服务器拒绝文件大小，请减小文件后补交',
                       415: '服务器不支持此格式，请转换后补交',
                       422: '文件未通过服务器校验，请检查格式或内容后补交'}[status]
            _append_issue(issues, ArtifactIssue('server_rejected', _name(item['path']), message))
    clean_body = _LINK.sub(lambda match: match[1][:-1] + '（未发布）'
                           if match[2] in rejected else match[0], plan.body)
    return ArtifactPublication(clean_body, issues, uploaded)
