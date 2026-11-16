(() => {
  "use strict";

  // Upgrade retained links in older detail pages before they can leave the
  // modern workspace. Genuine archive/file/task destinations remain explicit.
  for (const link of document.querySelectorAll('a[href]')) {
    try {
      const target = new URL(link.href, location.href);
      const tab = {members:'connections',control:'connections','codex-projects':'connections','codex-workspaces':'connections'}[target.hash.slice(1)];
      if (target.origin === location.origin && target.searchParams.get('view') === 'archive' && tab && /\/projects\/[^/]+\/?$/.test(target.pathname)) {
        target.searchParams.delete('view'); target.hash = 'settings=' + tab; link.href = target.href;
      }
    } catch (_) { /* Invalid legacy links are not settings routes. */ }
  }

  const header = document.querySelector("[data-site-header]");
  const toggle = header?.querySelector("[data-nav-toggle]");
  const navigation = header?.querySelector("[data-primary-navigation]");
  if (!header || !toggle || !navigation) return;

  const mobileQuery = window.matchMedia("(max-width: 680px)");

  function setOpen(open, { restoreFocus = false } = {}) {
    const isOpen = Boolean(open && mobileQuery.matches);
    header.classList.toggle("mobile-nav-open", isOpen);
    toggle.setAttribute("aria-expanded", String(isOpen));
    toggle.setAttribute("aria-label", isOpen ? "关闭主导航" : "打开主导航");
    if (!isOpen && restoreFocus) toggle.focus();
  }

  toggle.addEventListener("click", () => {
    setOpen(toggle.getAttribute("aria-expanded") !== "true");
  });

  navigation.addEventListener("click", (event) => {
    if (event.target.closest("a")) setOpen(false);
  });

  document.addEventListener("click", (event) => {
    if (!header.contains(event.target)) setOpen(false);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
      setOpen(false, { restoreFocus: true });
    }
  });

  const closeForViewportChange = () => setOpen(false);
  if (typeof mobileQuery.addEventListener === "function") {
    mobileQuery.addEventListener("change", closeForViewportChange);
  } else {
    mobileQuery.addListener(closeForViewportChange);
  }
})();

