# Native mobile client contract — API 1

Android 0.2.0 and iOS 0.2.0 render their own installed views. No Hub HTML,
JavaScript, page redirects or WebView history are used for application navigation.
The HTTPS base includes `/hub`; paths below are relative to that base. Requests
send `Accept: application/json`. Errors are JSON `{"detail":"..."}` with real
HTTP status codes, including 401 for an expired session (never a login redirect).

## Authentication

- `GET /api/mobile/session` returns `api_version:1`, `release`,
  `user:{id,username}`, `csrf_token`, `session_key`.
- `POST /api/mobile/login`: JSON `{username,password}`. Same response, plus
  existing Hub session/CSRF cookies. Persist cookies in private native storage;
  never persist the password. Existing WebKit cookies may be migrated once
  without opening a web page.
- `POST /api/mobile/logout`: `X-CSRF-Token`. Clears and invalidates this session.
- Every mutation needs the exact session CSRF in `X-CSRF-Token` (or multipart
  `_csrf`). `session_key` is an irreversible login digest for local cache scoping,
  not an authentication credential. User/session changes fence every callback.

## Read models

- `GET /api/mobile/projects`: `{projects:[{id,slug,name,description}]}`.
- `GET /api/mobile/projects/{slug}/workspace?conversation_id=42`: native read
  model. Omit conversation_id for latest; `0` means a new, unsent conversation.
  `before` (also `before_entry_id`) and `rooms_before` paginate older messages
  and rooms. Never poll an older page into the latest view.
- Workspace keys: `project`, `conversation` (nullable), `conversations`, `agents`,
  `tasks` (project active board), `entries` / `messages` (identical arrays),
  `devices`, `progress`, `default_agent_id`, `older_entries_before`,
  `older_rooms_before`, `revision`, `session_key`, `csrf_token`, `upload_limits`.
- Message: `{id,author,role,text,created_at,task_id,receipt_label,body_truncated,
  attachments:[{id,name,mime_type,byte_size,download_url,preview_url}]}`.
  Role is `user` or `assistant`/`system`; text is untrusted content, never HTML.
- Agent: `{id,name,job,instructions,kind,role_key,active,can_dispatch,device_id,
  connection_state,connection_label,connection_hint}`. Looking at an agent must
  not change the composer recipient; explicit "交给他" does.
- Task: `{id,title,description,status,priority,conversation_id,
  assigned_agent_id,assigned_agent_name,result_summary,board_label,
  execution_label,execution_hint,...}`. Status is the existing Hub enum.
- Progress: `{task_id,agent_name,generation,sequence,status_text,assistant_text,
  updated_at,execution_state,execution_contact_valid,device_contact_recent,
  report_age_label,run_age_label,...}`. Text is actual execution
  telemetry, never a fabricated percentage. A terminal result replaces progress.
- Device: `{device_id,device_label,is_online,is_agent_online,presence_label,
  last_seen_at,last_agent_seen_at}`. Device online is not proof a model replied.
- `GET .../tasks/{id}`: `{task,entries,messages,progress}`. `before` paginates
  entries; response has `older_entries_before` when older entries exist.
- `GET .../tasks?history=1&before=ID`: paginated archived task board.
- `GET .../devices`: `{devices}`.
- `GET .../library?before=ID`: `{attachments,older_before}`, 50 at a time.
- Attachment URLs are same-origin private file requests, **not app navigation**.
  Download to a native temporary file, then use image/QuickLook/system preview
  and Save/Share. Reject redirects, login HTML and a mismatched account callback.
- `GET /api/mobile/toolbox`: `{tools:[{id,title,description,action}]}` with native
  actions `library`, `devices`, `feedback`, `downloads`. No webpage fallback.
- `GET /api/releases`: existing installer metadata for native Downloads page.

## Send and receipt recovery

`POST /api/mobile/projects/{slug}/messages`: multipart `description`, optional
`title`, `conversation_id` (empty/new or positive), repeated `agent_ids`,
`request_key` (8–100 safe UUID characters), repeated `attachments` files.
Text-only requests may use JSON with the same keys (agent_ids is an int array).
One atomic dispatch attaches exact bytes and creates the task/user message.
Response: `{created,request_key,conversation_id,task_ids}`.

`GET .../receipts/{request_key}` returns `{found,conversation_id,task_ids}`.
Keys are scoped to user and project. An identical explicit retry returns the
original result; changing content/room/recipient/files under one key is 409.
Persist the immutable pending send and key before transport. On a timeout or
resume, GET the receipt. Do not auto-repeat a POST or silently generate a new key.
If not found, retain the draft/files and offer an explicit retry using that key.
The receipt, not a local spinner or transport completion, acknowledges delivery.

`POST .../feedback`: JSON `{request_key,kind:"bug"|"idea",title,body,
reproduction?,expected?,conversation_id?,task_id?,agent_id?,platform?}` plus CSRF.
Returns `{feedback:{id,status,title}}`, retry-safe; records actual endpoint and
selected execution-device context. It does not start or replay a production job.

Cache data is account/session/project/room scoped and labelled with save time.
An offline user may navigate native pages and compose drafts without any network
request succeeding. Uncached data shows an honest empty/offline state. Refresh
never overwrites draft text/attachment selection, scroll position or local route.
Cancel stale requests and check generation/account/room again before applying
results. Pause foreground polling in background; compare `revision` before render.
