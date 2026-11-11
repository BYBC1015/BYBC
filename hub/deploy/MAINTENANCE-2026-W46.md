# Maintenance continuation — 2026-11-12

The worker/Windows release is 1.2.7; the server remains 2.12.0. See
[the changes and compatibility boundaries](BRIDGE-1.2.7.md).

## Verified behavior

- Four real, isolated controller turns used actual inspect, delegate and revise
  calls. Screenshot corrections stayed with visual production; missing video
  sources deferred final video replacement only. Follow-up corrections updated
  the original task and preserved the other regions and delivery requirements.
- The same inspection state serialized to 12,056 bytes instead of 36,713 bytes
  (67.16% less). This is a payload measurement, not a measured price reduction
  or an end-to-end latency guarantee. The updated real turn used exact task
  detail directly because the target was already known.
- Real cumulative CLI counts reproduced a repeated-accounting defect. The
  verified next-turn delta was 156,448 input / 149,248 cached input / 1,066 output;
  cached input is included in input. Regression covers migration without a
  baseline, missing counts, impossible deltas and receipt crash recovery.
- A real CLI write succeeded in its assigned disposable task directory and
  failed in a neighboring task directory. Directory junction checks and existing
  project-path compatibility also have isolated regression coverage.
- A matched uncertain execution with no completion receipt survived two full
  task-pool restarts without invoking the model, changing generation or clearing
  the original record. An uncertain result is still not a completed task.

## Regression and packaging

The full run collected 2,059 cases: 2,055 passed initially, one browser assertion
failed, one setup errored and two Windows file-symlink cases were skipped.
Both exact failing case IDs passed a single isolated rerun. The setup error was
Windows socket resource exhaustion before business logic. The browser failure
was an existing immediate status-text assertion; its original transient text
was not captured, so the precise cause remains unconfirmed. The full attachment
order, bytes, retry key and no-duplicate-task checks passed on that rerun.

Combined latest evidence for those case IDs is 2,057 passed and two skipped.
One additional uncertain-restart case passed after collection. No assertion was
weakened to obtain a pass, and the original failure evidence remains retained.

Windows packaging independently verified 116 frozen module instances against
source, 40 bundled resources and 1,258 installed files. Fresh installation,
same-version replacement, actual 1.2.6-to-1.2.7 upgrade, uninstall and preservation
of configuration/user files/tool preferences passed in isolated directories.
Real hidden WebView tests covered drafts, attachments, live messages, delayed
refresh, offline display and reconnect across two processes. These were hidden
source-runtime tests plus separate frozen-code equality, not a visible frozen
application walkthrough.

## Activation and remaining boundaries

The local source worker was drained, restarted and verified online, with its
task receipts preserved and no production task replay. Existing native windows
and tools were left running; their installed 1.2.6 binaries were not overwritten.
Installer publication must not be described as an in-place native upgrade.

This round did not validate new visual output quality, final video replacement,
Android physical-device behavior or signed iOS distribution. Historical uncertain
work remains preserved for evidence-based recovery. The weekly maintenance
timer is not replaced by another scheduler or duplicate maintenance task.

Public backups contain portable source, tests, required assets and deployment
notes only. Machine-specific launch scripts, credentials, databases, private
logs, user files and generated installers are excluded.
