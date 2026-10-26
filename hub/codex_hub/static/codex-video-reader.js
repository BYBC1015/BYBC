(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.CodexVideoReader = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const VIDEO_EXTENSIONS = new Set(["mp4", "webm", "ogv", "ogg", "mov", "m4v"]);
  const VIDEO_TYPES = new Set([
    "video/mp4", "video/webm", "video/ogg", "video/quicktime", "video/x-m4v",
  ]);
  const GENERIC_TYPES = new Set(["", "application/octet-stream", "binary/octet-stream"]);
  const ORDINARY_EXTENSIONS = new Set([
    "doc", "docm", "docx", "htm", "html", "odp", "ods", "odt", "pdf",
    "ppt", "pptm", "pptx", "rtf", "svg", "xls", "xlsm", "xlsx", "xml",
  ]);
  const MAX_PROGRESS_AGE_MS = 90 * 24 * 60 * 60 * 1000;
  const preservedCards = new Map();

  function normalizeContentType(value) {
    return String(value || "").split(";", 1)[0].trim().toLowerCase();
  }

  function extension(value) {
    const clean = String(value || "").split(/[?#]/, 1)[0];
    const match = clean.match(/\.([a-z0-9]+)$/i);
    return match ? match[1].toLowerCase() : "";
  }

  function isVideoAttachment(filename, contentType) {
    const suffix = extension(filename);
    if (ORDINARY_EXTENSIONS.has(suffix)) return false;
    const mime = normalizeContentType(contentType);
    if (VIDEO_TYPES.has(mime)) return true;
    if (!GENERIC_TYPES.has(mime)) return false;
    return VIDEO_EXTENSIONS.has(suffix);
  }

  function safeMediaUrl(raw, baseUrl) {
    const value = String(raw || "").trim();
    if (!value || /[\u0000-\u001f\u007f]/.test(value)) return null;
    const scheme = value.match(/^([^/?#]*):/);
    if (scheme && !/^(https?)$/i.test(scheme[1].replace(/\s+/g, ""))) return null;
    try {
      const parsed = new URL(value, baseUrl || "https://codex-hub.invalid/");
      return parsed.protocol === "http:" || parsed.protocol === "https:" ? value : null;
    } catch (_error) {
      return null;
    }
  }

  function formatBytes(value) {
    let size = Number(value);
    if (!Number.isFinite(size) || size < 0) size = 0;
    const units = ["B", "KB", "MB", "GB", "TB"];
    let unitIndex = 0;
    while (size >= 1024 && unitIndex < units.length - 1) {
      size /= 1024;
      unitIndex += 1;
    }
    return unitIndex === 0 ? Math.round(size) + " B" : size.toFixed(1) + " " + units[unitIndex];
  }

  function availableStorage(documentRef) {
    try {
      return documentRef && documentRef.defaultView
        ? documentRef.defaultView.localStorage
        : null;
    } catch (_error) {
      return null;
    }
  }

  function stableHash(value) {
    let hash = 2166136261;
    const text = String(value || "");
    for (let index = 0; index < text.length; index += 1) {
      hash ^= text.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(36);
  }

  function progressStorageKey(identity) {
    return "codexHub.videoProgress.v1." + stableHash(identity);
  }

  function readProgress(storage, key, now) {
    if (!storage) return null;
    try {
      const value = JSON.parse(storage.getItem(key) || "null");
      if (!value || !Number.isFinite(value.time) || value.time < 0) return null;
      if (!Number.isFinite(value.updatedAt) || (now || Date.now()) - value.updatedAt > MAX_PROGRESS_AGE_MS) {
        storage.removeItem(key);
        return null;
      }
      return {
        time: value.time,
        duration: Number.isFinite(value.duration) ? value.duration : null,
        updatedAt: value.updatedAt,
      };
    } catch (_error) {
      return null;
    }
  }

  function resumePosition(progress, duration) {
    if (!progress || !Number.isFinite(progress.time) || progress.time < 5) return 0;
    const knownDuration = Number.isFinite(duration) && duration > 0 ? duration : progress.duration;
    if (Number.isFinite(knownDuration) && progress.time >= knownDuration - 10) return 0;
    return progress.time;
  }

  function writeProgress(storage, key, time, duration, now) {
    if (!storage || !Number.isFinite(time) || time < 0) return false;
    try {
      storage.setItem(key, JSON.stringify({
        time: time,
        duration: Number.isFinite(duration) ? duration : null,
        updatedAt: now || Date.now(),
      }));
      return true;
    } catch (_error) {
      return false;
    }
  }

  function appendInlineHint(raw, baseUrl) {
    try {
      const parsed = new URL(raw, baseUrl || "https://codex-hub.invalid/");
      parsed.searchParams.set("inline", "1");
      if (/^https?:\/\//i.test(raw)) return parsed.href;
      return parsed.pathname + parsed.search + parsed.hash;
    } catch (_error) {
      return raw;
    }
  }

  function modelFromLink(link, documentRef) {
    const filenameNode = link.querySelector("strong");
    const sizeNode = link.querySelector("small");
    const filename = link.dataset.fileName || (filenameNode && filenameNode.textContent.trim()) || "video";
    const contentType = normalizeContentType(link.dataset.contentType);
    if (!isVideoAttachment(filename, contentType)) return null;
    const href = safeMediaUrl(link.getAttribute("href"), documentRef.baseURI);
    if (!href) return null;
    const byteSize = Number(link.dataset.byteSize);
    return {
      id: link.dataset.videoId || href,
      filename: filename,
      contentType: contentType || null,
      href: href,
      sourceUrl: appendInlineHint(href, documentRef.baseURI),
      posterUrl: safeMediaUrl(link.dataset.posterUrl, documentRef.baseURI),
      sizeLabel: Number.isFinite(byteSize) && byteSize >= 0
        ? formatBytes(byteSize)
        : ((sizeNode && sizeNode.textContent.trim()) || "视频附件"),
    };
  }

  function createVideoCard(model, options) {
    const settings = options || {};
    const documentRef = settings.document || document;
    const storage = settings.storage === undefined
      ? availableStorage(documentRef)
      : settings.storage;
    const card = documentRef.createElement("section");
    card.className = "codex-video-card";
    card.setAttribute("data-video-ready", "true");

    const cover = documentRef.createElement("div");
    cover.className = "codex-video-cover";
    if (model.posterUrl) {
      const poster = documentRef.createElement("img");
      poster.src = model.posterUrl;
      poster.alt = "";
      poster.loading = "lazy";
      poster.decoding = "async";
      cover.appendChild(poster);
    } else {
      const placeholder = documentRef.createElement("span");
      placeholder.className = "codex-video-placeholder";
      placeholder.setAttribute("aria-hidden", "true");
      placeholder.textContent = "▶";
      cover.appendChild(placeholder);
    }

    const info = documentRef.createElement("div");
    info.className = "codex-video-info";
    const title = documentRef.createElement("strong");
    title.textContent = model.filename;
    const details = documentRef.createElement("small");
    details.textContent = [model.sizeLabel, model.contentType || "视频"].filter(Boolean).join(" · ");
    const progress = readProgress(storage, progressStorageKey(model.id));
    const savedAt = resumePosition(progress, progress && progress.duration);
    const resume = documentRef.createElement("small");
    resume.className = "codex-video-resume-label";
    resume.textContent = savedAt ? "可从 " + formatTime(savedAt) + " 继续" : "点击后才加载视频";
    info.appendChild(title);
    info.appendChild(details);
    info.appendChild(resume);

    const actions = documentRef.createElement("div");
    actions.className = "codex-video-actions";
    const loadButton = documentRef.createElement("button");
    loadButton.type = "button";
    loadButton.className = "codex-video-load";
    loadButton.textContent = savedAt ? "加载并恢复" : "加载视频";
    const download = documentRef.createElement("a");
    download.href = model.href;
    download.textContent = "下载原文件";
    actions.appendChild(loadButton);
    actions.appendChild(download);
    info.appendChild(actions);

    card.appendChild(cover);
    card.appendChild(info);

    loadButton.addEventListener("click", () => {
      if (card.querySelector("video")) return;
      const video = documentRef.createElement("video");
      video.controls = true;
      video.autoplay = false;
      video.preload = "metadata";
      video.playsInline = true;
      video.src = model.sourceUrl;
      if (model.posterUrl) video.poster = model.posterUrl;
      const key = progressStorageKey(model.id);
      let lastSavedAt = 0;
      video.addEventListener("loadedmetadata", () => {
        const current = readProgress(storage, key);
        const position = resumePosition(current, video.duration);
        if (position) video.currentTime = position;
      }, { once: true });
      const save = (force) => {
        const now = Date.now();
        if (!force && now - lastSavedAt < 5000) return;
        lastSavedAt = now;
        writeProgress(storage, key, video.currentTime, video.duration, now);
      };
      video.addEventListener("timeupdate", () => save(false));
      video.addEventListener("pause", () => save(true));
      const view = documentRef.defaultView;
      if (view) view.addEventListener("pagehide", () => save(true), { once: true });
      video.addEventListener("ended", () => {
        try { storage && storage.removeItem(key); } catch (_error) { /* storage is optional */ }
        resume.textContent = "已播放完毕";
      });
      card.replaceChildren(video, info);
      loadButton.remove();
      resume.textContent = "视频已加载，不会自动播放";
    }, { once: true });

    return card;
  }

  function formatTime(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const minutes = Math.floor(total / 60);
    const remainder = total % 60;
    return minutes + ":" + String(remainder).padStart(2, "0");
  }

  function candidates(rootNode) {
    const selector = "a[data-video-attachment], .message-attachments a.message-file";
    const result = [];
    if (!rootNode || !rootNode.querySelectorAll) return result;
    if (rootNode.matches && rootNode.matches(selector)) result.push(rootNode);
    rootNode.querySelectorAll(selector).forEach((node) => result.push(node));
    return result;
  }

  function enhanceVideos(rootNode, options) {
    const settings = options || {};
    const documentRef = settings.document || (rootNode && rootNode.ownerDocument) ||
      (typeof document !== "undefined" ? document : null);
    if (!documentRef) return [];
    const cards = [];
    candidates(rootNode || documentRef).forEach((link) => {
      if (!link.isConnected || link.dataset.videoEnhanced === "true") return;
      const model = modelFromLink(link, documentRef);
      if (!model) return;
      link.dataset.videoEnhanced = "true";
      const preserved = preservedCards.get(String(model.id));
      const card = preserved && !preserved.isConnected
        ? preserved
        : createVideoCard(model, {
          document: documentRef,
          storage: settings.storage === undefined
            ? availableStorage(documentRef)
            : settings.storage,
        });
      if (!preserved || !preserved.isConnected) preservedCards.set(String(model.id), card);
      link.replaceWith(card);
      cards.push(card);
    });
    return cards;
  }

  function init(options) {
    const settings = options || {};
    const documentRef = settings.document || (typeof document !== "undefined" ? document : null);
    if (!documentRef) return null;
    const run = (rootNode) => enhanceVideos(rootNode || documentRef, settings);
    run(documentRef);
    if (typeof MutationObserver === "undefined") return null;
    const observer = new MutationObserver((records) => {
      records.forEach((record) => record.addedNodes.forEach((node) => {
        if (node.nodeType === 1) run(node);
      }));
    });
    observer.observe(documentRef.body, { childList: true, subtree: true });
    return observer;
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => init());
    else init();
  }

  return {
    appendInlineHint,
    availableStorage,
    createVideoCard,
    enhanceVideos,
    extension,
    formatBytes,
    formatTime,
    init,
    isVideoAttachment,
    modelFromLink,
    progressStorageKey,
    readProgress,
    resumePosition,
    safeMediaUrl,
    writeProgress,
  };
});
