# 2.12.0 — native mobile data API

Mobile navigation no longer needs a remote Hub document. Android 0.2.0 and iOS
0.2.0 source use installed native screens for chat, projects, durable agents,
tasks and a phone-specific toolbox. Browser and Windows clients keep their
existing workspace. Mobile views request JSON or authenticated file bytes;
an HTML response or login redirect is a connection error, never a fallback UI.

The session API supports existing private login cookies, repairs a missing or
mismatched CSRF cookie without extending the original session expiry, and
returns an opaque session identity for cache isolation. Data reads do not claim
or restart tasks. Messages reuse the existing atomic task/attachment transaction
with user/project-scoped immutable request keys. An uncertain send is recovered
through a read-only receipt; explicit retries retain the original key and bytes.
Uploads recheck session validity after asynchronous file work so logout prevents
an obsolete in-flight upload from creating work.

Native navigation, bounded account-scoped caches and private drafts are separate
from remote availability. File pickers, previews, downloads and sharing use
native surfaces. Mobile clients keep unread/pending content distinct from a
confirmed delivery or a live execution report. Local screens do not provide an
on-device Codex executor; existing online computers still perform assigned work.

Contract: [native mobile API](../docs/native-mobile-api.md). Core web/API regression
passed 205 cases with one Windows file-symlink environment skip. The initial
native contract suite passed 54 isolated API cases and 10 JVM state cases,
covering authentication, migration, pagination, actual attachments, concurrent
idempotency, interrupted upload cleanup and receipt-only recovery. Native screen
build and device/simulator checks are recorded separately in each client README;
source presence is not evidence of a signed iPhone release.

Deploy the additive API before distributing native mobile clients. Preserve the
SQLite database, attachments, session expiry and worker execution state. The
Windows application remains 1.2.6; no desktop reinstall is required for this API
addition. Do not replay production tasks as an upgrade test.
