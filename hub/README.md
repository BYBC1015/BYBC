# BYBC Hub

Web workspace for persistent agents, shared conversations and device-bound execution.

Source version 2.9.4 (2026-10-29), desktop and worker source 1.2.3. Agents are durable roles, independent of conversations, models and devices.

This source snapshot does not prove that a particular computer has installed or started the new desktop binaries. Background source/server updates leave the currently running desktop application uninterrupted; a prepared native update takes effect only after that application is subsequently started with the new binaries.

- Chat attachments stay in the composer and follow the assigned task.
- High-resolution image references receive bounded vision copies automatically; verified original files remain intact for production and detailed inspection.
- Conversation context retains the original brief and a room-scoped reference catalog. The controller can explicitly inherit and read earlier PDF/image inputs without requesting another upload.
- Queued revisions use a bounded, relevant set of inherited reference files. Historical outputs remain available through the conversation reference catalog without automatically adding the entire accumulated archive to each new task's preprocessing inputs.
- Ordinary controller turns keep one CLI conversation and preserve message order within that room. Independent rooms remain parallel; read-only status questions have a separate slot and cannot dispatch or revise work.
- Internal reflection formatting failures are recorded internally without leaking tags into the visible reply or repeating the actual work.
- Verified published results can move out of the active work list while remaining reviewable and unaccepted. Manual restore is respected; obsolete blockers require an explicit successor relationship.
- Completed work creates a visible receipt and an idempotent, read-only controller summary. Late artifacts update the same receipt, and temporary notification failures have bounded durable retries.
- Delegation preserves each deliverable's source reference and permitted changes; delivery summaries distinguish reported checks from verified acceptance and retain newer revision status.
- Long-running work shows the last reported stage, report age, execution contact and device contact. Existing live refresh updates these timestamps without consuming model tokens or inventing progress.
- A confirmed execution is visible before its first public report. Unconfirmed execution retains its received draft without claiming continued progress; completed and cancelled work cannot accept late text. Parallel replies keep stable order, and status-only refreshes preserve rendered response content.
- File downloads preserve the conversation and draft; preview links remain separate, and canceled native downloads do not cause navigation-recovery loops.
- The controller chooses a reported model and reasoning level per task; explicit user overrides take priority.
- Feedback records page/device context and tracks evidence-based engineering updates.
- Weekly maintenance is queued idempotently; offline execution waits for its actual device.
- Bounded hourly health scans collect repeated failures and measured slow replies without model calls. Evidence merges into untouched maintenance, and new diagnostic work is limited to once per day; maintenance cannot trigger itself.
- Generation-scoped response timings and optional reported token counters survive completion. Unknown or conflicting measurements are not estimated, and current task reassignment cannot alter the historical executing device.
- Short read-only status prompts preserve the original goal and latest correction. Repeated revision aliases cannot duplicate work, and newer requirements survive delivery summaries.
- Verified attachment reuse avoids redundant downloads. Within-call identical vision inputs are deduplicated without silently discarding original reference metadata.
- Slow draft/outbox recovery cannot overwrite text typed after sending; reconnection preserves a reading anchor while explicit sends reveal the new message.
- Generation-fenced execution, atomic result publication and local completion receipts prevent unsafe replay.
- User messages persist immediately; live replies preserve reading position and drafts.
- The Windows desktop hosts the same workspace with persistent sign-in, local project access and separate result previews.
- Desktop UI assets are bundled with exact version matching; bounded authenticated snapshots restore previously visited workspaces offline.
- Draft attachment bytes survive restarts; explicit sends use a durable account-scoped outbox with receipt reconciliation and cross-window ownership.
- Other conversations expose pending-send links. Local drafts never submit themselves, and only an opened conversation resumes its local queue.
- Login persistence follows the server session's original remaining lifetime, at most 14 days; logout still revokes the session.
- Execution publishes public response snapshots before completion; SSE notifications have polling recovery and session-expiry handling.
- Separate controller and worker lanes support chat, delegation and follow-up changes during execution.
- Real handoff questions and answers appear live in the original conversation, with a configurable two-round default limit.
- Durable per-task runners overlap independent jobs while shared workspaces and declared external resources remain serialized.
- Task cards support completion, reversible archiving and paginated history while preserving chat and drafts.
- Executor presence refreshes in place across avatars, profiles and the composer; dedicated worker heartbeats expire after two minutes.
- Desktop companion source 1.2.3 hosts the durable agent worker with shared start/stop controls, supervised recovery, local MCP configuration inheritance and same-family CLI path recovery after desktop updates.
- Results are published as viewable images and document previews in the conversation.

## Run locally

Requires Python 3.10+. Create a virtual environment, install `pip install -e ".[dev]"`, set
`CODEX_HUB_DATA_DIR`, `CODEX_HUB_SECRET_KEY` and `CODEX_HUB_OWNER_PASSWORD`, then run
`uvicorn codex_hub.main:app --host 127.0.0.1 --port 8797`.
Use your own deployment configuration; examples contain placeholders, never production credentials.

See `deploy/MAINTENANCE.md` for weekly operations, `deploy/RELEASE-2.9.4.md` for this source revision and `deploy/BRIDGE-1.2.3.md` for native update boundaries.
The `codex_bridge` package includes the dedicated agent worker and compatibility code for existing clients.
Windows desktop source, pinned build instructions and isolated installer verification are included; generated binaries and mobile clients are not part of this source backup.

This backup includes source, required web assets, tests, dependency lock and deployment examples.
It excludes live databases, user uploads, private logs, environment files, authentication state and
unrelated workspace projects. It preserves existing files elsewhere in the BYBC repository.
