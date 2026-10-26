(() => {
  const stream = document.querySelector('[data-auto-refresh="chat"]');
  const shell = document.querySelector(".workbench-shell");
  if (!stream || !shell) return;

  const body = document.body;
  const projectSlug = stream.dataset.projectSlug || window.location.pathname;
  const stateKey = `codexHub.reading.v2.${projectSlug}`;
  const focusToggle = document.querySelector("[data-focus-reading-toggle]");
  const mediaLibrary = document.querySelector("[data-media-library]");
  let saveTimer = 0;
  let state = readState();
  let unreadAfter = Number(state.lastSeenEntryId || 0);

  function readState() {
    try {
      const value = JSON.parse(window.localStorage.getItem(stateKey) || "{}");
      return value && typeof value === "object" ? value : {};
    } catch {
      return {};
    }
  }

  function writeState(patch = {}) {
    state = { ...state, ...patch, updatedAt: Date.now() };
    try {
      window.localStorage.setItem(stateKey, JSON.stringify(state));
    } catch {
      // Reading remains usable when storage is unavailable.
    }
  }

  function entryElements() {
    return Array.from(stream.querySelectorAll(".chat-message[data-entry-id]"));
  }

  function hashEntry() {
    if (!window.location.hash) return null;
    try {
      const id = decodeURIComponent(window.location.hash.slice(1));
      const target = document.getElementById(id);
      return target?.classList?.contains("chat-message") ? target : null;
    } catch {
      return null;
    }
  }

  function pinToBottom(markRead = false) {
    if (markRead) {
      const latestEntry = entryElements().at(-1);
      const latestEntryId = Number(latestEntry?.dataset.entryId || 0);
      if (latestEntryId > unreadAfter) {
        unreadAfter = latestEntryId;
        writeState({ lastSeenEntryId: latestEntryId });
      }
      stream.querySelector(":scope > .unread-divider")?.remove();
    }
    const apply = () => {
      stream.scrollTop = Math.max(0, stream.scrollHeight - stream.clientHeight);
    };
    apply();
    window.requestAnimationFrame(() => {
      apply();
      window.requestAnimationFrame(apply);
    });
  }

  function scrollEntryInsideStream(entry, behavior = "auto") {
    if (!entry || !stream.contains(entry)) return;
    const streamRect = stream.getBoundingClientRect();
    const entryRect = entry.getBoundingClientRect();
    const maxScroll = Math.max(0, stream.scrollHeight - stream.clientHeight);
    const targetTop = Math.max(
      0,
      Math.min(maxScroll, stream.scrollTop + entryRect.top - streamRect.top - 16),
    );
    if (typeof stream.scrollTo === "function") {
      stream.scrollTo({ top: targetTop, behavior });
    } else {
      stream.scrollTop = targetTop;
    }
  }

  function highestVisibleEntryId() {
    const streamRect = stream.getBoundingClientRect();
    let highest = Number(state.lastSeenEntryId || 0);
    for (const entry of entryElements()) {
      const rect = entry.getBoundingClientRect();
      if (rect.top < streamRect.bottom - 12 && rect.bottom > streamRect.top + 12) {
        highest = Math.max(highest, Number(entry.dataset.entryId || 0));
      }
    }
    return highest;
  }

  function saveReadingPosition() {
    window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(() => {
      const latestVisible = highestVisibleEntryId();
      if (latestVisible <= unreadAfter) return;
      unreadAfter = latestVisible;
      writeState({ lastSeenEntryId: latestVisible });
    }, 140);
  }

  function setInitialReadingPosition() {
    const hashTarget = hashEntry();
    if (hashTarget) {
      window.scrollTo({ left: 0, top: 0, behavior: "auto" });
      scrollEntryInsideStream(hashTarget);
      hashTarget.focus({ preventScroll: true });
      return;
    }
    pinToBottom();
  }

  function addUnreadDivider() {
    const existing = stream.querySelector(":scope > .unread-divider");
    const firstUnread = unreadAfter
      ? entryElements().find((entry) => Number(entry.dataset.entryId || 0) > unreadAfter)
      : null;
    if (!firstUnread) {
      existing?.remove();
      return;
    }
    if (existing?.nextElementSibling === firstUnread) return;
    existing?.remove();
    const next = document.createElement("div");
    next.className = "unread-divider";
    next.setAttribute("role", "separator");
    next.textContent = "以下为未读消息";
    firstUnread.before(next);
  }

  function setFocusMode(enabled) {
    body.classList.toggle("focus-reading-mode", enabled);
    focusToggle?.setAttribute("aria-pressed", String(enabled));
    const label = focusToggle?.querySelector(".reader-focus-label");
    if (label) label.textContent = enabled ? "退出" : "专注";
    if (focusToggle) {
      const accessibleLabel = enabled ? "退出专注阅读" : "进入专注阅读";
      focusToggle.setAttribute("aria-label", accessibleLabel);
      focusToggle.title = accessibleLabel;
    }
  }

  function videoProgress() {
    return state.videoProgress && typeof state.videoProgress === "object"
      ? state.videoProgress
      : {};
  }

  function loadVideo(trigger) {
    const card = trigger.closest("[data-video-card]");
    if (!card || card.querySelector("video")) return;
    const source = trigger.dataset.videoSrc;
    if (!source) return;
    trigger.classList.add("is-loading");
    trigger.setAttribute("aria-busy", "true");
    const video = document.createElement("video");
    const mediaId = String(trigger.dataset.mediaId || source);
    video.controls = true;
    video.preload = "metadata";
    video.playsInline = true;
    video.src = source;
    video.dataset.mediaId = mediaId;
    video.addEventListener("loadedmetadata", () => {
      const saved = Number(videoProgress()[mediaId] || 0);
      if (saved > 0 && saved < video.duration - 1) video.currentTime = saved;
      trigger.replaceWith(video);
      video.focus();
    }, { once: true });
    video.addEventListener("error", () => {
      trigger.classList.remove("is-loading");
      trigger.removeAttribute("aria-busy");
      const status = trigger.querySelector("[data-video-status]");
      if (status) status.textContent = "视频加载失败，可使用下载按钮打开";
    }, { once: true });
    video.addEventListener("timeupdate", () => {
      const progress = videoProgress();
      progress[mediaId] = Math.floor(video.currentTime || 0);
      writeState({ videoProgress: progress });
    });
  }

  function copyText(text, button, successLabel = "已复制") {
    const original = button?.textContent || "复制";
    const fallback = () => {
      const area = document.createElement("textarea");
      area.value = text;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      const copied = document.execCommand("copy");
      area.remove();
      return copied;
    };
    const clipboardWrite = navigator.clipboard?.writeText
      ? navigator.clipboard.writeText(text)
      : Promise.reject(new Error("clipboard unavailable"));
    Promise.resolve(clipboardWrite)
      .catch(() => {
        if (!fallback()) throw new Error("copy failed");
      })
      .then(() => {
        if (!button) return;
        button.textContent = successLabel;
        window.setTimeout(() => { button.textContent = original; }, 1400);
      });
  }

  function copyCode(button) {
    const code = button.closest(".code-block")?.querySelector("code")
      || button.closest("pre")?.querySelector("code");
    if (code) copyText(code.textContent || "", button);
  }

  function mediaMatches(card) {
    if (!mediaLibrary) return true;
    const activeCategory = mediaLibrary.querySelector("[data-media-category][aria-pressed='true']")
      ?.dataset.mediaCategory || "all";
    const device = mediaLibrary.querySelector("[data-media-device]")?.value || "";
    const batch = (mediaLibrary.querySelector("[data-media-batch]")?.value || "").trim().toLowerCase();
    const message = (mediaLibrary.querySelector("[data-media-message]")?.value || "").trim();
    const range = mediaLibrary.querySelector("[data-media-time]")?.value || "all";
    if (activeCategory !== "all" && card.dataset.mediaType !== activeCategory) return false;
    if (device && card.dataset.device !== device) return false;
    if (batch && !(card.dataset.batch || "").toLowerCase().includes(batch)) return false;
    if (message && card.dataset.entryId !== message.replace(/^#/, "")) return false;
    if (range !== "all") {
      const created = Date.parse(card.dataset.createdAt || "");
      const days = Number(range);
      if (!created || Date.now() - created > days * 86400000) return false;
    }
    return true;
  }

  function filterMediaLibrary() {
    if (!mediaLibrary) return;
    let shown = 0;
    mediaLibrary.querySelectorAll("[data-media-library-item]").forEach((card) => {
      card.hidden = !mediaMatches(card);
      if (!card.hidden) shown += 1;
    });
    const count = mediaLibrary.querySelector("[data-media-filter-count]");
    if (count) count.textContent = `${shown} 项`;
  }

  function markLoadedImages(root = document) {
    root.querySelectorAll("img[data-media-image-preview]").forEach((image) => {
      const done = () => image.closest(".media-image-frame")?.classList.add("is-loaded");
      if (image.complete) done();
      else image.addEventListener("load", done, { once: true });
    });
  }

  focusToggle?.addEventListener("click", () => {
    setFocusMode(!body.classList.contains("focus-reading-mode"));
  });

  stream.addEventListener("scroll", saveReadingPosition, { passive: true });
  stream.addEventListener("click", (event) => {
    const videoButton = event.target.closest("[data-video-load]");
    if (videoButton) {
      event.preventDefault();
      loadVideo(videoButton);
      return;
    }
    const copyButton = event.target.closest("[data-copy-code]");
    if (copyButton) {
      event.preventDefault();
      copyCode(copyButton);
      return;
    }
  });

  mediaLibrary?.addEventListener("click", (event) => {
    const category = event.target.closest("[data-media-category]");
    if (category) {
      mediaLibrary.querySelectorAll("[data-media-category]").forEach((button) => {
        button.setAttribute("aria-pressed", String(button === category));
      });
      filterMediaLibrary();
      return;
    }
    const videoButton = event.target.closest("[data-video-load]");
    if (videoButton) loadVideo(videoButton);
  });
  mediaLibrary?.addEventListener("input", filterMediaLibrary);
  mediaLibrary?.addEventListener("change", filterMediaLibrary);

  window.addEventListener("popstate", (event) => {
    if (Number.isFinite(Number(event.state?.readingScroll))) {
      stream.scrollTop = Number(event.state.readingScroll);
    }
  });
  window.addEventListener("pagehide", () => {
    window.clearTimeout(saveTimer);
    const latestVisible = highestVisibleEntryId();
    if (latestVisible > unreadAfter) {
      unreadAfter = latestVisible;
      writeState({ lastSeenEntryId: latestVisible });
    }
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && body.classList.contains("focus-reading-mode")) {
      setFocusMode(false);
    }
  });

  const observer = new MutationObserver((records) => {
    const added = records.some((record) => Array.from(record.addedNodes).some(
      (node) => !(node.nodeType === Node.ELEMENT_NODE && node.classList.contains("unread-divider")),
    ));
    if (!added) return;
    markLoadedImages(stream);
    addUnreadDivider();
  });
  observer.observe(stream, { childList: true, subtree: true });

  window.CodexHubReadingExperience = {
    back() {
      if (!body.classList.contains("focus-reading-mode")) return false;
      setFocusMode(false);
      return true;
    },
    pinToBottom,
    save: saveReadingPosition,
  };

  setFocusMode(false);
  addUnreadDivider();
  markLoadedImages();
  filterMediaLibrary();
  window.requestAnimationFrame(setInitialReadingPosition);
})();
