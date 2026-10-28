# Desktop and Bridge 1.2.1

This native revision adds automatic image input adaptation, persistent controller chat sessions, room-ordered scheduling, a separate read-only follow-up slot, and robust separation of private reflection from public delivery.

High-resolution references keep their original verified local file. The CLI receives a resized derivative when needed, with a clear original-file path for production and detailed reading. Source-size and decoding protections remain active; the change does not promise unlimited input size or memory use.

Ordinary controller turns keep one explicit Codex session per device, project, conversation and agent. Session identity is recorded from actual CLI events, and uncertain outcomes retain their receipts instead of silently creating another thread or repeating the turn. Read-only questions use a separate isolated workspace and do not mutate that session or inherit writable local MCP configuration. Ordinary work keeps its existing configured CLI/MCP behavior.

The reference-inheritance helper verifies the current execution and conversation before downloading selected source files. It supports the exact configured legacy controller root as well as the new conversation workspace, allowing an already-running legacy worker to use an updated helper without moving its files into another project.

Download links and previews retain their distinct behavior. Canceled download navigations no longer cause repeated workspace recovery; a failed recovery is bounded instead of continuously navigating away from the user's work.

## Background update boundary

The user's running desktop and Bridge processes must remain uninterrupted during preparation. No installer, desktop restart, foreground navigation, mouse or keyboard operation is part of source preparation. An available installer or staged binary is not an installed update: native changes become active only when the application is subsequently started with those binaries.

Build after web templates and native helpers are final. Regenerate the bundled static asset manifest before freezing, use an explicit output directory, and validate installation/upgrade/uninstall only with isolated disposable state. Preserve the audited companion tool versions and existing user configuration.

## Publication and verification

Published on 2026-10-29 as `CodexHub-Setup-v1.2.1.exe`, **384,537,499 bytes**, SHA-256 `ffc8c4bc770cef38cc0d175ee5660f3c26488b545ab3f39c9a0264da7b4a24c5`.

- [Primary Windows download](https://203.0.113.10/hub/downloads/windows): the complete server-side installer SHA-256 matched the verified local installer.
- [Baidu backup](https://pan.baidu.com/netdisk/share?surl=2O3NKYcROHz2nt-xYJfyTw), extraction code `h121`, no scheduled expiry (share period `0`): upload, share code, filename and remote byte size were verified. The available Baidu evidence did not verify a full remote SHA-256; no such claim is made.
- Disposable fresh install, same-version upgrade and uninstall verified **1,258** owned files, preserved existing configuration bytes and user files, and checked the frozen uninstaller's cleanup. These checks did not alter the live installation, register shortcuts/startup entries, scan the system or call paid APIs.

The running user installation was not restarted or replaced. Publication and verified isolated installation do not mean the user's current desktop process has upgraded; the native changes take effect on a later start with the new binaries. The subsequent web 2.9.2 continuation fix retains this native 1.2.1 release.
