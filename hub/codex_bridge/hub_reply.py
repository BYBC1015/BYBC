from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .client import HubClient, HubClientError
from .config import BridgeConfig, default_config_path, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Post a Codex Hub result from a UTF-8 file without passing the reply "
            "through a Windows PowerShell text pipeline."
        )
    )
    parser.add_argument("--project", required=True, help="Hub project slug")
    parser.add_argument(
        "--body-file",
        required=True,
        type=Path,
        help="UTF-8 or UTF-8-with-BOM Markdown file containing the complete reply",
    )
    parser.add_argument("--task-id", type=int, help="Post to this task instead")
    parser.add_argument(
        "--mention-id",
        type=int,
        help="Mark this mention handled after the reply is accepted",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="CodexBridge config.json path",
    )
    parser.add_argument(
        "--binding-id",
        help="Use the token and network settings from this configured binding",
    )
    return parser


def select_runtime_config(
    config: BridgeConfig, binding_id: str | None
) -> BridgeConfig:
    if not binding_id:
        return config
    for runtime in config.runtime_configs():
        if runtime.binding_id == binding_id:
            return runtime
    raise ValueError(f"Configured binding not found: {binding_id}")


def read_body(path: Path) -> str:
    try:
        body = path.expanduser().resolve().read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Reply file is not valid UTF-8: {path}") from exc
    if not body.strip():
        raise ValueError(f"Reply file is empty: {path}")
    return body.strip()


def post_reply(arguments: argparse.Namespace) -> dict:
    config = select_runtime_config(load_config(arguments.config), arguments.binding_id)
    body = read_body(arguments.body_file)
    client = HubClient(
        config.hub,
        config.token,
        timeout_seconds=config.hub_request_timeout_seconds,
        get_retry_attempts=config.hub_get_retry_attempts,
        retry_backoff_seconds=config.hub_retry_backoff_seconds,
        source_ip=config.hub_source_ip,
    )
    if arguments.task_id is None:
        result = client.post_project_entry(
            arguments.project, body, reply_to_entry_id=arguments.mention_id
        )
    else:
        result = client.post_task_entry(
            arguments.project, arguments.task_id, body,
            reply_to_entry_id=arguments.mention_id,
        )
    if arguments.mention_id is not None:
        client.mark_mention_handled(arguments.project, arguments.mention_id)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        result = post_reply(arguments)
    except (OSError, ValueError, HubClientError) as exc:
        parser.exit(1, f"codex-hub-reply: {exc}\n")
    entry = result.get("entry", {})
    print(
        json.dumps(
            {
                "ok": True,
                "entry_id": entry.get("id"),
                "project": arguments.project,
                "task_id": arguments.task_id,
                "mention_id": arguments.mention_id,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
