/* Durable composer storage. A queued message is created only by an explicit send.
 * Files live in IndexedDB, never localStorage, and are written once per selection.
 * Queue transitions and claims are atomic across windows; server request keys are
 * the final duplicate-delivery fence after a process dies or a reply is lost. */
(() => {
  'use strict';
  const active = job => job && !['sent', 'cancelled', 'rejected'].includes(job.state);
  let database;
  function ready() {
    if (!database) database = new Promise((resolve, reject) => {
      const request = indexedDB.open('codexHub.workspace.local.v1', 1);
      request.onupgradeneeded = () => {
        for (const name of ['drafts', 'outbox', 'files']) request.result.createObjectStore(name, {keyPath: 'id'});
      };
      request.onsuccess = () => { request.result.onversionchange = () => request.result.close(); resolve(request.result); };
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error('本机存储正被旧窗口占用'));
    });
    return database;
  }
  async function transaction(mode, operation) {
    const db = await ready();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(['drafts', 'outbox', 'files'], mode);
      let result;
      tx.oncomplete = () => resolve(result);
      tx.onabort = tx.onerror = () => reject(tx.error || new Error('无法写入本机存储'));
      try { operation(tx, value => { result = value; }); } catch (error) { tx.abort(); reject(error); }
    });
  }
  function pack(scope, payload, tx) {
    const files = payload.files.map(item => {
      const id = `${scope}:${item.id}`;
      const request = tx.objectStore('files').get(id);
      request.onsuccess = () => { if (!request.result) tx.objectStore('files').put({id, file:item.file}); };
      return {id:item.id, name:item.file.name, type:item.file.type, size:item.file.size, lastModified:item.file.lastModified};
    });
    return {...payload, files, updated:Date.now()};
  }
  function hydrate(scope, payload, tx) {
    if (!payload) return;
    payload.files = (payload.files || []).map(item => {
      const entry = {...item};
      const request = tx.objectStore('files').get(`${scope}:${item.id}`);
      request.onsuccess = () => { entry.file = request.result?.file; };
      return entry;
    });
  }
  function cleanup(scope, oldPayload, newPayload, job, tx) {
    const keep = new Set([...(newPayload?.files || []), ...(active(job) ? job.payload.files : [])].map(item => item.id));
    for (const item of oldPayload?.files || []) if (!keep.has(item.id)) tx.objectStore('files').delete(`${scope}:${item.id}`);
  }
  function read(scope) {
    return transaction('readonly', (tx, done) => {
      const result = {draft:null, job:null}; done(result);
      const draft = tx.objectStore('drafts').get(scope);
      draft.onsuccess = () => { result.draft = draft.result?.payload; hydrate(scope, result.draft, tx); };
      const job = tx.objectStore('outbox').get(scope);
      job.onsuccess = () => { result.job = job.result || null; hydrate(scope, result.job?.payload, tx); };
    });
  }
  function save(scope, payload) {
    return transaction('readwrite', (tx, done) => {
      const job = tx.objectStore('outbox').get(scope);
      job.onsuccess = () => {
        if (active(job.result) && job.result.key === payload.key) { done(false); return; }
        // A stale tab cannot resurrect a message already delivered/cancelled.
        if (job.result?.key === payload.key) { done(false); return; }
        const old = tx.objectStore('drafts').get(scope);
        old.onsuccess = () => {
          const packed = pack(scope, payload, tx);
          tx.objectStore('drafts').put({id:scope, payload:packed});
          cleanup(scope, old.result?.payload, packed, job.result, tx); done(true);
        };
      };
    });
  }
  function enqueue(scope, payload) {
    return transaction('readwrite', (tx, done) => {
      const existing = tx.objectStore('outbox').get(scope);
      existing.onsuccess = () => {
        if (active(existing.result) || existing.result?.key === payload.key) {
          hydrate(scope, existing.result.payload, tx); done(existing.result); return;
        }
        const packed = pack(scope, payload, tx);
        const job = {id:scope, key:payload.key, state:'queued', attempted:false, payload:packed, updated:Date.now(), retryAt:0};
        tx.objectStore('outbox').put(job);
        const previous = tx.objectStore('drafts').get(scope);
        previous.onsuccess = () => {
          // Do not erase another window's newer, still unsubmitted draft.
          if (!previous.result || previous.result.payload.key === payload.key) tx.objectStore('drafts').put({id:scope, payload:packed});
        };
        // Return hydrated data only after putting the metadata-only records.
        // Newly selected files may only be queued for put by pack's get callback.
        // Reading them again in this transaction could overtake that put. Return
        // the exact input File references after the transaction has committed.
        const returned = {...job, payload:{...packed, files:packed.files.map((item, index) => ({...item, file:payload.files[index].file}))}};
        done(returned);
      };
    });
  }
  function claim(scope, key, owner) {
    return transaction('readwrite', (tx, done) => {
      const request = tx.objectStore('outbox').get(scope);
      request.onsuccess = () => {
        const job = request.result;
        if (!active(job) || job.key !== key || (job.owner && job.owner !== owner && job.leaseUntil > Date.now())) { done(false); return; }
        job.owner = owner; job.leaseUntil = Date.now() + 45000;
        tx.objectStore('outbox').put(job); done(true);
      };
    });
  }
  function change(scope, key, owner, patch, {start=false, finish=false, cancel=false}={}) {
    return transaction('readwrite', (tx, done) => {
      const request = tx.objectStore('outbox').get(scope);
      request.onsuccess = () => {
        const job = request.result;
        if (!active(job) || job.key !== key || (owner && job.owner !== owner) || (cancel && (job.attempted || job.state !== 'queued'))) { done(false); return; }
        if (start && job.owner !== owner) { done(false); return; }
        const next = {...job, ...patch, updated:Date.now()};
        tx.objectStore('outbox').put(next);
        if (finish) {
          const draft = tx.objectStore('drafts').get(scope);
          draft.onsuccess = () => {
            if (draft.result?.payload.key === key) tx.objectStore('drafts').delete(scope);
            cleanup(scope, job.payload, draft.result?.payload.key === key ? null : draft.result?.payload, next, tx);
          };
          next.payload = {...job.payload, files:[]}; tx.objectStore('outbox').put(next);
        }
        done(true);
      };
    });
  }
  function list(prefix) {
    return transaction('readonly', (tx, done) => {
      const items = []; done(items);
      const request = tx.objectStore('outbox').openCursor();
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) return;
        if (cursor.key.startsWith(prefix) && active(cursor.value)) {
          const job = cursor.value;
          items.push({scope:job.id, key:job.key, state:job.state, workspace_url:job.payload.workspace_url,
            title:job.payload.description?.slice(0,40) || '附件工作', updated:job.updated});
        }
        cursor.continue();
      };
    });
  }
  window.CodexWorkspaceStore = Object.freeze({ready, read, save, enqueue, claim, change, active, list});
})();
