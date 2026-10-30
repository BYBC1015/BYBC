# Hub 2.9.5 maintenance

This revision restores the existing owl mark, uses a Chinese sans-serif font stack with regular body text and stronger headings, and adds keyboard-aware mobile workspace layout. Normal text is 16 px. Fonts come from the device; no external font download is required. Mobile drawers, composer attachments and agent creation use the visible viewport and safe-area insets.

Desktop/Bridge/runtime source is 1.2.4. Native move and resize callbacks update memory immediately and coalesce optional position-file writes. Closing flushes the latest position. Built-in connecting and offline pages use the same owl and font direction.

Execution lease renewal now covers attachment preparation as well as model execution. A cancelled or revoked permit cannot proceed to launch after slow preparation. Within one execution, verified image/document preprocessing results can be reused after fresh attachment authorization and original/derived file hash checks, avoiding duplicate decoding. PDF originals remain available to the executing tools; this does not add a new PDF rendering feature. This is not a measured end-to-end model latency claim and does not add model calls.

The Bridge supports `--start --background`: start to the tray without opening HubDesktop. Worker startup waits for the tray to be ready; invalid configuration or tray failure exits visibly in logs instead of leaving an uncontrolled hidden worker. Ordinary interactive startup remains available.

Windows installer upgrades also preserve the declared Beat Workshop local preference document instead of replacing it with the package default. Its original bytes are verified separately; release manifests remain pristine and uninstall leaves this user preference intact. The migration refuses symlinks and directory junctions before reading.

Android 0.1.25 recognizes current workspace and conversation routes, keeps authenticated file downloads in the app, handles current drawers with native Back, and releases imported attachment cache files only for the exact confirmed submission batch. An older send cannot remove newly selected files.

The first independent iOS client source is under `ios/`. Build and signing status must be checked against its README and actual CI/distribution records; source availability is not proof of an installed or signed iPhone app.

Validation includes isolated API/runtime regressions, headless workspace tests, native-host tests and packaged installer checks. Mobile keyboard geometry tests are simulations unless identified as physical-device evidence. Preserve separate live deployment, local installation and distribution evidence; do not infer activation from source versions alone.

Release procedure: back up code and data, validate the bounded source delta, run regression, verify installer and mobile artifacts, publish version-specific downloads with verified backup shares, then record deployed hashes and actual local runtime presence. Never replay historical project tasks as a maintenance test.
