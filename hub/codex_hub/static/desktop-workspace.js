(() => {
  'use strict';
  function initialize() {
    const api=window.pywebview?.api;
    const actions=document.querySelector('.ws-head-actions');
    if (!api?.list_projects || !api?.open_project || !actions || actions.querySelector('[data-ws-local-projects]')) return;
    const button=document.createElement('button');button.type='button';button.className='ws-local-projects';
    button.textContent='本机工程';button.dataset.wsLocalProjects='';actions.prepend(button);
    const dialog=document.createElement('dialog');dialog.className='ws-local-dialog';
    dialog.setAttribute('aria-labelledby','ws-local-title');
    dialog.innerHTML='<header><h2 id="ws-local-title">本机工程</h2><button type="button" class="ws-local-close" aria-label="关闭">×</button></header><p>打开当前电脑上的工程文件夹，继续制作或查看文件。</p><p data-local-state role="status"></p><div class="ws-local-list"></div>';
    document.body.append(dialog);
    dialog.querySelector('.ws-local-close').addEventListener('click',()=>dialog.close());
    dialog.addEventListener('click',event=>{if(event.target===dialog){const box=dialog.getBoundingClientRect();if(event.clientX<box.left||event.clientX>box.right||event.clientY<box.top||event.clientY>box.bottom)dialog.close();}});
    dialog.addEventListener('close',()=>button.focus());
    button.addEventListener('click',async()=>{
      dialog.showModal();const state=dialog.querySelector('[data-local-state]');const list=dialog.querySelector('.ws-local-list');
      list.replaceChildren();state.textContent='正在读取本机工程…';
      try {
        const response=await api.list_projects();const projects=Array.isArray(response)?response:response.projects;
        if(!Array.isArray(projects))throw new Error('unavailable');
        state.textContent=projects.length?'':'尚未配置本机工程，可从系统托盘打开“连接与设置”。';
        projects.forEach(project=>{
          const item=document.createElement('button');item.type='button';item.className='ws-local-project';
          item.textContent=project.name||project.id;item.disabled=project.available===false;
          item.addEventListener('click',async()=>{
            item.disabled=true;
            try { const result=await api.open_project(project.id);if(!result?.opened)throw new Error('unavailable');state.textContent='已打开工程文件夹。'; }
            catch { state.textContent='暂时无法打开工程，请检查本机目录是否仍然存在。'; }
            finally {item.disabled=false;}
          });list.append(item);
        });
      } catch {state.textContent='本机工程连接暂不可用，可从系统托盘检查连接与设置。';}
    });
  }
  window.addEventListener('pywebviewready',initialize);initialize();
})();
