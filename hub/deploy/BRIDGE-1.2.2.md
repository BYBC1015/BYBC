# Desktop and Bridge 1.2.2

This native revision improves controller responsiveness and reliable recovery without changing the user's agent identities, model preferences, local MCP configuration or companion tool versions.

- Controller and worker inbox polling are independent. A slow worker request no longer blocks controller polling.
- Attachment downloads use a bounded cache scoped to the same Hub, project and content hash. Bytes and SHA-256 are verified on reuse. This saves repeated downloads; it does not itself imply fewer model tokens.
- SHA-identical images within one CLI call share one vision input while retaining reference metadata. Images are not blindly removed across resumed or compacted conversations.
- Public progress is prioritized over routine tool activity, with bounded retry on transient failure. Tool completion produces an actual stage update without fabricating task progress.
- A completed persistent controller session can recover its missing outer receipt without invoking the model again. A genuine timeout preserves its original cause instead of being reported only as an ambiguous session outcome.
- Optional token usage is transported from actual `turn.completed.usage` events. Stable turn IDs permit deduplication; ambiguous event streams remain unknown. Missing counters do not block otherwise valid completion.
- Noncritical subprocess probe text tolerates malformed output bytes. Strict task receipt and JSON parsing are unchanged.

## Build and isolated validation

`CodexHub-Setup-v1.2.2.exe`: **384,560,348 bytes**, SHA-256 `8430428cb2d40cf9ac1a164865e54e110cc79bb39eda6412b792d59eb86a3e8d`.

The installer contains verified 1.2.2 Bridge, Desktop and Worker binaries and matching bundled web assets. Companion versions remain video 1.1.1, Kling 1.2.1, music 0.5.0 and color 1.0.1. Their public files were copied only from the previous audited preparation manifest, after verifying each listed file's bytes and SHA-256. Installed user state and unrelated files were not copied into the package.

Disposable fresh installation, upgrade and uninstallation verified **1,258 owned files**, preserved configuration bytes and user files, and tested the frozen uninstaller. Native response regressions passed 205 tests with one host-specific symlink skip; the overlapping token/interface/package combination passed 65. These overlapping counts must not be added together.

No live installation or application process was restarted, replaced or controlled. The currently running old process does not acquire these changes merely because this package is available.

## Publication

Published on 2026-10-29:

- [Primary Windows download](https://203.0.113.10/hub/downloads/windows): complete server-side bytes and SHA-256 match the verified installer; the public release API, attachment filename and ranged binary download were checked.
- [Baidu backup](https://pan.baidu.com/netdisk/share?surl=lXm12e0d2GW8oNfxWhOMkQ), extraction code `h122`, no scheduled expiry (share period `0`). The newly uploaded file, share code, sole shared filename and byte size were verified. The available Baidu evidence does not verify a complete remote SHA-256; no such claim is made.

The previous installers remain available. Publishing did not restart any service or install over the user's running Desktop/Bridge 1.2.0.
