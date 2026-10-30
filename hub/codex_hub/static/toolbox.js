(() => {
  "use strict";
  const shell = document.querySelector("[data-ws-shell]");
  const sidebar = shell?.querySelector(".ws-sidebar-bottom");
  if (!sidebar) return;
  // The desktop can restore an older workspace body with current static assets.
  // Upgrade only this missing entry; leave its messages and unsent draft intact.
  let downloadURL;
  try {
    const projectURL = new URL(shell.dataset.projectUrl, location.href);
    const marker = projectURL.pathname.lastIndexOf("/projects/");
    if (projectURL.origin !== location.origin || marker < 0) return;
    downloadURL = projectURL.pathname.slice(0, marker) + "/downloads";
  } catch (_) { return; }
  let entry = document.querySelector("[data-ws-toolbox]");
  if (!entry) {
    entry = document.createElement("a");
    entry.className = "ws-toolbox-entry";
    entry.setAttribute("data-ws-toolbox", "");
    entry.href = downloadURL;
    entry.setAttribute("aria-haspopup", "dialog");
    entry.setAttribute("aria-controls", "ws-toolbox-panel");
    entry.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6V4h6v2M4 6h16v14H4zM4 11h16M10 10v3h4v-3"/></svg><span>工具箱<small>视频 · 音乐 · 调色</small></span><span class="ws-toolbox-arrow" aria-hidden="true">↗</span>';
    sidebar.prepend(entry);
  }
  let status = document.querySelector("[data-ws-toolbox-status]");
  if (!status) {
    status = document.createElement("p");
    status.className = "ws-toolbox-status";
    status.setAttribute("data-ws-toolbox-status", "");
    status.setAttribute("role", "status");
    status.hidden = true;
    entry.after(status);
  }
  let dialog = document.querySelector("[data-ws-toolbox-dialog]");
  if (!dialog) {
    dialog = document.createElement("dialog");
    dialog.id = "ws-toolbox-panel";
    dialog.className = "ws-toolbox-dialog";
    dialog.setAttribute("data-ws-toolbox-dialog", "");
    dialog.setAttribute("aria-labelledby", "ws-toolbox-title");
    dialog.setAttribute("aria-describedby", "ws-toolbox-description");
    dialog.innerHTML = '<header><div><span class="ws-eyebrow">创作与后期</span><h2 id="ws-toolbox-title">工具箱</h2></div><button type="button" data-ws-toolbox-close aria-label="关闭工具箱说明" autofocus>×</button></header><p id="ws-toolbox-description" data-ws-toolbox-description></p><ul class="ws-toolbox-list"><li><span aria-hidden="true">↦</span><div><strong>视频末帧延长</strong><p>延长视频尾帧，支持批量处理。</p></div></li><li><span aria-hidden="true">◈</span><div><strong>可灵批处理</strong><p>场景替换、抽帧检查与逐镜头返修。</p></div></li><li><span aria-hidden="true">♫</span><div><strong>音乐制作 · 节拍工坊</strong><p>鼓机、乐器伴奏与自然环境声。</p></div></li><li><span aria-hidden="true">◐</span><div><strong>达芬奇调色 · 节点预设</strong><p>应用风格与节点模板，保留原版本。</p></div></li></ul><p class="ws-toolbox-note">音乐与调色工具需安装达芬奇 Studio 21.1。</p><div class="ws-toolbox-actions"><button type="button" data-ws-toolbox-retry hidden>重新打开工具箱</button><a target="_blank" rel="noopener noreferrer">获取 Windows 桌面版 <span aria-hidden="true">↗</span></a></div>';
    dialog.querySelector("a").href = downloadURL;
    document.body.append(dialog);
  }
  if (!entry || !dialog || typeof dialog.showModal !== "function") return;
  if (entry.dataset.toolboxReady) return;
  entry.dataset.toolboxReady = "true";
  const description = dialog.querySelector("[data-ws-toolbox-description]");
  const retry = dialog.querySelector("[data-ws-toolbox-retry]");
  let busy = false;
  let returnFocus = entry;
  const nativeAPI = () => window.pywebview?.api;
  function announce(message) {
    status.hidden = !message;
    status.textContent = message;
  }
  function showHelp(message, canRetry) {
    description.textContent = message;
    retry.hidden = !canRetry;
    const shell = document.querySelector("[data-ws-shell]");
    if (shell?.classList.contains("is-sidebar-open")) {
      document.querySelector("[data-ws-sidebar-close]")?.click();
      returnFocus = document.querySelector("[data-ws-sidebar-open]") || entry;
    } else if (!dialog.open) returnFocus = entry;
    if (!dialog.open) dialog.showModal();
  }
  async function openToolbox() {
    if (busy) return;
    const api = nativeAPI();
    if (typeof api?.open_toolbox !== "function") {
      showHelp(window.pywebview
        ? "此桌面版本尚未接入工具箱快捷入口，请更新 Windows 桌面版。也可以在本机后台控制台中点击「打开工具箱」。"
        : "这些工具需要在 Windows 桌面版运行。回到电脑上的 Hub，点击「工具箱」即可打开。", false);
      return;
    }
    busy = true;
    entry.setAttribute("aria-disabled", "true");
    retry.disabled = true;
    announce("正在打开工具箱…");
    try {
      const result = await api.open_toolbox();
      if (!result?.opened) throw new Error("Toolbox did not launch");
      if (dialog.open) dialog.close();
      announce("工具箱已启动");
    } catch (_) {
      announce("工具箱暂时未能打开");
      showHelp("工具箱暂时未能打开。可以重试；如果组件缺失，请重新安装 Windows 桌面版，已有项目与聊天会保留。", true);
    } finally {
      busy = false;
      entry.removeAttribute("aria-disabled");
      retry.disabled = false;
    }
  }
  entry.addEventListener("click", event => {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
    event.preventDefault();
    openToolbox();
  });
  retry.addEventListener("click", openToolbox);
  dialog.querySelector("[data-ws-toolbox-close]").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => returnFocus?.focus({preventScroll:true}));
})();
