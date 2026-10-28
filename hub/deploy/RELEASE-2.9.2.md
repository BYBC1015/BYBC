# Hub 2.9.2 — bounded continuation references

This follow-up addresses a queued revision that inherited 21 attachments and was blocked before model execution by the worker's 12-file preprocessing limit. The earlier production work had already completed; retrying that completed work would not resolve the continuation's input-selection error.

The correction belongs to server-side revision inheritance: prepare the current revision with the relevant references and preserve access to older sources/results through the conversation catalog, instead of automatically adding the entire accumulated archive to every new execution. Original file records remain available. The worker's existing input protections stay active, and the desktop/Bridge/worker release remains **1.2.1**.

## Implemented behavior

- Revisions retain original inputs, explicitly identified source references and the latest user correction before selecting the latest completed delivery. Earlier deliveries and redundant generated previews remain in the conversation catalog.
- Selection respects the existing 12-file, 50 MiB per-file and 100 MiB total preprocessing limits. If a full image cannot fit, its smaller generated preview remains eligible. User-uploaded previews and explicitly selected older reference images are not discarded as duplicates.
- Required inputs that cannot be preloaded are explicitly listed as unread, with their source and reason, in the actual server-generated prompt used by older workers.
- Each execution stores an immutable input manifest when acquired. Prepared, running and uncertain executions keep those same inputs when late files arrive; project/conversation boundaries and execution generations remain enforced.
- Attachment and context reads do not change bindings. Revision creation, source delivery and execution acquisition update them transactionally. Original files and historical result records are preserved.

## Release status and validation

Web **2.9.2** was deployed on 2026-10-29. The 114-file production baseline was checked before applying **11** changed source/dependency files. An isolated production database copy passed additive migration, execution/handoff/FIFO/reference tests and the real blocked revision's 11-input selection and prepared/running snapshot checks before production code changed.

Post-deployment verification passed ten authenticated/health pages, conditional refresh and workspace SSE. All 11 deployed source hashes matched; database integrity was `ok`, and all 33 tasks, 1,530 entries, 1,216 attachments, seven agent profiles and 58 experience records were preserved at that checkpoint. Device contact remained current.

The focused revision regression passed **105/105** checks. An independent overlapping combination passed **67**, with one platform-specific skip. Two tests using the archived **1.2.0** runner and attachment modules verified pre-execution recovery exactly once and reloading the acquired input snapshot after a simulated late source file. These overlapping counts must not be added together.

The final full regression recorded **1,538 tests: 1,537 passed, one Windows symlink test skipped, zero failures and zero errors**, in approximately eight minutes. The two tests that require foreground Tk windows were excluded while the user was using the computer. Warnings remain for a test-client dependency deprecation and a subprocess output decoding warning in the unreachable-Hub CLI test; the latter's assertions passed. This is not a zero-warning result.

The blocked revision's 11 selected files total **26,819,676 bytes**. Their original bytes, SHA-256 and image dimensions were verified before a guarded transaction backed up the old task/bindings, replaced only its input bindings and reopened the never-started revision. The completed source delivery was not reopened. The running desktop/Bridge processes and local execution receipts were left untouched.

The existing worker subsequently started the revision automatically in **generation 1**, with exactly those 11 input IDs in its frozen manifest and a fresh public progress event. There was only one execution record; the source task retained its completed generation and result entry. This proves recovery and input continuity, not final acceptance of the ongoing visual revision.

The existing live desktop application remains uninterrupted, and no new native installer is required by this web-only increment. Desktop/Bridge **1.2.1** remains published; availability is not evidence that the user's currently running application has upgraded.

Previous verified release and native publication records: [Hub 2.9.1](RELEASE-2.9.1.md), [Desktop/Bridge 1.2.1](BRIDGE-1.2.1.md).
