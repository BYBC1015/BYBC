(() => {
  'use strict';
  const shell = document.querySelector('[data-ws-shell]');
  if (!shell) return;
  const narrow = matchMedia('(max-width:899px)');
  const viewport = window.visualViewport;
  const targets = [shell, document.documentElement];
  let frame = 0;
  let active = true;
  let baselineHeight = window.innerHeight;
  let baselineWidth = window.innerWidth;

  function update() {
    frame = 0;
    if (!active) return;
    if (!narrow.matches) {
      for (const target of targets) {
        target.style.removeProperty('--ws-viewport-height');
        target.style.removeProperty('--ws-viewport-top');
      }
      delete shell.dataset.wsKeyboardOpen;
      delete shell.dataset.wsCompactViewport;
      return;
    }
    // Pinch zoom is a user choice. Do not force the layout to its smaller visual viewport.
    if (viewport && Math.abs(viewport.scale - 1) > .02) return;
    const height = Math.max(1, Math.min(window.innerHeight, viewport?.height || window.innerHeight));
    const top = Math.max(0, viewport?.offsetTop || 0);
    const focused = document.activeElement?.matches('textarea,input,[contenteditable="true"]');
    if (Math.abs(window.innerWidth - baselineWidth) > 60) {
      baselineWidth = window.innerWidth;
      baselineHeight = window.innerHeight;
    } else if (!focused) baselineHeight = Math.max(baselineHeight, window.innerHeight);
    // Keep the reduced layout until the viewport actually recovers, including
    // the gap between losing input focus and the keyboard finishing its exit.
    const keyboard = baselineHeight - height > 120;
    for (const target of targets) {
      target.style.setProperty('--ws-viewport-height',`${Math.round(height)}px`);
      target.style.setProperty('--ws-viewport-top',`${Math.round(top)}px`);
    }
    shell.dataset.wsKeyboardOpen = String(keyboard);
    // Some native webviews restore directly into a short viewport. Compact
    // geometry also serves landscape without claiming a keyboard is open.
    shell.dataset.wsCompactViewport = String(keyboard || height < 500);
  }
  function queue() { if (active && !frame) frame = requestAnimationFrame(update); }
  window.addEventListener('resize',queue,{passive:true});
  window.addEventListener('orientationchange',queue,{passive:true});
  viewport?.addEventListener('resize',queue,{passive:true});
  viewport?.addEventListener('scroll',queue,{passive:true});
  document.addEventListener('focusin',queue);
  document.addEventListener('focusout',queue);
  narrow.addEventListener('change',queue);
  window.addEventListener('pagehide',() => { active = false; cancelAnimationFrame(frame); frame = 0; });
  window.addEventListener('pageshow',event => { if (event.persisted) { active = true; queue(); } });
  update();
})();
