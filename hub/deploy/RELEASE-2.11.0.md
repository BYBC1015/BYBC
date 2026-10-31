# Hub 2.11.0 — mobile recovery and interaction reliability

This revision targets mobile layout, current-page navigation and local recovery.
Desktop/worker source is 1.2.6, Android source is 0.1.27 (versionCode 28), and
iOS source is 0.1.1. The preceding production workflow contracts remain intact.

## Mobile interaction

Compact touch devices keep the mobile navigation in landscape. Chat, composer,
attachments and delivery controls share a bounded viewport when the keyboard is
open. Extra notices and attachments can scroll without hiding the send button;
actionable send errors remain visible. Identity menus remain within the viewport.
Administrative task, agent and project-history pages retain their existing
functions with mobile controls and a consistent visual treatment. Project history
is explicitly labelled and links back to the current chat workspace; its displayed
version follows the actual release instead of a historical hardcoded value.

## Local recovery

Mobile history/resume recovery performs a new GET for eligible workspace routes.
Downloads, attachments and submitted forms are not replayed as recovery requests.
Legitimate maintenance routes remain reachable. Recovery uses the current interface
and does not execute an obsolete saved workspace document.

The mobile native recovery view can display bounded recent conversation text.
The local record is tied to the account, project, conversation, session digest and
navigation generation. Login/logout and session changes invalidate readable saved
records without deleting existing draft or attachment stores. Offline reading is
labelled as saved content, and never claims a message was delivered or work ran.

Android native communication is restricted to the configured origin and current
main frame, with per-navigation validation. The old all-frame JavaScript interface
is removed. Unsupported WebView capabilities fall back to web functions instead of
restoring that unrestricted bridge. iOS has corresponding scoped native handling.

## PC and web corrections

- The Windows tray has a Toolbox entry. Tray, desktop and control-panel access
  share a configuration-scoped toolbox instance; repeated clicks wake its existing
  window without restarting execution workers.
- Desktop cached responses recheck account and cache generation after asynchronous
  preparation and immediately before serving saved HTML, preventing a late response
  from restoring content belonging to the previous login.
- A late workspace refresh cannot overwrite a newly selected task view. View changes
  discard the old response and promptly fetch the current selection, preserving drafts.
- Active HTML/SVG documents cannot become inline application content by using a PNG
  filename or a misleading media type. Attachment responses are isolated from script
  execution; normal image/video previews, original bytes and range downloads remain.
- Shared maintenance pages expose their actual release identity to native clients.

## Validation and delivery boundaries

Final Python/browser regression reconciles 1,918 distinct cases: 1,916 passed,
two Windows file-symlink permission cases skipped, and no unresolved failures.
Two foreground Tk cases were excluded while the computer was in use. A real,
hidden WebView2 run separately verified restart, attachment-byte persistence,
late-refresh ordering and actual test-server disconnection/reconnection.
Frozen native code and all 40 bundled resources match the inspected source.
Fresh/same-version installation and upgrade from the published 1.2.5 installer
preserve configuration, user files and documented local tool preferences.

Regression uses isolated data, synthetic conversations and background browsers;
it does not replay production tasks or call paid production models. Private release
evidence records failures reproduced before the fixes, final tests, package hashes,
deployment checks and backup verification. Source publication alone is not proof
that a phone or already running desktop process has installed the new binaries.

Android retains its existing private application signing identity. iOS still needs
Apple signing and device distribution; simulator CI is not an installable iPhone
release. Physical phone and real network switching results must be recorded
separately from policy tests and headless browser checks.

See [Desktop 1.2.6](BRIDGE-1.2.6.md), [Android](../android/README.md),
[iOS](../ios/README.md), and [maintenance](MAINTENANCE.md).
