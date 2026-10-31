(() => {
  const shell = document.querySelector('[data-ws-shell]');
  if (!shell || !shell.dataset.wsRelease || !shell.dataset.user || !shell.dataset.project || !shell.dataset.conversation) return null;
  const messages = Array.from(shell.querySelectorAll('[data-ws-live] > .ws-message[data-ws-entry]')).slice(-30).map(node => ({
    author: (node.querySelector('.ws-message-meta strong')?.textContent || '').trim().slice(0, 100),
    text: (node.querySelector('[data-ws-message-body]')?.innerText || '').trim().slice(0, 1800)
  })).filter(item => item.text);
  return { user: shell.dataset.user, project: shell.dataset.project, conversation: shell.dataset.conversation,
    title: (shell.querySelector('.ws-chat-title h1')?.textContent || document.title).trim().slice(0, 160), messages };
})()
