# Desktop / worker 1.2.7

This update fixes delivery and conversation accounting without changing the
server schema or mobile UI. It pairs with Hub 2.12.0.

- Valid deliverables upload even when another declared file is missing,
  unsupported or rejected. The completion explains each omission; incomplete
  deliveries remain blocked. A failed XML attachment no longer hides the
  Markdown report. Unknown network outcomes retain receipts and do not rerun
  the model. Completion retries reuse the saved publication.
- A binding may set `backend_options.artifact_delivery_root` to its disposable
  delivery base. The trusted worker fixes a project/task subdirectory and saves
  it in the execution receipt. Only that directory is added to CLI write scope
  and artifact upload scope. Source, credentials, state, internal logs and unique
  project assets remain in their normal long-term locations. Existing in-flight
  work keeps its original upload scope; a missing delivery drive is reported
  before execution instead of silently changing destination.
- Default `inspect` returns a bounded scheduling index while preserving the
  current task and conversation requirements. `target_task_id` retrieves exact
  requirements before a revision; `detail: full` preserves full inspection.
  Repeated identity instructions and full descriptions of unrelated tasks are
  no longer sent on every inspection.
- The observed CLI `turn.completed` usage is cumulative for its thread. Project
  and controller resumes now subtract the previous verified cumulative count.
  Missing, regressing or incompatible counters remain unknown. Old sessions
  without a baseline regain measurement after one newly observed counter;
  historical measurements are not silently rewritten. This corrects accounting
  and does not itself represent a reduction in provider charges.

Regression evidence must cover partial delivery, lost upload/completion replies,
exact-once execution recovery, directory junction boundaries, frozen write scope,
multi-turn counters and crash recovery. Real model validation uses a synthetic,
isolated Hub and actual dispatch/revision records. It establishes routing and
continuity, not the visual quality of generated images or completion of a video.

Release gates remain frozen-source agreement, isolated installer/upgrade checks,
hidden desktop regression, an idle-worker check before local activation, verified
primary and mirror installers, and a sanitized source backup. Publishing the
installer does not by itself update a running worker or another device.
