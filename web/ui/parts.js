// «Разметка»: the figure broken into body parts, hands and fingers (docs/PARTS.md).
// The document lives in the backend (figure_doc.py, one project folder per picture); every edit
// here is a command through api.parts_call — the same commands an AI agent uses through the
// agent bridge, with one undo history. Analysis runs in a separate process (parts_service.py);
// its result goes in directly while the figure is untouched, otherwise as a proposal.

const GROUP = 'Разметка';
const LAYER_ID = 'parts-editor';
const MODES = [
  ['original', 'Оригинал', 'Только картинка'],
  ['parts', 'Области', 'Цветные области частей'],
  ['markup', 'Оригинал + разметка', 'Картинка, области поверх и границы'],
  ['skeleton', 'Скелет', 'Суставы тела и кистей'],
  ['uncertain', 'Сомнительное', 'Штриховка: пиксели без части и предположения'],
];
const TOOLS = [
  ['view', 'Просмотр', 'Двигать и приближать (колесо, перетаскивание). Наведение — имя части', 'V'],
  ['brush', 'Кисть', 'Добавить пиксели выбранной части (в пределах силуэта)', 'B'],
  ['erase', 'Ластик', 'Убрать пиксели выбранной части (станут «неизвестно»)', 'E'],
  ['background', 'Фон', 'Сделать пиксели фоном (не фигура)', 'N'],
  ['fill', 'Заливка', 'Клик — залить область рисунка, ограниченную контуром; Alt+клик — убрать', 'G'],
  ['joints', 'Суставы', 'Перетащить сустав или точку кисти', 'J'],
  ['split', 'Разрез', 'Провести линию: часть слева от неё уйдёт в «куда»', 'X'],
  ['roi', 'Область', 'Рамка фигуры для анализа (картинка с несколькими людьми)', 'R'],
];
const GROUP_NAMES = {
  head_neck: 'Голова и шея', torso: 'Туловище', left_arm: 'Левая рука', left_hand: 'Левая кисть',
  right_arm: 'Правая рука', right_hand: 'Правая кисть', left_leg: 'Левая нога', right_leg: 'Правая нога',
};
const STATES = [['visible', 'видно'], ['uncertain', 'сомнительно'], ['hidden', 'закрыто'], ['absent', 'нет']];
const REVIEWS = [['unreviewed', 'не проверено'], ['accepted', 'верно'], ['rejected', 'неверно'], ['edited', 'исправлено']];
const SOURCES = { model: 'модель', estimated: 'оценка', user: 'вы', ai: 'ИИ' };
const STATE_ICON = { visible: '●', uncertain: '◐', hidden: '○', absent: '·' };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(className, text, title) {
  const node = el('button', className, text);
  node.type = 'button';
  if (title) node.title = title;
  return node;
}

function select(options, className = 'parts-select') {
  const node = el('select', className);
  for (const [value, label] of options) {
    const option = el('option', '', label);
    option.value = value;
    node.append(option);
  }
  return node;
}

const rgb = (c) => `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const prefix = () => location.pathname.replace(/\/web\/index\.html$/, '');

function partsIcon() {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  for (const d of ['M12 3.5a2.2 2.2 0 1 1 0 4.4 2.2 2.2 0 0 1 0-4.4Z', 'M8.5 10h7l-1 5.5h-5Z', 'M8.5 10 5 14', 'M15.5 10l3.5 4',
    'M9.5 15.5 8.5 21', 'M14.5 15.5l1 5.5']) {
    const path = document.createElementNS(ns, 'path');
    path.setAttribute('d', d);
    svg.append(path);
  }
  return svg;
}

export function install(app) {
  let caps = null; // get_capabilities: parts {id: {value, parent, name, color}}, joints
  let project = null; // summary of the open project
  let pid = null;
  let iid = 'fig1';
  let parts = {}; // get_parts().parts
  let unknownPx = 0;
  let joints = {};
  let bones = [];
  let chains = [];
  let revision = -1;
  let mode = 'markup';
  let tool = 'view';
  let activePart = 'head';
  let selectedJoint = null;
  let radius = 8;
  let opacity = 0.55;
  let job = null;
  let jobTimer = 0;
  let issues = null;
  let toastTimer = 0;
  let busy = false;
  const images = { source: null, parts: null, uncertain: null, outline: null };
  let pick = null; // {data, width} of the parts layer, to name the part under the cursor
  let colorToPart = new Map();
  const view = { s: 1, ox: 0, oy: 0 };
  let drag = null; // current pointer gesture

  // --- DOM ------------------------------------------------------------------------------
  const openButton = button('parts-open', '', 'Разметка частей тела (U)');
  openButton.append(partsIcon());
  openButton.addEventListener('click', () => (isOpen() ? close() : open()));

  const layer = el('div', 'parts-layer');
  layer.hidden = true;
  layer.setAttribute('role', 'dialog');
  layer.setAttribute('aria-modal', 'true');
  layer.setAttribute('aria-label', 'Разметка частей тела');

  const top = el('div', 'parts-top');
  const title = el('div', 'parts-title', 'Разметка');
  const meta = el('span', 'parts-meta');
  const openMenu = select([['', 'Открыть…'], ['pose', 'Текущая поза'], ['image', 'Картинка…'], ['folder', 'Папка проекта…']], 'parts-select parts-menu');
  const undoButton = button('parts-btn', '↶', 'Отменить (Ctrl+Z)');
  const redoButton = button('parts-btn', '↷', 'Повторить (Ctrl+Y)');
  const analyzeButton = button('parts-btn primary', 'Распознать', 'Найти суставы, кисти и части автоматически');
  const masksButton = button('parts-btn', 'Маски по суставам', 'Пересчитать области по текущим суставам (после правки скелета)');
  const progress = el('div', 'parts-progress');
  const progressBar = el('div', 'parts-progress-bar');
  const progressText = el('span', 'parts-progress-text');
  const cancelButton = button('parts-btn subtle', 'Отмена', 'Прервать анализ');
  progress.append(progressBar, progressText, cancelButton);
  progress.hidden = true;
  const validateButton = button('parts-btn', 'Проверить', 'Диагностика: пустые части, суставы вне фигуры, стороны…');
  const exportButton = button('parts-btn', 'Экспорт…', 'Исходник, карта меток, маски частей и JSON в выбранную папку');
  const closeButton = button('parts-btn subtle', '✕', 'Закрыть (Esc)');
  top.append(title, meta, openMenu, el('span', 'parts-spacer'), progress, analyzeButton, masksButton, undoButton, redoButton,
    validateButton, exportButton, closeButton);

  const proposal = el('div', 'parts-proposal');
  proposal.hidden = true;
  const proposalText = el('span', '', 'Новый анализ готов. Ваши правки и зафиксированное не тронуты, пока вы не примете его.');
  const acceptButton = button('parts-btn primary', 'Принять', 'Заменить незафиксированное результатом анализа');
  const discardButton = button('parts-btn', 'Отклонить');
  proposal.append(proposalText, el('span', 'parts-spacer'), acceptButton, discardButton);

  const notice = el('div', 'parts-notice');
  notice.hidden = true;

  const body = el('div', 'parts-body');
  const side = el('div', 'parts-side');
  const partList = el('div', 'parts-list');
  side.append(partList);

  const stage = el('div', 'parts-stage');
  const canvas = el('canvas', 'parts-canvas');
  const ctx = canvas.getContext('2d');
  const toolbar = el('div', 'parts-toolbar');
  const modeBar = el('div', 'parts-modes');
  const modeButtons = MODES.map(([id, label, hint]) => {
    const node = button('parts-chip', label, hint);
    node.addEventListener('click', () => setMode(id));
    modeBar.append(node);
    return [id, node];
  });
  const toolBar = el('div', 'parts-tools');
  const toolButtons = TOOLS.map(([id, label, hint, key]) => {
    const node = button('parts-chip', label, `${hint} (${key})`);
    node.addEventListener('click', () => setTool(id));
    toolBar.append(node);
    return [id, node];
  });
  const sizeLabel = el('label', 'parts-range');
  const sizeInput = el('input');
  sizeInput.type = 'range';
  sizeInput.min = '1';
  sizeInput.max = '80';
  sizeInput.value = String(radius);
  const sizeValue = el('span', '', `${radius}px`);
  sizeLabel.append('Кисть ', sizeInput, sizeValue);
  const opacityLabel = el('label', 'parts-range');
  const opacityInput = el('input');
  opacityInput.type = 'range';
  opacityInput.min = '0.1';
  opacityInput.max = '1';
  opacityInput.step = '0.05';
  opacityInput.value = String(opacity);
  opacityLabel.append('Прозрачность ', opacityInput);
  const splitTarget = el('select', 'parts-select');
  const splitLabel = el('label', 'parts-range');
  splitLabel.append('Куда ', splitTarget);
  toolbar.append(modeBar, toolBar, sizeLabel, splitLabel, opacityLabel);
  const status = el('div', 'parts-status');
  stage.append(toolbar, canvas, status);

  const details = el('div', 'parts-details');
  body.append(side, stage, details);
  const toastBox = el('div', 'parts-toast');
  toastBox.setAttribute('role', 'status');
  layer.append(top, proposal, notice, body, toastBox);
  document.body.append(openButton, layer);

  function toast(text, error = false) {
    toastBox.textContent = text;
    toastBox.classList.toggle('error', error);
    toastBox.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastBox.classList.remove('show'), error ? 4200 : 2200);
  }

  const isOpen = () => !layer.hidden;

  // --- Backend --------------------------------------------------------------------------
  async function call(command, args = {}, withRevision = false) {
    const reply = await app.api.parts_call(pid, command, args, withRevision ? revision : null);
    if (reply && reply.error) {
      if (reply.code === 'conflict') await refresh();
      throw new Error(reply.error);
    }
    return reply;
  }

  async function run(command, args = {}) {
    // A user edit: one undo step, based on the revision on screen.
    if (busy || !pid) return null;
    busy = true;
    try {
      const reply = await call(command, args, true);
      await refresh();
      return reply;
    } catch (error) {
      toast(String(error.message || error), true);
      return null;
    } finally {
      busy = false;
    }
  }

  async function refresh() {
    if (!pid) return;
    project = await app.api.parts_summary(pid);
    const inst = project.instances.find((i) => i.id === iid) || project.instances[0];
    iid = inst.id;
    const [p, j] = await Promise.all([call('get_parts', { instance_id: iid }), call('get_joints', { instance_id: iid })]);
    parts = p.parts;
    unknownPx = p.unknown_px;
    joints = j.joints;
    bones = j.bones;
    chains = j.hand_chains;
    const changed = project.revision !== revision;
    revision = project.revision;
    if (changed) await loadLayers();
    renderTop();
    renderList();
    renderDetails();
    draw();
  }

  function layerUrl(kind) {
    return `${prefix()}/figures/${pid}/${iid}/${kind}.png?r=${revision}`;
  }

  function loadImage(url) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error(`Не загрузилось: ${url}`));
      image.src = url;
    });
  }

  async function loadLayers() {
    const kinds = images.source ? ['parts', 'uncertain', 'outline'] : ['source', 'parts', 'uncertain', 'outline'];
    const loaded = await Promise.all(kinds.map((k) => loadImage(layerUrl(k))));
    kinds.forEach((k, i) => { images[k] = loaded[i]; });
    const off = document.createElement('canvas');
    off.width = images.parts.naturalWidth;
    off.height = images.parts.naturalHeight;
    const octx = off.getContext('2d', { willReadFrequently: true });
    octx.drawImage(images.parts, 0, 0);
    pick = { data: octx.getImageData(0, 0, off.width, off.height).data, width: off.width, height: off.height };
  }

  async function openProject(summary) {
    if (!summary) return;
    if (summary.error) throw new Error(summary.error);
    if (pid && pid !== summary.id) await app.api.parts_close(pid).catch(() => {});
    pid = summary.id;
    iid = summary.instances[0].id;
    revision = -1;
    images.source = null;
    issues = null;
    selectedJoint = null;
    job = summary.job;
    await refresh();
    fit();
    if (job) watchJob();
    const inst = project.instances[0];
    const empty = !Object.values(parts).some((part) => part.px) && !inst.analysis;
    if (empty && project.analyzer.available && !job) analyze('full');
  }

  // --- Open / close ---------------------------------------------------------------------
  async function open(how = 'pose') {
    if (!caps) caps = await app.api.parts_capabilities();
    buildColorIndex();
    layer.hidden = false;
    document.body.classList.add('parts-open-editor');
    app.overlays.open(LAYER_ID, close);
    window.addEventListener('keydown', onKey, true);
    resizeCanvas();
    try {
      if (how === 'pose') {
        const pose = app.current();
        if (!pose) throw new Error('Нет открытой позы');
        await openProject(await app.api.parts_open_pose(pose.file));
      } else if (how === 'image') {
        await openProject(await app.api.parts_open_image());
      } else if (how === 'folder') {
        await openProject(await app.api.parts_open_folder());
      }
    } catch (error) {
      toast(String(error.message || error), true);
      renderTop();
    }
  }

  function close() {
    if (!isOpen()) return;
    layer.hidden = true;
    document.body.classList.remove('parts-open-editor');
    app.overlays.close(LAYER_ID);
    window.removeEventListener('keydown', onKey, true);
    clearTimeout(jobTimer);
  }

  openMenu.addEventListener('change', () => {
    const how = openMenu.value;
    openMenu.value = '';
    if (how) open(how);
  });

  function buildColorIndex() {
    colorToPart = new Map();
    for (const [id, part] of Object.entries(caps.parts)) {
      const key = part.color.join(',');
      colorToPart.set(key, colorToPart.has(key) ? null : id); // null: two parts share a colour
    }
    splitTarget.replaceChildren();
    for (const [id, part] of Object.entries(caps.parts)) {
      const option = el('option', '', part.name);
      option.value = id;
      splitTarget.append(option);
    }
    splitTarget.value = 'left_hand.thumb';
  }

  // --- Analysis -------------------------------------------------------------------------
  async function analyze(kind) {
    if (!pid) return;
    const reply = await app.api.parts_analyze(pid, kind);
    if (reply.error) {
      toast(reply.error, true);
      return;
    }
    job = reply;
    watchJob();
  }

  function watchJob() {
    clearTimeout(jobTimer);
    renderTop();
    if (!job || !['queued', 'running'].includes(job.status)) return;
    jobTimer = setTimeout(async () => {
      try {
        job = await app.api.parts_job(job.job_id);
      } catch (error) {
        job = null;
      }
      if (job && ['queued', 'running'].includes(job.status)) {
        watchJob();
        return;
      }
      jobFinished();
    }, 400);
  }

  async function jobFinished() {
    const done = job;
    renderTop();
    if (!done) return;
    if (done.status === 'done') {
      toast(done.result?.applied ? `Готово за ${done.result.seconds ?? '?'} с` : 'Готово: результат — предложение, проверьте и примите');
    } else if (done.status === 'error') {
      toast(done.error || 'Анализ не удался', true);
    } else if (done.status === 'cancelled') {
      toast('Анализ отменён');
    }
    job = null;
    await refresh();
  }

  analyzeButton.addEventListener('click', () => analyze('full'));
  masksButton.addEventListener('click', () => analyze('masks'));
  cancelButton.addEventListener('click', async () => {
    if (job) job = await app.api.parts_cancel(job.job_id);
  });
  acceptButton.addEventListener('click', () => run('apply_proposal', { instance_id: iid }));
  discardButton.addEventListener('click', () => run('discard_proposal', { instance_id: iid }));
  undoButton.addEventListener('click', () => history('undo'));
  redoButton.addEventListener('click', () => history('redo'));
  validateButton.addEventListener('click', async () => {
    try {
      issues = (await call('validate', { instance_id: iid })).issues;
      renderDetails();
    } catch (error) {
      toast(String(error.message || error), true);
    }
  });
  exportButton.addEventListener('click', async () => {
    try {
      const result = await app.api.parts_export(pid);
      if (result) toast(`Экспортировано: ${result.folder}`);
    } catch (error) {
      toast(String(error.message || error), true);
    }
  });

  async function history(which) {
    if (!pid || busy) return;
    busy = true;
    try {
      await call(which);
      await refresh();
    } catch (error) {
      toast(String(error.message || error), true);
    } finally {
      busy = false;
    }
  }

  // Analysis progress and agent edits arrive from the backend (app.py parts_changed).
  window.__partsChanged = (change) => {
    if (!isOpen() || !change || change.project_id !== pid) return;
    if (change.job) {
      job = change.job;
      if (['queued', 'running'].includes(job.status)) renderTop();
      return;
    }
    if (change.revision !== revision && !busy) {
      if (change.source === 'ai') toast('Изменение от ИИ-агента');
      refresh().catch((error) => toast(String(error.message || error), true));
    }
  };

  // --- Rendering: top bar, list, details -----------------------------------------------
  function renderTop() {
    const inst = project?.instances?.find((i) => i.id === iid);
    title.textContent = project ? project.name || 'Разметка' : 'Разметка';
    meta.textContent = project ? `${project.source.width}×${project.source.height} · ревизия ${project.revision}` : '';
    undoButton.disabled = !project?.can_undo;
    redoButton.disabled = !project?.can_redo;
    const running = job && ['queued', 'running'].includes(job.status);
    const available = !!project?.analyzer?.available;
    analyzeButton.hidden = masksButton.hidden = !!running;
    analyzeButton.disabled = masksButton.disabled = !available || !pid;
    progress.hidden = !running;
    if (running) {
      progressBar.style.setProperty('--p', `${Math.round((job.progress || 0) * 100)}%`);
      progressText.textContent = `${job.mode === 'masks' ? 'Маски' : 'Анализ'}: ${Math.round((job.progress || 0) * 100)}%`;
    }
    proposal.hidden = !inst?.proposal;
    const lines = [];
    if (project && !available) lines.push(`Автоматический анализ недоступен: ${project.analyzer.reason}. Разметку можно делать вручную.`);
    const analysis = inst?.analysis;
    if (analysis) {
      const hands = Object.entries(analysis.hands || {}).map(([s, h]) => `${s === 'left' ? 'левая' : 'правая'} кисть${h.issues?.length ? ' (сомнительно)' : ''}`);
      lines.push(`Анализ: ${analysis.body === 'document joints' ? 'по суставам документа' : analysis.model?.name || 'модель'}`
        + `${analysis.view ? `, ракурс ${analysis.view}` : ''}${hands.length ? `, найдены: ${hands.join(', ')}` : ', кисти не найдены'}.`
        + ' Границы частей — оценка по костям и контурам рисунка.');
    }
    notice.textContent = lines.join(' ');
    notice.hidden = !lines.length;
  }

  function renderList() {
    partList.replaceChildren();
    if (!caps) return;
    const groups = new Map();
    for (const [id, part] of Object.entries(parts)) {
      if (!groups.has(part.parent)) groups.set(part.parent, []);
      groups.get(part.parent).push([id, part]);
    }
    for (const [group, items] of groups) {
      partList.append(el('div', 'parts-group', GROUP_NAMES[group] || group));
      for (const [id, part] of items) {
        const row = el('div', `parts-row${id === activePart ? ' active' : ''}${part.px ? '' : ' empty'}`);
        row.title = `${part.name}: ${part.px} px`;
        const swatch = el('span', 'parts-swatch');
        swatch.style.background = rgb(part.color);
        const name = el('span', 'parts-name', part.name);
        const state = el('span', `parts-state ${part.state}`, STATE_ICON[part.state] || '');
        state.title = STATES.find(([s]) => s === part.state)?.[1] || part.state;
        const lock = button(`parts-lock${part.locked ? ' on' : ''}`, part.locked ? '🔒' : '🔓', part.locked ? 'Зафиксировано: анализ не тронет' : 'Зафиксировать');
        lock.addEventListener('click', (event) => {
          event.stopPropagation();
          run('set_locked', { entity_id: id, locked: !part.locked, instance_id: iid });
        });
        row.append(swatch, name, state, lock);
        row.addEventListener('click', () => {
          activePart = id;
          selectedJoint = null;
          renderList();
          renderDetails();
        });
        partList.append(row);
      }
    }
    const unknown = el('div', 'parts-row unknown');
    unknown.append(el('span', 'parts-swatch magenta'), el('span', 'parts-name', 'Без части'), el('span', 'parts-px', `${unknownPx} px`));
    unknown.title = 'Пиксели фигуры, которые ещё не отнесены ни к одной части';
    partList.append(unknown);
  }

  function field(label, control) {
    const row = el('label', 'parts-field');
    row.append(el('span', '', label), control);
    return row;
  }

  function renderDetails() {
    details.replaceChildren();
    if (!pid) {
      details.append(el('p', 'parts-muted', 'Откройте позу, картинку или проект.'));
      return;
    }
    if (selectedJoint && joints[selectedJoint]) {
      const joint = joints[selectedJoint];
      details.append(el('h3', '', `Сустав ${selectedJoint}`));
      const state = select(STATES);
      state.value = joint.state || 'visible';
      state.addEventListener('change', () => run('set_visibility', { entity_id: selectedJoint, state: state.value, instance_id: iid }));
      details.append(field('Состояние', state));
      const lock = el('input');
      lock.type = 'checkbox';
      lock.checked = !!joint.locked;
      lock.addEventListener('change', () => run('set_locked', { entity_id: selectedJoint, locked: lock.checked, instance_id: iid }));
      details.append(field('Зафиксирован', lock));
      details.append(el('p', 'parts-muted', `x ${joint.xy?.[0]}, y ${joint.xy?.[1]} · источник: ${SOURCES[joint.source?.kind] || joint.source?.kind || '—'}`
        + `${joint.score != null ? ` · оценка модели ${joint.score} (${joint.score_kind})` : ''}`));
      for (const d of joint.diagnostics || []) details.append(el('p', 'parts-diag', d));
    } else if (parts[activePart]) {
      const part = parts[activePart];
      const head = el('h3');
      const swatch = el('span', 'parts-swatch');
      swatch.style.background = rgb(part.color);
      head.append(swatch, ` ${part.name}`);
      details.append(head);
      const state = select(STATES);
      state.value = part.state;
      state.addEventListener('change', () => run('set_visibility', { entity_id: activePart, state: state.value, instance_id: iid }));
      details.append(field('Состояние', state));
      const review = select(REVIEWS);
      review.value = part.review || 'unreviewed';
      review.addEventListener('change', () => run('set_review', { entity_id: activePart, review: review.value, instance_id: iid }));
      details.append(field('Проверка', review));
      const lock = el('input');
      lock.type = 'checkbox';
      lock.checked = !!part.locked;
      lock.addEventListener('change', () => run('set_locked', { entity_id: activePart, locked: lock.checked, instance_id: iid }));
      details.append(field('Зафиксирована', lock));
      const relabel = select([['', '—'], ...Object.entries(caps.parts).filter(([id]) => id !== activePart).map(([id, p]) => [id, p.name])]);
      relabel.addEventListener('change', () => {
        if (relabel.value) run('relabel', { part_id: activePart, target: relabel.value, instance_id: iid });
      });
      details.append(field('Переименовать в', relabel));
      details.append(el('p', 'parts-muted', `${part.px} px${part.bbox ? ` · рамка ${part.bbox.join(', ')}` : ''} · источник: `
        + `${SOURCES[part.source?.kind] || part.source?.kind || '—'}${part.source?.method ? ` (${part.source.method})` : ''}`));
      for (const d of part.diagnostics || []) details.append(el('p', 'parts-diag', d));
    }
    if (issues) {
      details.append(el('h3', '', `Проверка: ${issues.length ? issues.length : 'замечаний нет'}`));
      for (const issue of issues) {
        const row = el('p', `parts-issue ${issue.severity}`, issue.message);
        if (issue.entity) {
          row.classList.add('link');
          row.addEventListener('click', () => {
            if (parts[issue.entity]) {
              activePart = issue.entity;
              selectedJoint = null;
            } else if (joints[issue.entity]) {
              selectedJoint = issue.entity;
              setTool('joints');
            }
            renderList();
            renderDetails();
            draw();
          });
        }
        details.append(row);
      }
    }
  }

  // --- Canvas: view, drawing -------------------------------------------------------------
  function resizeCanvas() {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.round(rect.width * dpr));
    canvas.height = Math.max(1, Math.round(rect.height * dpr));
    draw();
  }

  function fit() {
    if (!project) return;
    const rect = canvas.getBoundingClientRect();
    const w = project.source.width;
    const h = project.source.height;
    view.s = Math.min(rect.width / w, rect.height / h) * 0.94;
    view.ox = (rect.width - w * view.s) / 2;
    view.oy = (rect.height - h * view.s) / 2;
    draw();
  }

  const toSource = (x, y) => [(x - view.ox) / view.s, (y - view.oy) / view.s];
  const toScreen = (x, y) => [x * view.s + view.ox, y * view.s + view.oy];

  function draw() {
    if (!isOpen()) return;
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!images.source) return;
    ctx.setTransform(dpr * view.s, 0, 0, dpr * view.s, dpr * view.ox, dpr * view.oy);
    ctx.imageSmoothingEnabled = view.s < 2;
    const w = images.source.naturalWidth;
    const h = images.source.naturalHeight;
    ctx.fillStyle = '#e9e9e9';
    ctx.fillRect(0, 0, w, h);
    if (mode !== 'parts') {
      ctx.globalAlpha = mode === 'skeleton' ? 0.55 : 1;
      ctx.drawImage(images.source, 0, 0);
    }
    const partsAlpha = { parts: 1, markup: opacity, uncertain: 0.25, original: 0, skeleton: 0 }[mode];
    if (partsAlpha && images.parts) {
      ctx.globalAlpha = partsAlpha;
      ctx.drawImage(images.parts, 0, 0);
    }
    ctx.globalAlpha = 1;
    if ((mode === 'markup' || mode === 'parts') && images.outline) ctx.drawImage(images.outline, 0, 0);
    if (mode === 'uncertain' && images.uncertain) ctx.drawImage(images.uncertain, 0, 0);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const roi = project?.instances?.find((i) => i.id === iid)?.roi;
    if (roi && tool === 'roi') {
      const [x1, y1] = toScreen(roi[0], roi[1]);
      const [x2, y2] = toScreen(roi[2], roi[3]);
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = 'rgba(120, 170, 255, 0.9)';
      ctx.setLineDash([6, 5]);
      ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
      ctx.setLineDash([]);
    }
    if (mode === 'skeleton' || tool === 'joints') drawSkeleton();
    drawGesture();
  }

  function drawSkeleton() {
    const P = {};
    for (const [id, j] of Object.entries(joints)) {
      if (j.xy && j.state !== 'absent') P[id] = toScreen(j.xy[0], j.xy[1]);
    }
    ctx.lineCap = 'round';
    ctx.strokeStyle = 'rgba(25, 25, 25, 0.9)';
    ctx.lineWidth = 3;
    for (const [a, b] of bones) {
      if (P[a] && P[b]) {
        ctx.beginPath();
        ctx.moveTo(...P[a]);
        ctx.lineTo(...P[b]);
        ctx.stroke();
      }
    }
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.95)';
    ctx.lineWidth = 1.6;
    for (const sideName of ['left', 'right']) {
      for (const chain of chains) {
        const pts = chain.map((c) => P[`${sideName}_hand.${c}`]).filter(Boolean);
        if (pts.length < 2) continue;
        ctx.beginPath();
        ctx.moveTo(...pts[0]);
        for (const p of pts.slice(1)) ctx.lineTo(...p);
        ctx.stroke();
      }
    }
    for (const [id, [x, y]] of Object.entries(P)) {
      const j = joints[id];
      const r = id.includes('_hand.') ? 3.2 : 5.5;
      ctx.beginPath();
      ctx.arc(x, y, id === selectedJoint ? r + 2.5 : r, 0, Math.PI * 2);
      ctx.fillStyle = j.state === 'visible' ? '#ffffff' : j.state === 'uncertain' ? '#ffd400' : '#9a9a9a';
      ctx.fill();
      ctx.lineWidth = j.locked ? 2.4 : 1.2;
      ctx.strokeStyle = j.locked ? '#4f8cff' : '#151515';
      ctx.stroke();
    }
  }

  function drawGesture() {
    if (!drag) return;
    if (drag.kind === 'stroke' && drag.points.length) {
      const color = tool === 'brush' ? rgb(caps.parts[activePart].color) : tool === 'erase' ? '#ff00ff' : '#e9e9e9';
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.globalAlpha = 0.6;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.lineWidth = radius * 2 * view.s;
      ctx.beginPath();
      const first = toScreen(...drag.points[0]);
      ctx.moveTo(...first);
      for (const p of drag.points) ctx.lineTo(...toScreen(...p));
      if (drag.points.length === 1) ctx.lineTo(first[0] + 0.01, first[1]);
      ctx.stroke();
      ctx.globalAlpha = 1;
    } else if (drag.kind === 'line' || drag.kind === 'rect') {
      const a = toScreen(...drag.start);
      const b = toScreen(...drag.end);
      ctx.strokeStyle = drag.kind === 'line' ? '#ff4d4d' : 'rgba(120, 170, 255, 0.95)';
      ctx.lineWidth = 2;
      ctx.setLineDash(drag.kind === 'rect' ? [6, 5] : []);
      ctx.beginPath();
      if (drag.kind === 'line') {
        ctx.moveTo(...a);
        ctx.lineTo(...b);
      } else {
        ctx.rect(a[0], a[1], b[0] - a[0], b[1] - a[1]);
      }
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  function partAt(sx, sy) {
    if (!pick) return null;
    const [x, y] = toSource(sx, sy).map(Math.floor);
    if (x < 0 || y < 0 || x >= pick.width || y >= pick.height) return null;
    const i = (y * pick.width + x) * 4;
    if (!pick.data[i + 3]) return { id: null, name: 'фон' };
    const key = `${pick.data[i]},${pick.data[i + 1]},${pick.data[i + 2]}`;
    if (key === '255,0,255') return { id: null, name: 'без части' };
    const id = colorToPart.get(key);
    return id ? { id, name: caps.parts[id].name } : null;
  }

  function jointAt(sx, sy) {
    let best = null;
    let bestD = 12;
    for (const [id, j] of Object.entries(joints)) {
      if (!j.xy || j.state === 'absent') continue;
      const [x, y] = toScreen(j.xy[0], j.xy[1]);
      const d = Math.hypot(x - sx, y - sy) - (id.includes('_hand.') ? 0 : 3);
      if (d < bestD) {
        best = id;
        bestD = d;
      }
    }
    return best;
  }

  // --- Pointer --------------------------------------------------------------------------
  canvas.addEventListener('pointerdown', (event) => {
    if (!pid || !images.source) return;
    canvas.setPointerCapture(event.pointerId);
    const rect = canvas.getBoundingClientRect();
    const sx = event.clientX - rect.left;
    const sy = event.clientY - rect.top;
    const at = toSource(sx, sy);
    if (event.button === 1 || event.button === 2 || tool === 'view' || event.shiftKey && tool !== 'joints') {
      drag = { kind: 'pan', x: event.clientX, y: event.clientY };
      return;
    }
    if (event.ctrlKey) { // pick the part under the cursor
      const found = partAt(sx, sy);
      if (found?.id) {
        activePart = found.id;
        renderList();
        renderDetails();
      }
      return;
    }
    if (tool === 'brush' || tool === 'erase' || tool === 'background') {
      drag = { kind: 'stroke', points: [at] };
    } else if (tool === 'fill') {
      run('refine_mask', { part_id: activePart, instance_id: iid, [event.altKey ? 'negative_points' : 'positive_points']: [at],
        ...(event.altKey ? { positive_points: [] } : {}) });
    } else if (tool === 'joints') {
      const id = jointAt(sx, sy);
      if (id) {
        selectedJoint = id;
        drag = { kind: 'joint', id, moved: false };
        renderDetails();
      }
    } else if (tool === 'split') {
      drag = { kind: 'line', start: at, end: at };
    } else if (tool === 'roi') {
      drag = { kind: 'rect', start: at, end: at };
    }
    draw();
  });

  canvas.addEventListener('pointermove', (event) => {
    const rect = canvas.getBoundingClientRect();
    const sx = event.clientX - rect.left;
    const sy = event.clientY - rect.top;
    if (!drag) {
      const found = partAt(sx, sy);
      const [x, y] = toSource(sx, sy).map(Math.round);
      status.textContent = project ? `${x}, ${y}${found ? ` · ${found.name}` : ''} · выбрано: ${caps?.parts[activePart]?.name || ''}` : '';
      if (tool === 'brush' || tool === 'erase' || tool === 'background') drawCursor(sx, sy);
      return;
    }
    const at = toSource(sx, sy);
    if (drag.kind === 'pan') {
      view.ox += event.clientX - drag.x;
      view.oy += event.clientY - drag.y;
      drag.x = event.clientX;
      drag.y = event.clientY;
    } else if (drag.kind === 'stroke') {
      const last = drag.points[drag.points.length - 1];
      if (Math.hypot(at[0] - last[0], at[1] - last[1]) * view.s > 2) drag.points.push(at);
    } else if (drag.kind === 'joint') {
      joints[drag.id] = { ...joints[drag.id], xy: [Math.round(at[0] * 10) / 10, Math.round(at[1] * 10) / 10] };
      drag.moved = true;
    } else if (drag.kind === 'line' || drag.kind === 'rect') {
      drag.end = at;
    }
    draw();
  });

  function drawCursor(sx, sy) {
    draw();
    ctx.beginPath();
    ctx.arc(sx, sy, Math.max(1, radius * view.s), 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(0, 0, 0, 0.8)';
    ctx.lineWidth = 1;
    ctx.stroke();
  }

  canvas.addEventListener('pointerup', () => finishGesture());
  canvas.addEventListener('pointercancel', () => {
    drag = null;
    draw();
  });

  async function finishGesture() {
    const gesture = drag;
    drag = null;
    if (!gesture) return;
    const round = (p) => [Math.round(p[0] * 10) / 10, Math.round(p[1] * 10) / 10];
    if (gesture.kind === 'stroke') {
      const strokeMode = { brush: 'add', erase: 'erase', background: 'background' }[tool];
      await run('edit_mask', { part_id: activePart, instance_id: iid, strokes: [{ points: gesture.points.map(round), radius, mode: strokeMode }] });
    } else if (gesture.kind === 'joint' && gesture.moved) {
      await run('set_joint', { joint_id: gesture.id, xy: joints[gesture.id].xy, instance_id: iid });
    } else if (gesture.kind === 'line') {
      if (Math.hypot(gesture.end[0] - gesture.start[0], gesture.end[1] - gesture.start[1]) >= 2) {
        const reply = await run('split_part', { part_id: activePart, target: splitTarget.value, line: [round(gesture.start), round(gesture.end)], instance_id: iid });
        if (reply) toast(`Перенесено ${reply.moved_px} px`);
      }
    } else if (gesture.kind === 'rect') {
      const [a, b] = [gesture.start, gesture.end];
      const roi = [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])].map(Math.round);
      if (roi[2] - roi[0] >= 16 && roi[3] - roi[1] >= 16) await run('set_roi', { roi, instance_id: iid });
    }
    draw();
  }

  canvas.addEventListener('wheel', (event) => {
    event.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const sx = event.clientX - rect.left;
    const sy = event.clientY - rect.top;
    const k = Math.exp(-event.deltaY * 0.0015);
    const s = clamp(view.s * k, 0.05, 40);
    view.ox = sx - (sx - view.ox) * (s / view.s);
    view.oy = sy - (sy - view.oy) * (s / view.s);
    view.s = s;
    draw();
  }, { passive: false });
  canvas.addEventListener('contextmenu', (event) => event.preventDefault());
  layer.addEventListener('wheel', (event) => event.stopPropagation(), { passive: true });
  window.addEventListener('resize', () => {
    if (isOpen()) resizeCanvas();
  });

  // --- Modes, tools, keys ---------------------------------------------------------------
  function setMode(id) {
    mode = id;
    for (const [m, node] of modeButtons) node.classList.toggle('on', m === mode);
    draw();
  }

  function setTool(id) {
    tool = id;
    for (const [t, node] of toolButtons) node.classList.toggle('on', t === tool);
    sizeLabel.hidden = !['brush', 'erase', 'background'].includes(tool);
    splitLabel.hidden = tool !== 'split';
    canvas.dataset.tool = tool;
    if (tool === 'joints' && mode !== 'skeleton' && mode !== 'markup') setMode('markup');
    draw();
  }

  sizeInput.addEventListener('input', () => {
    radius = Number(sizeInput.value);
    sizeValue.textContent = `${radius}px`;
  });
  opacityInput.addEventListener('input', () => {
    opacity = Number(opacityInput.value);
    draw();
  });

  function onKey(event) {
    if (!isOpen()) return;
    const typing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement?.tagName) && event.key !== 'Escape';
    event.stopPropagation(); // the viewer's shortcuts are off while the editor is open
    if (typing) return;
    const ctrl = event.ctrlKey || event.metaKey;
    if (event.key === 'Escape') {
      event.preventDefault();
      if (drag) {
        drag = null;
        draw();
      } else {
        close();
      }
    } else if (ctrl && event.code === 'KeyZ') {
      event.preventDefault();
      history(event.shiftKey ? 'redo' : 'undo');
    } else if (ctrl && event.code === 'KeyY') {
      event.preventDefault();
      history('redo');
    } else if (!ctrl && event.code === 'BracketLeft') {
      sizeInput.value = String(Math.max(1, radius - (radius > 10 ? 3 : 1)));
      sizeInput.dispatchEvent(new Event('input'));
    } else if (!ctrl && event.code === 'BracketRight') {
      sizeInput.value = String(Math.min(80, radius + (radius >= 10 ? 3 : 1)));
      sizeInput.dispatchEvent(new Event('input'));
    } else if (!ctrl && /^Digit[1-5]$/.test(event.code)) {
      setMode(MODES[Number(event.code.slice(5)) - 1][0]);
    } else if (!ctrl && event.code === 'KeyF') {
      fit();
    } else if (!ctrl) {
      const found = TOOLS.find(([, , , key]) => `Key${key}` === event.code);
      if (found) setTool(found[0]);
    }
  }

  // --- Integration ----------------------------------------------------------------------
  app.shortcuts.register({
    keys: ['KeyU'],
    title: 'Разметка: части тела, кисти и пальцы текущей позы',
    group: GROUP,
    handler: (event) => {
      if (!event?.repeat) open();
    },
  });
  app.shortcuts.register({
    keys: [],
    hint: 'В разметке',
    title: '1–5 режим показа; V B E N G J X R — инструменты; [ ] — кисть; Ctrl+клик — взять часть; Shift/правая кнопка — двигать; Ctrl+Z / Ctrl+Y',
    group: GROUP,
  });
  setMode('markup');
  setTool('view');
  renderDetails();
}
