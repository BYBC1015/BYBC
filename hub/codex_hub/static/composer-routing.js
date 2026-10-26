(() => {
  "use strict";
  const composer = document.querySelector("[data-mention-composer]");
  if (!composer) return;
  const mode = composer.querySelector("[data-delivery-mode]");
  const members = Array.from(composer.querySelectorAll("[data-delivery-member]"));
  const settings = composer.querySelector("[data-delivery-settings]");
  const summary = composer.querySelector("[data-delivery-summary]");
  const note = composer.querySelector("[data-delivery-note]");
  const modelSelect = composer.querySelector("[data-codex-model]");
  const effortSelect = composer.querySelector("[data-codex-reasoning]");
  const catalogStatus = composer.querySelector("[data-model-catalog-status]");
  const messageStatus = composer.querySelector("[data-message-status]");
  const refreshButton = composer.querySelector("[data-model-catalog-refresh]");
  if (!mode || !modelSelect || !effortSelect) return;
  let devices = [];
  let models = [];
  let catalogRequest = null;
  const effortNames = {
    none: "关闭推理", minimal: "最轻", low: "轻度", medium: "中", high: "高",
    xhigh: "极高", max: "最大", ultra: "超强（消耗更多额度）",
  };

  function targets() {
    return members.filter((member) => mode.value === "group" || member.checked);
  }

  function notifyControl(control) {
    control.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function replaceOptions(control, items, previous) {
    control.replaceChildren(new Option("设备默认", ""));
    items.forEach((item) => control.add(new Option(item.label, item.value)));
    control.value = items.some((item) => item.value === previous) ? previous : "";
  }

  function syncEfforts() {
    let candidates;
    if (modelSelect.value) {
      candidates = models.filter((model) => model.model === modelSelect.value);
    } else {
      // Default models can differ on each device. Only offer efforts known to work on all.
      candidates = selectedCodexDevices().map((device) =>
        (device.codex?.models || []).find((model) => model.isDefault));
      if (candidates.some((model) => !model)) candidates = [];
    }
    const first = candidates[0]?.supportedReasoningEfforts || [];
    const efforts = first.map((item) => item.reasoningEffort).filter((effort) =>
      candidates.every((model) => model.supportedReasoningEfforts?.some(
        (item) => item.reasoningEffort === effort)));
    const previous = effortSelect.value;
    replaceOptions(effortSelect, efforts.map((effort) => ({
      value: effort, label: effortNames[effort] || effort,
    })), previous);
    effortSelect.disabled = efforts.length === 0;
    if (previous && previous !== effortSelect.value) {
      catalogStatus.textContent += " 原推理强度不适用于当前模型或成员，已恢复设备默认。";
    }
    notifyControl(effortSelect);
  }

  function selectedCodexDevices() {
    return targets().map((member) => {
      const device = devices.find((item) => String(item.device_id) === member.value);
      return {
        ...device,
        execution_backend: device?.execution_backend || member.dataset.executionBackend || "",
        codex: device?.codex || {},
      };
    }).filter((device) => !device.execution_backend || device.execution_backend === "codex");
  }

  function syncModels() {
    const selectedDevices = selectedCodexDevices();
    const catalogs = selectedDevices.map((device) => device.codex?.models || []);
    models = (catalogs[0] || []).filter((model) =>
      catalogs.every((catalog) => catalog.some((other) => other.model === model.model)))
      .map((model) => ({
        ...model,
        supportedReasoningEfforts: (model.supportedReasoningEfforts || []).filter((effort) =>
          catalogs.every((catalog) => catalog.find((item) => item.model === model.model)
            ?.supportedReasoningEfforts?.some((item) => item.reasoningEffort === effort.reasoningEffort))),
      }));
    const previous = modelSelect.value;
    replaceOptions(modelSelect, models.map((model) => ({ value: model.model, label: model.displayName || model.model })), previous);
    modelSelect.disabled = models.length === 0;
    if (!targets().length) {
      catalogStatus.textContent = "请先选择执行成员。";
    } else if (!selectedDevices.length) {
      catalogStatus.textContent = "当前所选成员使用其他模型服务，无需设置 Codex 模型。";
    } else if (catalogs.some((catalog) => !catalog.length)) {
      catalogStatus.textContent = "等待所选设备同步模型目录；可先使用设备默认。";
    } else if (!models.length) {
      catalogStatus.textContent = "所选设备没有共同模型；可使用各设备默认，或调整执行成员。";
    } else {
      const cached = selectedDevices.some((device) => device.codex.source === "models-cache");
      catalogStatus.textContent = `${selectedDevices.length} 台 Codex 设备 · ${models.length} 个可选模型${cached ? " · 来自设备本地目录" : " · 已同步"}`;
    }
    if (previous && previous !== modelSelect.value) {
      catalogStatus.textContent += " 原模型不适用于当前成员，已恢复设备默认。";
    }
    notifyControl(modelSelect);
  }

  function syncDelivery() {
    const group = mode.value === "group";
    composer.querySelector("[data-delivery-members]").disabled = group;
    const selected = targets();
    summary.textContent = group ? "全组协作" : selected.length === 1
      ? selected[0].dataset.deviceLabel : selected.length ? `${selected.length} 个成员` : "选择成员";
    summary.title = selected.map((member) => member.dataset.deviceLabel).join("、");
    note.textContent = group
      ? "由全组成员与角色共同研讨，主设备汇总决策。"
      : "只由勾选成员直接执行；可选一个或多个成员。正文 @ 不会扩大执行范围。";
    composer.querySelector("[data-mention-input]").placeholder = group
      ? "输入任务，由全组成员共同研讨…" : "输入任务，由下方选中的成员直接执行…";
    syncModels();
  }

  async function refreshCatalog() {
    if (catalogRequest) return catalogRequest;
    catalogRequest = (async () => {
      const abort = typeof AbortController === "function" ? new AbortController() : null;
      const timer = window.setTimeout(() => abort?.abort(), 10000);
      refreshButton.disabled = true;
      try {
        const response = await fetch(composer.dataset.modelCatalogUrl, {
          credentials: "same-origin", cache: "no-store", signal: abort?.signal,
          headers: { Accept: "application/json" },
        });
        if (!response.ok) throw new Error("catalog unavailable");
        const payload = await response.json();
        if (!Array.isArray(payload.devices)) throw new Error("invalid catalog");
        devices = payload.devices;
        syncModels();
      } catch {
        catalogStatus.textContent = "模型目录暂未同步；可刷新重试或使用设备默认。";
      } finally {
        window.clearTimeout(timer);
        refreshButton.disabled = false;
        catalogRequest = null;
      }
    })();
    return catalogRequest;
  }

  window.CodexHubDelivery = {
    validate() {
      if (mode.value !== "selected" || targets().length) return true;
      settings.open = true;
      messageStatus.textContent = "请至少选择一个执行成员。";
      messageStatus.classList.add("is-error");
      return false;
    },
  };
  mode.addEventListener("change", syncDelivery);
  members.forEach((member) => member.addEventListener("change", syncDelivery));
  modelSelect.addEventListener("change", syncEfforts);
  refreshButton.addEventListener("click", refreshCatalog);
  document.addEventListener("click", (event) => {
    if (settings.open && !settings.contains(event.target)) settings.open = false;
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") settings.open = false;
  });
  window.addEventListener("focus", refreshCatalog);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshCatalog();
  });
  window.setInterval(() => {
    if (document.visibilityState === "visible") refreshCatalog();
  }, 60000);
  syncDelivery();
  refreshCatalog();
})();
