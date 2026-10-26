(function imageViewerModule(global) {
  "use strict";

  const MIN_SCALE = 1;
  const MAX_SCALE = 6;
  const SWIPE_SETTLE_MS = 240;
  const IMAGE_LINK_SELECTOR = ".message-image-link";

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  function clampZoom(value) {
    return clamp(Number(value) || MIN_SCALE, MIN_SCALE, MAX_SCALE);
  }

  function scopedItems(items, entryId, scope) {
    if (scope !== "message") return items.slice();
    return items.filter((item) => item.entryId === entryId);
  }

  function adjacentIndexes(length, index) {
    const result = [];
    if (index > 0) result.push(index - 1);
    if (index + 1 < length) result.push(index + 1);
    return result;
  }

  function deriveSwipeAction({ dx, dy, scale = MIN_SCALE, viewportWidth = 0 }) {
    if (scale > MIN_SCALE + 0.01) return "none";
    const horizontalThreshold = Math.max(48, Math.min(96, viewportWidth * 0.14));
    const verticalThreshold = Math.max(72, Math.min(128, viewportWidth * 0.2));
    if (dy > verticalThreshold && dy > Math.abs(dx) * 1.1) return "close";
    if (Math.abs(dx) > horizontalThreshold && Math.abs(dx) > Math.abs(dy) * 1.1) {
      return dx < 0 ? "next" : "previous";
    }
    return "none";
  }

  const testApi = {
    MIN_SCALE,
    MAX_SCALE,
    adjacentIndexes,
    clampZoom,
    deriveSwipeAction,
    scopedItems,
  };

  if (typeof module === "object" && module.exports) {
    module.exports = testApi;
  }
  if (typeof document === "undefined") return;

  const scriptElement =
    document.currentScript || document.querySelector('script[src*="image-viewer.js"]');
  let dialog = null;
  let elements = null;
  let gridRefreshFrame = 0;
  let closeTimer = 0;

  const state = {
    allItems: [],
    chatRoot: null,
    closeRestoreTarget: null,
    dragX: 0,
    dragY: 0,
    entryId: "",
    gesture: null,
    index: 0,
    isOpen: false,
    items: [],
    lastTap: null,
    lockedBodyStyle: null,
    panX: 0,
    panY: 0,
    pinch: null,
    pointers: new Map(),
    preloads: [],
    preferredScope: "conversation",
    scale: MIN_SCALE,
    scope: "conversation",
    scrollSnapshots: [],
    isSettling: false,
    settleTimer: 0,
    sourceItem: null,
  };

  function installStylesheet() {
    if (document.querySelector('link[data-image-viewer-styles]')) return;
    const source = scriptElement && scriptElement.src;
    let href;
    if (source) {
      const sourceUrl = new URL(source);
      const stylesheetUrl = new URL("image-viewer.css", sourceUrl);
      stylesheetUrl.search = sourceUrl.search;
      href = stylesheetUrl.href;
    } else {
      href = new URL("static/image-viewer.css", document.baseURI).href;
    }
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = href;
    link.dataset.imageViewerStyles = "";
    document.head.append(link);
  }

  function createDialog() {
    if (dialog) return;
    const root = document.createElement("div");
    root.className = "iv-root";
    root.dataset.imageViewer = "";
    root.hidden = true;
    root.innerHTML = `
      <div class="iv-backdrop" data-iv-close aria-hidden="true"></div>
      <section class="iv-dialog" role="dialog" aria-modal="true" aria-labelledby="iv-filename" aria-describedby="iv-source">
        <header class="iv-toolbar">
          <div class="iv-heading">
            <span class="iv-counter" data-iv-counter aria-live="polite"></span>
            <strong class="iv-filename" id="iv-filename" data-iv-filename></strong>
            <span class="iv-source" id="iv-source" data-iv-source></span>
          </div>
          <div class="iv-actions">
            <a class="iv-action" data-iv-download download>
              <span aria-hidden="true">↓</span><span>下载</span>
            </a>
            <button class="iv-action" type="button" data-iv-download-all>
              <span aria-hidden="true">⇩</span><span>批量下载</span>
            </button>
            <button class="iv-action" type="button" data-iv-share>
              <span aria-hidden="true">↗</span><span>分享</span>
            </button>
            <button class="iv-action iv-close" type="button" data-iv-close aria-label="关闭图片阅读器">
              <span aria-hidden="true">×</span>
            </button>
          </div>
        </header>
        <div class="iv-scope" role="group" aria-label="图片范围">
          <button type="button" data-iv-scope="message">本条消息</button>
          <button type="button" data-iv-scope="conversation">当前会话</button>
        </div>
        <div class="iv-canvas" data-iv-canvas tabindex="-1">
          <button class="iv-nav iv-previous" type="button" data-iv-previous aria-label="上一张图片">‹</button>
          <div class="iv-image-shell" data-iv-image-shell data-loading="false">
            <div class="iv-image-track" data-iv-image-track>
              <div class="iv-slide" data-iv-slide="previous" aria-hidden="true"></div>
              <div class="iv-slide" data-iv-slide="current">
                <img class="iv-image" data-iv-image alt="" draggable="false">
              </div>
              <div class="iv-slide" data-iv-slide="next" aria-hidden="true"></div>
            </div>
          </div>
          <button class="iv-nav iv-next" type="button" data-iv-next aria-label="下一张图片">›</button>
          <output class="iv-zoom" data-iv-zoom aria-live="polite">100%</output>
        </div>
        <footer class="iv-footer">
          <div class="iv-filmstrip" data-iv-filmstrip role="list" aria-label="图片缩略图"></div>
          <p class="iv-help">方向键切换 · 滚轮缩放 · Esc 关闭</p>
        </footer>
      </section>
      <div class="iv-toast" data-iv-toast role="status" aria-live="polite"></div>
    `;
    document.body.append(root);
    dialog = root;
    elements = {
      canvas: root.querySelector("[data-iv-canvas]"),
      counter: root.querySelector("[data-iv-counter]"),
      download: root.querySelector("[data-iv-download]"),
      downloadAll: root.querySelector("[data-iv-download-all]"),
      filename: root.querySelector("[data-iv-filename]"),
      filmstrip: root.querySelector("[data-iv-filmstrip]"),
      image: root.querySelector("[data-iv-image]"),
      imageShell: root.querySelector("[data-iv-image-shell]"),
      imageTrack: root.querySelector("[data-iv-image-track]"),
      next: root.querySelector("[data-iv-next]"),
      nextSlide: root.querySelector('[data-iv-slide="next"]'),
      previous: root.querySelector("[data-iv-previous]"),
      previousSlide: root.querySelector('[data-iv-slide="previous"]'),
      share: root.querySelector("[data-iv-share]"),
      source: root.querySelector("[data-iv-source]"),
      toast: root.querySelector("[data-iv-toast]"),
      zoom: root.querySelector("[data-iv-zoom]"),
    };

    root.addEventListener("click", handleDialogClick);
    elements.canvas.addEventListener("wheel", handleWheel, { passive: false });
    elements.canvas.addEventListener("dblclick", handleDoubleClick);
    elements.canvas.addEventListener("pointerdown", handlePointerDown);
    elements.canvas.addEventListener("pointermove", handlePointerMove);
    elements.canvas.addEventListener("pointerup", handlePointerEnd);
    elements.canvas.addEventListener("pointercancel", handlePointerEnd);
    elements.image.addEventListener("load", handleImageLoad);
    elements.image.addEventListener("error", handleImageError);
  }

  function filenameFromLink(link, image) {
    const explicit = (image.alt || "").trim();
    if (explicit) return explicit;
    try {
      const lastSegment = new URL(link.href).pathname.split("/").filter(Boolean).pop();
      return decodeURIComponent(lastSegment || "图片");
    } catch {
      return "图片";
    }
  }

  function itemFromLink(link) {
    const image = link.querySelector("img");
    if (!image) return null;
    const message = link.closest(".chat-message");
    const source = message && message.querySelector(".chat-author strong");
    return {
      downloadUrl: link.dataset.mediaDownload || link.href,
      entryId: link.dataset.mediaEntryId || (message && message.dataset.entryId) || "message",
      filename: filenameFromLink(link, image),
      image,
      link,
      mediaId: link.dataset.mediaId || "",
      scopeId: link.dataset.mediaScope || "",
      source: link.dataset.mediaSource || (source && source.textContent.trim()) || "消息附件",
      src: link.dataset.mediaPreview || link.dataset.mediaSrc || image.currentSrc || image.src,
      thumbSrc: link.dataset.mediaThumb || image.currentSrc || image.src,
    };
  }

  function collectItems(chatRoot) {
    if (!chatRoot) return [];
    const seen = new Set();
    return Array.from(chatRoot.querySelectorAll(IMAGE_LINK_SELECTOR))
      .map(itemFromLink)
      .filter((item) => {
        if (!item) return false;
        const key = item.mediaId || item.downloadUrl;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      });
  }

  function decorateImageGrids(root = document) {
    root.querySelectorAll(".message-attachments").forEach((container) => {
      const imageLinks = Array.from(container.children).filter((child) =>
        child.matches && child.matches(IMAGE_LINK_SELECTOR),
      );
      const isGrid = imageLinks.length > 1;
      container.classList.toggle("image-viewer-grid", isGrid);
      if (isGrid) {
        container.dataset.imageCount = String(imageLinks.length);
      } else {
        delete container.dataset.imageCount;
      }
    });
  }

  function scheduleGridRefresh() {
    if (gridRefreshFrame) return;
    gridRefreshFrame = global.requestAnimationFrame(() => {
      gridRefreshFrame = 0;
      decorateImageGrids();
    });
  }

  function captureScrollState(target) {
    const snapshots = [
      { element: global, left: global.scrollX, top: global.scrollY },
    ];
    let parent = target && target.parentElement;
    while (parent && parent !== document.body) {
      if (
        parent.scrollHeight > parent.clientHeight ||
        parent.scrollWidth > parent.clientWidth
      ) {
        snapshots.push({ element: parent, left: parent.scrollLeft, top: parent.scrollTop });
      }
      parent = parent.parentElement;
    }
    return snapshots;
  }

  function restoreScrollState() {
    state.scrollSnapshots.forEach(({ element, left, top }) => {
      if (element === global) {
        global.scrollTo(left, top);
      } else if (element && element.isConnected) {
        element.scrollLeft = left;
        element.scrollTop = top;
      }
    });
  }

  function lockPage() {
    state.lockedBodyStyle = document.body.getAttribute("style");
    document.body.classList.add("image-viewer-open");
    document.body.style.position = "fixed";
    document.body.style.inset = `${-global.scrollY}px 0 0 ${-global.scrollX}px`;
    document.body.style.width = "100%";
  }

  function unlockPage() {
    if (state.lockedBodyStyle === null) {
      document.body.removeAttribute("style");
    } else {
      document.body.setAttribute("style", state.lockedBodyStyle);
    }
    document.body.classList.remove("image-viewer-open");
    restoreScrollState();
  }

  function resolveReturnTarget() {
    if (state.closeRestoreTarget && state.closeRestoreTarget.isConnected) {
      return state.closeRestoreTarget;
    }
    const match = collectItems(state.chatRoot).find(
      (item) =>
        item.entryId === (state.sourceItem && state.sourceItem.entryId) &&
        item.downloadUrl === (state.sourceItem && state.sourceItem.downloadUrl),
    );
    return match && match.link;
  }

  function setScope(scope, activeDownloadUrl, activeMediaId = "") {
    const normalizedScope = scope === "message" ? "message" : "conversation";
    const current = state.items[state.index];
    state.allItems = collectItems(state.chatRoot);
    state.scope = normalizedScope;
    state.preferredScope = normalizedScope;
    if (normalizedScope === "message" && current) state.entryId = current.entryId;
    state.items = scopedItems(state.allItems, state.entryId, normalizedScope);
    const wantedUrl = activeDownloadUrl || (current && current.downloadUrl);
    const wantedEntry = activeDownloadUrl
      ? (state.sourceItem && state.sourceItem.entryId) || state.entryId
      : (current && current.entryId) || state.entryId;
    const wantedMediaId =
      activeMediaId || (!activeDownloadUrl && current && current.mediaId) || "";
    const nextIndex = state.items.findIndex(
      (item) =>
        (wantedMediaId && item.mediaId === wantedMediaId) ||
        (item.downloadUrl === wantedUrl && item.entryId === wantedEntry),
    );
    state.index = nextIndex >= 0 ? nextIndex : 0;
    dialog.querySelectorAll("[data-iv-scope]").forEach((button) => {
      const isActive = button.dataset.ivScope === normalizedScope;
      button.classList.toggle("is-active", isActive);
      button.setAttribute("aria-pressed", String(isActive));
    });
    renderCurrentImage();
  }

  function resetGestureState() {
    global.clearTimeout(state.settleTimer);
    state.dragX = 0;
    state.dragY = 0;
    state.gesture = null;
    state.isSettling = false;
    state.pinch = null;
    state.pointers.clear();
    elements?.imageTrack?.classList.remove("is-animated");
  }

  function open(link) {
    const sourceItem = itemFromLink(link);
    if (!sourceItem) return;
    installStylesheet();
    createDialog();
    global.clearTimeout(closeTimer);
    state.chatRoot =
      link.closest('[data-auto-refresh="chat"], [data-media-library]') ||
      link.closest(".message-attachments") ||
      document;
    state.sourceItem = sourceItem;
    state.entryId = sourceItem.entryId;
    state.closeRestoreTarget = link;
    state.scrollSnapshots = captureScrollState(link);
    state.isOpen = true;
    resetGestureState();
    link.classList.add("is-viewer-source");
    link.setAttribute("aria-expanded", "true");
    lockPage();
    dialog.hidden = false;
    dialog.classList.remove("is-closing");
    global.requestAnimationFrame(() => dialog.classList.add("is-open"));
    setScope(state.preferredScope, sourceItem.downloadUrl, sourceItem.mediaId);
    elements.canvas.focus({ preventScroll: true });
  }

  function close() {
    if (!state.isOpen) return;
    state.isOpen = false;
    resetGestureState();
    dialog.classList.remove("is-open");
    dialog.classList.add("is-closing");
    state.closeRestoreTarget?.classList.remove("is-viewer-source");
    state.closeRestoreTarget?.removeAttribute("aria-expanded");
    unlockPage();
    const returnTarget = resolveReturnTarget();
    global.requestAnimationFrame(() => {
      restoreScrollState();
      if (returnTarget) {
        returnTarget.focus({ preventScroll: true });
        restoreScrollState();
      }
    });
    closeTimer = global.setTimeout(() => {
      dialog.hidden = true;
      dialog.classList.remove("is-closing");
      elements.image.removeAttribute("src");
    }, 180);
  }

  function renderFilmstrip() {
    elements.filmstrip.replaceChildren();
    state.items.forEach((item, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "iv-thumbnail";
      button.dataset.ivIndex = String(index);
      button.setAttribute("role", "listitem");
      button.setAttribute("aria-label", `查看第 ${index + 1} 张：${item.filename}`);
      if (index === state.index) {
        button.classList.add("is-active");
        button.setAttribute("aria-current", "true");
      }
      const image = document.createElement("img");
      image.src = item.thumbSrc || item.src;
      image.alt = "";
      image.loading = "lazy";
      image.draggable = false;
      button.append(image);
      elements.filmstrip.append(button);
    });
    const activeThumbnail = elements.filmstrip.querySelector(".is-active");
    activeThumbnail?.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
  }

  function renderAdjacentSlide(slide, item) {
    slide.replaceChildren();
    if (!item) return;
    const image = document.createElement("img");
    image.className = "iv-adjacent-image";
    image.src = item.src;
    image.alt = "";
    image.decoding = "async";
    image.draggable = false;
    slide.append(image);
  }

  function renderAdjacentSlides() {
    renderAdjacentSlide(elements.previousSlide, state.items[state.index - 1]);
    renderAdjacentSlide(elements.nextSlide, state.items[state.index + 1]);
  }

  function preloadNeighbors() {
    state.preloads = adjacentIndexes(state.items.length, state.index).map((index) => {
      const preload = new Image();
      preload.decoding = "async";
      preload.src = state.items[index].src;
      return preload;
    });
  }

  function resetZoom(animate = false) {
    state.scale = MIN_SCALE;
    state.panX = 0;
    state.panY = 0;
    state.dragX = 0;
    state.dragY = 0;
    applyTransform(animate);
  }

  function renderCurrentImage() {
    const item = state.items[state.index];
    if (!item) {
      close();
      return;
    }
    resetZoom(false);
    elements.counter.textContent = `${state.index + 1} / ${state.items.length}`;
    elements.filename.textContent = item.filename;
    elements.source.textContent = `来自 ${item.source}`;
    elements.download.href = item.downloadUrl;
    elements.download.setAttribute("download", item.filename);
    elements.previous.disabled = state.index <= 0;
    elements.next.disabled = state.index >= state.items.length - 1;
    elements.imageShell.dataset.loading = "true";
    elements.image.alt = item.filename;
    elements.image.src = item.src;
    try {
      global.localStorage.setItem(
        `codexHub.lastImage.${global.location.pathname}`,
        JSON.stringify({
          entryId: item.entryId,
          mediaId: item.mediaId || item.downloadUrl,
          scope: state.scope,
          updatedAt: Date.now(),
        }),
      );
    } catch {
      // Remembering the last image is optional when storage is unavailable.
    }
    if (elements.image.complete) global.queueMicrotask(handleImageLoad);
    renderAdjacentSlides();
    renderFilmstrip();
    preloadNeighbors();
  }

  function showAt(index) {
    if (!state.items.length || state.isSettling) return;
    const nextIndex = clamp(index, 0, state.items.length - 1);
    if (nextIndex === state.index) {
      resetZoom(true);
      return;
    }
    state.index = nextIndex;
    renderCurrentImage();
  }

  function showNext() {
    if (state.index < state.items.length - 1) {
      settleSwipe(1);
    } else {
      applyTransform(true);
    }
  }

  function showPrevious() {
    if (state.index > 0) {
      settleSwipe(-1);
    } else {
      applyTransform(true);
    }
  }

  function panBounds(scale = state.scale) {
    const imageWidth = elements.image.offsetWidth || 0;
    const imageHeight = elements.image.offsetHeight || 0;
    const viewportWidth = elements.imageShell.clientWidth || 0;
    const viewportHeight = elements.imageShell.clientHeight || 0;
    return {
      x: Math.max(0, (imageWidth * scale - viewportWidth) / 2),
      y: Math.max(0, (imageHeight * scale - viewportHeight) / 2),
    };
  }

  function clampPan() {
    if (state.scale <= MIN_SCALE + 0.01) {
      state.panX = 0;
      state.panY = 0;
      return;
    }
    const bounds = panBounds();
    state.panX = clamp(state.panX, -bounds.x, bounds.x);
    state.panY = clamp(state.panY, -bounds.y, bounds.y);
  }

  function applyTransform(animate = false) {
    clampPan();
    elements.image.classList.toggle("is-animated", animate);
    elements.imageTrack.classList.toggle("is-animated", animate);
    elements.image.style.setProperty("--iv-scale", String(state.scale));
    elements.image.style.setProperty("--iv-pan-x", `${state.panX}px`);
    elements.image.style.setProperty("--iv-pan-y", `${state.panY}px`);
    elements.image.style.setProperty("--iv-drag-x", "0px");
    elements.image.style.setProperty("--iv-drag-y", `${state.dragY}px`);
    elements.imageTrack.style.setProperty(
      "--iv-track-x",
      `${state.scale <= MIN_SCALE + 0.01 ? state.dragX : 0}px`,
    );
    elements.zoom.value = `${Math.round(state.scale * 100)}%`;
    elements.zoom.textContent = `${Math.round(state.scale * 100)}%`;
    elements.canvas.classList.toggle("is-zoomed", state.scale > MIN_SCALE + 0.01);
    if (animate) {
      global.setTimeout(() => {
        if (state.isSettling) return;
        elements.image.classList.remove("is-animated");
        elements.imageTrack.classList.remove("is-animated");
      }, SWIPE_SETTLE_MS);
    }
  }

  function settleSwipe(direction) {
    if (state.isSettling || !direction) return;
    const nextIndex = state.index + direction;
    if (nextIndex < 0 || nextIndex >= state.items.length) {
      state.dragX = 0;
      state.dragY = 0;
      applyTransform(true);
      return;
    }
    if (state.scale > MIN_SCALE + 0.01) {
      showAt(nextIndex);
      return;
    }
    state.isSettling = true;
    state.dragY = 0;
    state.dragX = -direction * Math.max(1, elements.imageShell.clientWidth);
    applyTransform(true);
    state.settleTimer = global.setTimeout(() => {
      elements.image.classList.remove("is-animated");
      elements.imageTrack.classList.remove("is-animated");
      state.index = nextIndex;
      state.dragX = 0;
      state.dragY = 0;
      state.isSettling = false;
      renderCurrentImage();
    }, SWIPE_SETTLE_MS);
  }

  function setZoom(nextScale, clientX, clientY, animate = false) {
    const previousScale = state.scale;
    const next = clampZoom(nextScale);
    if (Math.abs(next - previousScale) < 0.001) return;
    const rect = elements.imageShell.getBoundingClientRect();
    const pointX = Number.isFinite(clientX) ? clientX : rect.left + rect.width / 2;
    const pointY = Number.isFinite(clientY) ? clientY : rect.top + rect.height / 2;
    const relativeX = pointX - (rect.left + rect.width / 2);
    const relativeY = pointY - (rect.top + rect.height / 2);
    const ratio = next / previousScale;
    state.panX = relativeX - (relativeX - state.panX) * ratio;
    state.panY = relativeY - (relativeY - state.panY) * ratio;
    state.scale = next;
    if (next <= MIN_SCALE + 0.01) {
      state.panX = 0;
      state.panY = 0;
    }
    applyTransform(animate);
  }

  function toggleZoom(clientX, clientY) {
    if (state.scale > MIN_SCALE + 0.01) {
      resetZoom(true);
    } else {
      setZoom(2.5, clientX, clientY, true);
    }
  }

  function handleImageLoad() {
    elements.imageShell.dataset.loading = "false";
    applyTransform(false);
  }

  function handleImageError() {
    elements.imageShell.dataset.loading = "false";
    showToast("图片加载失败，可尝试下载原文件");
  }

  function handleWheel(event) {
    if (!state.isOpen) return;
    event.preventDefault();
    const multiplier = event.deltaY < 0 ? 1.18 : 0.85;
    setZoom(state.scale * multiplier, event.clientX, event.clientY);
  }

  function handleDoubleClick(event) {
    if (event.pointerType === "touch") return;
    event.preventDefault();
    toggleZoom(event.clientX, event.clientY);
  }

  function pointerDistance(first, second) {
    return Math.hypot(second.x - first.x, second.y - first.y);
  }

  function pointerCenter(first, second) {
    return { x: (first.x + second.x) / 2, y: (first.y + second.y) / 2 };
  }

  function startPinch() {
    const points = Array.from(state.pointers.values());
    if (points.length < 2) return;
    const center = pointerCenter(points[0], points[1]);
    const rect = elements.imageShell.getBoundingClientRect();
    state.pinch = {
      center,
      contentX: (center.x - (rect.left + rect.width / 2) - state.panX) / state.scale,
      contentY: (center.y - (rect.top + rect.height / 2) - state.panY) / state.scale,
      distance: Math.max(1, pointerDistance(points[0], points[1])),
      panX: state.panX,
      panY: state.panY,
      scale: state.scale,
    };
  }

  function handlePointerDown(event) {
    if (!state.isOpen || state.isSettling || event.target.closest("button, a")) return;
    if (event.pointerType === "mouse" && event.button !== 0) return;
    elements.canvas.setPointerCapture?.(event.pointerId);
    state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (state.pointers.size === 1) {
      state.gesture = {
        axis: "",
        moved: false,
        panX: state.panX,
        panY: state.panY,
        pointerId: event.pointerId,
        startTime: Date.now(),
        x: event.clientX,
        y: event.clientY,
      };
    } else if (state.pointers.size === 2) {
      startPinch();
    }
  }

  function handlePointerMove(event) {
    if (!state.pointers.has(event.pointerId)) return;
    event.preventDefault();
    state.pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (state.pointers.size >= 2 && state.pinch) {
      const points = Array.from(state.pointers.values());
      const center = pointerCenter(points[0], points[1]);
      const rect = elements.imageShell.getBoundingClientRect();
      state.scale = clampZoom(
        state.pinch.scale *
          (pointerDistance(points[0], points[1]) / state.pinch.distance),
      );
      state.panX =
        center.x -
        (rect.left + rect.width / 2) -
        state.pinch.contentX * state.scale;
      state.panY =
        center.y -
        (rect.top + rect.height / 2) -
        state.pinch.contentY * state.scale;
      state.dragX = 0;
      state.dragY = 0;
      applyTransform(false);
      return;
    }
    if (!state.gesture || state.gesture.pointerId !== event.pointerId) return;
    const dx = event.clientX - state.gesture.x;
    const dy = event.clientY - state.gesture.y;
    state.gesture.moved = state.gesture.moved || Math.hypot(dx, dy) > 8;
    if (state.scale > MIN_SCALE + 0.01) {
      state.panX = state.gesture.panX + dx;
      state.panY = state.gesture.panY + dy;
      state.dragX = 0;
      state.dragY = 0;
    } else {
      if (!state.gesture.axis && Math.hypot(dx, dy) > 8) {
        state.gesture.axis = Math.abs(dx) >= Math.abs(dy) ? "horizontal" : "vertical";
      }
      if (state.gesture.axis === "horizontal") {
        const isPastStart = state.index === 0 && dx > 0;
        const isPastEnd = state.index === state.items.length - 1 && dx < 0;
        state.dragX = isPastStart || isPastEnd ? dx * 0.28 : dx;
        state.dragY = 0;
      } else if (state.gesture.axis === "vertical") {
        state.dragX = 0;
        state.dragY = dy > 0 ? dy : dy * 0.28;
      }
    }
    applyTransform(false);
  }

  function handlePointerEnd(event) {
    if (!state.pointers.has(event.pointerId)) return;
    const point = state.pointers.get(event.pointerId);
    const endedGesture = state.gesture && state.gesture.pointerId === event.pointerId
      ? { ...state.gesture }
      : null;
    state.pointers.delete(event.pointerId);
    try {
      elements.canvas.releasePointerCapture?.(event.pointerId);
    } catch {
      // Pointer capture may already be released by the browser during cancellation.
    }

    if (state.pinch) {
      state.pinch = null;
      clampPan();
      applyTransform(true);
      if (state.pointers.size === 1) {
        const [pointerId, remaining] = Array.from(state.pointers.entries())[0];
        state.gesture = {
          moved: true,
          panX: state.panX,
          panY: state.panY,
          pointerId,
          startTime: Date.now(),
          x: remaining.x,
          y: remaining.y,
        };
      } else {
        state.gesture = null;
      }
      return;
    }

    if (!endedGesture) return;
    const dx = point.x - endedGesture.x;
    const dy = point.y - endedGesture.y;
    const action = event.type === "pointercancel" ? "none" : deriveSwipeAction({
      dx,
      dy,
      scale: state.scale,
      viewportWidth: elements.canvas.clientWidth,
    });
    state.gesture = null;
    if (action === "next") {
      settleSwipe(1);
    } else if (action === "previous") {
      settleSwipe(-1);
    } else if (action === "close") {
      close();
    } else {
      state.dragX = 0;
      state.dragY = 0;
      applyTransform(true);
    }

    const isTap =
      event.pointerType === "touch" &&
      Date.now() - endedGesture.startTime < 280 &&
      Math.hypot(dx, dy) < 12;
    if (isTap) {
      const previousTap = state.lastTap;
      const now = Date.now();
      if (
        previousTap &&
        now - previousTap.time < 320 &&
        Math.hypot(point.x - previousTap.x, point.y - previousTap.y) < 36
      ) {
        state.lastTap = null;
        toggleZoom(point.x, point.y);
      } else {
        state.lastTap = { time: now, x: point.x, y: point.y };
      }
    }
  }

  function showToast(message) {
    elements.toast.textContent = message;
    elements.toast.classList.add("is-visible");
    global.setTimeout(() => elements.toast.classList.remove("is-visible"), 1800);
  }

  async function copyShareLink(item) {
    try {
      await navigator.clipboard.writeText(item.downloadUrl);
      showToast("图片链接已复制");
    } catch {
      showToast("当前环境不支持系统分享");
    }
  }

  async function shareCurrent() {
    const item = state.items[state.index];
    if (!item) return;
    if (global.HubMedia && typeof global.HubMedia.share === "function") {
      global.HubMedia.share(item.downloadUrl, item.filename);
      return;
    }
    if (!navigator.share) {
      await copyShareLink(item);
      return;
    }
    try {
      let files = [];
      if (typeof File === "function" && typeof navigator.canShare === "function") {
        try {
          const response = await fetch(item.src, { credentials: "same-origin" });
          if (response.ok) {
            const blob = await response.blob();
            const file = new File([blob], item.filename, {
              type: blob.type || "image/jpeg",
            });
            if (navigator.canShare({ files: [file] })) files = [file];
          }
        } catch {
          files = [];
        }
      }
      const payload = files.length
        ? { files, text: `来自 ${item.source}`, title: item.filename }
        : { text: `来自 ${item.source}`, title: item.filename, url: item.downloadUrl };
      await navigator.share(payload);
    } catch (error) {
      if (error && error.name !== "AbortError") await copyShareLink(item);
    }
  }

  async function downloadCurrentScope() {
    if (!state.items.length) return;
    showToast(`正在下载 ${state.items.length} 张图片`);
    for (const item of state.items) {
      const anchor = document.createElement("a");
      anchor.href = item.downloadUrl;
      anchor.download = item.filename;
      anchor.hidden = true;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      await new Promise((resolve) => global.setTimeout(resolve, 140));
    }
  }

  function handleDialogClick(event) {
    const closeButton = event.target.closest("[data-iv-close]");
    if (closeButton) {
      close();
      return;
    }
    if (state.isSettling) return;
    if (event.target.closest("[data-iv-previous]")) {
      showPrevious();
      return;
    }
    if (event.target.closest("[data-iv-next]")) {
      showNext();
      return;
    }
    if (event.target.closest("[data-iv-share]")) {
      shareCurrent();
      return;
    }
    if (event.target.closest("[data-iv-download-all]")) {
      downloadCurrentScope();
      return;
    }
    const scopeButton = event.target.closest("[data-iv-scope]");
    if (scopeButton) {
      setScope(scopeButton.dataset.ivScope);
      return;
    }
    const thumbnail = event.target.closest("[data-iv-index]");
    if (thumbnail) showAt(Number(thumbnail.dataset.ivIndex));
  }

  function focusableElements() {
    return Array.from(
      dialog.querySelectorAll('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'),
    ).filter((element) => element.offsetParent !== null);
  }

  function handleKeydown(event) {
    if (!state.isOpen) return;
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      showPrevious();
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      showNext();
    } else if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      setZoom(state.scale * 1.25);
    } else if (event.key === "-") {
      event.preventDefault();
      setZoom(state.scale * 0.8);
    } else if (event.key === "0") {
      event.preventDefault();
      resetZoom(true);
    } else if (event.key === "Tab") {
      const focusable = focusableElements();
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  }

  function handleDocumentClick(event) {
    const link = event.target.closest(IMAGE_LINK_SELECTOR);
    if (!link || !link.querySelector("img")) return;
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    event.preventDefault();
    open(link);
  }

  installStylesheet();
  createDialog();
  decorateImageGrids();
  document.addEventListener("click", handleDocumentClick);
  document.addEventListener("keydown", handleKeydown);
  const observer = new MutationObserver(scheduleGridRefresh);
  observer.observe(document.body, { childList: true, subtree: true });

  const publicApi = {
    back() {
      if (!state.isOpen) return false;
      close();
      return true;
    },
    close,
    isOpen() {
      return state.isOpen;
    },
    open,
    refreshGrids: decorateImageGrids,
    test: testApi,
  };
  global.CodexHubImageViewer = publicApi;
  global.CodexHubMediaViewer = publicApi;
})(typeof window !== "undefined" ? window : globalThis);
