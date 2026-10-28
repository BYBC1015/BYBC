(() => {
  "use strict";
  const shell = document.querySelector("[data-ws-shell]");
  if (!shell) return;
  const form = shell.querySelector("[data-ws-dispatch]");
  const input = form.querySelector("[data-ws-input]");
  const inputEditedBeforeInit = input.value !== input.defaultValue;
  const send = form.querySelector("[data-ws-send]");
  const errors = shell.querySelector("[data-ws-dispatch-error]");
  const scroll = shell.querySelector("[data-ws-scroll]");
  const newMessages = shell.querySelector("[data-ws-new-messages]");
  const sidebar = shell.querySelector("[data-ws-sidebar]");
  const inspector = shell.querySelector("[data-ws-inspector]");
  const backdrop = shell.querySelector("[data-ws-backdrop]");
  const recipientPicker = form.querySelector("[data-ws-recipient-picker]");
  const fileInput = form.querySelector("[data-ws-attachment-input]");
  const attachButton = form.querySelector("[data-ws-attach]");
  const attachmentList = form.querySelector("[data-ws-attachments]");
  const attachmentError = form.querySelector("[data-ws-attachment-error]");
  const uploadProgress = form.querySelector("[data-ws-upload-progress]");
  const pendingNotice = shell.querySelector("[data-ws-pending]");
  const receiptButton = shell.querySelector("[data-ws-receipt-check]");
  const uploadLimits = { count:Number(form.dataset.maxFiles) || 12, file:Number(form.dataset.maxFileBytes) || 52428800, total:Number(form.dataset.maxTotalBytes) || 104857600 };
  const previewTypes = new Set(["image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"]);
  const attachments = [];
  const search = shell.querySelector("[data-ws-room-search]");
  const query = new URLSearchParams(location.search);
  const scope = `${shell.dataset.user || "owner"}.${shell.dataset.project}.${shell.dataset.conversation}`;
  const draftKey = `codexHub.workspace.draft.v1.${scope}`;
  const uiKey = `codexHub.workspace.view.v1.${scope}`;
  const narrow = matchMedia("(max-width:1199px)");
  const small = matchMedia("(max-width:899px)");
  const hasServerError = errors.textContent.trim().length > 0;
  const savedUi = read(uiKey);
  let submitting = false;
  let committed = false;
  let composing = false;
  let drawer = null;
  let drawerOpener = null;
  // Earlier view state also wrote responsive hiding into `closed`. Only the
  // explicit preference is authoritative; retain the other saved view details.
  let userClosed = savedUi.closedByUser === true;
  let inspectorOpen = !narrow.matches && !userClosed;
  let selectedAgent = savedUi.agent || shell.dataset.initialAgent;
  let selectedTask = savedUi.task || inspector.querySelector("[data-ws-task-panel]:not([hidden])")?.dataset.wsTaskPanel || inspector.querySelector("[data-ws-task-panel]")?.dataset.wsTaskPanel;
  let activeTab = savedUi.tab || (inspector.querySelector("[data-ws-task-panels]").hidden ? "agent" : "task");
  let etag = shell.dataset.etag || "";
  let refreshTimer = 0;
  let refreshController = null;
  let saveTimer = 0;
  let attachmentSequence = 0;
  let dragDepth = 0;
  let pendingSubmission = null;
  let receiptChecking = false;
  // Following new work is a reader preference, not a measurement after an
  // unloaded image has just increased the page height.
  let followBottom = shell.dataset.conversation !== "new" && !(Number.isFinite(savedUi.scroll) && !savedUi.bottom);
  let readingPosition = null;
  let controlledScrollTop = null;
  let layoutFrame = 0;
  let readingReady = false;

  function read(key) { try { return JSON.parse(localStorage.getItem(key) || "{}") || {}; } catch { return {}; } }
  function write(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* The page still works without local storage. */ } }
  function remove(key) { try { localStorage.removeItem(key); } catch { /* Optional storage. */ } }
  function recipients() { return Array.from(form.querySelectorAll("[data-ws-recipient]")); }
  function selectedRecipients() { return recipients().filter((item) => item.checked && (!item.disabled || item.dataset.uploadLocked === "true")); }
  function saveDraft() {
    if (committed) return;
    // Files stay in this page's memory. Persist only attachment counts and a
    // pending request's name/size/type metadata; never bytes or local file paths.
    write(draftKey, { description: input.value, agent_ids: selectedRecipients().map((item) => item.value), request_key: form.elements.request_key.value, attachment_count:pendingSubmission ? pendingSubmission.files.length : attachments.length, pending_submission:pendingSubmission });
  }
  function renewRequestKey() {
    if (crypto.randomUUID) form.elements.request_key.value = crypto.randomUUID();
    else {
      const bytes = crypto.getRandomValues(new Uint8Array(16));
      bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
      const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
      form.elements.request_key.value = `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
    }
  }
  function contentChanged() {
    if (pendingSubmission || submitting) return;
    renewRequestKey(); renderRecipients(); saveDraft();
    errors.hidden = true;
    shell.querySelector("[data-ws-draft-status]").textContent = attachments.length ? `${attachments.length} 个附件 · 仅保留在当前页面` : "文字草稿已保存在此浏览器";
  }
  function fileSize(bytes) {
    if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} MB`;
    if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${bytes} B`;
  }
  function renderAttachments() {
    const nodes = attachments.map((item) => {
      const card = document.createElement("div");
      card.className = "ws-attachment"; card.dataset.wsAttachment = item.id; card.setAttribute("role", "listitem");
      const preview = document.createElement("span"); preview.className = "ws-attachment-preview";
      if (item.url) {
        const image = document.createElement("img"); image.src = item.url; image.alt = "";
        image.addEventListener("error", () => { preview.replaceChildren(); preview.textContent = "图片"; });
        preview.append(image);
      } else preview.textContent = item.file.name.includes(".") ? item.file.name.split(".").at(-1).slice(0,7).toUpperCase() : "文件";
      const info = document.createElement("span"); info.className = "ws-attachment-info";
      const name = document.createElement("strong"); name.textContent = item.file.name; name.title = item.file.name;
      const size = document.createElement("small"); size.textContent = fileSize(item.file.size); info.append(name, size);
      const removeButton = document.createElement("button"); removeButton.type = "button"; removeButton.dataset.wsAttachmentRemove = item.id;
      removeButton.textContent = "×"; removeButton.setAttribute("aria-label", `移除附件 ${item.file.name}`); removeButton.disabled = submitting || Boolean(pendingSubmission);
      card.append(preview, info, removeButton); return card;
    });
    attachmentList.replaceChildren(...nodes); attachmentList.hidden = !attachments.length;
    renderRecipients();
  }
  function addFiles(files) {
    if (submitting) return;
    const rejected = [];
    let added = false;
    let total = attachments.reduce((sum, item) => sum + item.file.size, 0);
    Array.from(files).forEach((file) => {
      if (!(file instanceof File)) return;
      let pendingIndex;
      if (pendingSubmission) {
        pendingIndex = pendingSubmission.files.findIndex((original, index) => original.name === file.name && original.size === file.size && original.type === file.type && !attachments.some((item) => item.pendingIndex === index));
        if (pendingIndex < 0) { rejected.push(`“${file.name}”与原任务附件不匹配，请重新选择原文件`); return; }
      }
      if (attachments.length >= uploadLimits.count) { rejected.push(`每次最多添加 ${uploadLimits.count} 个文件`); return; }
      if (file.size > uploadLimits.file) { rejected.push(`“${file.name}”超过单文件 ${fileSize(uploadLimits.file)} 的限制`); return; }
      if (total + file.size > uploadLimits.total) { rejected.push(`“${file.name}”未添加：附件总量不能超过 ${fileSize(uploadLimits.total)}`); return; }
      attachments.push({ id:String(++attachmentSequence), file, pendingIndex, url:previewTypes.has(file.type) ? URL.createObjectURL(file) : null });
      total += file.size; added = true;
    });
    attachmentError.textContent = Array.from(new Set(rejected)).slice(0,3).join("；");
    attachmentError.hidden = !rejected.length;
    if (added) {
      if (pendingSubmission) {
        attachments.sort((a,b) => a.pendingIndex - b.pendingIndex);
        renderAttachments(); syncSubmissionControls(); saveDraft();
      } else { renderAttachments(); contentChanged(); }
    }
  }
  function clearAttachments() {
    attachments.forEach((item) => { if (item.url) URL.revokeObjectURL(item.url); });
    attachments.length = 0; fileInput.value = ""; renderAttachments();
  }
  function setSubmitting(value) {
    submitting = value;
    syncSubmissionControls();
  }
  function syncSubmissionControls() {
    const locked = submitting || Boolean(pendingSubmission);
    const needsOriginalFiles = pendingSubmission && attachments.length < pendingSubmission.files.length;
    input.readOnly = locked;
    attachButton.disabled = submitting || (Boolean(pendingSubmission) && !needsOriginalFiles);
    fileInput.disabled = attachButton.disabled;
    attachButton.setAttribute("aria-label", needsOriginalFiles ? "重新选择原附件" : "添加图片或文件");
    attachButton.title = needsOriginalFiles ? "重新选择原任务的附件，文件名、大小和类型需一致" : "添加图片或文件，也可拖入或粘贴图片";
    form.setAttribute("aria-busy", String(submitting));
    recipients().forEach((item) => {
      if (locked && !item.disabled) { item.dataset.uploadLocked = "true"; item.disabled = true; }
      else if (!locked && item.dataset.uploadLocked === "true") { item.disabled = false; delete item.dataset.uploadLocked; }
    });
    attachmentList.querySelectorAll("button").forEach((button) => { button.disabled = locked; });
    pendingNotice.hidden = !pendingSubmission || submitting;
    receiptButton.disabled = submitting || receiptChecking;
    if (pendingSubmission) {
      const missing = pendingSubmission.files.length - attachments.length;
      shell.querySelector("[data-ws-draft-status]").textContent = missing ? `请重新选择 ${missing} 个原附件，再重试原任务` : "可检查发送结果，或原样重试任务";
    }
    send.setAttribute("aria-label", pendingSubmission ? "重试原任务" : "安排新任务");
    renderRecipients();
  }
  function completeSubmission(url) {
    remove(draftKey); committed = true; pendingSubmission = null;
    input.value = ""; clearAttachments();
    location.assign(url);
  }
  function updateCsrf(freshShell) {
    if (!freshShell || freshShell.dataset.user !== shell.dataset.user || freshShell.dataset.project !== shell.dataset.project) return false;
    const token = freshShell.querySelector('[data-ws-dispatch] [name="_csrf"]')?.value;
    if (!token) return false;
    form.elements._csrf.value = token;
    return true;
  }
  async function refreshCsrf() {
    // A login in another tab rotates the session token without changing the
    // conversation ETag. Fetch without its conditional header for this retry.
    const response = await fetch(shell.dataset.workspaceUrl || location.href, { credentials:"same-origin", cache:"no-store", headers:{ "Accept":"text/html" } });
    const page = new DOMParser().parseFromString(await response.text(), "text/html");
    if (!response.ok || !updateCsrf(page.querySelector("[data-ws-shell]"))) throw new Error("请在另一页面登录原账号后重试，当前文字和附件仍保留。");
  }
  async function checkReceipt() {
    if (!pendingSubmission || receiptChecking) return null;
    const key = pendingSubmission.key;
    receiptChecking = true; syncSubmissionControls();
    try {
      const response = await fetch(`${form.action}/${encodeURIComponent(key)}`, { credentials:"same-origin", cache:"no-store", headers:{ "Accept":"application/json" } });
      if (!response.ok) throw new Error();
      const receipt = await response.json();
      if (!pendingSubmission || pendingSubmission.key !== key) return null;
      if (receipt.found && receipt.conversation_url) {
        const destination = new URL(receipt.conversation_url, location.href);
        if (destination.origin !== location.origin || !destination.pathname.startsWith(new URL(shell.dataset.projectUrl, location.href).pathname + "/conversations/")) throw new Error();
        completeSubmission(destination.href); return true;
      }
      if (receipt.found !== false) throw new Error();
      pendingNotice.querySelector("[data-ws-pending-message]").textContent = "暂未查到完成记录。原任务可能仍在处理，可稍后检查，或使用原内容重试；不会重复安排。";
      return false;
    } catch {
      if (pendingSubmission?.key === key) pendingNotice.querySelector("[data-ws-pending-message]").textContent = "暂时无法确认发送结果。文字、收件人和请求编号已保留，可恢复连接后检查，或原样重试。";
      return null;
    } finally { receiptChecking = false; if (!committed) syncSubmissionControls(); }
  }
  function showUploadProgress(percent, label) {
    uploadProgress.hidden = false;
    const bar = uploadProgress.querySelector("progress");
    if (percent === null) bar.removeAttribute("value"); else bar.value = percent;
    uploadProgress.querySelector("[data-ws-upload-label]").textContent = label;
    uploadProgress.querySelector("[data-ws-upload-percent]").textContent = percent === null ? "" : `${percent}%`;
  }
  function postWork(data) {
    return new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open("POST", form.action); request.withCredentials = true; request.timeout = 600000;
      request.setRequestHeader("Accept", "text/html");
      request.upload.addEventListener("progress", (event) => {
        if (!attachments.length) return;
        const percent = event.lengthComputable ? Math.min(100, Math.round(event.loaded / event.total * 100)) : null;
        showUploadProgress(percent, percent === 100 ? "上传完成，正在安排工作…" : `正在上传 ${attachments.length} 个附件`);
      });
      request.addEventListener("load", () => resolve({ status:request.status, ok:request.status >= 200 && request.status < 300, html:request.responseText, url:request.responseURL, redirected:request.responseURL !== new URL(form.action, location.href).href }));
      request.addEventListener("error", () => reject(new Error("连接中断，文字和附件仍保留在当前页面，请重试。")));
      request.addEventListener("timeout", () => reject(new Error("上传等待超时，文字和附件仍保留在当前页面，请重试。")));
      request.addEventListener("abort", () => reject(new Error("上传已中断，文字和附件仍保留在当前页面。")));
      request.send(data);
    });
  }
  function saveUi() {
    write(uiKey, { tab: activeTab, task: selectedTask, agent: selectedAgent, closed: userClosed, closedByUser: userClosed,
      scroll: scroll.scrollTop, bottom: followBottom });
  }
  function captureReadingPosition() {
    const bounds = scroll.getBoundingClientRect();
    const entry = Array.from(scroll.querySelectorAll("[data-ws-entry]")).find((node) => node.getBoundingClientRect().bottom > bounds.top + 1);
    return { id:entry?.id, offset:entry ? entry.getBoundingClientRect().top - bounds.top : 0, top:scroll.scrollTop };
  }
  function restoreReadingPosition(position = readingPosition) {
    if (!readingReady) return;
    const previous = scroll.scrollTop;
    if (followBottom) scroll.scrollTop = scroll.scrollHeight;
    else if (position) {
      const anchor = position.id && document.getElementById(position.id);
      scroll.scrollTop = anchor && scroll.contains(anchor)
        ? scroll.scrollTop + anchor.getBoundingClientRect().top - scroll.getBoundingClientRect().top - position.offset
        : position.top;
    }
    if (Math.abs(scroll.scrollTop - previous) > .5) controlledScrollTop = scroll.scrollTop;
    readingPosition = captureReadingPosition();
  }
  function messageLayoutChanged() {
    if (!readingReady || layoutFrame) return;
    layoutFrame = requestAnimationFrame(() => { layoutFrame = 0; restoreReadingPosition(); });
  }
  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(230, Math.max(70, input.scrollHeight)) + "px";
  }
  function renderRecipients() {
    const checked = selectedRecipients();
    form.querySelector("[data-ws-recipient-label]").textContent = checked.length === 1 ? checked[0].dataset.agentName : checked.length ? `${checked.length} 位执行者` : "选择执行者";
    send.disabled = submitting || receiptChecking || !checked.length || (!input.value.trim() && !attachments.length) || Boolean(pendingSubmission && attachments.length !== pendingSubmission.files.length);
    send.setAttribute("aria-busy", String(submitting));
    if (!checked.length) shell.querySelector("[data-ws-draft-status]").textContent = "选择一位已绑定连接的执行者";
    const note = form.querySelector("[data-ws-recipient-status]");
    const offline = checked.filter((item) => item.dataset.connectionState === "offline");
    const unverified = checked.filter((item) => item.dataset.connectionState === "unverified");
    const unknown = checked.some((item) => item.dataset.connectionState === "unknown");
    const message = unknown ? "连接状态待确认，当前文字和附件已保留。" : offline.length ? `${offline.map((item) => item.dataset.agentName).join('、')}的连接离线，工作会先排队。` : unverified.length ? "已绑定连接，自动执行能力仍待核验；提交后可在任务里查看进展。" : "";
    if (note.textContent !== message) note.textContent = message;
    note.hidden = !message;
  }
  function updateFeedbackLinks() {
    const task = activeTab === "task" && selectedTask ? inspector.querySelector(`[data-ws-task-panel="${CSS.escape(selectedTask)}"]`) : null;
    const agentId = task ? task.dataset.wsOwner : activeTab === "agent" ? selectedAgent : "";
    shell.querySelectorAll("[data-ws-feedback]").forEach((link) => {
      const url = new URL(link.href, location.href);
      ["conversation_id", "task_id", "agent_id"].forEach((key) => url.searchParams.delete(key));
      if (shell.dataset.conversation !== "new") url.searchParams.set("conversation_id", shell.dataset.conversation);
      if (task) url.searchParams.set("task_id", selectedTask);
      if (agentId) url.searchParams.set("agent_id", agentId);
      url.searchParams.set("page_url", location.pathname + location.search);
      link.href = url.href;
    });
  }
  function formatTimes() {
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    shell.querySelectorAll("[data-ws-time]").forEach((node) => {
      const date = new Date(node.dataset.wsTime);
      if (!Number.isFinite(date.getTime())) return;
      const day = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
      const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate()-1).getTime();
      const clock = date.toLocaleTimeString("zh-CN", { hour:"2-digit", minute:"2-digit", hour12:false });
      const label = day === today ? "今天" : day === yesterday ? "昨天" : date.toLocaleDateString("zh-CN", { ...(date.getFullYear() !== now.getFullYear() ? { year:"numeric" } : {}), month:"short", day:"numeric" });
      node.textContent = node.dataset.wsTimeMode === "date" ? label : `${label} ${clock}`;
      node.title = date.toLocaleString("zh-CN", { year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit", hour12:false, timeZoneName:"short" });
    });
  }
  function restoreDraft() {
    if (hasServerError) return;
    const saved = read(draftKey);
    if (saved.pending_submission && typeof saved.pending_submission.key === "string" && Array.isArray(saved.pending_submission.files)) pendingSubmission = saved.pending_submission;
    if (typeof saved.description === "string" && (!inputEditedBeforeInit || pendingSubmission)) input.value = saved.description;
    if (Array.isArray(saved.agent_ids) && (!query.has("assign_agent") || pendingSubmission)) {
      const ids = new Set(saved.agent_ids.map(String));
      recipients().forEach((item) => { item.checked = !item.disabled && ids.has(item.value); });
    }
    if (saved.request_key && typeof saved.request_key === "string") form.elements.request_key.value = saved.request_key;
    if (pendingSubmission) form.elements.request_key.value = pendingSubmission.key;
    if (input.value.trim()) shell.querySelector("[data-ws-draft-status]").textContent = "已恢复这场对话的草稿";
    if (saved.attachment_count && !pendingSubmission) {
      renewRequestKey();
      attachmentError.textContent = "文字草稿已恢复，上次选择的附件需要重新添加。";
      attachmentError.hidden = false;
    }
  }
  function filterRooms() {
    const term = search.value.trim().toLocaleLowerCase();
    let found = 0;
    shell.querySelectorAll("[data-ws-room]").forEach((room) => {
      room.hidden = !room.dataset.roomSearch.toLocaleLowerCase().includes(term);
      if (!room.hidden) found++;
    });
    shell.querySelector("[data-ws-no-rooms]").hidden = !term || found > 0;
  }
  function selectPanel(kind, id) {
    if (kind === "agent") selectedAgent = String(id || selectedAgent || "");
    else selectedTask = String(id || selectedTask || "");
    activeTab = kind;
    const panels = inspector.querySelectorAll(`[data-ws-${kind}-panel]`);
    const selectedId = kind === "agent" ? selectedAgent : selectedTask;
    let matched = false;
    panels.forEach((panel) => { panel.hidden = panel.dataset[kind === "agent" ? "wsAgentPanel" : "wsTaskPanel"] !== selectedId; if (!panel.hidden) matched = true; });
    if (!matched && panels.length) {
      panels[0].hidden = false;
      if (kind === "agent") selectedAgent = panels[0].dataset.wsAgentPanel;
      else selectedTask = panels[0].dataset.wsTaskPanel;
    }
    inspector.querySelector("[data-ws-task-panels]").hidden = kind !== "task";
    inspector.querySelector("[data-ws-agent-panels]").hidden = kind !== "agent";
    inspector.querySelectorAll("[data-ws-inspector-tab]").forEach((tab) => {
      const chosen = tab.dataset.wsInspectorTab === kind;
      tab.setAttribute("aria-selected", String(chosen));
      tab.tabIndex = chosen ? 0 : -1;
    });
    shell.querySelectorAll("[data-ws-agent]").forEach((button) => button.setAttribute("aria-pressed", String(kind === "agent" && button.dataset.wsAgent === selectedAgent)));
    updateFeedbackLinks();
    saveUi();
  }
  function syncDrawers() {
    const rightVisible = inspectorOpen && (!narrow.matches || drawer === "inspector");
    const leftVisible = !small.matches || drawer === "sidebar";
    shell.classList.toggle("is-inspector-hidden", !rightVisible && !narrow.matches);
    shell.classList.toggle("is-inspector-open", narrow.matches && drawer === "inspector");
    shell.classList.toggle("is-sidebar-open", small.matches && drawer === "sidebar");
    inspector.inert = !rightVisible;
    inspector.setAttribute("aria-hidden", String(!rightVisible));
    sidebar.inert = !leftVisible;
    sidebar.setAttribute("aria-hidden", String(!leftVisible));
    shell.querySelectorAll("[data-ws-inspector-open]").forEach((button) => button.setAttribute("aria-expanded", String(rightVisible)));
    shell.querySelectorAll("[data-ws-sidebar-open]").forEach((button) => button.setAttribute("aria-expanded", String(leftVisible)));
    const modal = drawer && ((drawer === "inspector" && narrow.matches) || (drawer === "sidebar" && small.matches));
    backdrop.hidden = !modal;
    shell.querySelector(".ws-center").inert = Boolean(modal);
    if (modal) {
      const surface = drawer === "inspector" ? inspector : sidebar;
      surface.setAttribute("role", "dialog"); surface.setAttribute("aria-modal", "true");
      if (surface === inspector) sidebar.inert = true;
      else inspector.inert = true;
    } else {
      [sidebar, inspector].forEach((surface) => { surface.removeAttribute("role"); surface.removeAttribute("aria-modal"); });
    }
  }
  function openInspector(opener) {
    if (drawer) closeDrawer(false, false);
    userClosed = false;
    inspectorOpen = true;
    if (narrow.matches) { drawer = "inspector"; drawerOpener = opener || document.activeElement; }
    syncDrawers(); saveUi();
    if (narrow.matches) inspector.querySelector("[data-ws-inspector-close]").focus({ preventScroll: true });
  }
  function closeDrawer(restore = true, persistClose = true) {
    const was = drawer;
    drawer = null;
    if (was === "inspector") {
      inspectorOpen = false;
      if (persistClose) userClosed = true;
    }
    syncDrawers(); saveUi();
    if (restore && drawerOpener?.isConnected) drawerOpener.focus({ preventScroll: true });
    drawerOpener = null;
  }

  // Old project bookmarks retain their original archive destination.
  const oldAnchors = new Set(["members","files","tasks","project-memory","memory","control","codex-projects","codex-workspaces"]);
  if (shell.dataset.conversation === "new" && oldAnchors.has(location.hash.slice(1))) {
    location.replace(shell.dataset.projectUrl + "?view=archive" + location.hash);
    return;
  }
  restoreDraft(); autosize(); syncSubmissionControls(); formatTimes();
  if (inputEditedBeforeInit && !pendingSubmission) contentChanged();
  if (pendingSubmission) void checkReceipt();
  if (query.has("agent") || query.has("assign_agent")) { selectedAgent = shell.dataset.initialAgent; activeTab = "agent"; }
  selectPanel(activeTab, activeTab === "agent" ? selectedAgent : selectedTask);
  syncDrawers();
  readingReady = true;
  restoreReadingPosition({ top:Number.isFinite(savedUi.scroll) ? savedUi.scroll : 0 });
  // The feed keeps its DOM node across refreshes. Observe both its changing
  // content height and the viewport left by the composer; delayed image loads
  // follow the same bottom/reading-anchor rule as refreshed messages.
  const messageResizeObserver = new ResizeObserver(messageLayoutChanged);
  messageResizeObserver.observe(scroll.querySelector("[data-ws-live]"));
  messageResizeObserver.observe(scroll);
  scroll.addEventListener("load", (event) => { if (event.target.tagName === "IMG") messageLayoutChanged(); }, true);
  scroll.addEventListener("error", (event) => { if (event.target.tagName === "IMG") messageLayoutChanged(); }, true);
  if (location.hash === "#agents") document.querySelector("[data-agents-open]")?.click();

  shell.addEventListener("click", (event) => {
    const target = event.target;
    const taskView=target.closest('[data-ws-task-view]');
    if(taskView){
      event.preventDefault();
      shell.dataset.workspaceUrl=taskView.href;
      history.replaceState(null,'',taskView.href);
      etag='';refreshController?.abort();
      const list=shell.querySelector('[data-ws-task-list]');if(list)list.open=true;
      input.focus({preventScroll:true});
      clearTimeout(refreshTimer);refreshTimer=setTimeout(refresh,50);
      return;
    }
    const agent = target.closest("[data-ws-agent]");
    if (agent) {
      if (!inspector.querySelector(`[data-ws-agent-panel="${CSS.escape(agent.dataset.wsAgent)}"]`)) {
        saveDraft(); saveUi(); location.assign(shell.dataset.projectUrl + "/agents/" + encodeURIComponent(agent.dataset.wsAgent)); return;
      }
      selectPanel("agent", agent.dataset.wsAgent); openInspector(agent);
    }
    const task = target.closest("[data-ws-task]");
    if (task) {
      if (!inspector.querySelector(`[data-ws-task-panel="${CSS.escape(task.dataset.wsTask)}"]`)) {
        saveDraft(); saveUi(); location.assign(shell.dataset.projectUrl + "/tasks/" + encodeURIComponent(task.dataset.wsTask)); return;
      }
      selectPanel("task", task.dataset.wsTask); openInspector(task);
    }
    const tab = target.closest("[data-ws-inspector-tab]");
    if (tab) selectPanel(tab.dataset.wsInspectorTab);
    const open = target.closest("[data-ws-inspector-open]");
    if (open) openInspector(open);
    if (target.closest("[data-ws-inspector-close]")) {
      if (drawer) closeDrawer(); else { userClosed = true; inspectorOpen = false; syncDrawers(); saveUi(); shell.querySelector("[data-ws-inspector-open]").focus({ preventScroll:true }); }
    }
    const navigation = target.closest("[data-ws-sidebar-open]");
    if (navigation) { closeDrawer(false, false); drawer = "sidebar"; drawerOpener = navigation; syncDrawers(); sidebar.querySelector("[data-ws-sidebar-close]").focus({ preventScroll:true }); }
    if (target.closest("[data-ws-sidebar-close],[data-ws-backdrop]")) closeDrawer();
    const suggestion = target.closest("[data-ws-suggestion]");
    if (suggestion && !submitting && !pendingSubmission) { input.value = suggestion.dataset.wsSuggestion; autosize(); contentChanged(); input.focus(); }
    if (target.closest("[data-ws-create-agent]")) {
      if (drawer) closeDrawer(false, false);
      queueMicrotask(() => document.querySelector("[data-agent-workspace] [data-agent-create-open]")?.click());
    }
    if (target.closest("a")) { saveDraft(); saveUi(); }
  });
  document.addEventListener("codexhub:overlay-open", (event) => {
    if (event.detail?.name === "agents") {
      if (drawer) closeDrawer(false, false);
      recipientPicker.open = false;
    }
  });
  shell.querySelector("[data-ws-project-switch]").addEventListener("change", (event) => { saveDraft(); saveUi(); location.assign(event.target.value); });
  search.addEventListener("input", filterRooms);
  form.addEventListener("change", (event) => { if (!submitting && event.target.matches("[data-ws-recipient]")) contentChanged(); });
  input.addEventListener("input", () => { autosize(); contentChanged(); });
  attachButton.addEventListener("click", () => { if (!submitting) fileInput.click(); });
  receiptButton.addEventListener("click", () => { if (!submitting) void checkReceipt(); });
  fileInput.addEventListener("change", () => { addFiles(fileInput.files); fileInput.value = ""; });
  // Native controls can receive input while this deferred script is loading.
  // Adopt that FileList before clearing it; those change events cannot replay.
  if (fileInput.files.length) { addFiles(fileInput.files); fileInput.value = ""; }
  attachmentList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-ws-attachment-remove]");
    if (!button || submitting || pendingSubmission) return;
    const index = attachments.findIndex((item) => item.id === button.dataset.wsAttachmentRemove);
    if (index < 0) return;
    if (attachments[index].url) URL.revokeObjectURL(attachments[index].url);
    attachments.splice(index, 1); attachmentError.hidden = true;
    renderAttachments(); contentChanged(); attachButton.focus({ preventScroll:true });
  });
  function carriesFiles(event) { return Array.from(event.dataTransfer?.types || []).includes("Files"); }
  form.addEventListener("dragenter", (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault(); dragDepth++;
    if (!submitting) form.classList.add("is-file-dragging");
  });
  form.addEventListener("dragover", (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault(); event.dataTransfer.dropEffect = submitting ? "none" : "copy";
  });
  form.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth-1); if (!dragDepth) form.classList.remove("is-file-dragging"); });
  form.addEventListener("drop", (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault(); dragDepth = 0; form.classList.remove("is-file-dragging");
    addFiles(event.dataTransfer.files);
  });
  form.addEventListener("paste", (event) => {
    const files = Array.from(event.clipboardData?.items || []).filter((item) => item.kind === "file" && item.type.startsWith("image/")).map((item) => item.getAsFile()).filter(Boolean);
    if (files.length) { event.preventDefault(); addFiles(files); }
  });
  input.addEventListener("compositionstart", () => { composing = true; });
  input.addEventListener("compositionend", () => { composing = false; });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing && !composing && event.keyCode !== 229) {
      event.preventDefault(); if (!send.disabled) form.requestSubmit();
    }
  });
  document.addEventListener("keydown", (event) => {
    const activeTabButton = event.target.closest?.("[data-ws-inspector-tab]");
    if (activeTabButton && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const tabs = Array.from(inspector.querySelectorAll("[data-ws-inspector-tab]"));
      const index = tabs.indexOf(activeTabButton);
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length-1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      selectPanel(tabs[next].dataset.wsInspectorTab); tabs[next].focus(); return;
    }
    if (event.key === "Escape") {
      if (document.querySelector("dialog[open]")) return;
      if (recipientPicker.open) { recipientPicker.open = false; recipientPicker.querySelector("summary").focus(); }
      else if (drawer) closeDrawer();
    }
    if (event.key === "Tab" && drawer && !document.querySelector("dialog[open]")) {
      const surface = drawer === "inspector" ? inspector : sidebar;
      const focusables = Array.from(surface.querySelectorAll('a[href],button:not(:disabled),input:not(:disabled),select:not(:disabled),summary,[tabindex="0"]')).filter((node) => node.getClientRects().length && !node.closest("[hidden]"));
      if (!focusables.length) { event.preventDefault(); surface.focus(); return; }
      if (event.shiftKey && document.activeElement === focusables[0]) { event.preventDefault(); focusables.at(-1).focus(); }
      else if (!event.shiftKey && document.activeElement === focusables.at(-1)) { event.preventDefault(); focusables[0].focus(); }
    }
    if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey && !event.target.matches("input,textarea,select,[contenteditable]") && !document.querySelector("dialog[open]")) {
      if (small.matches) return;
      event.preventDefault(); search.focus();
    }
  });
  document.addEventListener("click", (event) => { if (recipientPicker.open && !recipientPicker.contains(event.target)) recipientPicker.open = false; });
  scroll.addEventListener("scroll", () => {
    const controlled = controlledScrollTop !== null && Math.abs(scroll.scrollTop - controlledScrollTop) < 2;
    controlledScrollTop = null;
    if (!controlled) {
      const movedUp = readingPosition && scroll.scrollTop < readingPosition.top - 2;
      followBottom = shell.dataset.conversation !== "new" && !movedUp && scroll.scrollHeight-scroll.clientHeight-scroll.scrollTop < 16;
    }
    readingPosition = captureReadingPosition();
    if (followBottom && newMessages) newMessages.hidden = true;
    clearTimeout(saveTimer); saveTimer = setTimeout(saveUi, 180);
  }, { passive:true });
  newMessages?.addEventListener("click", () => { followBottom = true; restoreReadingPosition(); newMessages.hidden = true; });
  window.addEventListener("pagehide", () => { saveDraft(); saveUi(); refreshController?.abort(); });
  function viewportChanged() {
    drawer = null; drawerOpener = null;
    inspectorOpen = !narrow.matches && !userClosed;
    syncDrawers();
  }
  narrow.addEventListener("change", viewportChanged); small.addEventListener("change", viewportChanged);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting || receiptChecking || !form.reportValidity()) return;
    if (!selectedRecipients().length) { errors.textContent = "请选择一位可分配工作的执行者。"; errors.hidden = false; recipientPicker.open = true; return; }
    if (!input.value.trim() && !attachments.length) { errors.textContent = "写下工作目标，或添加图片和文件。"; errors.hidden = false; input.focus(); return; }
    if (pendingSubmission && attachments.length !== pendingSubmission.files.length) { attachmentError.textContent = "请重新选择原任务的全部附件，再重试。"; attachmentError.hidden = false; return; }
    if (pendingSubmission) {
      setSubmitting(true);
      try { await refreshCsrf(); }
      catch (error) { errors.textContent = error.message; errors.hidden = false; setSubmitting(false); return; }
      setSubmitting(false);
    }
    const data = new FormData(form);
    // Temporarily locked recipients are deliberately disabled in the UI, but
    // remain part of an identical retry's payload.
    data.delete("agent_ids"); selectedRecipients().forEach((item) => data.append("agent_ids", item.value));
    data.delete("attachments");
    attachments.forEach((item) => data.append("attachments", item.file, item.file.name));
    if (!pendingSubmission) {
      pendingSubmission = { key:form.elements.request_key.value, files:attachments.map((item, index) => {
        item.pendingIndex = index;
        return { name:item.file.name, size:item.file.size, type:item.file.type };
      }) };
    }
    saveDraft(); setSubmitting(true); errors.hidden = true; recipientPicker.open = false;
    shell.querySelector("[data-ws-draft-status]").textContent = attachments.length ? "附件正在上传，请稍候…" : "正在安排工作…";
    if (attachments.length) showUploadProgress(0, `正在上传 ${attachments.length} 个附件`);
    try {
      const response = await postWork(data);
      const page = new DOMParser().parseFromString(response.html, "text/html");
      const failure = page.querySelector("[data-ws-dispatch-error]")?.textContent.trim();
      if (!response.ok || failure || !page.querySelector("[data-ws-shell]")) {
        const error = new Error(failure || (response.status === 401 || page.querySelector('form[action$="/login"]') ? "登录状态已过期。文字和附件仍保留在当前页面，请在另一页面重新登录后重试。" : response.status === 413 ? "附件超过上传限制，请移除较大的文件后重试。文字和附件仍保留。" : "暂时未能安排工作，文字和附件仍保留在当前页面，请稍后重试。"));
        error.definite = (response.status >= 400 && response.status < 500) || (response.status === 200 && Boolean(failure));
        error.conflict = response.status === 409 || Boolean(failure?.includes("同一请求标识不能"));
        error.refreshSession = response.status === 401 || response.status === 403;
        error.retryKey = page.querySelector('[data-ws-dispatch] [name="request_key"]')?.value;
        throw error;
      }
      if (!response.redirected) throw new Error("未确认任务已安排，草稿已保留，请重试。重复提交不会重复创建任务。");
      completeSubmission(response.url);
    } catch (error) {
      errors.textContent = error.message || "连接中断，文字和附件仍保留在当前页面。";
      errors.hidden = false;
      uploadProgress.hidden = true;
      if (error.refreshSession) {
        try { await refreshCsrf(); errors.textContent = "登录凭据已更新，文字和附件已保留，请重试。"; }
        catch { /* The original session message remains visible until login. */ }
      }
      // A response may be lost after the server committed. Never rotate its key
      // or unlock edits until checking the existing dispatch receipt.
      setSubmitting(false);
      if (error.definite && !error.conflict && !pendingSubmission.uncertain) {
        pendingSubmission = null;
        if (error.retryKey) form.elements.request_key.value = error.retryKey;
        saveDraft(); syncSubmissionControls();
      } else {
        pendingSubmission.uncertain = true; saveDraft();
        if (error.conflict) {
          errors.textContent = "重选附件的内容与原请求不一致。正在检查原任务；不会另行派单。";
          // A matching filename and size cannot prove identical bytes. If the
          // server rejects its hash, allow the original files to be reselected.
          clearAttachments();
        }
        if (await checkReceipt()) return;
      }
      if (!pendingSubmission) shell.querySelector("[data-ws-draft-status]").textContent = attachments.length ? "文字与附件已保留，可修改后重试" : "草稿已保留";
      input.focus({ preventScroll:true });
    }
  });

  let organizingTask=false;
  shell.addEventListener('submit',async(event)=>{
    const actionForm=event.target.closest('[data-ws-task-action]');if(!actionForm)return;
    event.preventDefault();if(organizingTask)return;
    const action=event.submitter?.value;if(!action)return;
    organizingTask=true;
    const notice=shell.querySelector('[data-ws-task-feedback]');
    const buttons=Array.from(actionForm.querySelectorAll('button'));
    const data=new FormData(actionForm);data.set('action',action);
    data.set('_csrf',form.querySelector('[name="_csrf"]').value);
    buttons.forEach(button=>button.disabled=true);
    try{
      const response=await fetch(actionForm.getAttribute('action'),{method:'POST',body:data,credentials:'same-origin',headers:{Accept:'application/json'}});
      if(response.redirected)throw new Error('登录已过期，请重新登录后操作。当前草稿和附件仍保留。');
      const result=await response.json().catch(()=>{throw new Error('连接未完成，记录已保留，请稍后重试。');});
      if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'未能更新，请稍后重试');
      notice.textContent=action==='restore'?'已恢复到待办。':action==='complete'?'已完成并归档，聊天与成果已保留。':'已归档，可在历史任务中恢复。';
      etag='';refreshController?.abort();input.focus({preventScroll:true});
      clearTimeout(refreshTimer);refreshTimer=setTimeout(refresh,50);
    }catch(error){notice.textContent=error.message||'连接未完成，记录已保留，请稍后重试。';}
    finally{notice.hidden=false;buttons.forEach(button=>button.disabled=false);organizingTask=false;}
  });

  function refreshPresence(freshShell) {
    // Patch status only: never replace focused avatars, selected recipients,
    // open identity forms or unsent attachments during a heartbeat refresh.
    function patch(current, incoming, selector) {
      const destination = current?.querySelector(selector);
      const source = incoming?.querySelector(selector);
      if (!destination || !source) return;
      destination.className = source.className;
      if (destination.textContent !== source.textContent) {
        destination.replaceChildren(...Array.from(source.childNodes, node => document.importNode(node, true)));
      }
      // Status dots may change class without changing text.
      const dot = destination.querySelector('i');
      if (dot && source.querySelector('i')) dot.className = source.querySelector('i').className;
    }
    shell.querySelectorAll('[data-ws-agent-dock] [data-ws-agent]').forEach(button => {
      const incoming = freshShell.querySelector(`[data-ws-agent-dock] [data-ws-agent="${CSS.escape(button.dataset.wsAgent)}"]`);
      patch(button, incoming, '.ws-agent-presence');
      patch(button, incoming, '.ws-agent-state');
    });
    recipients().forEach(choice => {
      const incoming = freshShell.querySelector(`[data-ws-recipient][value="${CSS.escape(choice.value)}"]`);
      if (!incoming) return;
      choice.dataset.connectionState = incoming.dataset.connectionState;
      patch(choice.closest('label'), incoming.closest('label'), 'small');
    });
    inspector.querySelectorAll('[data-ws-agent-panel]').forEach(panel => {
      const incoming = freshShell.querySelector(`[data-ws-agent-panel="${CSS.escape(panel.dataset.wsAgentPanel)}"]`);
      patch(panel, incoming, '.ws-identity-status');
      patch(panel, incoming, '.ws-connection-hint');
    });
    document.querySelectorAll('[data-agent-card]').forEach(card => {
      const incoming = freshShell.ownerDocument.querySelector(`[data-agent-card="${CSS.escape(card.dataset.agentCard)}"]`);
      patch(card, incoming, '.agent-presence');
    });
    renderRecipients();
  }
  function presenceUnconfirmed() {
    shell.querySelectorAll('[data-ws-agent-dock] .ws-agent-state').forEach(label => { label.textContent = '状态待确认'; });
    shell.querySelectorAll('[data-ws-agent-dock] .ws-agent-presence').forEach(dot => { dot.className = 'ws-agent-presence ws-agent-presence-unknown'; });
    inspector.querySelectorAll('.ws-identity-status').forEach(status => {
      status.querySelector('span').textContent = '状态待确认';
      status.querySelector('i').classList.remove('is-online');
    });
    document.querySelectorAll('[data-agent-card] .agent-presence').forEach(status => { status.textContent = '状态待确认'; });
    recipients().forEach(choice => { choice.dataset.connectionState = 'unknown'; });
    renderRecipients();
    etag = ''; // Recovery must fetch fresh status rather than accept a 304.
  }
  let eventSource = null;
  let eventHealthy = false;
  let eventRefreshPending = false;
  let eventSessionExpired = false;
  let loginRequired = false;
  let lastRefreshAt = 0;
  const refreshInterval = () => loginRequired ? 30000 : eventHealthy && !pendingSubmission ? 10000 : 2000;
  function showLoginRequired() {
    loginRequired = true; eventSessionExpired = true; closeEventStream(); saveDraft();
    const sync = shell.querySelector('[data-ws-sync]');
    const link = document.createElement('a');
    link.href = new URL(shell.dataset.projectUrl, location.href).pathname.split('/projects/')[0] + '/login';
    link.target = '_blank'; link.rel = 'noopener'; link.textContent = '重新登录';
    sync.replaceChildren(document.createTextNode('登录已过期 · '), link);
    errors.textContent = '登录已过期，当前文字和附件仍保留。重新登录后可继续发送。'; errors.hidden = false;
    presenceUnconfirmed();
  }
  function scheduleEventRefresh(delay = 350) {
    eventRefreshPending = true;
    if (refreshController) return;
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, Math.max(delay, 500 - (Date.now() - lastRefreshAt)));
  }
  function closeEventStream() {
    eventSource?.close(); eventSource = null; eventHealthy = false;
  }
  function connectEventStream() {
    if (eventSource || eventSessionExpired || document.hidden || !navigator.onLine || !window.EventSource || !shell.dataset.eventsUrl) return;
    eventSource = new EventSource(shell.dataset.eventsUrl);
    eventSource.addEventListener('open', () => { eventHealthy = true; });
    eventSource.addEventListener('workspace', event => {
      try {
        const data = JSON.parse(event.data);
        if (data.version && data.version !== shell.dataset.eventVersion) scheduleEventRefresh();
      } catch { scheduleEventRefresh(); }
    });
    eventSource.addEventListener('error', () => { eventHealthy = false; scheduleEventRefresh(1000); });
    eventSource.addEventListener('session-expired', () => {
      eventSessionExpired = true; closeEventStream(); etag = ''; scheduleEventRefresh(50);
    });
  }
  async function refresh() {
    clearTimeout(refreshTimer);
    if (pendingSubmission && !submitting && !document.hidden) { await checkReceipt(); if (committed) return; }
    if (document.hidden || submitting) { refreshTimer = setTimeout(refresh, 2000); return; }
    if (refreshController) return;
    eventRefreshPending = false;
    lastRefreshAt = Date.now();
    refreshController = new AbortController();
    const timeout = setTimeout(() => refreshController?.abort(), 12000);
    try {
      const response = await fetch(shell.dataset.workspaceUrl || location.href, { headers:etag ? { "If-None-Match":etag } : {}, credentials:"same-origin", signal:refreshController.signal });
      if (response.status === 401 || (response.redirected && new URL(response.url).pathname.endsWith('/login'))) { showLoginRequired(); return; }
      if (response.status === 304) { shell.querySelector("[data-ws-sync]").textContent = "已同步"; return; }
      if (!response.ok) throw new Error();
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const freshShell = page.querySelector("[data-ws-shell]");
      if (!freshShell || freshShell.dataset.conversation !== shell.dataset.conversation || !updateCsrf(freshShell)) throw new Error();
      if (loginRequired || eventSessionExpired) {
        loginRequired = false; eventSessionExpired = false; errors.hidden = true; connectEventStream();
      }
      shell.dataset.eventVersion = freshShell.dataset.eventVersion || '';
      refreshPresence(freshShell);
      const nextEtag = response.headers.get("ETag") || "";
      const positionBeforeRefresh = captureReadingPosition();
      const focused = document.activeElement;
      const taskListOpen = shell.querySelector("[data-ws-task-list]")?.open;
      const insideInspector = inspector.contains(focused);
      let skipped = false;
      ["[data-ws-live]", "[data-ws-conversations]", "[data-ws-room-count]", "[data-ws-task-panels]", "[data-ws-agent-panels]"].forEach((selector) => {
        // Do not replace a details panel while someone is using its controls.
        if (insideInspector && selector.includes("panels")) { skipped = true; return; }
        const destination = shell.querySelector(selector); const incoming = freshShell.querySelector(selector);
        if (selector === "[data-ws-live]" && destination && incoming) {
          const previous = new Map(Array.from(destination.querySelectorAll("[data-ws-entry]")).map(node => [node.dataset.wsEntry,node]));
          const selection = window.getSelection();
          let added = false;
          const children = Array.from(incoming.children).map(node => {
            const old = node.dataset.wsEntry && previous.get(node.dataset.wsEntry);
            if (node.dataset.wsEntry && !old) added = true;
            if (old && (old.contains(focused) || (selection && !selection.isCollapsed && old.contains(selection.anchorNode)))) { skipped = true; return old; }
            if (old && old.dataset.wsEntryVersion === node.dataset.wsEntryVersion) return old;
            return document.importNode(node,true);
          });
          // Keep the bounded part of the preceding page while someone is reading it.
          // New replies must not evict their focused message at the 40-message boundary.
          if (!followBottom && incoming.querySelector('.ws-load-records')) {
            const incomingIds = new Set(children.filter(node => node.dataset.wsEntry).map(node => node.dataset.wsEntry));
            const older = Array.from(previous.values()).filter(node => !node.hasAttribute('data-ws-progress'));
            const firstShared = older.findIndex(node => incomingIds.has(node.dataset.wsEntry));
            const keep = firstShared > 0 ? older.slice(0, firstShared).slice(-40) : [];
            const firstMessage = children.findIndex(node => node.dataset.wsEntry);
            if (keep.length && firstMessage >= 0) { children.splice(firstMessage,0,...keep); skipped = true; }
          }
          // Move only changed nodes; stable messages retain focus, image state and selections.
          const retained = new Set(children);
          Array.from(destination.children).forEach(node => { if (!retained.has(node)) node.remove(); });
          let cursor = destination.firstElementChild;
          children.forEach(node => { if (node === cursor) cursor = cursor.nextElementSibling; else destination.insertBefore(node,cursor); });
          while (cursor) { const next = cursor.nextElementSibling; cursor.remove(); cursor = next; }
          if (added && !followBottom && newMessages) newMessages.hidden = false;
          return;
        }
        if (destination && incoming) destination.replaceChildren(...Array.from(incoming.childNodes).map((node) => document.importNode(node, true)));
      });
      if (!skipped) etag = nextEtag;
      const taskList = shell.querySelector("[data-ws-task-list]");
      if (taskList && taskListOpen) taskList.open = true;
      window.CodexMarkdown?.enhanceMarkdown(shell.querySelector("[data-ws-live]"));
      window.CodexMarkdown?.enhanceMarkdown(inspector);
      formatTimes();
      filterRooms(); selectPanel(activeTab, activeTab === "agent" ? selectedAgent : selectedTask);
      restoreReadingPosition(positionBeforeRefresh);
      shell.querySelector("[data-ws-sync]").textContent = "已同步";
    } catch (error) {
      presenceUnconfirmed();
      shell.querySelector("[data-ws-sync]").textContent = error.name === "AbortError" ? "连接超时 · 正在重试" : "等待连接";
    } finally { clearTimeout(timeout); refreshController = null; refreshTimer = setTimeout(refresh, eventRefreshPending ? 350 : refreshInterval()); }
  }
  refreshTimer = setTimeout(refresh, 2000);
  connectEventStream();
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { etag = ""; connectEventStream(); scheduleEventRefresh(300); } else closeEventStream(); });
  window.addEventListener('online', () => { etag = ''; connectEventStream(); scheduleEventRefresh(50); });
  window.addEventListener('offline', () => { closeEventStream(); presenceUnconfirmed(); });
  window.addEventListener('pagehide', closeEventStream);
  window.addEventListener('pageshow', connectEventStream);
  setInterval(() => { if (!document.hidden) formatTimes(); }, 60000);
  shell.dataset.wsReady = "true";
})();
