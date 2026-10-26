(() => {
  "use strict";
  const workspace = document.querySelector("[data-agent-workspace]");
  if (!workspace) return;
  const search = workspace.querySelector("[data-agent-search]");
  const roster = workspace.querySelector("[data-agent-roster]");
  const assignment = workspace.querySelector("[data-agent-assignment]");
  const create = workspace.querySelector("[data-agent-create]");
  const discussion = workspace.querySelector("[data-agent-discussion]");
  const views = [roster, assignment, create, discussion];
  const selected = new Set();
  let filter = "all";
  let opener = null;
  function cards() { return Array.from(workspace.querySelectorAll("[data-agent-card]")); }
  function show(view) { views.forEach((item) => { item.hidden = item !== view; }); }
  function render() {
    const available = new Set(cards().filter((card) => card.dataset.ready === "true").map((card) => card.dataset.agentCard));
    for (const id of selected) if (!available.has(id)) selected.delete(id);
    const query = search.value.trim().toLocaleLowerCase();
    let visible = 0;
    let hiddenSelected = 0;
    cards().forEach((card) => {
      const matches = card.dataset.agentSearchText.toLocaleLowerCase().includes(query)
        && (filter === "all" || card.dataset[filter] === "true");
      card.hidden = !matches;
      if (matches) visible++;
      const checked = selected.has(card.dataset.agentCard);
      if (!matches && checked) hiddenSelected++;
      card.querySelector("[data-agent-select]").checked = checked;
      card.classList.toggle("is-selected", checked);
    });
    workspace.querySelector("[data-agent-no-match]").hidden = visible > 0 || !cards().length;
    workspace.querySelector("[data-agent-use-selection]").disabled = !selected.size;
    workspace.querySelector("[data-agent-selection-status]").textContent = selected.size
      ? `已选 ${selected.size} 个执行者${hiddenSelected ? `（${hiddenSelected} 个在当前筛选外）` : ""} · 将参与同一场协作`
      : "勾选执行者，开启一场协作";
    workspace.querySelectorAll("[data-agent-filter]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.agentFilter === filter)));
  }
  function close(restoreFocus = true) {
    workspace.close();
    if (restoreFocus) opener?.focus({ preventScroll: true });
  }
  document.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-agents-open]");
    if (!button) return;
    opener = button.closest(".hub-more")?.querySelector("summary") || button;
    document.dispatchEvent(new CustomEvent("codexhub:overlay-open", { detail: { name: "agents" } }));
    render();
    show(roster);
    if (!workspace.open) workspace.showModal();
    search.focus({ preventScroll: true });
  });
  workspace.addEventListener("click", (event) => {
    const target = event.target;
    if (target.closest("[data-agents-close]")) close();
    const filterButton = target.closest("[data-agent-filter]");
    if (filterButton) { filter = filterButton.dataset.agentFilter; render(); }
    const viewButton = target.closest("[data-agent-view]");
    if (viewButton) {
      workspace.classList.toggle("agent-list-view", viewButton.dataset.agentView === "list");
      workspace.querySelectorAll("[data-agent-view]").forEach((button) => button.setAttribute("aria-pressed", String(button === viewButton)));
    }
    if (target.closest("[data-agent-create-open]")) { show(create); create.querySelector('[name="name"]').focus({ preventScroll: true }); }
    const assign = target.closest("[data-agent-assign]:not(:disabled)");
    if (assign) {
      const choice = assignment.querySelector('[name="assigned_agent_id"]');
      choice.replaceChildren(...cards().filter((card) => card.dataset.ready === "true").map((card) => new Option(card.dataset.agentName, card.dataset.agentCard)));
      choice.value = assign.dataset.agentAssign;
      show(assignment);
      assignment.querySelector('[name="title"]').focus({ preventScroll: true });
    }
    if (target.closest("[data-agent-use-selection]:not(:disabled)")) {
      render();
      if (!selected.size) return;
      const members = discussion.querySelector("[data-agent-discussion-members]");
      members.replaceChildren();
      cards().filter((card) => selected.has(card.dataset.agentCard)).forEach((card) => {
        const label = document.createElement("span"); label.textContent = card.dataset.agentName;
        const input = document.createElement("input"); input.type = "hidden"; input.name = "agent_ids"; input.value = card.dataset.agentCard;
        members.append(label, input);
      });
      show(discussion);
      discussion.querySelector('[name="title"]').focus({ preventScroll: true });
    }
    if (target.closest("[data-agent-back]")) { show(roster); render(); search.focus({ preventScroll: true }); }
    if (target.closest("[data-agent-panel-link]")) close(false);
  });
  workspace.addEventListener("change", (event) => {
    if (!event.target.matches("[data-agent-select]")) return;
    if (event.target.checked) selected.add(event.target.value); else selected.delete(event.target.value);
    render();
  });
  search.addEventListener("input", render);
  document.addEventListener("codexhub:agents-refreshed", render);
  // Failed identity edits stay in context, with the user's original fields intact.
  [create, assignment, discussion].filter(Boolean).forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (form.dataset.submitting === "true") return;
      const submit = event.submitter || form.querySelector('[type="submit"]');
      let notice = form.querySelector("[data-agent-form-error]");
      if (!notice) {
        notice = document.createElement("p");
        notice.dataset.agentFormError = "";
        notice.className = "agent-form-error";
        notice.setAttribute("role", "alert");
        form.querySelector(".agent-assignment-body").prepend(notice);
      }
      notice.hidden = true;
      form.dataset.submitting = "true";
      form.setAttribute("aria-busy", "true");
      if (submit) submit.disabled = true;
      try {
        const response = await fetch(form.action, {
          method: "POST", body: new FormData(form), credentials: "same-origin",
        });
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          throw new Error(typeof data.detail === "string" ? data.detail : "提交未成功，请检查填写内容后重试。");
        }
        if (response.redirected) { window.location.assign(response.url); return; }
        throw new Error("没有收到完成结果，请保留当前内容并稍后重试。");
      } catch (error) {
        notice.textContent = error instanceof TypeError
          ? "连接暂时中断，填写内容已保留。请稍后重试。" : error.message;
        notice.hidden = false;
        notice.scrollIntoView({ block: "nearest" });
      } finally {
        delete form.dataset.submitting;
        form.removeAttribute("aria-busy");
        if (submit) submit.disabled = false;
      }
    });
  });
})();
