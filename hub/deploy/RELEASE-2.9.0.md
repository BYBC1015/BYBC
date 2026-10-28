# Hub 2.9.0 — durable desktop conversations

The composer keeps selected attachment bytes and draft text in account/project/conversation-scoped IndexedDB storage. Only an explicit send creates an outbox item. Delivery retains its original request key, verifies the current account and CSRF token, and checks an uncertain receipt before retrying. Atomic storage transitions and cross-window ownership prevent independent tabs from making separate tasks for the same send.

Draft persistence failures are visible. A queued item that has never been submitted can be cancelled back to a draft; an unknown server outcome remains pending until reconciled. The server's idempotent dispatch remains authoritative.

Desktop 1.2.0 bundles matching UI assets and bounded, account-scoped snapshots of previously visited workspaces. Offline snapshots have no CSRF token and are visibly marked. Real authenticated responses, rather than the browser's network indicator alone, unlock sending again. Unknown pages and uncached result media need a connection.

This remains a WebView2 desktop application with native local execution, not an offline AI model. Local unsent items resume when their conversation is open; jobs already accepted by Hub continue in the separate background executor.

Real Hub login cookies now survive a complete app exit. Login and authenticated page responses set persistence only for the existing server session's remaining lifetime (at most 14 days). This also migrates existing logins during a normal refresh; it does not extend the database expiry. Logout still revokes the session and removes both cookies.

Verification: 286 core tests passed (1 opt-in test skipped); the composer group passed 50 checks including 27 browser cases, with an additional real timeout check; 11 existing continuity browser checks passed. Independent review exercised 8 account, attachment and multi-window cases. The frozen desktop also passed offline/restart/resource-version checks, and real WebView2 login was checked over three independent processes.

Desktop installer: `CodexHub-Setup-v1.2.0.exe`, 375,629,082 bytes, SHA-256 `5be5914f8afcaa3b9ca848a9af7bd550b7e5e7d3a8cd7437e6c59ab8bfad084e`. The real isolated install, upgrade and uninstall verified 1,258 files and preserved user configuration and personal files; 40 installer regressions passed.

The source backup excludes user snapshots, drafts, attachment bytes, account cookies and live device configuration.
