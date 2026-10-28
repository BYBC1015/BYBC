# CodexBridge 1.0.32 — local CLI and MCP continuity

Hub executes work on the selected device using Codex CLI. The executable can be
the runtime shipped with Codex Desktop; these are not mutually exclusive choices.
The model reasons remotely while the local runtime executes file and MCP tools.

This update repairs stale desktop CLI paths after an application upgrade. A valid
explicit path is preserved; a missing desktop-version path resolves within the
same installed desktop runtime family rather than switching to an unrelated npm
CLI with different model availability. Execution, planning and model discovery use
the same resolution helper.

The dedicated agent execution configuration now loads the local user's Codex
configuration (`codex_exec_ignore_user_config=false`). Local MCP configuration can
therefore be inherited. The small routing/model-planning stages retain their
explicit read-only, ephemeral configuration isolation. Existing legacy bindings
retain their settings.

Filesystem scope remains explicit: shared tasks work in the mapped project;
isolated tasks use their task workspace; the controller uses its coordination
workspace. Files on other drives are not automatically writable just because a
device is online. App-only integrations also require their own compatible runtime;
loading a configuration is not proof that every integration is connected.

Validation includes a real CLI invocation which created/read back a disposable
file and invoked the locally configured node_repl MCP. The probe changed no user
project file and called no external data service. Tests cover stale runtime paths,
preserved explicit executables, missing runtime families, and config inheritance.

Applied as a local companion update; no downloadable installer is published here.
