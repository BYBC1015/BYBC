# Hub 2.9.4 — stable live replies and desktop presentation

The workspace now separates a confirmed execution start, its latest public report, connection uncertainty and final delivery. A new execution can show a waiting card before its first report. Existing text remains visible if its execution lease becomes uncertain; the interface never invents a fresh report from a device heartbeat.

- Soft rounded workspace panels and reply cards, legible system typography and restrained arrival motion make response states easier to scan. Only real new content produces a short animation; clock updates do not animate. Reduced-motion preferences are respected.
- Public text, the most recent tool report and optional running details have separate presentation. Connection loss is visible directly on the reply. Review status remains unaccepted until the user accepts it.
- Status/age refreshes retain the rendered reply, open details, media and reading position. While text is selected for copying, only replacement of that body is deferred; execution and connection status still update. Clearing the selection resumes the pending body update.
- The first real reply on an existing waiting card shows the new-message indicator when the reader is in older history. Parallel activities keep their start order instead of jumping with each report.
- Online browser events and an old conditional response do not prove that the current workspace is fresh. Recovery requires a validated same-user workspace response. Previously received replies and drafts remain available while reconnecting.
- Generation, task version, assignment and lease checks continue to reject late progress after cancellation or final completion. A new execution generation replaces the old transient card.
- A delayed local queue read cannot restore a pre-send state after an upload has started or failed. The current attempt keeps its original request key and attachment order, checks the server receipt and does not falsely offer cancellation of already-attempted work. This race was caught by the complete regression and reproduced at both in-flight and post-failure boundaries.

These changes add no model call, synthetic typing stream or token-consuming status generation. Existing bounded refresh and event notifications carry the presentation updates.

Desktop/Bridge 1.2.3 adds native retry, navigation and window-lifecycle fixes; see [native release](BRIDGE-1.2.3.md). A published installer does not change a running older desktop process. Newly loaded pages use the current web assets; already open pages are not claimed to hot-replace JavaScript.

Historical feedback records with absent or malformed environment sections no longer fail the feedback page. The list, individual API response and maintenance snapshot share a read-only normalization step. Missing device/browser/viewport details remain unknown; the original stored feedback is not rewritten.

## Verification

The complete suite plus final targeted checks covers 1,749 distinct cases: 1,748 passed and one host-dependent symbolic-link case skipped. Two tests that open foreground Tk windows were excluded to keep the user's current desktop uninterrupted.

This is a reconciled result, not a claim that the raw complete-suite invocation was all green. The retained final full run had 1,734 cases, three failures and one skip. Two failing browser assertions observed restored Blob request bodies unreliably; they now check the original server receipt and database request-key mapping. The third asserted a retry control before its receipt lookup had settled. The final 19-case targeted run passed, including both stale-queue timing boundaries and a controlled 5.5-second receipt delay. All three prior failures have matching passing final cases. A subsequent feedback compatibility fix passed 63 relevant checks, including 14 new malformed/legacy context cases; overlapping checks are counted once.

The earlier full run found the actual stale queue read defect. Its failing evidence remains retained separately from the successful fixes. No production task was replayed and no paid model was called for release validation.

## Web deployment

Web 2.9.4 is deployed. Thirteen released source files match their recorded hashes. Authenticated project, conversation, agent, task, archive, feedback and maintenance pages returned HTTP 200; live event notifications and refreshed workspace responses were verified.

Database integrity passed. Deployment comparison preserved 34 tasks, 1,533 entries, 1,216 attachments, seven agent profiles and 61 experience records, with profiles unchanged. A separate presence check found the existing execution worker online after deployment. These counts describe this release's verification snapshot, not a permanent limit.

This public source backup contains code, tests, dependency metadata, required assets and deployment examples. It excludes live databases, uploads, account credentials, runtime logs and generated installers. Native installation is separate from the web deployment; see the native release notes for the verified package and activation boundary.
