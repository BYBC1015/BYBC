# Desktop and Bridge 1.2.0

- Static UI responses are served from the installer only when the exact request version and SHA-256 match the bundled manifest. Other versions use the server.
- Authenticated workspace snapshots retain the real Hub origin. Storage is limited to 20 pages / 10 MB and excludes CSRF values, login pages and session headers.
- Logout, account changes, late responses and obsolete navigation callbacks cannot reactivate an old account snapshot.
- Offline write requests fail explicitly. Reauthentication and a fresh workspace response restore the original page without discarding its input.
- Snapshot writes are atomic and verified on read. Damaged or mismatched snapshots are treated as unavailable.
- The browser profile retains draft attachment bytes across process restarts. The background executor keeps its established task, lease, CLI and MCP behavior.

Build with `tools/build-codex-bridge-exe.ps1` using an explicit build directory, then use `tools/hub-suite/build_desktop.py`. The static manifest is generated from final templates and their CSS dependencies before freezing. The installer retains the previously audited companion tool releases.

Run `tests/desktop_local_probe.py` for source-level WebView2 verification and `tests/desktop_local_packaged_probe.py --exe <HubDesktop.exe> --output <new-report-dir>` against the frozen executable. Both use isolated loopback fixtures and disposable state.
