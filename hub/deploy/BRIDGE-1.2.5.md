# Desktop 1.2.5

This release fixes network-dependent UI differences and restores Toolbox access
from the working conversation. It bundles content-versioned UI resources and
upgrades compatible offline page presentation without deleting the saved work.

Toolbox is launched through the installed CodexBridge executable, not by sending
the control-panel argument to HubDesktop. Opening it requires a user click in a
trusted Hub page. Maintenance does not open a visible tool window.

Upgrade preserves configuration, project bindings, drafts, local preferences,
tools and rollback files. Model connectivity observations come from actual
execution; background monitoring does not make paid model probes.

Supported older snapshots use current bundled content-digest resources; unknown
or newer layouts retain readable records without executing obsolete scripts.
Legacy Windows newline conversion is accepted only when normalized length and
hash match the original record. Navigation changes or closure during snapshot
preparation prevent its late worker from marking another conversation offline.

The source/server release is [Hub 2.10.0](RELEASE-2.10.0.md). Updating source or
publishing an installer does not replace an already running desktop process;
the new native behavior takes effect after starting the updated application.

Publication must retain the Hub installer download and a verified same-version
Baidu share, with size and SHA-256 recorded in the private delivery evidence.
