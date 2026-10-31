# Desktop 1.2.6

Adds Toolbox to the system tray beside Open Hub. Every installed Hub entry point
uses the same configuration-scoped toolbox instance and wakes an existing window,
including a minimized one. This does not stop or restart execution workers.

Offline workspace recovery validates the shared cache account and generation both
after preparing a saved document and when WebView requests it. A concurrent logout
or account change prevents the earlier snapshot from being displayed.

The native desktop bundles the Hub 2.11.0 interface. Web corrections cover late
refreshes, responsive touch/keyboard layouts and modern project-history styling.
Drafts, attachment bytes, application configuration and local tool preferences are
preserved by the installer and existing recovery system.

Release verification must cover source/frozen module agreement, bundled resource
digests, fresh and same-version installation, upgrade from the published 1.2.5
installer, user-file preservation and removal of only installer-owned files.
Runtime GUI checks must remain hidden when the user is working on the computer.

Updating the published installer does not update a running process. A local upgrade
requires an idle worker check and re-verification of the new background heartbeat;
do not interrupt an active project or start its production tasks as a health test.
