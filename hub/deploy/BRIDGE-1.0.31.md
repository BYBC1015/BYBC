# CodexBridge 1.0.31 — integrated Hub execution

The desktop application now owns the dedicated persistent-agent worker. Its
Hub agent connection is displayed independently of the older Codex and Hermes
connections; those older connections do not imply that the agent worker is online.

- Start/stop controls cover all connections. Opening the app with an existing
  dedicated-agent configuration also starts the agent connection.
- The existing Windows startup entry launches the integrated service. Closing
  the window keeps it running in the tray. Explicit exit drains existing work.
- Online status requires a real server response and expires after two minutes.
  An unexpected worker exit is logged and retried with bounded backoff.
- Existing worker configuration, dedicated device identity, execution receipts,
  controller lane and task concurrency are retained. The singleton prevents
  simultaneous ownership by the app and the standalone fallback launcher.
- `HubAgentRuntime.exe` is a console companion for structured agent requests.
  Ship it beside `CodexBridge.exe`; frozen workers must not invoke the windowed
  application as if it were a Python interpreter.
- Stopped legacy connections no longer retain a stale online badge. The header
  leaves room for the additional device identity.

Validation: service lifecycle, crash retry, missing configuration, stale presence,
singleton contention, graceful draining, GUI status separation, frozen helper
selection, execution recovery, state preservation and installer compatibility.
The built helper was also checked against the configured Hub with a read-only
identity inspection; the installed application was checked for actual heartbeat,
unified stop/start and tray persistence.

This release was applied as a local application update. No new downloadable
installer is published by this change; installer publication retains the required
Hub and Baidu mirror workflow. Existing companion tools are preserved.
