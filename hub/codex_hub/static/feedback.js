(() => {
  "use strict";
  const form = document.querySelector("[data-feedback-form]");
  if (!form) return;
  const value = (name) => form.elements.namedItem(name);
  if (value("viewport_width")) value("viewport_width").value = String(window.innerWidth);
  if (value("viewport_height")) value("viewport_height").value = String(window.innerHeight);
  if (value("platform")) value("platform").value = navigator.userAgentData?.platform || navigator.platform || "未知";
  if (value("page_url") && !value("page_url").value) {
    let source = location.pathname;
    try { const prior = new URL(document.referrer); if (prior.origin === location.origin) source = prior.pathname + prior.search; } catch { /* A direct visit has no source page. */ }
    value("page_url").value = source;
  }
  const kind = value("kind");
  if (kind) {
    const hint = document.createElement("small"); hint.className = "feedback-form-hint"; hint.id = "feedback-kind-help";
    kind.closest(".feedback-field")?.append(hint);
    kind.setAttribute("aria-describedby", hint.id);
    const updateKind = () => {
      const idea = kind.value === "idea";
      hint.textContent = idea ? "说说希望改进的体验，以及它能帮助你完成什么。" : "描述发生了什么、怎样重现，方便工程师核对。";
      if (value("body")) value("body").placeholder = idea ? "我希望可以……这样就能……" : "我在……操作时发现……，实际结果是……";
    };
    kind.addEventListener("change", updateKind); updateKind();
  }
  form.querySelectorAll("input[maxlength],textarea[maxlength]").forEach((input) => {
    const count = document.createElement("small"); count.className = "feedback-char-count";
    const update = () => { count.textContent = `${input.value.length} / ${input.maxLength}`; };
    input.insertAdjacentElement("afterend", count); input.addEventListener("input", update); update();
  });
  const submit = form.querySelector('[type="submit"]');
  const caption = submit?.textContent;
  let submitting = false;
  form.addEventListener("submit", (event) => {
    if (submitting) { event.preventDefault(); return; }
    if (!form.checkValidity()) return;
    submitting = true;
    form.setAttribute("aria-busy", "true");
    if (submit) { submit.disabled = true; submit.textContent = "正在保存反馈…"; }
  });
  window.addEventListener("pageshow", () => {
    submitting = false; form.removeAttribute("aria-busy");
    if (submit) { submit.disabled = false; submit.textContent = caption; }
  });
})();
