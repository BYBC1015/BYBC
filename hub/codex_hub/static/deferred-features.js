(() => {
  "use strict";

  const source = document.currentScript;
  if (!source) return;

  const requests = new Map();
  const loadingControls = new WeakSet();
  const replaying = new WeakSet();

  function loadScript(url) {
    if (!url) return Promise.reject(new Error("missing feature URL"));
    if (requests.has(url)) return requests.get(url);

    const request = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = url;
      script.async = true;
      script.dataset.deferredFeature = "";
      script.addEventListener("load", resolve, { once: true });
      script.addEventListener("error", () => {
        script.remove();
        reject(new Error(`failed to load ${url}`));
      }, { once: true });
      document.head.append(script);
    }).catch((error) => {
      requests.delete(url);
      throw error;
    });

    requests.set(url, request);
    return request;
  }

  const interactiveFeatures = [
    {
      selector: "[data-media-image]",
      src: source.dataset.imageViewerSrc || "",
      ready: () => Boolean(window.CodexHubMediaViewer),
      activate: (control) => {
        if (typeof window.CodexHubMediaViewer?.open !== "function") return false;
        window.CodexHubMediaViewer.open(control);
        return true;
      },
    },
    {
      selector: "[data-voice-session]",
      src: source.dataset.voiceSessionSrc || "",
      ready: () => Boolean(window.CodexHubVoice),
    },
  ].filter((feature) => feature.src);

  const automaticFeatures = [
    {
      selector: "[data-markdown-source]",
      src: source.dataset.markdownSrc || "",
      ready: () => Boolean(window.CodexMarkdown),
    },
    {
      selector: "[data-video-attachment]",
      src: source.dataset.videoReaderSrc || "",
      ready: () => Boolean(window.CodexVideoReader),
    },
  ].filter((feature) => feature.src);

  function eventTarget(event) {
    return event.target instanceof Element ? event.target : event.target?.parentElement;
  }

  function matchingInteraction(event) {
    const target = eventTarget(event);
    if (!target) return null;
    for (const feature of interactiveFeatures) {
      const control = target.closest(feature.selector);
      if (control) return { control, feature };
    }
    return null;
  }

  function warmInteraction(event) {
    const match = matchingInteraction(event);
    if (!match || match.feature.ready()) return;
    loadScript(match.feature.src).catch(() => {});
  }

  document.addEventListener("pointerover", warmInteraction, {
    capture: true,
    passive: true,
  });
  document.addEventListener("focusin", warmInteraction, { capture: true });
  document.addEventListener("click", async (event) => {
    const match = matchingInteraction(event);
    if (!match || replaying.has(match.control)) return;
    if (match.feature.ready()) {
      if (
        event.button === 0 &&
        !event.metaKey &&
        !event.ctrlKey &&
        !event.shiftKey &&
        !event.altKey &&
        match.feature.activate?.(match.control)
      ) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
      return;
    }
    if (loadingControls.has(match.control)) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    if (
      event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey
    ) {
      return;
    }

    event.preventDefault();
    event.stopImmediatePropagation();
    loadingControls.add(match.control);
    match.control.setAttribute("aria-busy", "true");
    match.control.classList.add("is-loading-feature");
    try {
      await loadScript(match.feature.src);
      if (!match.feature.ready()) throw new Error("feature did not initialize");
      if (!match.control.isConnected) return;
      if (match.feature.activate?.(match.control)) return;
      replaying.add(match.control);
      match.control.click();
      queueMicrotask(() => replaying.delete(match.control));
    } catch {
      if (match.control instanceof HTMLAnchorElement && match.control.href) {
        window.location.assign(match.control.href);
        return;
      }
      const status = document.querySelector("[data-message-status]");
      if (status) status.textContent = "功能加载失败，请重试";
    } finally {
      loadingControls.delete(match.control);
      match.control.removeAttribute("aria-busy");
      match.control.classList.remove("is-loading-feature");
    }
  }, true);

  function containsMatch(root, selector) {
    if (!(root instanceof Element)) return false;
    return root.matches(selector) || Boolean(root.querySelector(selector));
  }

  function loadAutomaticFeatures(root = document.documentElement) {
    automaticFeatures.forEach((feature) => {
      if (feature.ready() || !containsMatch(root, feature.selector)) return;
      loadScript(feature.src).catch(() => {});
    });
  }

  loadAutomaticFeatures();
  if (automaticFeatures.length && document.body) {
    const observer = new MutationObserver((records) => {
      records.forEach((record) => {
        record.addedNodes.forEach((node) => loadAutomaticFeatures(node));
      });
      if (automaticFeatures.every((feature) => feature.ready())) observer.disconnect();
    });
    observer.observe(document.body, { childList: true, subtree: true });
  }

  window.CodexHubDeferredFeatures = {
    load: loadScript,
  };
})();
