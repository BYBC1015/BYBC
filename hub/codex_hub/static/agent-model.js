(() => {
  'use strict';
  const form = document.querySelector('#agent-model-settings');
  if (!form) return;
  const mode = form.elements.mode;
  const model = form.elements.model;
  const effort = form.elements.reasoning_effort;
  const help = form.querySelector('[data-model-help]');
  const submit = form.querySelector('button[type="submit"]');
  const labels = { none:'无', minimal:'极低', low:'低', medium:'中', high:'高', xhigh:'更高', max:'最大', ultra:'极高' };
  let catalogue;
  try { catalogue = JSON.parse(form.dataset.modelCatalog || '[]'); } catch (_) { return; }
  if (!Array.isArray(catalogue)) return;
  let rememberedEffort = effort.value;

  function update(preserveInvalid = false) {
    const manual = mode.value === 'manual';
    const item = catalogue.find(row => row.model === model.value);
    const supported = (item?.supportedReasoningEfforts || []).map(row => row.reasoningEffort).filter(value => value in labels);
    const previous = rememberedEffort || effort.value;
    model.disabled = !manual;
    model.required = manual;
    model.setCustomValidity(manual && model.value && !item ? '这个型号尚未获执行连接核验，请重新选择。' : '');
    effort.replaceChildren();
    effort.setCustomValidity('');
    if (manual && item && !supported.length) {
      effort.add(new Option('尚未上报支持的档位', ''));
      effort.setCustomValidity('执行连接尚未上报这个模型支持的档位，请稍后重试或选择其他模型。');
    } else if (supported.length) {
      supported.forEach(value => effort.add(new Option(labels[value], value)));
      if (preserveInvalid && previous && !supported.includes(previous)) {
        const unavailable = new Option((labels[previous] || previous) + ' · 当前不支持', previous, true, true);
        unavailable.disabled = true;
        effort.add(unavailable, 0);
        effort.setCustomValidity('请从这个模型当前支持的档位中重新选择。');
      } else {
        effort.value = supported.includes(previous) ? previous : (supported.includes(item.defaultReasoningEffort) ? item.defaultReasoningEffort : supported[0]);
      }
    } else {
      effort.add(new Option(manual ? '先选择模型' : '按任务选择', ''));
    }
    effort.disabled = !manual || !item;
    effort.required = manual && !!item;
    if (effort.value) rememberedEffort = effort.value;
    if (help) help.textContent = manual ? '仅列出执行连接已上报的支持档位。' : '总控会结合每项工作选择模型与思考档位。';
  }

  mode.addEventListener('change', () => update());
  model.addEventListener('change', () => update());
  effort.addEventListener('change', () => {
    rememberedEffort = effort.value;
    effort.setCustomValidity('');
  });
  form.addEventListener('submit', () => {
    submit.disabled = true;
    submit.textContent = '正在保存…';
    form.setAttribute('aria-busy', 'true');
  });
  window.addEventListener('pageshow', () => {
    submit.disabled = false;
    submit.textContent = '保存模型设置';
    form.removeAttribute('aria-busy');
  });
  update(form.dataset.hasError === 'true' || mode.value === 'manual');
  if (form.dataset.hasError === 'true') {
    const alert = form.querySelector('[role="alert"]');
    if (alert) {
      alert.tabIndex = -1;
      alert.focus({ preventScroll:true });
      alert.scrollIntoView({ block:'center' });
    }
  }
})();
