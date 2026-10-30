# Hub 2.9.6 — network-consistent workspace

The browser/phone connects to Hub; Codex runs on a registered execution computer.
The viewing device does not need its own Codex installation or provider connection.
An executor heartbeat and a successful model request are separate observations.

- Static URLs now contain a digest of their actual content. Only a matching
  digest receives immutable cache headers; old date labels are revalidated.
- Workspace responses carry a UI release marker. An idle recovered page loads
  the current shell. A draft is kept in place until its text and attachment bytes
  have been durably saved before the user loads the updated shell.
- Responses from an older release cannot downgrade an already newer workspace.
- The sidebar includes Toolbox on desktop and in the phone navigation drawer.
  Windows opens the installed toolbox; other clients explain where it runs and
  provide the existing desktop download entry.
- Desktop 1.2.5 aligns supported older snapshots with packaged current resources
  while keeping saved messages, account isolation and the unsent-message queue.

No system or browser proxy settings are changed. An unavailable execution
computer/provider is not represented as working; unverified computers are not
used as automatic failover targets. Offline drafts are not claimed as delivered.

Private deployment records contain regression results, preserved-state checks,
installer hashes and primary/backup release links. Credentials, live databases,
cached conversation content and installation backups are excluded from GitHub.
