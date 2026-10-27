# Hub 2.5.0 — continuous conversation and deliverable previews

Release date: 2026-10-28.

Web messages, including text-only follow-ups, now create one durable user entry in the same transaction as dispatch. Exact retries reuse that entry. Historical repair uses proven web-dispatch records and preserves their original timestamps.

The workspace refreshes every two seconds when visible. Focused messages, reading position, selected text and drafts survive incoming replies. New messages can be opened without reloading. A bounded preceding page remains while it is being read. Request timeouts retry, and persisted receipts distinguish received work from completed work.

The dedicated worker has separate controller and execution lanes and separate recovery state. Long execution no longer blocks the controller queue. The controller uses a separate writable coordination directory with network access for authenticated delegation and revision helpers; project edits belong to the execution lane. Pending work can be revised in place; running work receives a dependent continuation, which starts after its predecessor has returned a result. Repeated amendments coalesce into the unstarted continuation. Receiving an amendment does not mean it has already been applied.

Explicit output files and valid linked workspace files are uploaded through a fenced, idempotent artifact endpoint before completion. Images appear with the result, text/Markdown and PDF files have authenticated previews, and other documents remain downloadable. Images use bounded web previews while original files remain downloadable. Absolute and relative references to the same file are normalized to one upload; historical duplicate cards are suppressed without deleting files. Publication does not require a public media host. Upload retries reuse artifacts; invalid delivery manifests become visible blockers rather than endless retries or false success. Limits: 12 files, 50 MB each, 100 MB total.

Controller defaults prefer reported GPT-6 Astra capabilities: medium for brief status communication, high for ordinary coordination, xhigh for explicit deep review. This is a documented preference rule, not a paid classifier for each chat message. GPT-6.1 models participate in per-task executor allocation. Manual user choices remain authoritative and running tasks retain their frozen model.

## Validation and operation

Run the server, execution, artifact, model-policy and real-browser regression suites before rollout. New coverage includes text-only receipts, historical repair, upload retry and stale-generation rejection, escaped document previews, ongoing amendments, concurrent controller/worker lanes, and live message updates at the pagination boundary.

The full suite completed with **1,100 passed, 1 skipped, 0 failed**. The skip was a Windows symlink-permission case. A legacy subprocess encoding warning and a framework deprecation warning remain non-blocking. After bounded final corrections, the artifact/recovery/attachment suite passed all 53 cases, with additional targeted preview and live-browser checks passing.

Authenticated production checks covered nine routes, conditional refresh responses, historical user messages, ten image/document links across the conversation and task inspector, and 1,440 / 1,024 / 390-pixel viewports with no browser script errors. The final public source export passed 27 focused execution/model/artifact tests. A real GPT-6 Astra controller delegated to a GPT-6.1 Sol executor, accepted a revision while the executor was still running, and created a continuation that started only after the original completed. The final document contained the requested added text and the image checksum was unchanged. The controller honestly reported that it could not rename the test conversation through its available helper; maintenance completed that test-only title change, preserving the original response. No native app, second-device handoff or generative-image quality claim is inferred from these checks.

Back up the server source and database, verify the baseline checksums, check migration on an isolated copy, deploy the bounded manifest and restart the web service. Restart only an idle dedicated worker after checking both recovery-state files. Confirm real authenticated previews, historical repair and current model capabilities. Publish only the allowlisted source snapshot to GitHub; never include live uploads, private configuration, tokens, databases or logs.

This release does not claim instantaneous model inference, interruption of a running editing process, verified handoff to an unconnected second computer, or new native mobile/desktop applications.
