# Desktop and runtime 1.2.4

The Windows package contains HubDesktop, CodexBridge and HubAgentRuntime 1.2.4. Existing companion tools retain their versions. Configuration, local project files and execution receipts must survive upgrades.

- Restore the owl on built-in loading/offline pages and bundle the matching workspace assets.
- Coalesce window geometry writes so movement/resizing does not synchronously write on every native event. Flush the last position at close.
- Renew execution leases during attachment preparation; respect cancellation before model startup.
- Reuse verified preprocessing results within the same execution after checking current attachment authorization and file hashes.
- `CodexBridge.exe --start --background` starts to the tray without opening the desktop window. It requires a functional tray and valid configuration. Failed initialization exits with diagnostics.
- Upgrades preserve the explicitly declared Beat Workshop local preference file after validating the pristine package. Private preference bytes never enter the release manifest; installed ownership excludes that user file so uninstall also retains it. Symbolic links and directory junctions are rejected before reading.

Changing source or publishing an installer does not update a running installation. Verify installed package version, live worker process and server heartbeat independently. Do not invent a reason for an earlier process exit when logs cannot establish it.
