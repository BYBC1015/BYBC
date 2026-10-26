/* Small shared interactions for the refined Hub toolbar. */
(() => {
  // Older APKs inject a document-wide scroll lock after this script runs.
  // Keep ordinary pages scrollable while the chat retains its own viewport.
  document.documentElement.classList.toggle(
    "hub-document-page", !document.body.classList.contains("hub-detail-screen"),
  );
  const menu = document.querySelector(".hub-more");
  if (!menu) return;
  const summary = menu.querySelector("summary");
  const close = (restoreFocus = false) => {
    menu.open = false;
    if (restoreFocus) summary?.focus({ preventScroll: true });
  };
  menu.addEventListener("toggle", () => {
    if (menu.open) {
      document.dispatchEvent(new CustomEvent("codexhub:overlay-open", { detail: { name: "more" } }));
    }
  });
  document.addEventListener("click", (event) => {
    if (menu.open && !menu.contains(event.target)) close();
    if (event.target.closest?.(".hub-more-menu a")) close();
  });
  document.addEventListener("focusin", (event) => {
    if (menu.open && !menu.contains(event.target)) close();
  });
  document.addEventListener("codexhub:overlay-open", (event) => {
    if (event.detail?.name !== "more" && menu.open) close();
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && menu.open) close(true);
  });
})();
