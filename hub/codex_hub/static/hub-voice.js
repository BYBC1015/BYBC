(() => {
  const input = document.querySelector("[data-mention-input]");
  const voiceButton = document.querySelector("[data-voice-session]");
  const form = document.querySelector("#project-message-form");
  const chat = document.querySelector('[data-auto-refresh="chat"]');
  if (!input || !voiceButton || !form || !chat) return;

  const storageScope = window.location.pathname.replace(/\W+/g, "-");
  const lastReadKey = `codexHub.voiceLastEntry.${storageScope}`;
  let phase = "idle";
  let recognition = null;
  let transcriptBase = input.value;
  let voiceTurnActive = false;
  let speechTimer = null;
  let lastReadFallback = 0;

  function readLastReadId() {
    try {
      return Number(window.localStorage.getItem(lastReadKey) || lastReadFallback || 0);
    } catch {
      return lastReadFallback;
    }
  }

  function writeLastReadId(value) {
    lastReadFallback = Number(value || 0);
    try {
      window.localStorage.setItem(lastReadKey, String(lastReadFallback));
    } catch {
      // Voice reply tracking remains available in memory when storage is blocked.
    }
  }

  function nativeVoice() {
    return window.HubVoice && typeof window.HubVoice.startListening === "function"
      ? window.HubVoice
      : null;
  }

  function phaseLabel(next, message = "") {
    if (message) return message;
    return {
      idle: "开始语音对话",
      listening: "正在聆听，点击停止",
      ready: "语音已转为文字，发送后将自动朗读回复；点击取消",
      waiting: "已发送，正在等待回复；点击取消",
      speaking: "正在朗读回复，点击停止",
    }[next] || "开始语音对话";
  }

  function setPhase(next, message = "") {
    phase = next;
    const label = phaseLabel(next, message);
    voiceButton.dataset.voiceState = next;
    voiceButton.classList.toggle("is-active", next !== "idle");
    voiceButton.classList.toggle("is-listening", next === "listening");
    voiceButton.classList.toggle("is-waiting", next === "waiting");
    voiceButton.classList.toggle("is-speaking", next === "speaking");
    voiceButton.setAttribute("aria-pressed", String(next !== "idle"));
    voiceButton.setAttribute("aria-label", label);
    voiceButton.title = label;
  }

  function latestDeviceMessage() {
    const messages = Array.from(
      chat.querySelectorAll('.chat-message[data-entry-author-type="device"]'),
    );
    return messages.reduce((latest, message) => {
      const id = Number(message.dataset.entryId || 0);
      const latestId = Number(latest?.dataset.entryId || 0);
      return id > latestId ? message : latest;
    }, null);
  }

  function rememberLatestDeviceMessage() {
    const latest = latestDeviceMessage();
    if (!latest) return;
    writeLastReadId(latest.dataset.entryId);
  }

  function messageText(message) {
    return (message?.querySelector(".message-card pre")?.textContent || "")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 3000);
  }

  function stopSpeaking() {
    window.clearTimeout(speechTimer);
    speechTimer = null;
    const native = nativeVoice();
    if (native && typeof native.stopSpeaking === "function") {
      native.stopSpeaking();
    } else if ("speechSynthesis" in window) {
      window.speechSynthesis.cancel();
    }
  }

  function finishVoiceTurn() {
    stopSpeaking();
    voiceTurnActive = false;
    setPhase("idle");
  }

  function speak(text) {
    const clean = String(text || "").trim();
    if (!clean) {
      finishVoiceTurn();
      return false;
    }
    setPhase("speaking");
    const native = nativeVoice();
    if (native && typeof native.speak === "function") {
      native.speak(clean);
      const estimatedDuration = Math.min(24000, Math.max(2400, clean.length * 180));
      speechTimer = window.setTimeout(finishVoiceTurn, estimatedDuration);
      return true;
    }
    if (!("speechSynthesis" in window)) {
      finishVoiceTurn();
      return false;
    }
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(clean);
    utterance.lang = "zh-CN";
    utterance.rate = 1;
    utterance.onend = finishVoiceTurn;
    utterance.onerror = finishVoiceTurn;
    window.speechSynthesis.speak(utterance);
    return true;
  }

  function scanForReplies() {
    const latest = latestDeviceMessage();
    if (!latest) return;
    const latestId = Number(latest.dataset.entryId || 0);
    const lastReadId = readLastReadId();
    if (!lastReadId) {
      writeLastReadId(latestId);
      return;
    }
    if (latestId <= lastReadId) return;
    writeLastReadId(latestId);
    if (voiceTurnActive && phase === "waiting") speak(messageText(latest));
  }

  function applyTranscript(text, isFinal) {
    const clean = String(text || "").trim();
    if (!clean) return;
    const spacer = transcriptBase && !/\s$/.test(transcriptBase) ? " " : "";
    input.value = `${transcriptBase}${spacer}${clean}`;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
    if (isFinal) {
      transcriptBase = input.value;
      recognition = null;
      voiceTurnActive = true;
      setPhase("ready");
    }
  }

  function startBrowserRecognition() {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) {
      finishVoiceTurn();
      window.alert("当前浏览器不支持语音识别，请使用 Android Hub 应用或直接输入文字。");
      return;
    }
    recognition = new Recognition();
    recognition.lang = "zh-CN";
    recognition.interimResults = true;
    recognition.continuous = false;
    recognition.onresult = (event) => {
      let text = "";
      let final = false;
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        text += event.results[index][0]?.transcript || "";
        final = final || event.results[index].isFinal;
      }
      applyTranscript(text, final);
    };
    recognition.onerror = (event) => {
      recognition = null;
      voiceTurnActive = false;
      setPhase("idle", `语音识别失败：${event.error || "未知错误"}`);
    };
    recognition.onend = () => {
      recognition = null;
      if (phase === "listening") finishVoiceTurn();
    };
    recognition.start();
  }

  function startVoiceTurn() {
    rememberLatestDeviceMessage();
    transcriptBase = input.value;
    voiceTurnActive = true;
    setPhase("listening");
    const native = nativeVoice();
    if (native) {
      native.startListening();
      return;
    }
    startBrowserRecognition();
  }

  function cancelVoiceTurn() {
    const native = nativeVoice();
    if (native && typeof native.stopListening === "function") native.stopListening();
    if (recognition) {
      const activeRecognition = recognition;
      recognition = null;
      activeRecognition.stop();
    }
    finishVoiceTurn();
  }

  voiceButton.addEventListener("click", () => {
    if (phase === "idle") startVoiceTurn();
    else cancelVoiceTurn();
  });

  form.addEventListener("codex-hub:message-sent", () => {
    if (!voiceTurnActive || phase !== "ready") return;
    rememberLatestDeviceMessage();
    setPhase("waiting");
  });

  const observer = new MutationObserver(scanForReplies);
  observer.observe(chat, { childList: true, subtree: true });
  rememberLatestDeviceMessage();
  setPhase("idle");

  window.CodexHubVoice = {
    receiveTranscript(text, isFinal) {
      applyTranscript(text, Boolean(isFinal));
    },
    receiveError(message) {
      recognition = null;
      voiceTurnActive = false;
      setPhase("idle", String(message || "语音识别失败"));
    },
    setListeningState(active, message) {
      if (active) {
        voiceTurnActive = true;
        setPhase("listening", String(message || ""));
      } else if (phase === "listening") {
        finishVoiceTurn();
      }
    },
    scanForReplies,
  };
})();
