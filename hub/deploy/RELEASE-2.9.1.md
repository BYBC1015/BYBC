# Hub 2.9.1 — large references and continuous coordination

This revision addresses rejected high-resolution references, lost conversation context, slow follow-up replies, unreliable downloads, and missing controller summaries after delivery.

- The native worker retains verified original image bytes and creates a bounded vision copy for large references. JPEG decoding is reduced before resizing; orientation and transparency are preserved where applicable. Corrupt files and excessive decoding requirements remain bounded rather than disabling Pillow's protection.
- Web image metadata and preview creation handle oversized or corrupt inputs without breaking the page. Preview writes use separate temporary files and atomic publication, including concurrent requests.
- A conversation retains its initial user requirement, recent corrections, real work status, and a catalog of its original reference files. A currently authorized controller can explicitly inherit selected files from that same conversation, download the verified originals, and read them before delegating. A catalog entry or previous summary is not treated as proof that a file was read.
- Ordinary controller turns use a stable CLI conversation and run in message order within their Hub conversation. Both the local pool and server execution checks enforce that order. Independent conversations retain parallel execution and priority handling.
- Complete, short status or materials questions can use a separate read-only controller slot. They retain the selected model but cannot delegate, revise work, answer handoffs or change another task's model. They do not interrupt or duplicate the ongoing work.
- Reflection blocks remain internal even when invalid, unfinished or repeated. Missing structured reflection is recorded as missing evidence, without appending implementation terminology to the public reply or repeating the task. Artifact declarations remain available to the delivery pipeline.
- Work with verified files attached to its latest completed result can leave the active board while remaining unaccepted and available in history. Explicit user restoration is respected. Old blockers are retired only through a proven successor relationship, not through title similarity. Ordinary page reads do not mutate task organization.
- Download links retrieve original files while preview links keep the conversation open. Historical attachment links resolve from either project or conversation pages. Desktop download cancellation no longer starts a repeated offline-recovery loop.
- Worker results create an immediate visible receipt and a bounded, read-only controller summary in the same conversation. The summary distinguishes delivered work, unresolved requirements, missing information and next steps. Result receipts are idempotent; late files update the existing notice, and summaries never recursively summarize themselves.
- Delegation must retain the mapping from each deliverable to its source reference/version, required preserved elements and permitted changes. A visual deliverable must be checked against those requirements; generic dimension, spelling or file-count checks are not evidence of visual acceptance. Unconfirmed directions remain proposals.

Existing drafts, account-scoped outbox receipts, task execution generations, local completion receipts, original attachments, and user conversation records remain part of the established continuity model.

## Release status and validation

Web **2.9.1** was deployed on 2026-10-29. The bounded deployment verified 26 changed source files against the saved baseline, passed the isolated migration/execution checks and restored a healthy service. Post-deployment checks covered ten authenticated/health pages, conditional refresh responses and live workspace SSE. Database integrity was `ok`; agent profiles and existing record counts were preserved.

The final full regression recorded **1,528 tests: 1,527 passed, one Windows symlink test skipped, zero failures and zero errors**. Two tests that require visible Tk windows were intentionally not run while the user was using the computer. Earlier targeted/integration counts overlap with this suite and must not be added together. Validation does not establish perfect behavior for every future workload: a later queued revision exposed attachment accumulation beyond the worker's preprocessing limit; see [the 2.9.2 follow-up](RELEASE-2.9.2.md).

Desktop/Bridge/worker **1.2.1** was built and published. Isolated fresh-install, same-version upgrade and uninstall checks verified all **1,258** owned files and preserved configuration/user data. The installer is `CodexHub-Setup-v1.2.1.exe`, **384,537,499 bytes**, SHA-256 `ffc8c4bc770cef38cc0d175ee5660f3c26488b545ab3f39c9a0264da7b4a24c5`. See [Bridge 1.2.1](BRIDGE-1.2.1.md) for publication and backup evidence.

The currently running desktop application is intentionally left uninterrupted. A staged native update takes effect only after the application is subsequently started with the new binaries. A web deployment or source backup must not be described as a completed local installation.

Public source backups exclude live databases, user uploads, drafts, snapshots, device secrets, authentication state, and execution logs. See [Bridge 1.2.1](BRIDGE-1.2.1.md) for native update boundaries.
