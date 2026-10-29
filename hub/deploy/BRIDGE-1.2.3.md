# Desktop and Bridge 1.2.3

This revision combines the updated reply presentation with native window reliability fixes. It preserves existing local configuration, login storage, drafts, project files and tool versions.

- Retry returns to the current trusted conversation or preview. Login, API and download locations cannot replace that recovery destination. A new user navigation cancels a queued retry.
- Closing a window revokes pending retries and late callbacks. A preview created concurrently with closing its parent is disposed; unrelated windows and background workers remain independent.
- Only the download that belongs to the current navigation can suppress that navigation's failure handling. An unrelated background download no longer hides a real connection failure.
- Damaged optional snapshot metadata or unavailable bundled resources fall through to recovery/network behavior instead of preventing startup. User drafts and login data are not deleted as a repair step.
- Window restoration uses the display containing the saved title bar, avoiding unnecessary shrinking on a larger secondary screen. Disconnected-display recovery remains available.
- Native loading and reconnect screens use a rounded dark panel, clear Chinese status, accessible retry controls and reduced-motion support. Failed retry or missing bridge initialization has a visible recovery message.

The new Web 2.9.4 reply assets are bundled by exact version and hash. No extra model calls are used for animation or connection presentation.

Native lifecycle, geometry, download, cookie, cache and packaging regressions passed 122 related tests before packaging. These overlap the complete release regression and are not an additional independent count.

The final package is `CodexHub-Setup-v1.2.3.exe`, 384,575,161 bytes, SHA-256 `dab0a81dd8e49a73cccfe6cf2f1dcb5b4364e0a551b371c69eaed9cd75f8bab1`. Its Desktop, Bridge and Agent Runtime binaries report version 1.2.3. Thirty-six bundled web resources match the verified source assets, including `workspace-shell.js?v=20261029-reply-stability2`.

Isolated installer checks passed for fresh installation into a Chinese/spaced directory, same-version upgrade, file hashes, rollback metadata, byte-for-byte configuration preservation, user-file retention and the packaged uninstaller. All 1,258 owned installed files were verified. These checks used isolated directories and state; they did not register startup entries or launch the user's installed application. Companion tool versions remain video freeze 1.1.1, Kling batch 1.2.1, beat workshop 0.5.0 and color workshop 1.0.1.

An actual 1.2.2-to-1.2.3 isolated upgrade also passed all 1,258 file checks, configuration and user-file preservation, rollback metadata and owned-file removal by the packaged uninstaller. The first attempt to start the old installer's bootloader from a D-drive temporary directory failed; that attempt is retained separately. Retrying only that old bootstrap with the ordinary user temporary directory succeeded. No running production application was stopped for either attempt.

The user's current application is not closed, restarted or installed over during background work. Native changes take effect on a subsequent start with this version's binaries.
