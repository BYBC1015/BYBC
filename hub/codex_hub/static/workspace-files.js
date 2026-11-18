(() => {
  'use strict';
  const shell = document.querySelector('[data-ws-shell]');
  if (!shell || window.HubWorkspaceFiles) return;
  const project = new URL(shell.dataset.projectUrl, location.href);
  if (project.origin !== location.origin) return;
  const prefix = project.pathname.slice(0, project.pathname.lastIndexOf('/projects/'));
  const api = prefix + '/api/mobile/projects/' + encodeURIComponent(shell.dataset.project) + '/library';
  const uploadURL = project.pathname + '/attachments';
  for (const link of shell.querySelectorAll('[data-ws-files],.ws-sidebar-bottom a[href]')) {
    if (link.hasAttribute('data-ws-files') || new URL(link.href, location.href).hash === '#files') {
      link.href = '#files'; link.dataset.wsFiles = ''; link.setAttribute('aria-haspopup','dialog'); link.setAttribute('aria-controls','ws-files');
    }
  }
  const dialog = document.createElement('dialog');
  dialog.id = 'ws-files'; dialog.className = 'ws-files'; dialog.dataset.wsFilesDialog = '';
  dialog.setAttribute('aria-labelledby','ws-files-title');
  dialog.innerHTML = `<header class="ws-files-header"><div><h2 id="ws-files-title">项目资料</h2><p>查看与添加项目中的图片和文件</p></div><button type="button" data-ws-files-close aria-label="关闭资料">×</button></header>
    <div class="ws-files-body"><section class="ws-files-preview" data-files-preview hidden><header><h3 data-files-preview-title></h3><button type="button" data-files-preview-close>收起预览</button></header><div data-files-preview-content></div></section>
    <div class="ws-files-toolbar"><label class="ws-files-search"><input type="search" data-files-search placeholder="筛选本页资料" aria-label="筛选本页资料"></label><button type="button" data-files-reload>刷新</button><button type="button" data-files-add aria-controls="ws-files-upload">添加资料</button></div>
    <p class="ws-files-status" data-files-status role="status" aria-live="polite"></p><div class="ws-files-list" data-files-list></div><p class="ws-files-empty" data-files-empty hidden>还没有项目资料。可以在下方添加，也可以在聊天中发送附件。</p>
    <nav class="ws-files-pagination" aria-label="资料分页"><button type="button" data-files-newer disabled>← 上一页</button><button type="button" data-files-latest hidden>最近资料</button><button type="button" data-files-older disabled>下一页 →</button></nav>
    <form class="ws-files-upload" id="ws-files-upload" data-files-upload><h3>添加资料</h3><p>保存到项目资料库。需要智能体处理时，请在聊天输入框发送附件并安排工作。</p><input type="file" name="file" data-files-input aria-label="选择要添加的资料"><span class="ws-files-selected" data-files-selected>尚未选择文件</span><div class="ws-files-upload-actions"><button type="submit" disabled>上传到资料库</button><button type="button" data-files-clear disabled>移除选择</button></div><progress max="100" value="0" data-files-progress hidden aria-label="资料上传进度"></progress><p class="ws-files-status" data-files-upload-status role="status" aria-live="polite"></p></form>
    <p class="ws-files-note">图片可在此预览，其他文件可下载后打开。关闭资料会回到当前对话。</p></div>`;
  document.body.append(dialog);
  const find = value => dialog.querySelector(value);
  const list = find('[data-files-list]'), status = find('[data-files-status]'), uploadStatus = find('[data-files-upload-status]');
  const picker = find('[data-files-input]'), uploadButton = find('[data-files-upload] button[type=submit]');
  let opener, pageRows = [], cursors = [null], pageIndex = 0, older = null, loadId = 0, loadController;
  let hasLoaded = false, busy = false, pendingFile = null, pendingKey = '', uncertain = false, closingHistory = false, pendingOpen = false;
  function uuid() { return crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(20)), n=>n.toString(16).padStart(2,'0')).join(''); }
  function size(bytes) { return bytes >= 1048576 ? (bytes/1048576).toFixed(1)+' MB' : bytes >= 1024 ? (bytes/1024).toFixed(1)+' KB' : bytes+' B'; }
  function message(node, text, state='') { node.textContent = text; node.dataset.state = state; }
  function validURL(value, id) {
    const url = new URL(value, location.href);
    if (url.origin !== location.origin || url.pathname !== uploadURL + '/' + id) throw new Error('资料地址无效，请刷新后重试。');
    return url.href;
  }
  async function json(response) {
    if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('资料服务暂不可用。当前对话和草稿仍保留，请稍后刷新。');
    const body = await response.json();
    if (!response.ok) { const error = new Error(response.status===401 ? '登录已过期，请在另一页面登录原账号后重试。' : typeof body.detail==='string' ? body.detail : '暂时无法读取资料，请稍后重试。'); error.definite = response.status>=400 && response.status<500; throw error; }
    return body;
  }
  function preview(row) {
    const box = find('[data-files-preview]'), content = find('[data-files-preview-content]');
    content.replaceChildren(); find('[data-files-preview-title]').textContent = row.name;
    const image = document.createElement('img'); image.alt = row.name;
    image.addEventListener('error', () => { const note=document.createElement('p'); note.textContent='图片暂时无法加载，可刷新重试或下载原文件。'; content.replaceChildren(note); });
    image.src = validURL(row.preview_url, row.id); content.append(image); box.hidden=false;
    box.scrollIntoView({block:'nearest'}); find('[data-files-preview-close]').focus({preventScroll:true});
  }
  function closePreview() { find('[data-files-preview]').hidden=true; find('[data-files-preview-content]').replaceChildren(); }
  function filter() {
    const term=find('[data-files-search]').value.trim().toLocaleLowerCase(); let count=0;
    list.querySelectorAll('[data-file-id]').forEach(node=>{node.hidden=!node.dataset.fileName.includes(term); if(!node.hidden) count++;});
    const empty=find('[data-files-empty]'); empty.hidden=Boolean(count) || (!hasLoaded && !term);
    empty.textContent=term ? '本页没有匹配资料。清除筛选，或翻页查看更早资料。' : '还没有项目资料。可以在下方添加，也可以在聊天中发送附件。';
  }
  function render() {
    list.replaceChildren();
    for (const row of pageRows) {
      const card=document.createElement('article'); card.className='ws-file'; card.dataset.fileId=row.id; card.dataset.fileName=row.name.toLocaleLowerCase();
      const icon=document.createElement('span'); icon.className='ws-file-icon'; icon.setAttribute('aria-hidden','true'); icon.textContent=row.preview_url ? '图片' : (row.name.includes('.') ? row.name.split('.').at(-1).slice(0,6).toUpperCase() : '文件');
      const info=document.createElement('div'), title=document.createElement('strong'), detail=document.createElement('small'), actions=document.createElement('div');
      title.textContent=row.name; detail.textContent=size(row.byte_size); actions.className='ws-file-actions';
      if(row.preview_url) {const button=document.createElement('button');button.type='button';button.dataset.filePreview='';button.textContent='预览';button.addEventListener('click',()=>preview(row));actions.append(button);}
      const download=document.createElement('a'); download.href=validURL(row.download_url,row.id); download.setAttribute('download',row.name); download.dataset.fileDownload=''; download.textContent='下载'; actions.append(download);
      info.append(title,detail,actions);card.append(icon,info);list.append(card);
    }
    find('[data-files-newer]').disabled=pageIndex===0;find('[data-files-latest]').hidden=pageIndex===0;find('[data-files-older]').disabled=!older;
    filter();
  }
  async function load(index=pageIndex, cursor=cursors[index]) {
    const revision=++loadId;loadController?.abort();loadController=new AbortController();
    message(status,navigator.onLine ? '正在读取资料…' : '当前离线，保留已读取的资料；重新连接后可刷新。',navigator.onLine?'':'error');
    try {
      const result=await json(await fetch(api+(cursor?'?before='+encodeURIComponent(cursor):''),{credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'},signal:loadController.signal}));
      if(revision!==loadId) return;
      if(!Array.isArray(result.attachments)||result.attachments.length>50) throw new Error('资料列表格式无效，请稍后重试。');
      const incoming=result.attachments.map(row=>{if(!Number.isSafeInteger(row.id)||typeof row.name!=='string'||!Number.isFinite(row.byte_size))throw new Error('资料列表格式无效。');validURL(row.download_url,row.id);if(row.preview_url)validURL(row.preview_url,row.id);return row;});
      pageRows=incoming;pageIndex=index;cursors[index]=cursor;older=Number.isSafeInteger(result.older_before)&&result.older_before>0?result.older_before:null;hasLoaded=true;
      render();message(status,`第 ${pageIndex+1} 页 · ${pageRows.length} 项资料${older?' · 还有更早资料':''}`);
    } catch(error) {if(revision===loadId&&error.name!=='AbortError')message(status,navigator.onLine ? error.message || '暂时无法连接，请刷新重试。' : '当前离线，保留已读取的资料；重新连接后可刷新。','error');}
  }
  function show({fromHistory=false}={}) {
    if(closingHistory){pendingOpen=true;return;}
    if(dialog.open)return;
    opener=document.activeElement;
    if(shell.classList.contains('is-sidebar-open')){shell.querySelector('[data-ws-sidebar-close]')?.click();opener=shell.querySelector('[data-ws-sidebar-open]')||opener;}
    if(!fromHistory)history.pushState({...history.state,hubFiles:{returnHash:location.hash}},'','#files');
    document.dispatchEvent(new CustomEvent('codexhub:overlay-open',{detail:{name:'files'}}));
    dialog.showModal();window.HubMotion?.enter(dialog,{distance:7});find('[data-ws-files-close]').focus({preventScroll:true});void load();
  }
  function dismiss({fromHistory=false}={}) {
    ++loadId;loadController?.abort();closePreview();if(dialog.open)dialog.close();
    if(!fromHistory&&location.hash==='#files') {
      if(history.state?.hubFiles){closingHistory=true;history.back();}
      else history.replaceState(history.state,'',location.pathname+location.search);
    }
    if(opener?.isConnected&&!opener.closest('[inert]'))opener.focus({preventScroll:true});
  }
  function syncLocation(){closingHistory=false;if(pendingOpen){pendingOpen=false;show();return;}if(location.hash==='#files')show({fromHistory:true});else if(dialog.open)dismiss({fromHistory:true});}
  document.addEventListener('click',event=>{const link=event.target.closest('[data-ws-files]');if(!link||event.defaultPrevented||event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;event.preventDefault();show();});
  find('[data-ws-files-close]').addEventListener('click',()=>dismiss());
  dialog.addEventListener('cancel',event=>{event.preventDefault();dismiss();});
  find('[data-files-preview-close]').addEventListener('click',closePreview);
  find('[data-files-search]').addEventListener('input',filter);
  find('[data-files-reload]').addEventListener('click',()=>load());
  find('[data-files-add]').addEventListener('click',()=>{find('[data-files-upload]').scrollIntoView({block:'start'});picker.focus({preventScroll:true});});
  find('[data-files-older]').addEventListener('click',()=>{if(older)void load(pageIndex+1,older);});
  find('[data-files-newer]').addEventListener('click',()=>{if(pageIndex)void load(pageIndex-1,cursors[pageIndex-1]);});
  find('[data-files-latest]').addEventListener('click',()=>load(0,null));
  function uploadControls(){picker.disabled=busy||uncertain;uploadButton.disabled=busy||!pendingFile;find('[data-files-clear]').disabled=busy||!pendingFile||uncertain;uploadButton.textContent=busy?'正在上传…':uncertain?'重试原上传':'上传到资料库';find('[data-files-upload]').setAttribute('aria-busy',String(busy));}
  picker.addEventListener('change',()=>{if(busy)return;pendingFile=picker.files[0]||null;pendingKey=uuid();uncertain=false;find('[data-files-selected]').textContent=pendingFile?pendingFile.name+' · '+size(pendingFile.size):'尚未选择文件';message(uploadStatus,'');uploadControls();});
  find('[data-files-clear]').addEventListener('click',()=>{if(busy||uncertain)return;pendingFile=null;picker.value='';find('[data-files-selected]').textContent='尚未选择文件';message(uploadStatus,'');uploadControls();});
  async function csrf(){const body=await json(await fetch(prefix+'/api/mobile/session',{credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'}}));if(String(body.user?.id)!==shell.dataset.user||!body.csrf_token)throw new Error('登录账号已变化，请切回原账号；当前对话和文件仍保留。');return body.csrf_token;}
  function upload(data){return new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST',uploadURL);xhr.setRequestHeader('Accept','application/json');xhr.timeout=180000;xhr.upload.onprogress=event=>{if(event.lengthComputable)find('[data-files-progress]').value=event.loaded/event.total*100;};xhr.onerror=xhr.ontimeout=()=>reject(new Error('上传结果未确认。文件仍保留，可刷新资料核对，或重试原上传；同一上传不会重复添加。'));xhr.onload=()=>{try{if(!xhr.getResponseHeader('Content-Type')?.includes('application/json'))throw new Error('上传结果未确认，请恢复登录后重试原上传。');const value=JSON.parse(xhr.responseText);if(xhr.status<200||xhr.status>=300){const error=new Error(typeof value.detail==='string'?value.detail:'上传失败，请检查文件后重试。');error.definite=xhr.status>=400&&xhr.status<500;throw error;}if(!value.attachment?.id)throw new Error('上传结果未确认，请重试原上传。');resolve(value);}catch(error){reject(error);}};xhr.send(data);});}
  find('[data-files-upload]').addEventListener('submit',async event=>{
    event.preventDefault();if(busy||!pendingFile)return;busy=true;uploadControls();message(uploadStatus,'正在连接并上传…');
    let attempted=false;find('[data-files-progress]').hidden=false;find('[data-files-progress]').value=0;
    try{const token=await csrf();const data=new FormData();data.append('_csrf',token);data.append('request_key',pendingKey);data.append('file',pendingFile,pendingFile.name);attempted=true;await upload(data);const name=pendingFile.name;pendingFile=null;pendingKey='';uncertain=false;picker.value='';find('[data-files-selected]').textContent='尚未选择文件';message(uploadStatus,'已添加：'+name,'success');if(dialog.open)void load(0,null);}
    catch(error){if(attempted&&!error.definite)uncertain=true;message(uploadStatus,error.message||'上传未完成，选择的文件仍保留。','error');}
    finally{busy=false;find('[data-files-progress]').hidden=true;uploadControls();}
  });
  addEventListener('popstate',syncLocation);addEventListener('hashchange',syncLocation);
  window.HubWorkspaceFiles={open:show,close:dismiss};syncLocation();
})();
