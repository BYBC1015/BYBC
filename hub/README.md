# BYBC Hub

Web workspace for persistent agents, shared conversations and device-bound execution.

Version 2.6.2 (2026-10-28). Agents are durable roles, independent of conversations, models and devices.

- Chat attachments stay in the composer and follow the assigned task.
- The controller chooses a reported model and reasoning level per task; explicit user overrides take priority.
- Feedback records page/device context and tracks evidence-based engineering updates.
- Weekly maintenance is queued idempotently; offline execution waits for its actual device.
- Generation-fenced execution, atomic result publication and local completion receipts prevent unsafe replay.
- User messages persist immediately; live replies preserve reading position and drafts.
- Separate controller and worker lanes support chat, delegation and follow-up changes during execution.
- Real handoff questions and answers appear live in the original conversation, with a configurable two-round default limit.
- Durable per-task runners overlap independent jobs while shared workspaces and declared external resources remain serialized.
- Task cards support completion, reversible archiving and paginated history while preserving chat and drafts.
- Executor presence refreshes in place across avatars, profiles and the composer; dedicated worker heartbeats expire after two minutes.
- Desktop companion 1.0.32 hosts the durable agent worker with shared start/stop controls, supervised recovery, local MCP configuration inheritance and same-family CLI path recovery after desktop updates.
- Results are published as viewable images and document previews in the conversation.

## Run locally

Requires Python 3.10+. Create a virtual environment, install `pip install -e ".[dev]"`, set
`CODEX_HUB_DATA_DIR`, `CODEX_HUB_SECRET_KEY` and `CODEX_HUB_OWNER_PASSWORD`, then run
`uvicorn codex_hub.main:app --host 127.0.0.1 --port 8797`.
Use your own deployment configuration; examples contain placeholders, never production credentials.

See `deploy/MAINTENANCE.md` for weekly operations and `deploy/RELEASE-2.6.2.md` for verified release behavior.
The `codex_bridge` package includes the dedicated agent worker and compatibility code for existing clients.
Mobile native clients and installers are outside this web source snapshot.

This backup includes source, required web assets, tests, dependency lock and deployment examples.
It excludes live databases, user uploads, private logs, environment files, authentication state and
unrelated workspace projects. It preserves existing files elsewhere in the BYBC repository.
