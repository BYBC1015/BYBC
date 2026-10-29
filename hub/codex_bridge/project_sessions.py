"""Durable, exclusive Codex conversations for local project work.

This registry only creates bindings from the ``thread.started`` event of its own
CLI invocation. It never guesses a recent thread or adopts an open user chat.
The caller runs ``exec --json`` or ``exec ... resume <lease.session_id> --json``
inside the lease. Keep ``--cd`` and ``--sandbox`` before ``resume``; put resume's
``--image``/``--output-last-message``/``--json`` options after it.

CLI contract: https://learn.chatgpt.com/docs/non-interactive-mode and
https://learn.chatgpt.com/docs/cli/reference (verified with desktop CLI 0.165).
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class ProjectSessionError(RuntimeError):
    """A project conversation cannot safely start or continue."""


class ProjectSessionBusy(ProjectSessionError):
    """Another process currently holds the same conversation."""


class ProjectSessionRecoveryRequired(ProjectSessionError):
    """A previous invocation has an unknown outcome; do not duplicate it."""


class ProjectSessionMismatch(ProjectSessionError):
    """The workspace, identity or returned Codex thread does not match."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, label: str) -> str:
    if value is None or isinstance(value, bool):
        raise ProjectSessionError(f"项目对话缺少有效的 {label}。")
    value = str(value).strip()
    if not value or len(value) > 512 or any(ord(char) < 32 for char in value):
        raise ProjectSessionError(f"项目对话的 {label} 无效。")
    return value


def _session_id(value: Any) -> str:
    try:
        # Names and --last are intentionally unsupported: they are ambiguous.
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError) as exc:
        raise ProjectSessionMismatch("Codex 没有返回有效的项目对话编号。") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _read(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > 2_000_000:
            raise ValueError("oversized record")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("invalid record schema")
        return data
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError) as exc:
        # Unlike ordinary caches this is not disposable: resetting it would
        # silently lose a conversation or repeat a turn with external effects.
        raise ProjectSessionRecoveryRequired("项目对话记录无法读取，已保留原记录并停止重复执行。") from exc


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def _lock(path: Path, timeout: float) -> Iterator[None]:
    """OS lock: a crashed owner releases it, but its pending record survives."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ProjectSessionBusy("同一项目对话正在执行，请排队等待本轮完成。") from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class ProjectSessionStore:
    """Use one stable root per native bridge, never a task's temporary folder."""

    def __init__(self, root: str | Path, *, lock_timeout: float = 0.0):
        self.root = Path(root).expanduser().resolve()
        self.lock_timeout = max(0.0, float(lock_timeout))

    @contextmanager
    def acquire(
        self, *, device_id: str, project_slug: str, conversation_id: int | str,
        assigned_agent_id: int | str, project_path: str | Path, run_key: str,
        generation: int | str | None = None,
    ) -> Iterator["ProjectSessionLease"]:
        identity = {
            "device_id": _text(device_id, "设备编号").casefold(),
            "project_slug": _text(project_slug, "项目编号").casefold(),
            "conversation_id": _text(conversation_id, "Hub 对话编号"),
            "assigned_agent_id": _text(assigned_agent_id, "智能体编号"),
        }
        try:
            workspace = Path(project_path).expanduser()
            if not workspace.is_absolute():
                raise ValueError("workspace must be absolute")
            workspace = workspace.resolve(strict=True)
            if not workspace.is_dir():
                raise ValueError("workspace is not a directory")
        except (OSError, TypeError, ValueError) as exc:
            raise ProjectSessionMismatch("项目对话需要已存在的绝对项目目录。") from exc
        run_key = _text(run_key, "执行编号")
        generation = None if generation is None else _text(generation, "执行代次")
        binding_key = _digest(identity)
        path = self.root / "bindings" / f"{binding_key}.json"
        with ExitStack() as stack:
            stack.enter_context(_lock(self.root / "locks" / f"binding-{binding_key}.lock", self.lock_timeout))
            record = _read(path)
            if record is None:
                record = {"schema_version": 1, "binding_key": binding_key, "identity": identity,
                          "project_path": str(workspace), "session_id": None,
                          "created_at": _now(), "updated_at": _now(), "pending_run": None}
                _write(path, record)
            if "session_id" not in record or "pending_run" not in record:
                raise ProjectSessionRecoveryRequired("项目对话绑定记录不完整，已停止自动新建。")
            if (record.get("identity") != identity or record.get("binding_key") != binding_key
                    or not isinstance(record.get("project_path"), str)
                    or os.path.normcase(record.get("project_path", "")) != os.path.normcase(str(workspace))):
                raise ProjectSessionMismatch("项目对话已绑定其他目录或身份，不能自动迁移或覆盖。")
            lease = ProjectSessionLease(self, stack, path, record, run_key, generation)
            if lease.session_id:
                lease._claim_session(lease.session_id)
            lease._recover_receipt()
            if record.get("blocked_reason"):
                raise ProjectSessionRecoveryRequired(str(record["blocked_reason"]))
            lease.cached_receipt = lease._read_receipt(run_key, generation)
            try:
                yield lease
            finally:
                lease._closed = True


class ProjectSessionLease:
    """Exclusive invocation scope. Call start immediately before launching CLI.

    ``observe_jsonl`` accepts either complete stdout or individual lines. Feed
    live lines when the executor supports streaming so thread IDs are durable
    as soon as they arrive. Leaving a started lease without complete preserves
    an uncertain run; the next acquisition requires recovery rather than retry.
    """

    def __init__(self, store: ProjectSessionStore, stack: ExitStack, path: Path,
                 record: dict[str, Any], run_key: str, generation: str | None):
        self.store, self._stack, self._path, self._record = store, stack, path, record
        self.run_key, self.generation = run_key, generation
        self.cached_receipt: dict[str, Any] | None = None
        self._started = self._closed = self._completed = self._aborted = False
        self._observed_session = self._turn_completed = self._turn_failed = False
        self._observed_any_event = False
        self._claimed_session: str | None = None

    @property
    def session_id(self) -> str | None:
        value = self._record.get("session_id")
        return _session_id(value) if value is not None else None

    @property
    def started(self) -> bool:
        """Whether the invocation was durably recorded before process launch."""
        return self._started

    @property
    def project_path(self) -> Path:
        return Path(self._record["project_path"])

    @property
    def receipt_path(self) -> Path:
        return self._receipt_path(self.run_key, self.generation)

    def _receipt_path(self, run_key: str, generation: str | None) -> Path:
        return self.store.root / "receipts" / self._record["binding_key"] / f"{_digest([run_key, generation])}.json"

    def _save(self) -> None:
        self._record["updated_at"] = _now()
        _write(self._path, self._record)

    def _check_open(self) -> None:
        if self._closed:
            raise ProjectSessionError("项目对话锁已释放，不能继续写入。")

    def _claim_session(self, session_id: str) -> None:
        if self._claimed_session == session_id:
            return
        self._stack.enter_context(_lock(self.store.root / "locks" / f"session-{session_id}.lock", self.store.lock_timeout))
        owner_path = self.store.root / "sessions" / f"{session_id}.json"
        owner = _read(owner_path)
        if owner and (owner.get("binding_key") != self._record["binding_key"]
                      or owner.get("project_path") != self._record["project_path"]):
            raise ProjectSessionMismatch("Codex 项目对话已经属于另一个智能体或项目，已停止串用。")
        if not owner:
            _write(owner_path, {"schema_version": 1, "session_id": session_id,
                               "binding_key": self._record["binding_key"],
                               "project_path": self._record["project_path"]})
        self._claimed_session = session_id

    def _read_receipt(self, run_key: str, generation: str | None) -> dict[str, Any] | None:
        receipt = _read(self._receipt_path(run_key, generation))
        if receipt is not None:
            if (receipt.get("binding_key") != self._record["binding_key"]
                    or receipt.get("run_key") != run_key or receipt.get("generation") != generation
                    or not isinstance(receipt.get("ok"), bool)
                    or isinstance(receipt.get("returncode"), bool)
                    or not isinstance(receipt.get("returncode"), int)
                    or not isinstance(receipt.get("last_message"), str)
                    or (receipt.get("error") is not None and not isinstance(receipt["error"], str))
                    or receipt.get("identity") != self._record["identity"]
                    or receipt.get("project_path") != self._record["project_path"]
                    or receipt.get("session_id") != self.session_id):
                raise ProjectSessionRecoveryRequired("项目对话收据与当前执行不一致，已停止重复执行。")
        return receipt

    def _recover_receipt(self) -> None:
        pending = self._record.get("pending_run")
        if pending is not None:
            if not isinstance(pending, dict) or not isinstance(pending.get("run_key"), str):
                raise ProjectSessionRecoveryRequired("项目对话执行记录不完整，需要恢复。")
            receipt = self._read_receipt(pending["run_key"], pending.get("generation"))
            if receipt is None:
                raise ProjectSessionRecoveryRequired("上次项目执行的结果尚未确认，已停止重复工作；请先核对该轮结果。")
            self._record["pending_run"] = None
            self._record["last_receipt"] = str(self._receipt_path(pending["run_key"], pending.get("generation")))
            self._save()

    def start(self) -> None:
        self._check_open()
        if self.cached_receipt is not None:
            raise ProjectSessionError("本轮已有完成收据，应复用结果而不是重复运行。")
        if self._started or self._completed or self._aborted:
            raise ProjectSessionError("本轮项目对话已经启动。")
        self._record["pending_run"] = {"run_key": self.run_key, "generation": self.generation,
                                       "started_at": _now(), "mode": "resume" if self.session_id else "create"}
        self._save()
        self._started = True

    def abort_unstarted(self) -> None:
        """Release pending only when the caller proves no child process started.

        This is for pre-launch cancellation or a failed process constructor,
        never timeout, termination or an unknown/partially observed invocation.
        It writes no result receipt and preserves an existing thread binding.
        A fresh lease may retry the same run; this lease cannot launch again.
        """
        self._check_open()
        if not self._started or self._completed or self._aborted:
            raise ProjectSessionError("本轮没有可撤销的未启动执行。")
        if self._observed_any_event:
            raise ProjectSessionRecoveryRequired("已收到 Codex 执行事件，不能按未启动清除记录。")
        pending = self._record.get("pending_run")
        if (not isinstance(pending, dict) or pending.get("run_key") != self.run_key
                or pending.get("generation") != self.generation):
            raise ProjectSessionRecoveryRequired("未启动执行记录与本轮不一致，已保留记录。")
        self._record["pending_run"] = None
        self._save()
        self._started = False
        self._aborted = True

    def observe_jsonl(self, output: str) -> None:
        self._check_open()
        if not self._started or self._completed:
            raise ProjectSessionError("项目对话尚未启动或已经结束。")
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            self._observed_any_event = True
            if event.get("type") == "thread.started":
                try:
                    session_id = _session_id(event.get("thread_id"))
                    if self.session_id and self.session_id != session_id:
                        raise ProjectSessionMismatch("Codex 续接返回了另一个对话，已停止，不能丢失原项目上下文。")
                    self._claim_session(session_id)
                except ProjectSessionError as exc:
                    self._record["blocked_reason"] = str(exc)
                    self._save()
                    raise
                self._record["session_id"] = session_id
                self._observed_session = True
                self._save()
            elif event.get("type") == "turn.completed":
                self._turn_completed = True
            elif event.get("type") == "turn.failed":
                self._turn_failed = True

    def complete(self, *, ok: bool, returncode: int | None, last_message: str = "",
                 error: str | None = None, output_path: str | Path | None = None,
                 token_usage: dict[str, int] | None = None) -> dict[str, Any]:
        self._check_open()
        if not self._started or self._completed:
            raise ProjectSessionError("项目对话尚未启动或已经结束。")
        if not isinstance(ok, bool) or (returncode is not None and (isinstance(returncode, bool) or not isinstance(returncode, int))):
            raise ProjectSessionError("项目对话返回了无效的执行结果。")
        if self._record.get("blocked_reason"):
            raise ProjectSessionRecoveryRequired(str(self._record["blocked_reason"]))
        if self.session_id is None:
            raise ProjectSessionRecoveryRequired("未确认 Codex 项目对话编号，已保留本轮记录，不能自动新建重试。")
        if ok and (returncode != 0 or not self._observed_session or not self._turn_completed or self._turn_failed):
            raise ProjectSessionRecoveryRequired("项目执行缺少完整的成功事件，暂不作为已完成任务交付。")
        if returncode is None:
            raise ProjectSessionRecoveryRequired("项目执行结果不明确，已保留本轮记录以避免重复工作。")
        if len(last_message) > 262_144 or len(error or "") > 65_536:
            raise ProjectSessionError("项目执行收据过大，请将完整成果保存为文件。")
        receipt = {"schema_version": 1, "binding_key": self._record["binding_key"],
                   "identity": self._record["identity"], "project_path": self._record["project_path"],
                   "session_id": self.session_id, "run_key": self.run_key, "generation": self.generation,
                   "ok": ok, "returncode": returncode, "last_message": last_message,
                   "error": error, "output_path": str(output_path) if output_path is not None else None,
                   "completed_at": _now(), "turn_completed": self._turn_completed,
                   "turn_failed": self._turn_failed}
        from .token_usage import normalize_token_usage
        usage = normalize_token_usage(token_usage)
        if usage is not None:receipt['token_usage'] = usage
        # Receipt precedes clearing pending: a crash between writes is recovered
        # without submitting the same task twice.
        _write(self.receipt_path, receipt)
        self._record["pending_run"] = None
        self._record["last_receipt"] = str(self.receipt_path)
        self._save()
        self._completed = True
        self.cached_receipt = receipt
        return receipt
