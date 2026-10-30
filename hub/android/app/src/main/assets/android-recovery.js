/* Native lifecycle signals complement, never replace, the workspace outbox. */
(() => {
  'use strict';
  if (window.CodexHubNativeRecovery) return;
  let timer = 0, checking = false, lastWake = 0, attempted = false, networkOnline = navigator.onLine;
  const shell = () => document.querySelector('[data-ws-shell]');
  const modern = () => {
    const value = String(shell()?.dataset.wsRelease || '').split('.').map(Number);
    return value.length === 3 && value.every(Number.isFinite)
      && (value[0] > 2 || value[0] === 2 && (value[1] > 9 || value[1] === 9 && value[2] >= 6));
  };
  const busy = () => Boolean(document.querySelector('form[aria-busy="true"],dialog[open]'))
    || document.activeElement?.matches('input,textarea,select,[contenteditable="true"]')
    || Array.from(document.querySelectorAll('textarea,input:not([type="hidden"]),select')).some(field => {
      if (field.type === 'file') return field.files?.length;
      if (field.matches('[type="checkbox"],[type="radio"]')) return field.checked !== field.defaultChecked;
      if (field.tagName === 'SELECT') return Array.from(field.options).some(option => option.selected !== option.defaultSelected);
      return Boolean(field.value && (field.value !== field.defaultValue || field.matches('[data-ws-input],[data-mention-input]')));
    }) || Boolean(document.querySelector('[data-ws-attachments] [data-ws-attachment-remove], [data-attachment-list] > *'));
  function notice() {
    if (document.querySelector('[data-native-ui-update]')) return;
    const box = document.createElement('div'); box.dataset.nativeUiUpdate = ''; box.setAttribute('role', 'status');
    box.style.cssText = 'position:fixed;left:12px;right:12px;top:12px;z-index:99999;padding:14px;border:1px solid #74756e;border-radius:18px;background:#242929;color:#f2eee5;font:14px/1.5 system-ui,sans-serif;box-shadow:0 6px 24px #0005';
    const label = document.createElement('span'); label.textContent = '新版界面已就绪。当前草稿、附件和发送中的消息会保留，请完成当前操作后载入。';
    const button = document.createElement('button'); button.type = 'button'; button.textContent = '载入新版';
    button.style.cssText = 'margin-left:12px;min-height:44px;border-radius:12px;padding:0 12px';
    button.addEventListener('click', () => { button.blur(); lastWake = 0; wake(true); });
    box.append(label, button); document.body.append(box);
  }
  async function checkLegacy() {
    const current = shell();
    if (!current && !document.querySelector('.workbench-shell')) return;
    const address = new URL(location.href), controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(address.href, {credentials:'same-origin',cache:'no-store',signal:controller.signal});
      const resolved = new URL(response.url || address.href);
      if (!response.ok || resolved.origin !== address.origin || resolved.pathname !== address.pathname) return;
      const fresh = new DOMParser().parseFromString(await response.text(), 'text/html').querySelector('[data-ws-shell]');
      if (!fresh?.dataset.wsRelease || !fresh.querySelector('input[name="_csrf"]')?.value) return;
      // Old pages without an identity marker can only switch when completely idle.
      // No old draft or attachment is copied into another account's workspace.
      if (current?.dataset.user && fresh.dataset.user !== current.dataset.user) return;
      if (current && (fresh.dataset.project !== current.dataset.project || fresh.dataset.conversation !== current.dataset.conversation)) return;
      if (current) {
        if (!window.CodexWorkspaceStore?.read) { notice(); return; }
        const scope = `${current.dataset.user}.${current.dataset.project}.${current.dataset.conversation}`;
        const saved = await window.CodexWorkspaceStore.read(scope);
        if (saved.job && !['sent','cancelled','rejected'].includes(saved.job.state)
            || saved.draft?.description || saved.draft?.files?.length) { notice(); return; }
      }
      if (busy() || location.href !== address.href || document.hidden || !networkOnline) { notice(); return; }
      const guard = `codexHub.native-ui.${address.pathname}.${fresh.dataset.wsRelease}`;
      if (Date.now() - Number(sessionStorage.getItem(guard) || 0) < 300000) return;
      sessionStorage.setItem(guard, String(Date.now()));
      address.searchParams.set('_hub_native_ui', fresh.dataset.wsRelease);
      attempted = true;
      location.replace(address.href);
    } catch { /* Preserve the currently readable page and all local state. */ }
    finally { clearTimeout(timeout); }
  }
  function wake(online) {
    clearTimeout(timer);
    networkOnline = Boolean(online);
    if (!online) { window.dispatchEvent(new Event('offline')); return; }
    timer = setTimeout(async () => {
      if (checking || attempted || document.hidden || Date.now() - lastWake < 2000) return;
      checking = true; lastWake = Date.now();
      try {
        // Current server code verifies release, identity, drafts and receipt state.
        if (modern()) window.dispatchEvent(new Event('online'));
        else await checkLegacy();
      } finally { checking = false; }
    }, 450);
  }
  window.CodexHubNativeRecovery = {wake};
  document.addEventListener('visibilitychange', () => { if (!document.hidden) wake(navigator.onLine); });
  wake(navigator.onLine);
})();
