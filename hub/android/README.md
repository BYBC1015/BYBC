# Codex Hub Android

Android WebView shell for the private Codex Hub.

Current release: `v0.1.26` (`versionCode 27`).

v0.1.26 addresses stale UI after changing networks. Native network changes and
foreground resume now wake the shared workspace's release/connection check even
when the main page never failed. A fresh native GET recovery bypasses the HTTP
cache until HTML commits, then content-addressed resources use normal caching.
Cookies, IndexedDB drafts, queued sends and imported files are retained.
The modern workspace never receives legacy Android CSS or the legacy UI adapter.
An idle old workspace can migrate only after a fresh same-account/same-conversation
response; draft text, selected/saved files, or a queued/uncertain send keep the page
open with an update notice. Recovery performs no POST and never repeats a task.

v0.1.25 supports the current Hub workspace and `/projects/{slug}/conversations/{id}`
addresses. The native attachment picker and cold-start restoration now recognize
those chats. Back closes the current image, modal, sidebar or task detail before
navigating away. The shared web workspace owns drafts, keyboard layout and live
updates, avoiding a second competing mobile draft store.

Authenticated result downloads now stream inside the app without opening an
external browser or replacing the conversation. Android 10+ saves to
**Downloads / Codex Hub** with MediaStore; Android 6–9 opens the system Save picker.
Downloads preserve Hub cookies only for same-Hub file routes, validate redirects,
reject login HTML and truncated transfers, and bound actual transfer bytes to
1 GiB. Failed transfers remove only their own temporary output. API 23–28 allows
one transfer/save at a time so a second download cannot displace the Save picker.

Imported attachment cleanup is tied to the exact confirmed send request and its
import IDs. If attachment B is added while message A is finishing, A's delayed
receipt cannot delete B. Unknown/restored receipts retain imported files instead
of deleting an unrelated batch. Cancellation and failure keep unsent imports.

2026-10-30 validation: native APK compilation, matching previous debug certificate,
23 attachment/navigation/download boundary tests and 17 legacy mobile/browser
tests. This is an upgrade-compatible **debug-signed private APK**, not a Play Store
release. No Android device was attached for installation or native MediaStore /
WeChat validation; those device checks remain required. The iOS source and its
separate signing/build state are documented in `../ios/README.md`.

v0.1.24 adds explicit attachment sources and WeChat file handoff. In a project,
tap the attachment button to choose phone files, another app, or the WeChat
instructions. Android cannot enumerate WeChat's private chat-file directory.
Open and download the file in WeChat, then choose **其他应用打开 / 分享 → Codex Hub**.
In Hub, open the intended project and tap **附件 → 微信 / 其他应用待导入**. The
normal attachment tray appears; review it and tap Send yourself. A received share
never sends a message, uploads a file, changes projects, or replaces draft text.
If a WeChat version does not expose a readable share, save the file to the phone's
Downloads folder and use **手机文件**. The app also fixes scrolling on Downloads
and other full-page destinations reached from More.

Only user-granted `content://` files are accepted, including single-file and
multi-file shares and content-based Open With. File/network URLs, this app's own
provider URLs, and missing read grants are rejected. Sharing copies files into
the app's private cache in a background worker: at most 10 retained imports,
50 MiB per file, and 100 MiB total cache. Limits use actual streamed bytes even
when provider size metadata is absent or false. Unreadable/oversized files are
reported without attaching them; successfully copied files remain available.
Names, MIME types, and exact copied lengths are exposed through a non-exported,
read-only provider. No broad storage or media permission is requested.

Canceling the source picker leaves the import queue intact. **清空待导入** deletes
only that queue. Successfully sent composer attachments release their imported
copies; unsuccessful sends retain them. Activity recreation retains saved queue
handles; navigation/reload returns unsent copies to the source queue so they can
be selected again. Ordinary exit and the next cold launch remove unused cache files. Pending
imports are temporary and are not a durable file backup. Sign in and explicitly
choose the destination project's file input before importing them. This release
has native compilation and automated boundary checks; WeChat's actual Open With
menu and sharing behavior still require validation on the user's Android device.

v0.1.23 aligns the bundled Android shell with the refreshed Hub website: warm
neutral surfaces, restrained borders, yellow primary actions, and more readable
chat text. The drawer, bottom navigation, More cards, native connection recovery
panel, and system-bar backgrounds now share the same palette. Composer controls
keep 48 dp touch targets while fitting narrow phones, and login fields use 16 px
text. Native navigation remains independent of the website's overflow menu.

The APK still uses the existing application id and signing certificate, so it
can upgrade the existing installation without clearing its private local drafts.
The build script now rejects failed builds before copying an APK into `dist`.

v0.1.22 adds a persistent three-destination bottom navigation for chat,
projects, and more. The project drawer now has a modal scrim and focus return,
the More page uses larger two-column feature cards, and primary touch targets
are at least 48 dp. Chat text, controls, and status feedback have also been
adjusted for phone readability and more native press behavior.

The App now shows online/offline state in the project header, keeps offline
submissions in the private local draft instead of attempting an uncertain send,
and turns send progress and errors into visible, temporary feedback above the
composer. Main-frame failures use a centered native recovery card with bounded
automatic retry, manual reconnect, accessibility announcements, and explicit
draft-safety guidance.

v0.1.21 fixed the first five pages in the Android "更多功能" menu so the
embedded tool container is visible and interactive after switching away from
chat. It also restores the missing task page entry when the shared template
already provides the `tasks` id.

It also prioritizes the foreground WebView renderer, defers text-to-speech and
notification permission work until after the chat begins loading, and adds a
native connection recovery bar with bounded automatic retries. Active Android
chats now receive small incremental message updates about every 1.2 seconds;
the full project page is reconciled once per minute instead of every 10 seconds.

The project chat header stays compact and now includes a small connection-state
chip. Project Memory remains available inside the drawer alongside shortcuts for
creating a project and managing project members. Chat, Projects, and More are
always reachable from the bottom navigation; secondary tools also keep a visible
Return to Chat button.

The v0.1.20 composer reads only the files explicitly granted by Android's system
picker, generates small previews off the UI thread with native APIs, and passes
those previews back to the composer. Draft text and the last project location are
stored in the App's private local data so interrupted input survives and cold
starts can return directly to the current chat. Project chat history and sent
attachments remain server-backed and are not copied into the App's private data.

Security defaults: HTTPS-only traffic, no mixed content, no third-party cookies,
no filesystem access, Safe Browsing enabled, and no app-data backup or
device-transfer export. Content-provider access is enabled only so WebView can
read the `content://` grants the user explicitly selects in Android's picker.

Hub:

```text
https://203.0.113.10/hub/
```

Stable Android download:

```text
https://203.0.113.10/hub/downloads/android
```

Open `更多` from the bottom navigation and choose `下载更新`. It uses the same
card navigation as the other project tools. The App does not place a floating
native button over the project title.

Voice interaction is handled directly on the device: `说` converts speech to editable text, and `听` reads device replies aloud. Raw microphone audio is not uploaded to Hub. Sending always requires an explicit user confirmation through the normal Send button.

On a project chat, tap the drawer button or swipe right from the left edge to reveal the project drawer. The compact header keeps only the project name and APK version beside the drawer button. The drawer exposes `项目记忆`, `新建项目`, `管理项目成员`, and `更多功能` around the project list. Project members can contain writable devices plus named roles bound to those devices, with an independently selected primary device.

The app also runs a silent, low-importance foreground notification connection while signed in. New device replies produce audible, vibrating Android notifications when the app is in the background. Android 13+ users must allow notification permission; the app explains when that permission is denied. The v0.1.15 message channel intentionally uses a new channel id so upgrades do not inherit the old silent channel.

Build:

```powershell
codex-hub/android/build-apk.ps1
```

Versioned APK output:

```text
codex-hub/android/dist/CodexHub-v0.1.25-debug.apk
```
