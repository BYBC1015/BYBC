(() => {
  'use strict';
  const shell = document.querySelector('[data-ws-shell]');
  const feed = shell?.querySelector('[data-ws-live]');
  if (!shell || !feed) return;
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  const seenMessages = new Set();
  const progressStates = new Map();
  const observers = new Map();
  const dirty = new Set();
  let frame = 0;
  let active = true;

  function connectionState() {
    const explicit = shell.dataset.wsConnection;
    if (explicit) return explicit === 'online' ? 'online' : explicit === 'offline' ? 'offline' : 'unknown';
    // Compatibility with an already-loaded older shell: lack of confirmation
    // is not proof of a live executor, and the label remains the shell's own.
    const text = shell.querySelector('[data-ws-sync]')?.textContent || '';
    if (!navigator.onLine || text.includes('离线')) return 'offline';
    return text === '已同步' ? 'online' : 'unknown';
  }
  function contentSignature(node) {
    const source = [node.dataset.wsProgressGeneration, node.dataset.wsProgressKind,
      node.dataset.wsProgressBodyVersion || node.querySelector('[data-ws-progress-body]')?.textContent || '',
      node.querySelector('[data-ws-progress-status-text]')?.textContent || ''].join('\u001f');
    // A small content identity avoids retaining previous full replies in memory.
    let hash = 2166136261;
    for (let i = 0; i < source.length; i++) hash = Math.imul(hash ^ source.charCodeAt(i), 16777619);
    return hash >>> 0;
  }
  function visible(node) {
    const box = node.getBoundingClientRect();
    const viewport = shell.querySelector('[data-ws-scroll]')?.getBoundingClientRect();
    return viewport && box.bottom > viewport.top && box.top < viewport.bottom;
  }
  function animateOnce(node, className) {
    const selection = window.getSelection();
    if (reducedMotion.matches || document.hidden || connectionState() !== 'online' || !visible(node)
      || (selection && !selection.isCollapsed && node.contains(selection.anchorNode))) return;
    // No replay while an earlier real update's short pulse is still finishing.
    if (node.classList.contains(className)) return;
    node.classList.add(className);
    const name = className === 'ws-reply-arrived' ? 'ws-reply-arrive' : 'ws-reply-report';
    let timer;
    const cleanup = event => {
      if (event && event.animationName !== name) return;
      clearTimeout(timer); node.removeEventListener('animationend',cleanup);
      node.classList.remove(className);
    };
    node.addEventListener('animationend',cleanup);
    timer = setTimeout(cleanup,500);
  }
  function paintProgress(node, initial=false) {
    const id = node.dataset.wsProgress;
    const online = connectionState() === 'online';
    const contact = node.dataset.wsProgressContact === 'live';
    const reported = node.dataset.wsProgressReport === 'true';
    const kind = node.dataset.wsProgressKind;
    const state = !contact ? 'uncertain' : !online ? 'connection' : !reported ? 'waiting' : kind === 'assistant' ? 'reply' : 'working';
    node.dataset.replyState = state;
    const label = node.querySelector('[data-ws-progress-label-text]');
    const text = {uncertain:'执行状态待核对',connection:'等待连接',waiting:'等待公开进展',reply:'正在回复',working:'执行中'}[state];
    if (label && label.textContent !== text) label.textContent = text;
    const note = node.querySelector('[data-reply-connection-note]');
    if (note) note.hidden = online || !contact;
    if (!online || !contact) node.classList.remove('ws-reply-arrived','ws-reply-updated');
    const signature = contentSignature(node);
    const previous = progressStates.get(id);
    if (!initial && previous !== undefined && previous !== signature && reported && contact) animateOnce(node,'ws-reply-updated');
    progressStates.set(id,signature);
    if (progressStates.size > 200) progressStates.delete(progressStates.keys().next().value);
  }
  function queue(node) {
    if (!active || !node?.isConnected) return;
    dirty.add(node);
    if (!frame) frame = requestAnimationFrame(() => {
      frame = 0;
      for (const card of dirty) if (card.isConnected) paintProgress(card);
      dirty.clear();
    });
  }
  function register(node, initial=false) {
    if (!node.matches?.('[data-ws-entry]')) return;
    const id = node.dataset.wsEntry;
    const isNew = !seenMessages.has(id);
    seenMessages.add(id);
    if (seenMessages.size > 240) {
      // Keep IDs that are still on screen so long sessions cannot animate an
      // unchanged old message merely because the bounded history rotated.
      const displayed = new Set(Array.from(feed.children,node => node.dataset?.wsEntry));
      for (const old of seenMessages) {
        if (!displayed.has(old)) seenMessages.delete(old);
        if (seenMessages.size <= 240) break;
      }
    }
    if (node.matches('[data-ws-progress]')) {
      if (!observers.has(node)) {
        const observer = new MutationObserver(() => queue(node));
        observer.observe(node,{childList:true,characterData:true,subtree:true,attributes:true,
          attributeFilter:['data-ws-progress-generation','data-ws-progress-sequence','data-ws-progress-kind',
            'data-ws-progress-body-version','data-ws-progress-contact','data-ws-progress-report']});
        observers.set(node,observer);
      }
      paintProgress(node,initial);
    }
    if (!initial && isNew && !node.classList.contains('ws-message-user')) animateOnce(node,'ws-reply-arrived');
  }
  function syncConnection() {
    shell.dataset.replyConnection = connectionState();
    for (const node of observers.keys()) queue(node);
  }
  const feedObserver = new MutationObserver(records => {
    for (const record of records) for (const node of record.addedNodes) register(node);
    for (const [node, observer] of observers) if (!feed.contains(node)) { observer.disconnect(); observers.delete(node); dirty.delete(node); }
  });
  const syncObserver = new MutationObserver(syncConnection);
  function start() {
    active = true;
    for (const node of feed.children) register(node,true);
    feedObserver.observe(feed,{childList:true});
    const sync = shell.querySelector('[data-ws-sync]');
    if (sync) syncObserver.observe(sync,{childList:true,characterData:true,subtree:true});
    syncConnection();
  }
  window.addEventListener('hub:workspace-updated',() => {
    if (!active) return;
    for (const node of feed.children) register(node);
    syncConnection();
  });
  window.addEventListener('hub:workspace-connectivity',syncConnection);
  window.addEventListener('offline',syncConnection);
  window.addEventListener('pagehide',() => {
    active = false; feedObserver.disconnect(); syncObserver.disconnect();
    for (const observer of observers.values()) observer.disconnect();
    observers.clear(); dirty.clear(); cancelAnimationFrame(frame); frame = 0;
  });
  window.addEventListener('pageshow',event => { if (event.persisted) start(); });
  start();
})();
