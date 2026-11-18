/* One destination policy for server pages, retained links and workspace panels. */
(() => {
  'use strict';
  const settings = {members:'connections', control:'connections', 'codex-projects':'connections', 'codex-workspaces':'connections', settings:'project'};
  const archive = new Set(['tasks', 'project-memory', 'memory']);
  const projectPath = /\/projects\/[^/]+\/?$/;
  function settingsTab(hash) {
    const value = String(hash || '').replace(/^#/, '');
    return settings[value] || (value.startsWith('settings=') ? value.slice(9) : '');
  }
  function destination(value) {
    const url = new URL(value, location.href);
    if (url.origin !== location.origin || !projectPath.test(url.pathname)) return url;
    const fragment = url.hash.slice(1), tab = settingsTab(url.hash);
    if (fragment === 'files' || tab) {
      url.searchParams.delete('view');
      url.hash = fragment === 'files' ? 'files' : 'settings=' + tab;
    } else if (archive.has(fragment)) url.searchParams.set('view', 'archive');
    return url;
  }
  function upgradeLinks(root = document) {
    for (const link of root.querySelectorAll('a[href]')) {
      try { const target = destination(link.href); if (target.href !== link.href) link.href = target.href; } catch (_) {}
    }
  }
  window.HubNavigation = {destination, settingsTab, upgradeLinks};
  const target = destination(location.href);
  if (target.href !== location.href) { location.replace(target.href); return; }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => upgradeLinks(), {once:true});
  else upgradeLinks();
})();
