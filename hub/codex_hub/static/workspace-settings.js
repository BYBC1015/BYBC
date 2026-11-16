(() => {
  'use strict';
  const shell = document.querySelector('[data-ws-shell]');
  if (!shell || window.HubWorkspaceSettings) return;
  const projectURL = new URL(shell.dataset.projectUrl, location.href);
  if (projectURL.origin !== location.origin) return;
  const prefix = projectURL.pathname.slice(0, projectURL.pathname.lastIndexOf('/projects/'));
  const apiBase = prefix + '/api/projects/' + encodeURIComponent(shell.dataset.project);
  const preferenceKey = 'codexHub.motion.v1';
  const media = matchMedia('(prefers-reduced-motion:reduce)');
  const animations = new Map();
  let preference = 'system';
  try { preference = localStorage.getItem(preferenceKey) === 'reduce' ? 'reduce' : 'system'; } catch (_) {}
  function reduceMotion() { return preference === 'reduce' || media.matches; }
  function cancelMotion(node) { animations.get(node)?.cancel(); animations.delete(node); }
  function enter(node, {distance = 5, duration = 180} = {}) {
    if (!node) return;
    cancelMotion(node);
    if (reduceMotion() || !node.animate || !node.getClientRects().length) return;
    const motion = node.animate([{opacity:0, transform:`translateY(${distance}px)`}, {opacity:1, transform:'translateY(0)'}],
      {duration, easing:'cubic-bezier(.2,0,0,1)'});
    animations.set(node, motion);
    motion.finished.catch(() => {}).finally(() => { if (animations.get(node) === motion) animations.delete(node); });
  }
  function applyMotion() {
    document.documentElement.dataset.hubMotion = reduceMotion() ? 'reduce' : 'standard';
    if (reduceMotion()) { for (const node of animations.keys()) cancelMotion(node); }
  }
  media.addEventListener('change', applyMotion);
  addEventListener('storage', event => {
    if (event.key === preferenceKey) { preference = event.newValue === 'reduce' ? 'reduce' : 'system'; applyMotion(); syncMotionForm(); }
  });
  window.HubMotion = {enter, cancel:cancelMotion, reduced:reduceMotion};
  applyMotion();

  // Current packaged resources also run against older authenticated snapshots.
  // Repair the real entry and create the same local surface without fetching HTML.
  const entries = [...shell.querySelectorAll('[data-ws-settings],.ws-sidebar-bottom a')].filter(link =>
    link.hasAttribute('data-ws-settings') || /(?:\?|&)view=archive#members$/.test(link.getAttribute('href') || ''));
  for (const link of shell.querySelectorAll('.ws-model-settings')) link.href = '#settings=models';
  for (const link of entries) {
    link.href = '#settings=project'; link.dataset.wsSettings = '';
    link.setAttribute('aria-haspopup', 'dialog'); link.setAttribute('aria-controls', 'ws-settings');
  }
  const dialog = document.createElement('dialog');
  dialog.className = 'ws-settings'; dialog.id = 'ws-settings'; dialog.dataset.wsSettingsDialog = '';
  dialog.setAttribute('aria-labelledby', 'ws-settings-title');
  dialog.innerHTML = `
    <header class="ws-settings-header"><div><h2 id="ws-settings-title">设置</h2><p data-settings-project-name></p></div><button type="button" data-ws-settings-close aria-label="关闭设置">×</button></header>
    <div class="ws-settings-layout"><nav class="ws-settings-nav" role="tablist" aria-label="设置分类">
      <button type="button" role="tab" id="ws-settings-tab-project" data-settings-tab="project" aria-controls="ws-settings-project"><span aria-hidden="true">▦</span>项目</button>
      <button type="button" role="tab" id="ws-settings-tab-connections" data-settings-tab="connections" aria-controls="ws-settings-connections"><span aria-hidden="true">⌘</span>执行连接</button>
      <button type="button" role="tab" id="ws-settings-tab-models" data-settings-tab="models" aria-controls="ws-settings-models"><span aria-hidden="true">◈</span>模型服务</button>
      <button type="button" role="tab" id="ws-settings-tab-appearance" data-settings-tab="appearance" aria-controls="ws-settings-appearance"><span aria-hidden="true">◐</span>界面</button>
    </nav><div class="ws-settings-content">
      <section class="ws-settings-panel" id="ws-settings-project" role="tabpanel" aria-labelledby="ws-settings-tab-project" data-settings-panel="project"><h3>当前项目</h3><p>工作对话、执行连接和模型服务都属于这个项目。</p>
        <div class="ws-settings-card"><dl class="ws-settings-facts"><dt>项目名称</dt><dd data-settings-name></dd><dt>项目标识</dt><dd data-settings-slug></dd><dt>工作区版本</dt><dd data-settings-version></dd></dl></div>
        <div class="ws-settings-card"><h4>工作与连接</h4><p>执行连接负责运行任务。模型服务可接入兼容接口或本地模型；已开始的工作沿用原来的安排。</p><div class="ws-settings-actions"><button type="button" data-settings-go="connections">查看执行连接</button><button type="button" data-settings-go="models">管理模型服务</button></div></div>
        <p class="ws-settings-note">设置打开期间，当前对话、阅读位置和未发送草稿都会留在原处。</p>
      </section>
      <section class="ws-settings-panel" id="ws-settings-connections" role="tabpanel" aria-labelledby="ws-settings-tab-connections" data-settings-panel="connections" hidden><h3>执行连接</h3><p>查看项目设备，并指定任务使用的主要执行连接。</p><p class="ws-settings-status" data-settings-connection-status role="status"></p><div data-settings-members></div>
        <form data-settings-primary-form hidden><label>项目主设备<select name="device_id" aria-label="项目主设备"></select></label><div class="ws-settings-actions"><button type="submit">保存主设备</button></div></form>
        <form data-settings-member-form hidden><h4>添加执行设备</h4><div class="ws-settings-grid"><label>设备<select name="device_id" aria-label="添加执行设备" required></select></label><label>项目权限<select name="permission"><option value="write">执行与写入</option><option value="read">仅查看</option><option value="admin">管理</option></select></label></div><div class="ws-settings-actions"><button type="submit">加入项目</button></div></form>
        <div class="ws-settings-actions"><button type="button" data-settings-reload="connections">刷新连接</button></div>
      </section>
      <section class="ws-settings-panel" id="ws-settings-models" role="tabpanel" aria-labelledby="ws-settings-tab-models" data-settings-panel="models" hidden><h3>模型服务</h3><p>连接本地模型或其他模型 API。密钥只在保存时发送，不会显示在服务列表中。</p><p class="ws-settings-status" data-settings-model-status role="status"></p><div data-settings-services></div>
        <details class="ws-settings-card" data-settings-agent-policy><summary>身份的模型分配</summary><p class="ws-settings-hint">可以交由总控按任务安排，也可以为指定身份固定模型。执行中的工作保持原安排。</p><label>智能体<select data-settings-policy-agent aria-label="模型分配身份"></select></label><p class="ws-settings-status" data-settings-policy-status role="status"></p><form data-settings-policy-form hidden><div class="ws-settings-grid"><label>分配方式<select name="mode"><option value="controller">由总控按任务分配</option><option value="manual">手动指定</option></select></label><label>模型<select name="model"></select></label></div><label>思考档位<select name="reasoning_effort"></select></label><div class="ws-settings-actions"><button type="submit">保存身份模型设置</button></div></form></details><div class="ws-settings-actions"><button type="button" data-settings-add-service class="ws-settings-primary">添加模型服务</button><button type="button" data-settings-reload="models">刷新列表</button></div>
        <form data-settings-service-form hidden autocomplete="off"><h4 data-settings-service-heading>添加模型服务</h4><input type="hidden" name="id"><div class="ws-settings-grid"><label>服务名称<input name="name" maxlength="80" placeholder="例如：工作电脑 Ollama" required></label><label>服务类型<select name="kind"><option value="openai_compatible">OpenAI 兼容 API</option><option value="ollama">Ollama 本地模型</option><option value="lmstudio">LM Studio 本地模型</option></select></label></div><label>执行电脑<select name="device_id" required aria-label="模型服务执行电脑"></select></label><label>接口地址<input name="base_url" type="url" maxlength="2048" placeholder="https://api.example.com/v1" required></label><p class="ws-settings-hint" data-settings-endpoint-hint></p><div class="ws-settings-grid"><label>模型名称<input name="model" maxlength="200" placeholder="服务中的模型名称" list="ws-settings-discovered-models" required><datalist id="ws-settings-discovered-models"></datalist></label><label>API 密钥<input name="api_key" type="password" autocomplete="new-password" maxlength="4096" placeholder="本地服务可留空"></label></div><div class="ws-settings-grid"><label><input name="supports_images" type="checkbox">此模型支持图片输入</label><label><input name="supports_tools" type="checkbox">此模型支持工具调用</label></div><label><input name="clear_api_key" type="checkbox">清除已保存的 API 密钥</label><label><input name="enabled" type="checkbox" checked>允许本项目使用此服务</label><div class="ws-settings-actions"><button type="submit" class="ws-settings-primary">保存服务</button><button type="button" data-settings-cancel-service>取消编辑</button></div></form>
      </section>
      <section class="ws-settings-panel" id="ws-settings-appearance" role="tabpanel" aria-labelledby="ws-settings-tab-appearance" data-settings-panel="appearance" hidden><h3>界面与动效</h3><p>让界面变化清楚、轻快。此偏好保存在当前设备。</p><div class="ws-settings-card"><label>界面动效<select data-settings-motion aria-label="界面动效"><option value="system">跟随系统</option><option value="reduce">减少动态效果</option></select></label><p data-settings-motion-hint></p></div><div class="ws-settings-card"><h4>保持阅读位置</h4><p>消息更新不播放位移动画。打开设置和切换分类不会改变对话的阅读位置。</p></div><p class="ws-settings-status" data-settings-appearance-status role="status"></p></section>
    </div></div>`;
  document.body.append(dialog);
  const find = selector => dialog.querySelector(selector);
  const projectName = shell.querySelector('[data-ws-project-switch] option:checked')?.textContent || shell.dataset.project;
  find('[data-settings-project-name]').textContent = projectName;
  find('[data-settings-name]').textContent = projectName;
  find('[data-settings-slug]').textContent = shell.dataset.project;
  find('[data-settings-version]').textContent = shell.dataset.wsRelease || '本机保存版本';
  let activeTab = '';
  let opener = null;
  let closingHistory = false;
  let pendingOpen = '';
  let settings = null;
  let services = [];
  let serviceDevices = [];
  let modelPollTimer = 0;
  let policyRevision = 0;
  let policyController = null;
  let policyModels = [];
  let pendingAgent = null;
  let loadController = null;
  let loadRevision = 0;
  let mutationBusy = false;
  const tabs = ['project', 'connections', 'models', 'appearance'];
  const legacyTabs = {members:'connections', control:'connections', 'codex-projects':'connections', 'codex-workspaces':'connections', settings:'project'};
  function hashTab() { return legacyTabs[location.hash.slice(1)] || (location.hash.startsWith('#settings=') ? location.hash.slice(10) : ''); }
  function status(kind, text, state = '') { const node = find(`[data-settings-${kind}-status]`); node.textContent = text; node.dataset.state = state; }
  function syncMotionForm() {
    find('[data-settings-motion]').value = preference;
    find('[data-settings-motion-hint]').textContent = reduceMotion() ? '已关闭过渡动效，所有操作仍即时可用。' : '使用短促的淡入与轻微位移，系统减少动态效果开启时自动关闭。';
  }
  syncMotionForm(); media.addEventListener('change', syncMotionForm);
  find('[data-settings-motion]').addEventListener('change', event => {
    preference = event.target.value; applyMotion(); syncMotionForm();
    try { localStorage.setItem(preferenceKey, preference); status('appearance', '界面偏好已保存。', 'success'); }
    catch (_) { status('appearance', '本机存储不可用，偏好仅应用于当前窗口。'); }
  });
  function selectTab(tab, {focus = false, historyMode = 'replace'} = {}) {
    if (!tabs.includes(tab)) tab = 'project';
    const changed = activeTab !== tab;
    for (const panel of dialog.querySelectorAll('[data-settings-panel]')) {
      if (panel.dataset.settingsPanel !== tab) cancelMotion(panel);
      panel.hidden = panel.dataset.settingsPanel !== tab;
    }
    for (const button of dialog.querySelectorAll('[data-settings-tab]')) {
      const selected = button.dataset.settingsTab === tab;
      button.setAttribute('aria-selected', String(selected)); button.tabIndex = selected ? 0 : -1;
    }
    activeTab = tab;
    if (historyMode !== 'none') history.replaceState(history.state, '', '#settings=' + tab);
    if (focus) find(`[data-settings-tab="${tab}"]`).focus({preventScroll:true});
    if (changed) { find('.ws-settings-content').scrollTop = 0; enter(find(`[data-settings-panel="${tab}"]`), {distance:3, duration:160}); }
    if (changed) clearTimeout(modelPollTimer);
    if (changed && (tab === 'connections' || tab === 'models')) load(tab);
  }
  function show(tab = 'project', {fromHistory = false} = {}) {
    if (closingHistory) { pendingOpen = tab; return; }
    if (!dialog.open) {
      opener = document.activeElement;
      if (shell.classList.contains('is-sidebar-open')) {
        shell.querySelector('[data-ws-sidebar-close]')?.click();
        opener = shell.querySelector('[data-ws-sidebar-open]') || opener;
      }
      if (!fromHistory) history.pushState({...history.state, hubSettings:{returnHash:location.hash}}, '', '#settings=' + tab);
      dialog.showModal(); enter(dialog, {distance:7});
      selectTab(tab, {focus:true, historyMode:fromHistory ? 'none' : 'replace'});
    } else selectTab(tab, {focus:true, historyMode:fromHistory ? 'none' : 'replace'});
  }
  function dismiss({fromHistory = false} = {}) {
    loadRevision++; loadController?.abort(); loadController = null; clearTimeout(modelPollTimer); policyRevision++; policyController?.abort();
    for (const node of [dialog, ...dialog.querySelectorAll('[data-settings-panel]')]) cancelMotion(node);
    if (dialog.open) dialog.close();
    activeTab = '';
    find('[name="api_key"]').value = '';
    if (!fromHistory && hashTab()) {
      if (history.state?.hubSettings) { closingHistory = true; history.back(); }
      else history.replaceState(history.state, '', location.pathname + location.search);
    }
    if (opener?.isConnected && !opener.closest('[inert]')) opener.focus({preventScroll:true});
  }
  function synchronizeLocation() {
    closingHistory = false;
    if (pendingOpen) { const tab = pendingOpen; pendingOpen = ''; show(tab); return; }
    const tab = hashTab();
    if (tab) show(tabs.includes(tab) ? tab : 'project', {fromHistory:true});
    else if (dialog.open) dismiss({fromHistory:true});
  }
  document.addEventListener('click', event => {
    const modelLink = event.target.closest('.ws-model-settings');
    const link = event.target.closest('[data-ws-settings]') || modelLink;
    if (!link || event.defaultPrevented || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    if (modelLink) { pendingAgent = modelLink.closest('[data-ws-agent-panel]')?.dataset.wsAgentPanel || null; show('models'); find('[data-settings-agent-policy]').open = true; if (pendingAgent) { find('[data-settings-policy-agent]').value = pendingAgent; loadPolicy(pendingAgent); } }
    else show(link.dataset.wsSettings || 'project');
  });
  find('[data-ws-settings-close]').addEventListener('click', () => dismiss());
  dialog.addEventListener('cancel', event => { event.preventDefault(); dismiss(); });
  dialog.addEventListener('click', event => {
    const tab = event.target.closest('[data-settings-tab]')?.dataset.settingsTab || event.target.closest('[data-settings-go]')?.dataset.settingsGo;
    if (tab) selectTab(tab, {focus:true});
    const reload = event.target.closest('[data-settings-reload]')?.dataset.settingsReload;
    if (reload) load(reload);
    if (event.target === dialog) { const box = dialog.getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dismiss(); }
  });
  dialog.addEventListener('keydown', event => {
    const tab = event.target.closest('[data-settings-tab]');
    if (!tab || !['ArrowRight','ArrowLeft','ArrowDown','ArrowUp','Home','End'].includes(event.key)) return;
    event.preventDefault(); const index = tabs.indexOf(tab.dataset.settingsTab);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (['ArrowRight','ArrowDown'].includes(event.key) ? 1 : -1) + tabs.length) % tabs.length;
    selectTab(tabs[next], {focus:true});
  });
  addEventListener('popstate', synchronizeLocation);
  addEventListener('hashchange', synchronizeLocation);

  async function jsonRequest(path, {method = 'GET', body, signal} = {}) {
    if (!navigator.onLine) throw new Error('当前离线，连接恢复后可刷新或保存设置。');
    const csrf = shell.querySelector('[name="_csrf"]')?.value;
    if (method !== 'GET' && !csrf) throw new Error('当前连接尚未通过验证，请恢复连接后再保存。');
    const response = await fetch(apiBase + path, {method, credentials:'same-origin', cache:'no-store', redirect:'error', signal,
      headers:{Accept:'application/json', ...(body ? {'Content-Type':'application/json'} : {}), ...(method !== 'GET' ? {'x-csrf-token':csrf} : {})},
      ...(body ? {body:JSON.stringify(body)} : {})});
    if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('设置连接暂不可用。请稍后重试，当前工作已保留。');
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : response.status === 401 ? '登录已过期，请重新登录后修改设置。' : '无法保存此项设置，请检查输入后重试。');
    return data;
  }
  async function load(tab) {
    if (mutationBusy) return;
    const kind = tab === 'models' ? 'model' : 'connection';
    const revision = ++loadRevision; loadController?.abort(); loadController = new AbortController();
    const controller = loadController; const timeout = setTimeout(() => controller.abort(), 12000);
    status(kind, '正在读取…');
    try {
      const data = await jsonRequest(tab === 'models' ? '/model-services' : '/settings', {signal:controller.signal});
      if (revision !== loadRevision || !dialog.open) return;
      if (tab === 'models') { renderServices(data); populatePolicyAgents(data.agents); } else renderSettings(data);
    } catch (error) {
      if (revision !== loadRevision || !dialog.open) return;
      status(kind, error.name === 'AbortError' ? '读取超时，可点击刷新重试。' : error.message, 'error');
    } finally { clearTimeout(timeout); }
  }
  function create(tag, text, className = '') { const node = document.createElement(tag); node.textContent = text; if (className) node.className = className; return node; }
  function fillOptions(select, items, value, label, selected) { select.replaceChildren(); for (const item of items) { const option = document.createElement('option'); option.value = String(value(item)); option.textContent = label(item); option.selected = String(selected) === option.value; select.append(option); } }
  function renderSettings(data) {
    settings = data; populatePolicyAgents(data.agents);
    const members = data.members || data.memberships || [];
    const canManage = data.can_manage === true;
    const list = find('[data-settings-members]'); list.replaceChildren();
    for (const member of members) {
      const row = create('div', '', 'ws-settings-row'), copy = create('div', '');
      copy.append(create('strong', member.device_label || member.label || member.name || '执行设备'));
      const permission = {write:'执行与写入',read:'仅查看',admin:'管理'}[member.permission] || member.permission;
      copy.append(create('small', [permission, member.presence_label || (member.is_agent_online ? '在线' : '离线'), member.device_id === data.primary_device_id ? '主设备' : ''].filter(Boolean).join(' · '))); row.append(copy);
      if (canManage) { const button = create('button', '移除', 'ws-settings-danger'); button.type = 'button'; button.addEventListener('click', () => { if (confirm('从本项目移除此执行设备？正在进行的工作不会自动改派。')) mutate('/settings/members/' + member.device_id, {method:'DELETE'}, 'connection', renderSettings); }); row.append(button); }
      list.append(row);
    }
    if (!members.length) list.append(create('p', '本项目尚未添加执行设备。', 'ws-settings-hint'));
    const primary = find('[data-settings-primary-form]'); primary.hidden = !canManage;
    fillOptions(primary.elements.device_id, [{device_id:'',device_label:'未设置主设备'}, ...members.filter(item => !item.server_ai_provider && !item.agent_tasks_only && ['write','admin'].includes(item.permission))], item => item.device_id, item => item.device_label || item.label, data.primary_device_id || '');
    const add = find('[data-settings-member-form]'); const available = data.available_devices || data.devices || [];
    add.hidden = !canManage || !available.length;
    fillOptions(add.elements.device_id, available, item => item.id, item => item.label || item.name, '');
    status('connection', canManage ? '连接信息已更新。' : '连接信息已更新；只有管理员可以修改。', 'success');
  }
  async function mutate(path, options, kind, render) {
    if (mutationBusy) return; mutationBusy = true; loadRevision++; loadController?.abort();
    const controls = new Map([...dialog.querySelectorAll('form button, form input, form select, [data-settings-services] button, [data-settings-members] button, [data-settings-add-service], [data-settings-policy-agent]')].map(control => [control, control.disabled]));
    controls.forEach((_, control) => { control.disabled = true; }); status(kind, '正在保存…');
    const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const result = await jsonRequest(path, {...options, signal:controller.signal});
      render(result); status(kind, '设置已保存。', 'success'); return true;
    } catch (error) { status(kind, error.name === 'AbortError' ? '保存结果尚未确认，请刷新列表核对后再操作。' : error.message, 'error'); return false; }
    finally { clearTimeout(timeout); mutationBusy = false; controls.forEach((disabled, control) => { control.disabled = disabled; }); }
  }
  find('[data-settings-primary-form]').addEventListener('submit', event => { event.preventDefault(); mutate('/settings/primary-device', {method:'POST', body:{device_id:event.target.elements.device_id.value ? Number(event.target.elements.device_id.value) : null}}, 'connection', renderSettings); });
  find('[data-settings-member-form]').addEventListener('submit', event => { event.preventDefault(); mutate('/settings/members', {method:'POST', body:{device_id:Number(event.target.elements.device_id.value), permission:event.target.elements.permission.value}}, 'connection', renderSettings); });

  const policyForm = find('[data-settings-policy-form]');
  const agentPicker = find('[data-settings-policy-agent]');
  const effortLabels = {none:'关闭',minimal:'最低',low:'低',medium:'中',high:'高',xhigh:'更高',max:'最高',ultra:'最高'};
  function populatePolicyAgents(agents) {
    const current = pendingAgent || agentPicker.value;
    const list = agents || [...shell.querySelectorAll('[data-ws-agent-panel]')].map(node => ({id:node.dataset.wsAgentPanel, name:node.querySelector('.ws-profile-top h2')?.textContent || '智能体'}));
    fillOptions(agentPicker, [{id:'', name:'选择智能体'}, ...list], item => item.id, item => item.name, current);
    if (pendingAgent && agentPicker.value) { pendingAgent = null; loadPolicy(agentPicker.value); }
  }
  function policyOptions() {
    const manual = policyForm.elements.mode.value === 'manual';
    const item = policyModels.find(row => row.model === policyForm.elements.model.value);
    const effort = policyForm.elements.reasoning_effort;
    const previous = effort.value;
    const supported = (item?.supportedReasoningEfforts || []).map(row => typeof row === 'string' ? row : row.reasoningEffort);
    fillOptions(effort, supported.length ? supported : [''], value => value, value => effortLabels[value] || (manual ? '服务默认' : '按任务选择'), previous);
    policyForm.elements.model.disabled = !manual; policyForm.elements.model.required = manual;
    policyForm.elements.model.setCustomValidity(manual && policyForm.elements.model.value && !item ? '原模型当前不可用，请选择已上报的模型或恢复总控分配。' : '');
    effort.disabled = !manual || !supported.length;
  }
  async function loadPolicy(id) {
    const revision = ++policyRevision; policyController?.abort(); policyForm.hidden = true;
    if (!id) { status('policy', '选择身份后可查看当前分配方式。'); return; }
    policyController = new AbortController(); const controller = policyController;
    const timeout = setTimeout(() => controller.abort(), 12000); status('policy', '正在读取模型分配…');
    try {
      const data = await jsonRequest('/settings/agents/' + id + '/model-policy', {signal:controller.signal});
      if (revision !== policyRevision || agentPicker.value !== String(id) || !dialog.open) return;
      policyModels = data.models || []; const policy = data.policy || {};
      const unavailable = policy.model && !policyModels.some(item => item.model === policy.model);
      const options = [{model:'',displayName:'选择模型'}, ...policyModels];
      if (unavailable) options.push({model:policy.model,displayName:policy.model + ' · 当前不可用'});
      fillOptions(policyForm.elements.model, options, item => item.model, item => item.displayName || item.model, policy.model || '');
      policyForm.elements.mode.value = policy.mode || 'controller'; policyOptions();
      if (policy.reasoning_effort) policyForm.elements.reasoning_effort.value = policy.reasoning_effort;
      policyForm.hidden = false; status('policy', unavailable ? '已保留原手动选择，但此模型当前不可用。请选择其他模型或恢复总控分配。' : policyModels.length ? '只对新开始的工作生效。' : '此连接尚未上报可选模型，可继续由总控分配。', unavailable ? 'error' : '');
    } catch (error) { if (revision === policyRevision) status('policy', error.name === 'AbortError' ? '读取超时，请重新选择身份重试。' : error.message, 'error'); }
    finally { clearTimeout(timeout); }
  }
  agentPicker.addEventListener('change', () => loadPolicy(agentPicker.value));
  policyForm.elements.mode.addEventListener('change', policyOptions);
  policyForm.elements.model.addEventListener('change', policyOptions);
  policyForm.addEventListener('submit', async event => {
    event.preventDefault(); const id = agentPicker.value;
    const body = {mode:policyForm.elements.mode.value, model:policyForm.elements.model.value, reasoning_effort:policyForm.elements.reasoning_effort.value, reason:'用户在工作区设置中指定'};
    const saved = await mutate('/settings/agents/' + id + '/model-policy', {method:'POST', body}, 'policy', () => {});
    if (saved && dialog.open && agentPicker.value === id) loadPolicy(id);
  });
  populatePolicyAgents();

  // Model-service actions use the same JSON-only request gate as connections.
  function renderServices(data) {
    services = data.services || [];
    serviceDevices = data.devices || [];
    const selectedDevice = serviceForm.elements.device_id.value;
    fillOptions(serviceForm.elements.device_id, [{device_id:'',device_label:'选择执行电脑'}, ...serviceDevices], item => item.device_id, item => item.device_label + (item.device_id && !item.is_agent_online ? ' · 离线' : ''), selectedDevice);
    const list = find('[data-settings-services]'); list.replaceChildren();
    for (const service of services) {
      const row = create('div', '', 'ws-settings-row'), copy = create('div', '');
      copy.append(create('strong', service.name));
      copy.append(create('small', [service.model, service.enabled ? '已启用' : '已停用', service.has_api_key ? '已保存密钥' : '未设置密钥'].filter(Boolean).join(' · ')));
      copy.append(create('small', service.base_url || ''));
      const device = serviceDevices.find(item => item.device_id === service.device_id);
      copy.append(create('small', [device?.device_label || '未找到执行电脑', service.status_message, service.models?.length ? `发现 ${service.models.length} 个模型` : ''].filter(Boolean).join(' · '))); row.append(copy);
      const actions = create('div', '', 'ws-settings-service-actions');
      for (const [label, action] of [['编辑', () => editService(service)], ['测试', () => testService(service)], ['删除', () => { if (confirm('删除此模型服务？已开始的任务仍沿用原来的安排。')) mutate('/model-services/' + service.id, {method:'DELETE'}, 'model', renderServices); }]]) {
        const button = create('button', label); button.type = 'button'; button.addEventListener('click', action); actions.append(button);
      }
      if (data.can_manage !== false) row.append(actions); list.append(row);
    }
    find('[data-settings-add-service]').hidden = data.can_manage === false;
    if (!services.length) list.append(create('div', '还没有模型服务。添加本地模型或兼容 API 后，即可在项目中使用。', 'ws-settings-note'));
    status('model', '模型服务列表已更新。', 'success');
    clearTimeout(modelPollTimer);
    if (services.some(service => ['queued','pending','testing'].includes(service.status)) && dialog.open && activeTab === 'models') modelPollTimer = setTimeout(() => load('models'), 3500);
  }
  const serviceForm = find('[data-settings-service-form]');
  function endpointHint() { find('[data-settings-endpoint-hint]').textContent = serviceForm.elements.kind.value === 'openai_compatible' ? '填写兼容服务的 API 根地址。请求由上方所选电脑发起。' : '先在所选电脑启动模型服务。127.0.0.1 指该执行电脑；本地模型需要支持 OpenAI 兼容接口。'; }
  function editService(service = null) {
    serviceForm.reset(); serviceForm.hidden = false;
    if (service) for (const key of ['id','name','kind','base_url','model','device_id']) serviceForm.elements[key].value = service[key] || '';
    serviceForm.elements.enabled.checked = service ? Boolean(service.enabled) : true;
    serviceForm.elements.supports_images.checked = Boolean(service?.supports_images);
    serviceForm.elements.supports_tools.checked = Boolean(service?.supports_tools);
    serviceForm.elements.clear_api_key.checked = false;
    const discovered = find('#ws-settings-discovered-models'); discovered.replaceChildren();
    for (const model of service?.models || []) { const option = document.createElement('option'); option.value = typeof model === 'string' ? model : model.id; if (option.value) discovered.append(option); }
    serviceForm.elements.api_key.placeholder = service?.has_api_key ? '留空保留已保存密钥' : '本地服务可留空';
    find('[data-settings-service-heading]').textContent = service ? '编辑模型服务' : '添加模型服务';
    endpointHint(); serviceForm.elements.name.focus({preventScroll:true}); serviceForm.scrollIntoView({block:'nearest', behavior:'instant'}); enter(serviceForm, {distance:3});
  }
  find('[data-settings-add-service]').addEventListener('click', () => editService());
  find('[data-settings-cancel-service]').addEventListener('click', () => { serviceForm.elements.api_key.value = ''; serviceForm.hidden = true; });
  serviceForm.elements.kind.addEventListener('change', () => {
    const defaults = {ollama:'http://127.0.0.1:11434/v1', lmstudio:'http://127.0.0.1:1234/v1'};
    const endpoint = serviceForm.elements.base_url;
    if (!endpoint.value || Object.values(defaults).includes(endpoint.value)) endpoint.value = defaults[serviceForm.elements.kind.value] || '';
    endpointHint();
  }); endpointHint();
  serviceForm.addEventListener('submit', async event => {
    event.preventDefault(); const values = Object.fromEntries(new FormData(serviceForm)); const id = values.id; delete values.id;
    values.enabled = serviceForm.elements.enabled.checked;
    values.device_id = Number(serviceForm.elements.device_id.value);
    values.supports_images = serviceForm.elements.supports_images.checked;
    values.supports_tools = serviceForm.elements.supports_tools.checked;
    values.clear_api_key = serviceForm.elements.clear_api_key.checked;
    if (id && !values.api_key) delete values.api_key;
    const saved = await mutate('/model-services' + (id ? '/' + id : ''), {method:id ? 'PUT' : 'POST', body:values}, 'model', renderServices);
    serviceForm.elements.api_key.value = ''; if (saved) serviceForm.hidden = true;
  });
  async function testService(service) {
    if (mutationBusy) return; mutationBusy = true; status('model', '正在测试服务连接…');
    const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 20000);
    try { const data = await jsonRequest('/model-services/' + service.id + '/test', {method:'POST', body:{}, signal:controller.signal}); status('model', data.message || (data.queued ? '已安排连接测试，等待执行电脑返回结果。' : data.ok ? '服务连接正常。' : '服务未通过连接测试。'), data.queued ? '' : data.ok ? 'success' : 'error');
      if (data.queued && dialog.open && activeTab === 'models') modelPollTimer = setTimeout(() => load('models'), 3500); }
    catch (error) { status('model', error.name === 'AbortError' ? '连接测试超时，请检查服务地址后重试。' : error.message, 'error'); }
    finally { clearTimeout(timeout); mutationBusy = false; }
  }
  window.HubWorkspaceSettings = {open:show, close:dismiss};
  addEventListener('pagehide', () => { loadController?.abort(); for (const node of animations.keys()) cancelMotion(node); serviceForm.elements.api_key.value = ''; });
  synchronizeLocation();
})();
