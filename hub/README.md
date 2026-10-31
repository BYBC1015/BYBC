# BYBC Hub

Source version 2.12.0. Windows/worker 1.2.6, Android 0.2.0 and iOS 0.2.0.
Durable agents are independent of conversations, devices and models.

## Native mobile clients

Android uses installed native controls and iOS uses SwiftUI. Chat, projects,
agents, tasks and the mobile toolbox are local screens; switching between them
never loads Hub HTML. Network calls synchronize JSON data and authenticated file
bytes. Explicit external links and system Save/Share remain separate from app
navigation. There is no old-page fallback when the server cannot be reached.

Native stores separate bounded account/session caches from durable user drafts
and pending send records. Messages and attachments enter one atomic server
transaction; request keys and receipt-only recovery prevent accidental duplicate
work. Server reads do not claim or restart tasks. Expired login and late callbacks
must not overwrite another account or conversation. Existing browser and Windows
clients continue to use their workspace.

See [API contract](docs/native-mobile-api.md), [Android](android/README.md),
[iOS](ios/README.md) and [release notes](deploy/RELEASE-2.12.0.md).
An unsigned simulator build is not an installable signed iPhone distribution.
Actual binary publication, installation and device tests are verified separately.

## Existing collaboration

- Durable agent roles, project conversations, delegated tasks and public progress.
- Original requirements, corrections and authorized attachments survive handoff.
- Controller/worker execution lanes, execution leases and completion receipts.
- Per-task model choices, measured token usage and bounded relevant context.
- In-chat uploads, private previews/downloads and reversible task archiving.
- Feedback with device context and evidence-based scheduled maintenance.
- Narrative and art-direction contracts for short-drama and stage backgrounds.
- Windows owl branding, tray toolbox, local execution and private offline data.

## Run locally

Use Python 3.10+, install `pip install -e ".[dev]"`, set private
`CODEX_HUB_DATA_DIR`, `CODEX_HUB_SECRET_KEY`, `CODEX_HUB_OWNER_PASSWORD`, then run
`uvicorn codex_hub.main:app --host 127.0.0.1 --port 8797`.
Set your own HTTPS deployment endpoint for each client. Public examples use
reserved documentation endpoints and never connect to the original deployment.

This backup includes source, required assets, tests and deployment examples.
It excludes live databases, private credentials/configuration, user uploads,
local conversation records, logs, build caches and generated installers.
