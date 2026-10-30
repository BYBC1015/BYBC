(() => {
  "use strict";
  const shell = document.querySelector("[data-ws-shell]");
  if (!shell || shell.dataset.androidNativeReady === "true") return;
  shell.dataset.androidNativeReady = "true";
  document.documentElement.classList.add("codex-hub-android");
  const form = shell.querySelector("[data-ws-dispatch]");
  const imported = new Set();
  const submissions = new Map();
  const previews = new Map();
  const fileKey = (file) => `${String(file?.name || "")}:${Number(file?.size ?? -1)}`;
  window.CodexHubAndroidFiles = {
    receiveSelection(files) {
      for (const file of Array.from(files || [])) {
        if (typeof file.importId === "string" && file.importId) imported.add(file.importId);
        if (file.preview && file.name) previews.set(fileKey(file), String(file.preview));
      }
      window.dispatchEvent(new CustomEvent("codexhub:native-file-previews"));
    },
    previewFor(file) { return previews.get(fileKey(file)) || ""; },
    clear() { previews.clear(); },
  };
  form?.addEventListener("submit", () => {
    const key = String(form.elements.request_key?.value || "");
    if (key && !submissions.has(key)) submissions.set(key, Array.from(imported));
    while (submissions.size > 32) submissions.delete(submissions.keys().next().value);
  }, true);
  window.addEventListener("hub:workspace-sent", (event) => {
    const key = String(event.detail?.requestKey || "");
    const ids = submissions.get(key);
    if (!ids) return;
    submissions.delete(key);
    ids.forEach((id) => imported.delete(id));
    if (ids.length) {
      try { window.HubLocalData?.clearImportedFilesFor?.(JSON.stringify(ids)); } catch { /* Retry never clears a newer selection. */ }
    }
  });
  window.CodexHubAndroid = {
    back() {
      if (window.CodexHubMediaViewer?.back?.()) return true;
      const dialog = document.querySelector("dialog[open]");
      if (dialog) { if (dialog.dispatchEvent(new Event("cancel", {cancelable: true})) && dialog.open) dialog.close(); return true; }
      if (shell.classList.contains("is-sidebar-open")) {
        shell.querySelector("[data-ws-sidebar-close]")?.click(); return true;
      }
      if (shell.classList.contains("is-inspector-open")) {
        shell.querySelector("[data-ws-inspector-close]")?.click(); return true;
      }
      return false;
    },
  };
  document.addEventListener("click", (event) => {
    const anchor = event.target.closest?.("a[href]");
    if (!anchor || event.defaultPrevented || event.button > 0 || event.ctrlKey || event.metaKey) return;
    let url;
    try { url = new URL(anchor.href, location.href); } catch { return; }
    if (url.origin !== location.origin) return;
    const fileRoute = /\/projects\/[^/]+\/attachments\/[1-9][0-9]*\/?$/.test(url.pathname);
    const packageRoute = /\/downloads\/[a-z][a-z0-9_-]*\/?$/.test(url.pathname);
    const explicitDownload = anchor.hasAttribute("download") || anchor.dataset.hubDownload === "true"
      || (fileRoute && anchor.classList.contains("ws-file-link"));
    if ((explicitDownload && fileRoute || packageRoute) && typeof window.HubApp?.download === "function") {
      event.preventDefault(); event.stopImmediatePropagation();
      window.HubApp.download(url.href);
    } else if (anchor.target === "_blank") {
      // WebView has no browser-tab UI. The shared durable draft survives normal history navigation.
      anchor.target = "_self";
    }
  }, true);
})();
