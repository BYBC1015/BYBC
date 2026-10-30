(() => {
  const body = document.body;
  const shell = document.querySelector(".workbench-shell");
  const grid = document.querySelector(".workbench-grid");
  let topbarButton = document.querySelector(
    ".workbench-topbar > .icon-button:not(.mobile-rail-toggle)",
  );
  const chat = document.querySelector('[data-auto-refresh="chat"]');
  const controlPane = document.querySelector(".control-pane");
  const sidebar = document.querySelector(".workbench-sidebar");
  const focusToggle = document.querySelector("[data-focus-reading-toggle]");
  const topbar = document.querySelector(".workbench-topbar");

  if (!body || !shell || !grid || !topbar || !chat || !controlPane || !sidebar) {
    return;
  }

  // Keep native navigation independent of the web header's overflow menu.
  if (!topbarButton) {
    topbarButton = document.createElement("button");
    topbarButton.type = "button";
    topbarButton.className = "icon-button";
    topbar.append(topbarButton);
  }

  document.documentElement.classList.add("codex-hub-android");
  const chatWasNearBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 120;
  body.classList.remove("android-tools-mode");
  body.classList.add("android-chat-mode");

  const localPreviews = new Map();
  const localPreviewsByName = new Map();
  const localFileKey = (file) => `${String(file?.name || "")}:${Number(file?.size ?? -1)}`;
  window.CodexHubAndroidFiles = {
    receiveSelection(files) {
      Array.from(files || []).forEach((file) => {
        const preview = String(file?.preview || "");
        const name = String(file?.name || "");
        if (!preview || !name) return;
        localPreviews.set(localFileKey(file), preview);
        if (!localPreviewsByName.has(name)) localPreviewsByName.set(name, preview);
      });
      window.dispatchEvent(new CustomEvent("codexhub:native-file-previews"));
    },
    previewFor(file) {
      return localPreviews.get(localFileKey(file))
        || localPreviewsByName.get(String(file?.name || ""))
        || "";
    },
    clear() {
      localPreviews.clear();
      localPreviewsByName.clear();
      try { window.HubLocalData?.clearImportedFiles?.(); } catch {}
    },
  };

  const composerInput = document.querySelector("[data-mention-input]");
  const messageForm = document.querySelector("#project-message-form");
  const messageStatus = messageForm?.querySelector("[data-message-status]");
  const localDraftKey = window.location.pathname;
  if (composerInput && typeof window.HubLocalData?.loadDraft === "function") {
    try {
      const restoredDraft = String(window.HubLocalData.loadDraft(localDraftKey) || "");
      if (!composerInput.value && restoredDraft) {
        composerInput.value = restoredDraft;
        composerInput.dispatchEvent(new Event("input", { bubbles: true }));
      }
    } catch {}

    let draftSaveTimer = null;
    const saveLocalDraft = () => {
      window.clearTimeout(draftSaveTimer);
      draftSaveTimer = null;
      try {
        if (composerInput.value) {
          window.HubLocalData.saveDraft(localDraftKey, composerInput.value);
        } else {
          window.HubLocalData.clearDraft(localDraftKey);
        }
      } catch {}
    };
    composerInput.addEventListener("input", () => {
      window.clearTimeout(draftSaveTimer);
      draftSaveTimer = window.setTimeout(saveLocalDraft, 500);
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") saveLocalDraft();
    });
    window.addEventListener("pagehide", saveLocalDraft, { once: true });
  }

  const resizeComposer = () => {
    if (!composerInput) return;
    composerInput.style.height = "auto";
    composerInput.style.height = `${Math.min(112, Math.max(54, composerInput.scrollHeight))}px`;
  };
  composerInput?.addEventListener("input", resizeComposer);
  resizeComposer();

  let feedbackTimer = 0;
  const syncMessageFeedback = () => {
    if (!messageStatus) return;
    window.clearTimeout(feedbackTimer);
    const text = messageStatus.textContent?.trim() || "";
    const isError = messageStatus.classList.contains("is-error")
      || /失败|未确认|离线|不可用|请输入/.test(text);
    const isSuccess = /已发送|已恢复/.test(text) && !isError;
    messageStatus.classList.toggle("is-error", isError);
    messageStatus.classList.toggle("is-success", isSuccess);
    if (text && !isError && !/正在|等待/.test(text)) {
      feedbackTimer = window.setTimeout(() => {
        if (messageStatus.textContent?.trim() === text) messageStatus.textContent = "";
      }, 2600);
    }
  };
  if (messageStatus) {
    new MutationObserver(syncMessageFeedback).observe(messageStatus, {
      childList: true,
      characterData: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["class"],
    });
    syncMessageFeedback();
  }

  document.addEventListener("click", (event) => {
    const imageLink = event.target.closest?.(".message-image-link");
    if (!imageLink || typeof window.HubMedia?.openImage !== "function") return;
    if (window.CodexHubMediaViewer || window.CodexHubDeferredFeatures) return;
    const source = imageLink.dataset.mediaSrc || imageLink.dataset.mediaPreview || imageLink.href;
    if (!source) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const title = imageLink.dataset.mediaFilename
      || imageLink.querySelector("img")?.alt
      || "图片";
    window.HubMedia.openImage(new URL(source, window.location.href).href, title);
  }, true);

  topbarButton.removeAttribute("onclick");
  topbarButton.onclick = null;
  topbarButton.classList.add("android-tools-back");

  const title = document.querySelector(".workbench-title");
  const titleLine = title?.querySelector(".workbench-title-line");
  const memoryOpener = topbar.querySelector("[data-memory-open]");
  const version = String(window.CodexHubAndroidVersion || "").trim();
  if (titleLine && version && !titleLine.querySelector(".android-version-badge")) {
    const badge = document.createElement("span");
    badge.className = "android-version-badge";
    badge.textContent = `v${version}`;
    badge.setAttribute("aria-label", `APK version ${version}`);
    titleLine.insertBefore(badge, focusToggle || null);
  }

  let connectionState = titleLine?.querySelector(".android-connection-state");
  if (titleLine && !connectionState) {
    connectionState = document.createElement("span");
    connectionState.className = "android-connection-state";
    connectionState.setAttribute("role", "status");
    connectionState.setAttribute("aria-live", "polite");
    connectionState.innerHTML = '<i aria-hidden="true"></i><span>在线</span>';
    titleLine.insertBefore(connectionState, titleLine.querySelector(".android-version-badge"));
  }

  function updateNetworkState(online, announce = false) {
    body.classList.toggle("android-offline", !online);
    if (connectionState) {
      connectionState.classList.toggle("is-offline", !online);
      connectionState.querySelector("span").textContent = online ? "在线" : "离线";
    }
    if (!messageStatus) return;
    if (!online) {
      messageStatus.textContent = "当前离线，文字草稿已保存在本机";
      messageStatus.classList.add("is-error");
    } else if (announce) {
      messageStatus.classList.remove("is-error");
      messageStatus.textContent = "网络已恢复，正在同步";
    }
  }

  window.addEventListener("offline", () => updateNetworkState(false, true));
  window.addEventListener("online", () => updateNetworkState(true, true));
  updateNetworkState(navigator.onLine !== false);

  messageForm?.addEventListener("submit", (event) => {
    if (navigator.onLine !== false) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    try {
      if (composerInput?.value) {
        window.HubLocalData?.saveDraft?.(localDraftKey, composerInput.value);
      }
    } catch {}
    updateNetworkState(false, true);
    composerInput?.focus();
  }, true);

  let drawerToggle = topbar.querySelector(".android-drawer-toggle");
  if (!drawerToggle) {
    drawerToggle = document.createElement("button");
    drawerToggle.type = "button";
    drawerToggle.className = "android-drawer-toggle";
    drawerToggle.textContent = "☰";
    drawerToggle.setAttribute("aria-label", "打开项目侧边栏");
    drawerToggle.setAttribute("aria-expanded", "false");
    drawerToggle.title = "项目";
    topbar.insertBefore(drawerToggle, title || topbar.firstChild);
  }

  const taskSection = document.getElementById("tasks")
    || Array.from(controlPane.querySelectorAll(":scope > section"))
      .find((section) => !section.id && section.classList.contains("control-card"));
  if (taskSection && !taskSection.id) taskSection.id = "android-tasks";
  const taskTargetId = taskSection?.id || "android-tasks";
  if (!sidebar.id) sidebar.id = "android-projects";
  const sidebarTargetId = sidebar.id;

  let projectDrawer = shell.querySelector(".android-project-drawer");
  if (!projectDrawer) {
    const projectRail = sidebar.querySelector(".rail-section");
    projectDrawer = document.createElement("aside");
    projectDrawer.className = "android-project-drawer";
    projectDrawer.setAttribute("aria-label", "项目侧边栏");
    projectDrawer.setAttribute("aria-hidden", "true");

    const drawerHeader = document.createElement("header");
    drawerHeader.className = "android-drawer-header";
    drawerHeader.innerHTML = "<strong>Codex Hub</strong>";
    const closeDrawerButton = document.createElement("button");
    closeDrawerButton.type = "button";
    closeDrawerButton.textContent = "关闭";
    closeDrawerButton.setAttribute("aria-label", "关闭项目侧边栏");
    closeDrawerButton.dataset.androidDrawerClose = "";
    drawerHeader.appendChild(closeDrawerButton);
    projectDrawer.appendChild(drawerHeader);

    const shortcuts = document.createElement("nav");
    shortcuts.className = "android-drawer-shortcuts";
    shortcuts.setAttribute("aria-label", "项目快捷操作");

    if (memoryOpener) {
      const memoryButton = document.createElement("button");
      memoryButton.type = "button";
      memoryButton.className = "android-drawer-shortcut";
      memoryButton.innerHTML = '<span class="android-drawer-shortcut-icon" aria-hidden="true">忆</span><span class="android-drawer-shortcut-copy"><strong>项目记忆</strong><small>查看共享上下文与交接记录</small></span>';
      memoryButton.dataset.androidMemoryOpen = "";
      shortcuts.appendChild(memoryButton);
    }

    const newProject = document.createElement("a");
    newProject.className = "android-drawer-shortcut";
    newProject.href = projectRail?.querySelector(".rail-add")?.href || "/projects?list=1#new-project";
    const newProjectUrl = new URL(newProject.href, window.location.href);
    newProjectUrl.hash = "new-project";
    newProject.href = newProjectUrl.toString();
    newProject.innerHTML = '<span class="android-drawer-shortcut-icon" aria-hidden="true">＋</span><span class="android-drawer-shortcut-copy"><strong>新建项目</strong><small>创建独立协作空间</small></span>';
    shortcuts.appendChild(newProject);

    const manageProjectMembers = document.createElement("button");
    manageProjectMembers.type = "button";
    manageProjectMembers.className = "android-drawer-shortcut";
    manageProjectMembers.innerHTML = '<span class="android-drawer-shortcut-icon" aria-hidden="true">人</span><span class="android-drawer-shortcut-copy"><strong>管理项目成员</strong><small>设备、角色与群聊权限</small></span>';
    manageProjectMembers.dataset.androidTarget = "members";
    shortcuts.appendChild(manageProjectMembers);

    if (typeof window.HubApp?.openServerSettings === "function") {
      const serverSettings = document.createElement("button");
      serverSettings.type = "button";
      serverSettings.className = "android-drawer-shortcut";
      serverSettings.innerHTML = '<span class="android-drawer-shortcut-icon" aria-hidden="true">服</span><span class="android-drawer-shortcut-copy"><strong>Hub 服务器</strong><small>迁移服务器或切换访问地址</small></span>';
      serverSettings.dataset.androidServerSettings = "";
      shortcuts.appendChild(serverSettings);
    }
    projectDrawer.appendChild(shortcuts);

    const projectLabel = document.createElement("div");
    projectLabel.className = "android-drawer-section-label";
    projectLabel.textContent = "项目";
    projectDrawer.appendChild(projectLabel);

    const drawerProjects = projectRail?.querySelector(".project-rail-list")?.cloneNode(true);
    if (drawerProjects) projectDrawer.appendChild(drawerProjects);

    const moreFeatures = document.createElement("button");
    moreFeatures.type = "button";
    moreFeatures.className = "android-drawer-manage";
    moreFeatures.textContent = "更多功能";
    moreFeatures.dataset.androidTarget = "control";
    projectDrawer.appendChild(moreFeatures);

    shell.append(projectDrawer);
  }

  let drawerScrim = shell.querySelector(".android-drawer-scrim");
  if (!drawerScrim) {
    drawerScrim = document.createElement("button");
    drawerScrim.type = "button";
    drawerScrim.className = "android-drawer-scrim";
    drawerScrim.setAttribute("aria-label", "关闭项目侧边栏");
    drawerScrim.tabIndex = -1;
    shell.insertBefore(drawerScrim, projectDrawer);
  }

  let toolsNav = grid.querySelector(".android-tools-nav");
  if (!toolsNav) {
    toolsNav = document.createElement("nav");
    toolsNav.className = "android-tools-nav";
    toolsNav.setAttribute("aria-label", "更多功能");
    const toolsIntro = document.createElement("header");
    toolsIntro.className = "android-tools-intro";
    toolsIntro.innerHTML = "<strong>更多</strong><small>项目设置、任务、成员与应用服务</small>";
    toolsNav.appendChild(toolsIntro);
    [
      ["概览", "control", "⌂"],
      ["Codex 项目", "codex-workspaces", "◇"],
      ["任务", taskTargetId, "✓"],
      ["成员", "members", "人"],
      ["媒体库", "files", "▧"],
      ["项目与设备", sidebarTargetId, "☷"],
    ].forEach(([label, id, icon]) => {
      if (!document.getElementById(id)) return;
      const button = document.createElement("button");
      button.type = "button";
      button.innerHTML = `<span aria-hidden="true">${icon}</span><strong>${label}</strong>`;
      button.dataset.androidTarget = id;
      toolsNav.appendChild(button);
    });
    [
      ["下载更新", ".workbench-topbar a[href*='/downloads']", "↓"],
      ["邮箱", ".workbench-topbar a[href*='/inbox']", "✉"],
    ].forEach(([label, selector, icon]) => {
      const link = document.querySelector(selector);
      if (!link) return;
      const button = document.createElement("button");
      button.type = "button";
      button.innerHTML = `<span aria-hidden="true">${icon}</span><strong>${label}</strong>`;
      button.dataset.androidUrl = link.href;
      toolsNav.appendChild(button);
    });
    grid.prepend(toolsNav);
  }

  let bottomNav = shell.querySelector(".android-bottom-nav");
  if (!bottomNav) {
    bottomNav = document.createElement("nav");
    bottomNav.className = "android-bottom-nav";
    bottomNav.setAttribute("aria-label", "主要导航");
    [
      ["chat", "●", "对话"],
      ["projects", "▤", "项目"],
      ["more", "•••", "更多"],
    ].forEach(([view, icon, label]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.androidView = view;
      button.innerHTML = `<span aria-hidden="true">${icon}</span><strong>${label}</strong>`;
      bottomNav.appendChild(button);
    });
    shell.appendChild(bottomNav);
  }

  function setPrimaryNav(view) {
    body.dataset.androidView = view;
    bottomNav.querySelectorAll("button[data-android-view]").forEach((button) => {
      if (button.dataset.androidView === view) {
        button.setAttribute("aria-current", "page");
      } else {
        button.removeAttribute("aria-current");
      }
    });
  }

  function setHeaderButton(label, expanded) {
    topbarButton.textContent = label;
    topbarButton.setAttribute("aria-label", label);
    topbarButton.setAttribute("aria-expanded", String(expanded));
  }

  let lastDrawerFocus = null;
  function setDrawerOpen(open) {
    const wasOpen = body.classList.contains("android-drawer-open");
    if (open && !wasOpen) lastDrawerFocus = document.activeElement;
    body.classList.toggle("android-drawer-open", open);
    projectDrawer.inert = !open;
    projectDrawer.setAttribute("aria-hidden", String(!open));
    drawerToggle.setAttribute("aria-expanded", String(open));
    drawerScrim.tabIndex = open ? 0 : -1;
    if (open) {
      window.requestAnimationFrame(() => {
        projectDrawer.querySelector("[data-android-drawer-close]")?.focus();
      });
    } else if (wasOpen && lastDrawerFocus?.isConnected) {
      try {
        lastDrawerFocus.focus({ preventScroll: true });
      } catch {
        lastDrawerFocus.focus();
      }
    }
  }

  function showChat() {
    setDrawerOpen(false);
    body.classList.remove("android-tools-mode");
    body.classList.add("android-chat-mode");
    controlPane.style.setProperty("display", "none", "important");
    controlPane.inert = true;
    controlPane.setAttribute("aria-hidden", "true");
    sidebar.style.setProperty("display", "none", "important");
    sidebar.inert = true;
    sidebar.setAttribute("aria-hidden", "true");
    setHeaderButton("返回聊天", false);
    setPrimaryNav("chat");
  }

  function activateTool(targetId) {
    const selectedId = document.getElementById(targetId) ? targetId : "control";
    controlPane.classList.remove("android-tool-container-active");
    sidebar.classList.remove("android-tool-section-active");
    controlPane.style.setProperty("display", "none", "important");
    controlPane.inert = true;
    controlPane.setAttribute("aria-hidden", "true");
    sidebar.style.setProperty("display", "none", "important");
    sidebar.inert = true;
    sidebar.setAttribute("aria-hidden", "true");
    Array.from(controlPane.children).forEach((section) => {
      section.classList.remove("android-tool-section-active");
      section.style.setProperty("display", "none", "important");
      section.setAttribute("aria-hidden", "true");
    });

    const target = document.getElementById(selectedId);
    if (target === sidebar) {
      sidebar.inert = false;
      sidebar.classList.add("android-tool-section-active");
      sidebar.style.setProperty("display", "grid", "important");
      sidebar.style.setProperty("position", "static", "important");
      sidebar.style.setProperty("transform", "none", "important");
      sidebar.style.setProperty("inset", "auto", "important");
      sidebar.setAttribute("aria-hidden", "false");
    } else if (target && controlPane.contains(target)) {
      controlPane.inert = false;
      controlPane.setAttribute("aria-hidden", "false");
      controlPane.classList.add("android-tool-container-active");
      controlPane.style.setProperty("display", "grid", "important");
      controlPane.style.setProperty("position", "static", "important");
      controlPane.style.setProperty("inset", "auto", "important");
      controlPane.style.setProperty("transform", "none", "important");
      target.classList.add("android-tool-section-active");
      target.style.setProperty("display", "block", "important");
      target.setAttribute("aria-hidden", "false");
      const disclosure = target.querySelector("details.side-disclosure");
      if (disclosure) disclosure.open = true;
    }

    Array.from(toolsNav.querySelectorAll("button[data-android-target]")).forEach((button) => {
      if (button.dataset.androidTarget === selectedId) {
        button.setAttribute("aria-current", "page");
      } else {
        button.removeAttribute("aria-current");
      }
    });
    const selectedButton = Array.from(
      toolsNav.querySelectorAll("button[data-android-target]"),
    ).find((button) => button.dataset.androidTarget === selectedId);
    toolsNav.dataset.activeTool = selectedButton?.textContent?.trim() || selectedId;
    setPrimaryNav(selectedId === sidebarTargetId ? "projects" : "more");
  }

  function showTools(targetId = "control") {
    setDrawerOpen(false);
    body.classList.remove("android-chat-mode");
    body.classList.add("android-tools-mode");
    setHeaderButton("返回聊天", true);
    activateTool(targetId);
    window.requestAnimationFrame(() => {
      grid.scrollTop = 0;
    });
  }

  topbarButton.addEventListener("click", (event) => {
    event.preventDefault();
    if (body.classList.contains("android-tools-mode")) {
      showChat();
    } else {
      showTools();
    }
  });

  drawerToggle.addEventListener("click", (event) => {
    event.preventDefault();
    if (body.classList.contains("android-tools-mode")) showChat();
    setDrawerOpen(!body.classList.contains("android-drawer-open"));
  });

  projectDrawer.addEventListener("click", (event) => {
    if (event.target.closest("[data-android-drawer-close]")) {
      setDrawerOpen(false);
      return;
    }
    if (event.target.closest("[data-android-memory-open]")) {
      showTools("control");
      return;
    }
    if (event.target.closest("[data-android-server-settings]")) {
      window.HubApp?.openServerSettings?.();
      setDrawerOpen(false);
      return;
    }
    const manageButton = event.target.closest("button[data-android-target]");
    if (manageButton) showTools(manageButton.dataset.androidTarget || "members");
  });
  drawerScrim.addEventListener("click", () => setDrawerOpen(false));
  document.addEventListener("click", (event) => {
    if (!body.classList.contains("android-drawer-open")) return;
    const target = event.target;
    if (projectDrawer.contains(target) || drawerToggle.contains(target)) return;
    setDrawerOpen(false);
  });

  let swipeStart = null;
  shell.addEventListener("touchstart", (event) => {
    if (!body.classList.contains("android-chat-mode") || event.touches.length !== 1) {
      swipeStart = null;
      return;
    }
    const touch = event.touches[0];
    const drawerOpen = body.classList.contains("android-drawer-open");
    if (!drawerOpen && touch.clientX > 44) {
      swipeStart = null;
      return;
    }
    swipeStart = {
      x: touch.clientX,
      y: touch.clientY,
      drawerOpen,
    };
  }, { passive: true });

  shell.addEventListener("touchend", (event) => {
    if (!swipeStart || event.changedTouches.length !== 1) {
      swipeStart = null;
      return;
    }
    const touch = event.changedTouches[0];
    const dx = touch.clientX - swipeStart.x;
    const dy = touch.clientY - swipeStart.y;
    if (Math.abs(dx) > Math.abs(dy) && dx >= 64 && !swipeStart.drawerOpen) {
      setDrawerOpen(true);
    } else if (Math.abs(dx) > Math.abs(dy) && dx <= -64 && swipeStart.drawerOpen) {
      setDrawerOpen(false);
    }
    swipeStart = null;
  }, { passive: true });

  shell.addEventListener("touchcancel", () => {
    swipeStart = null;
  }, { passive: true });

  toolsNav.addEventListener("click", (event) => {
    const linkButton = event.target.closest("button[data-android-url]");
    if (linkButton) {
      window.location.assign(linkButton.dataset.androidUrl || "/");
      return;
    }
    const button = event.target.closest("button[data-android-target]");
    if (!button) return;
    event.preventDefault();
    showTools(button.dataset.androidTarget || "");
  });

  bottomNav.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-android-view]");
    if (!button) return;
    if (button.dataset.androidView === "chat") showChat();
    else if (button.dataset.androidView === "projects") showTools(sidebarTargetId);
    else showTools("control");
  });

  document.addEventListener("click", (event) => {
    const control = event.target.closest?.("button, a, summary, select, .composer-action");
    if (!control || control.disabled) return;
    window.HubMedia?.haptic?.();
  }, true);

  window.CodexHubAndroid = {
    showChat,
    showTools,
    back() {
      if (body.classList.contains("android-drawer-open")) {
        setDrawerOpen(false);
        return true;
      }
      if (window.CodexHubMediaViewer?.back?.()) return true;
      if (window.CodexHubMediaViewer?.isOpen?.()) {
        window.CodexHubMediaViewer.close?.();
        return true;
      }
      if (window.CodexHubReadingExperience?.back?.()) return true;
      if (!body.classList.contains("android-tools-mode")) return false;
      showChat();
      return true;
    },
  };

  showChat();
  if (chatWasNearBottom) {
    window.CodexHubReadingExperience?.pinToBottom?.();
  }
})();
