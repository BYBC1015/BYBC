# Hub 2.10.0 — consistent workspace and production contracts

This source release includes the 2.9.6 network/UI corrections and adds a bounded
production-planning contract for short drama and stage-background work. The
Windows desktop/worker source is 1.2.5, Android is 0.1.26 (versionCode 27), and the
independent iOS source remains 0.1.0.

## One workspace across network changes

- Static CSS/JS URLs carry their actual content digest. Native bundled resources
  are used only for the exact digest; mutable historical date labels cannot
  override a newer server asset.
- A verified release change refreshes an idle shell. Draft text, attachment bytes
  and pending receipt state prevent unsafe automatic replacement. An older
  response cannot silently downgrade the current page.
- Compatible desktop snapshots retain saved records while using current bundled
  resources. Unknown/newer layouts use a readable current-styled fallback without
  executing their old scripts. Original cache records and local drafts remain.
- Windows snapshot writes preserve exact UTF-8 bytes. Legacy CRLF conversion is
  recovered only when normalization matches both the original size and SHA256.
- A delayed snapshot preparation cannot mark a different conversation offline or
  reactivate a closed window; navigation identity is checked before state commit.
- Toolbox is restored in the sidebar and mobile drawer. Windows opens the
  installed CodexBridge toolbox with its current configuration; other clients
  explain where it runs and keep the desktop download entry available.
- Android network changes and foreground resume wake the shared release check,
  even if the main page never failed. Recovery fetches fresh HTML, then restores
  normal caching for content-addressed resources. The modern workspace does not
  receive legacy Android styles. Cookies, drafts, imports and queued sends remain.
- iOS source uses a fresh page GET and wakes the shared check on usable network
  path changes, without clearing website data or replaying submitted forms.

No system/browser proxy settings are changed. Viewing Hub and executing a model
remain separate connections. Heartbeat is not proof that a model request works,
and an offline draft is not represented as delivered.

## Professional ownership and source continuity

The one new role is **narrative direction (编导)**: scripts, episodes/acts, shot
breakdown, character narrative and shot continuity. It does not take over visual
production, art direction, postproduction or engineering. Existing role routing
still requires a configured professional identity; adding this catalog entry
does not silently create or reassign agents in every project.

Relevant tasks receive a read-only production contract. Unrelated maintenance,
status-only controller turns and other conversations do not inherit the whole
production plan. The contract creates no jobs or approval records and is not a
claim that any images were actually opened or reviewed.

For short drama, the contract covers script/shot planning, visual standards,
shot production, actual key-shot review and appropriate finishing. For stage
backgrounds, it covers a reusable scene master, recorded camera views, actual
multiview consistency review by art direction, and requested derivatives or
video composition. Existing-image cleanup stays with visual production; video
tracking, keying and timeline composition stay with postproduction.

Review evidence identifies source attachment, revision, hash, camera and scene
relationships, observed differences, and pass/fail/unknown conclusions. A hash
binds the source version but does not prove visual correctness. A new master
revision invalidates the earlier review. Requests to remove people or create
clean backgrounds preserve the established scene outside the requested change;
necessary reconstruction of an occluded area is identified instead of silently
redesigning the entire scene.

Derivative-only work reuses the existing source and does not restart a full
production chain. User-selected references take priority; where creation is
already authorized, internal professional review can establish a master without
asking the user to approve every stage. Real execution and actual visual review
remain the responsibility of the assigned agents and their available tools.

## Build and delivery boundaries

Public GitHub backup contains source, tests, required brand/web assets and
portable deployment examples. It excludes credentials, live databases, user
uploads, conversation snapshots, private logs, build caches and generated apps.

Android is a private debug-signed upgrade using the existing application identity.
Device-specific installation and network-switch behavior require a connected
phone; browser/native-policy tests alone do not prove a physical-device result.
iOS simulator CI builds and tests an unsigned App with its owl icon; this is not
a signed, installable iPhone package or TestFlight distribution.

Source/server publication does not replace an already running desktop process.
The prepared native update takes effect when started from the updated binaries.
See [Desktop 1.2.5](BRIDGE-1.2.5.md), [Android](../android/README.md),
[iOS](../ios/README.md) and [maintenance](MAINTENANCE.md).
