# Hub 2.9.3 — continuous conversation and measurable maintenance

This revision addresses real first-project failures: status follow-ups were sometimes queued as new work, the newest correction could disappear from a long summary, a slow local outbox could overwrite text typed after sending, and old failed uploads could remain active after a successful resend. It also adds durable response timing and actual executor-reported usage so later optimization can use evidence.

## Conversation and requirements

- Ordinary status questions, including “还要多久” and explicit task-number progress queries, use the read-only controller lane. Work changes remain writable work, retain the appropriate reasoning policy, and respect explicit user model settings.
- A status question that names an older task resolves that exact task within the same project and conversation, even after it has fallen outside the recent-work list. Missing or out-of-scope task numbers stay unknown rather than substituting the newest task.
- Read-only prompts retain identity, project constraints, original requirements, the latest correction, current task status and reported progress age. They omit delegation tools, unrelated team details and other instructions that cannot be used in this lane. The final representative fixture decreased from 5,247 to 1,877 characters (64.2%); this is prompt character reduction, not a measured billing-token or latency claim.
- Completion context retains the latest user updates even when those updates preceded the completion. Long descriptions retain their tail correction. Oversized revision requirements require accurate consolidation instead of silent truncation.
- Duplicate revision requests within one controller turn are idempotent across request aliases. Reusing an alias with different content remains an error.
- Sending from older history reveals the user's new message. Reconnection keeps a reading anchor when a large batch arrives. Delayed draft restore and IndexedDB writes cannot replace a more recent edit or intentional clear.

## Work lifecycle

- Failed pre-execution inputs may be archived after a later successful resend only when the full requirement, conversation, agent, user and complete attachment-content multiset match. A separately audited, user-confirmed relation supports resized uploads with different hashes.
- Archival preserves original messages, attachments, task status and history. A manual restore is respected. A task that actually executed or is waiting for coordination cannot be dismissed as an obsolete upload.
- A real timed-out production revision remains an unresolved execution, even if candidates exist on disk. Candidate files, reported completion and verified delivery remain distinct.

## Observability and improvement

- Response timing persists after live progress is cleared. Status heartbeats do not count as a public answer. Historical or partially observed executions have unknown first-response timing; negative clock intervals stay unknown. Device attribution uses the device that actually executed that generation, not a later task assignment.
- Optional token counters come only from actual CLI completion usage. The selected model/effort is frozen at acquisition. Missing, malformed or conflicting counters remain unknown; retries do not double-count them. Cached input is included in total input, not added to it. These counters do not establish a monetary cost or prove provider routing.
- The existing hourly maintenance check scans bounded, content-free timing/failure records without invoking a model. Repeated execution failures or three consecutive measured slow controller starts can add diagnostic evidence to a pending maintenance task. Running maintenance is not rewritten or duplicated; consumed evidence does not trigger again.
- Weekly maintenance remains configured per project. Fresh anomaly work is bounded to one diagnostic dispatch per day after earlier maintenance, excludes maintenance's own conversations, and preserves the engineer's device binding and disabled setting.
- Engineering instructions require reproduction, actual changes, regression results and a release/rollback record before reusable experience can be called verified. Task completion alone does not mark user feedback fixed. This is application maintenance and validated experience reuse, not model-weight training.

## Native activation and verification

Desktop/Bridge **1.2.2** contains the native counterpart: independent polling lanes, scoped verified attachment cache, bounded public progress retries, timeout-cause preservation, persistent-session completion recovery and actual token usage transport. See [native release](BRIDGE-1.2.2.md).

The running user application is left uninterrupted. Publishing a native package does not upgrade the current process; native changes become active only on a subsequent start using the new binaries. Web changes and native changes have separate activation evidence.

## Deployed verification

Web **2.9.3** was deployed on 2026-10-29 from a baseline-checked **20-file** delta. Before activation, the final archive passed isolated production-database migration, execution/handoff/resume, shared-resource serialization, cancellation, read-only fencing, original reference inheritance, archival and persisted timing/usage checks on the host's Python 3.10 runtime. The deployed file hashes matched the final manifest; code and database backups were retained for rollback.

The final complete regression recorded **1,686 tests: 1,685 passed, one host-specific symlink test skipped, zero failures and zero errors**, in 519 seconds. The two foreground Tk window tests were excluded to leave the user's computer usable. One existing Starlette/httpx test-client deprecation warning remains. An earlier full run caught the polite-prefix model-policy regression; it was fixed before the complete final rerun. Focused and independent review counts overlap this suite and must not be added together.

Post-release verification passed ten authenticated/health pages, conditional refresh behavior and a live workspace SSE event. Database integrity was `ok`; **34 tasks, 1,533 entries, 1,216 attachments, seven agent profiles and 61 experience records** were preserved at that checkpoint. The worker heartbeat remained current.

The two audited historical resends were then reversibly archived after private backup and content checks. Their original task status, messages, attachments and successful successor executions were preserved. The actual timed-out production revision was not archived or replayed.

The existing hourly maintenance service ran successfully with the new scanner and reported no repeated signal, creating no new diagnostic/model work. Five owner-reported issue/improvement records were saved with the actual release and verification scope; native activation limits and the unresolved production revision are explicit in those records. No experience was automatically relabeled verified.

Desktop/Bridge **1.2.2** was published to the primary download and a verified Baidu share. Isolated installer QA passed; the running user's **1.2.0** processes were not replaced or restarted. Existing open pages are not claimed to have hot-replaced their JavaScript; newly loaded pages use the released resources.
