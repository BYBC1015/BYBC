(() => {
  const scene = document.querySelector("[data-pixel-auth]");
  if (!scene) {
    return;
  }

  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const card = scene.querySelector("[data-tilt-card]");
  const character = scene.querySelector("[data-pixel-character]");
  const passwordInput = scene.querySelector('input[type="password"]');
  const openCharacter = scene.querySelector(".girl-open");
  const closedCharacter = scene.querySelector(".girl-closed");
  let typingTimer = 0;
  let frame = 0;
  let pointer = {
    x: window.innerWidth / 2,
    y: window.innerHeight / 2,
  };

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function render() {
    frame = 0;
    const width = Math.max(1, window.innerWidth);
    const height = Math.max(1, window.innerHeight);
    const nx = pointer.x / width - 0.5;
    const ny = pointer.y / height - 0.5;

    scene.style.setProperty("--mx", `${pointer.x}px`);
    scene.style.setProperty("--my", `${pointer.y}px`);
    scene.style.setProperty("--grid-x", `${nx * 18}px`);
    scene.style.setProperty("--grid-y", `${ny * 18}px`);
    scene.style.setProperty("--tilt-y", `${clamp(nx * 7, -7, 7)}deg`);
    scene.style.setProperty("--tilt-x", `${clamp(ny * -6, -6, 6)}deg`);
    scene.style.setProperty("--eye-x", `${clamp(nx * 9, -5, 5)}px`);
    scene.style.setProperty("--eye-y", `${clamp(ny * 7, -4, 4)}px`);
    scene.style.setProperty("--character-x", `${clamp(nx * 18, -14, 14)}px`);
    scene.style.setProperty("--character-y", `${clamp(ny * 11, -9, 9)}px`);
    scene.style.setProperty("--character-tilt", `${clamp(nx * -4, -4, 4)}deg`);
    scene.style.setProperty("--head-tilt", `${clamp(nx * 5, -5, 5)}deg`);
  }

  function schedule(event) {
    pointer = {
      x: event.clientX,
      y: event.clientY,
    };
    if (!frame) {
      frame = window.requestAnimationFrame(render);
    }
  }

  function setPasswordMode(enabled) {
    scene.classList.toggle("is-password-focused", enabled);
    if (openCharacter && closedCharacter) {
      openCharacter.style.opacity = enabled ? "0" : "";
      closedCharacter.style.opacity = enabled ? "1" : "";
    }
  }

  if (!reducedMotion) {
    scene.addEventListener("pointermove", schedule);
    scene.addEventListener("pointerleave", () => {
      pointer = {
        x: window.innerWidth / 2,
        y: window.innerHeight / 2,
      };
      if (!frame) {
        frame = window.requestAnimationFrame(render);
      }
    });

    card?.addEventListener("focusin", () => scene.classList.add("is-focused"));
    card?.addEventListener("focusout", () => scene.classList.remove("is-focused"));
    character?.addEventListener("animationiteration", () => {
      scene.classList.add("character-blink");
      window.setTimeout(() => scene.classList.remove("character-blink"), 160);
    });
  }

  document.addEventListener("focusin", (event) => {
    setPasswordMode(event.target === passwordInput);
  });
  document.addEventListener("pointerdown", (event) => {
    setPasswordMode(event.target === passwordInput);
  });
  passwordInput?.addEventListener("focus", () => setPasswordMode(true));
  passwordInput?.addEventListener("blur", () => {
    setPasswordMode(false);
    scene.classList.remove("is-typing");
  });
  passwordInput?.addEventListener("input", () => {
    setPasswordMode(true);
    scene.classList.add("is-typing");
    window.clearTimeout(typingTimer);
    typingTimer = window.setTimeout(() => scene.classList.remove("is-typing"), 260);
  });
  window.setInterval(() => {
    setPasswordMode(document.activeElement === passwordInput);
  }, 200);

  if (!reducedMotion) {
    render();
  }

  setPasswordMode(document.activeElement === passwordInput);
  scene.dataset.pixelAuthReady = "true";
  window.__pixelAuthReady = true;
})();
