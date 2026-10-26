// «Танцы»: the dance phrases of the collection (docs/DANCE.md) as their own category.
// Found phrases come from the backend (get_dances, searched in the background); own phrases
// (get_sequences / save_sequence) are made in the editor, from a found one or from scratch.
// A phrase plays on the podium through app.dance (core: bone-driven transitions, one pose per count).
import { mirrorPose } from '../scene/dance.js';

const GROUP = 'Танцы';
const LIST_ID = 'dance-list';
const POLL_MS = 1500;
const STYLE_LABELS = { soft: 'Плавно', groove: 'Ритм' };
const BPMS = [90, 100, 110, 120, 130, 140];
const MAX_POSES = 16;

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

function noteIcon() {
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(ns, 'path');
  path.setAttribute('d', 'M9 18.5a2.5 2.5 0 1 1-2.5-2.5H9V5l10-2v12.5a2.5 2.5 0 1 1-2.5-2.5H19');
  svg.append(path);
  return svg;
}

const posesWord = (n) => {
  const tens = n % 100;
  const ones = n % 10;
  if (tens >= 11 && tens <= 14) return 'поз';
  if (ones === 1) return 'поза';
  return ones >= 2 && ones <= 4 ? 'позы' : 'поз';
};

export function install(app) {
  let boards = []; // storyboard dances, frame by frame: {id, name, loop, bpm, times, poses: [{file, mirror, pose}]}
  let found = []; // backend phrases: {id, name, loop, poses: [{file, mirror, pose}]}
  let own = []; // saved phrases: {id, name, loop, poses: [{file, mirror}]}
  let status = 'idle'; // idle | working | ready | error
  let rows = []; // [{kind: "board"|"own"|"found", phrase}] in list order
  let selected = 0;
  let pollTimer = 0;
  let generation = 0; // bumped on a collection switch
  let prefs = { style: 'soft', speed: 1, bpm: 120 };
  let editing = null; // {id?, name, loop, poses: [{file, mirror}]} while the editor is open
  let toastTimer = 0;

  // --- DOM: button, list panel, editor, player bar ------------------------------
  const openButton = button('dance-open', '', 'Танцы (K)');
  openButton.append(noteIcon());
  openButton.addEventListener('click', () => toggleList());

  const layer = el('div', 'dance-layer');
  layer.hidden = true;
  layer.setAttribute('role', 'dialog');
  layer.setAttribute('aria-modal', 'true');
  layer.setAttribute('aria-label', 'Танцы');
  const panel = el('div', 'dance-panel');
  const header = el('div', 'dance-header');
  const title = el('h2', 'dance-title', 'Танцы');
  const statusLine = el('span', 'dance-status');
  const newButton = button('dance-btn', '+ Новая связка', 'Своя связка, начиная с текущей позы');
  const closeButton = button('dance-btn subtle', '✕', 'Закрыть (Esc)');
  newButton.addEventListener('click', () => openEditor(null));
  closeButton.addEventListener('click', () => closeList());
  header.append(title, statusLine, el('span', 'dance-spacer'), newButton, closeButton);
  const list = el('div', 'dance-list');
  list.setAttribute('role', 'listbox');
  list.tabIndex = -1;

  const editor = el('div', 'dance-editor');
  editor.hidden = true;
  const editorTop = el('div', 'dance-editor-top');
  const nameInput = el('input', 'dance-input');
  nameInput.type = 'text';
  nameInput.maxLength = 60;
  nameInput.placeholder = 'Название связки';
  nameInput.spellcheck = false;
  const loopLabel = el('label', 'dance-check');
  const loopInput = el('input');
  loopInput.type = 'checkbox';
  loopLabel.append(loopInput, document.createTextNode(' по кругу'));
  const addCurrent = button('dance-btn', '+ Текущая поза', 'Добавить позу, открытую на подиуме, в конец');
  editorTop.append(nameInput, loopLabel, addCurrent);
  const cards = el('div', 'dance-cards');
  const editorBottom = el('div', 'dance-editor-bottom');
  const tryButton = button('dance-btn', '▶ Проверить', 'Проиграть, не сохраняя');
  const saveButton = button('dance-btn primary', 'Сохранить');
  const deleteButton = button('dance-btn danger', 'Удалить');
  const cancelButton = button('dance-btn subtle', 'Отмена');
  const editorNote = el('span', 'dance-note');
  editorBottom.append(tryButton, el('span', 'dance-spacer'), editorNote, deleteButton, cancelButton, saveButton);
  editor.append(editorTop, cards, editorBottom);

  const hint = el('div', 'dance-hint', '↑↓ выбор · Enter — танцевать · E — изменить · Esc закрыть. Во время танца: пробел — пауза, «,» «.» — темп, Esc — стоп');
  panel.append(header, list, editor, hint);
  layer.append(panel);
  layer.addEventListener('pointerdown', (event) => {
    if (event.target === layer) closeList();
  });
  layer.addEventListener('wheel', (event) => event.stopPropagation(), { passive: true });

  const hud = el('div', 'dance-hud');
  hud.hidden = true;
  const hudName = el('span', 'dance-hud-name');
  const hudStep = el('span', 'dance-hud-step');
  const pauseButton = button('dance-btn', '⏸', 'Пауза (пробел)');
  const speedButton = button('dance-btn', '×1', 'Замедление: ×1 → ×0,5 → ×0,25 («,» «.»)');
  const bpmButton = button('dance-btn', '120 BPM', 'Темп, ударов в минуту');
  const styleButton = button('dance-btn', STYLE_LABELS.soft, 'Плавно / ритм: качание, замах и отдача');
  const stopButton = button('dance-btn', '■', 'Стоп (Esc)');
  hud.append(hudName, hudStep, pauseButton, speedButton, bpmButton, styleButton, stopButton);
  pauseButton.addEventListener('click', () => togglePause());
  speedButton.addEventListener('click', () => cycleSpeed(1));
  bpmButton.addEventListener('click', () => cycleBpm());
  styleButton.addEventListener('click', () => toggleStyle());
  stopButton.addEventListener('click', () => app.dance.stop());

  const toastBox = el('div', 'dance-toast');
  toastBox.setAttribute('role', 'status');
  document.body.append(openButton, layer, hud, toastBox);

  function toast(text, error = false) {
    toastBox.textContent = text;
    toastBox.classList.toggle('error', error);
    toastBox.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastBox.classList.remove('show'), error ? 3600 : 1900);
  }

  function report(error, text) {
    console.error(error);
    Promise.resolve(app.api?.report_error(`dance: ${text}: ${error?.stack || error}`)).catch(() => {});
    toast(`${text}: ${error?.message || error}`, true);
  }

  // --- Data ------------------------------------------------------------------------
  // Poses of the active collection by file; own phrases are resolved through it.
  function poseMap() {
    return new Map(app.poses().map((pose) => [pose.file, pose]));
  }

  // [{file, mirror}] → [{file, mirror, pose}] (mirrored data for mirrored entries); missing files dropped.
  function resolve(entries) {
    const byFile = poseMap();
    return entries.filter((entry) => byFile.has(entry.file)).map((entry) => {
      const pose = byFile.get(entry.file);
      return { file: entry.file, mirror: !!entry.mirror, pose: entry.mirror ? mirrorPose(pose) : pose };
    });
  }

  async function refresh() {
    clearTimeout(pollTimer);
    const api = app.api;
    if (!api || typeof api.get_dances !== 'function') return;
    const gen = generation;
    let result;
    let sequences = own;
    try {
      [result, sequences] = await Promise.all([api.get_dances(), api.get_sequences?.() ?? []]);
    } catch (error) {
      result = { status: 'error', phrases: [], error: error?.message || String(error) };
    }
    if (gen !== generation) return;
    status = result?.status || 'error';
    found = Array.isArray(result?.phrases) ? result.phrases : [];
    boards = Array.isArray(result?.storyboards) ? result.storyboards : [];
    own = Array.isArray(sequences) ? sequences : [];
    render(result?.error);
    if (status === 'working' && isOpen()) pollTimer = setTimeout(refresh, POLL_MS);
  }

  function row(kind, phrase, index) {
    const entries = kind === 'own' ? resolve(phrase.poses) : phrase.poses;
    const item = el('div', `dance-row ${kind}`);
    item.setAttribute('role', 'option');
    item.dataset.index = String(index);
    const info = el('div', 'dance-info');
    const missing = kind === 'own' ? phrase.poses.length - entries.length : 0;
    const length = phrase.times?.length ? Math.round(phrase.times[phrase.times.length - 1] + 60 / (phrase.bpm || 120)) : 0;
    const meta = kind === 'board'
      ? `${entries.length} кадров · ${length} с · свой темп ${phrase.bpm} BPM`
      : `${entries.length} ${posesWord(entries.length)}${phrase.loop ? ' · по кругу' : ' · туда и обратно'}${missing ? ` · нет файлов: ${missing}` : ''}`;
    info.append(el('span', 'dance-name', phrase.name), el('span', 'dance-meta', meta));
    const strip = el('div', 'dance-strip');
    for (const entry of entries) {
      const img = el('img', `dance-thumb${entry.mirror ? ' mirror' : ''}`);
      img.loading = 'lazy';
      img.decoding = 'async';
      img.alt = '';
      img.draggable = false;
      img.src = entry.pose.thumb;
      img.title = `${entry.file}${entry.mirror ? ' (зеркально)' : ''}`;
      strip.append(img);
    }
    const edit = button('dance-btn subtle dance-edit', '✎', kind === 'own' ? 'Изменить (E)' : 'Сделать своей и изменить (E)');
    if (kind === 'board') item.title = 'Готовый танец по раскадровке: кадры и темп из видео';
    edit.dataset.edit = '1';
    item.append(el('span', 'dance-play', '▶'), info, strip, edit);
    return item;
  }

  function render(error) {
    statusLine.textContent = {
      working: 'Ищу связки… В первый раз около минуты: читаю одежду на всех рисунках.',
      error: `Не удалось найти связки${error ? `: ${error}` : ''}`,
      ready: `${boards.length ? `готовых: ${boards.length} · ` : ''}${found.length} найдено${own.length ? ` · своих: ${own.length}` : ''}`,
      idle: '',
    }[status] || '';
    statusLine.classList.toggle('error', status === 'error');
    rows = [
      ...boards.map((phrase) => ({ kind: 'board', phrase })),
      ...own.map((phrase) => ({ kind: 'own', phrase })),
      ...found.map((phrase) => ({ kind: 'found', phrase })),
    ];
    const fragment = document.createDocumentFragment();
    const titles = { board: 'Готовые танцы', own: 'Мои связки', found: 'Найденные связки' };
    const sections = new Set(rows.map((entry) => entry.kind)).size > 1;
    rows.forEach(({ kind, phrase }, index) => {
      if (sections && rows[index - 1]?.kind !== kind) fragment.append(el('div', 'dance-section', titles[kind]));
      fragment.append(row(kind, phrase, index));
    });
    if (status === 'ready' && !rows.length) {
      fragment.append(el('div', 'dance-empty', 'Связок не нашлось: нужны позы со скелетом, близкие по движению и одежде. Можно собрать свою: «+ Новая связка».'));
    }
    list.replaceChildren(fragment);
    select(Math.min(selected, rows.length - 1), false);
  }

  function select(index, scroll = true) {
    selected = Math.max(0, index);
    list.querySelectorAll('.dance-row').forEach((item) => {
      const on = Number(item.dataset.index) === selected;
      item.classList.toggle('selected', on);
      item.setAttribute('aria-selected', String(on));
      if (on && scroll) item.scrollIntoView({ block: 'nearest' });
    });
  }

  list.addEventListener('click', (event) => {
    const item = event.target.closest('.dance-row');
    if (!item) return;
    const index = Number(item.dataset.index);
    if (event.target.closest('[data-edit]')) editRow(index);
    else play(index);
  });

  function phraseOf(index) {
    const entry = rows[index];
    if (!entry) return null;
    return entry.kind === 'own' ? { ...entry.phrase, poses: resolve(entry.phrase.poses) } : entry.phrase;
  }

  async function playPhrase(phrase) {
    try {
      // A storyboard dance keeps the tempo of its video; the speed (×0,5 / ×0,25) still applies.
      await app.dance.play(phrase, phrase.kind === 'storyboard' && phrase.bpm ? { ...prefs, bpm: phrase.bpm } : prefs);
    } catch (error) {
      report(error, 'Танец');
    }
  }

  function play(index) {
    const phrase = phraseOf(index);
    if (!phrase) return;
    if (phrase.poses.length < 2) {
      toast('В связке меньше двух поз', true);
      return;
    }
    closeList();
    playPhrase(phrase);
  }

  // --- Editor ------------------------------------------------------------------------
  function editRow(index) {
    const entry = rows[index];
    if (!entry) return;
    const { phrase, kind } = entry;
    openEditor({
      id: kind === 'own' ? phrase.id : undefined,
      name: kind === 'own' ? phrase.name : phrase.name,
      loop: !!phrase.loop,
      poses: phrase.poses.map((e) => ({ file: e.file, mirror: !!e.mirror })),
    });
  }

  function openEditor(phrase) {
    if (!isOpen()) openList();
    const current = app.current();
    editing = phrase ?? { name: 'Моя связка', loop: false, poses: current ? [{ file: current.file, mirror: false }] : [] };
    nameInput.value = editing.name;
    loopInput.checked = editing.loop;
    deleteButton.hidden = !editing.id;
    list.hidden = true;
    newButton.hidden = true;
    editor.hidden = false;
    layer.classList.add('editing'); // docked at the bottom: the podium stays visible, ←/→ change the pose
    title.textContent = editing.id ? 'Танцы · изменить связку' : 'Танцы · новая связка';
    renderCards();
    nameInput.focus();
  }

  function closeEditor() {
    editing = null;
    editor.hidden = true;
    list.hidden = false;
    newButton.hidden = false;
    layer.classList.remove('editing');
    title.textContent = 'Танцы';
    list.focus({ preventScroll: true });
  }

  function renderCards() {
    const byFile = poseMap();
    const fragment = document.createDocumentFragment();
    editing.poses.forEach((entry, i) => {
      const pose = byFile.get(entry.file);
      const card = el('div', 'dance-card');
      const img = el('img', `dance-card-img${entry.mirror ? ' mirror' : ''}`);
      img.alt = '';
      img.draggable = false;
      if (pose) img.src = pose.thumb;
      else card.classList.add('missing');
      card.title = `${entry.file}${entry.mirror ? ' (зеркально)' : ''}`;
      const number = el('span', 'dance-card-num', String(i + 1));
      const tools = el('div', 'dance-card-tools');
      const left = button('dance-mini', '◀', 'Раньше');
      const flip = button(`dance-mini${entry.mirror ? ' on' : ''}`, '⇋', 'Зеркально');
      const right = button('dance-mini', '▶', 'Позже');
      const remove = button('dance-mini', '✕', 'Убрать из связки');
      left.disabled = i === 0;
      right.disabled = i === editing.poses.length - 1;
      left.addEventListener('click', () => move(i, -1));
      right.addEventListener('click', () => move(i, 1));
      flip.addEventListener('click', () => {
        entry.mirror = !entry.mirror;
        renderCards();
      });
      remove.addEventListener('click', () => {
        editing.poses.splice(i, 1);
        renderCards();
      });
      tools.append(left, flip, right, remove);
      card.append(img, number, tools);
      fragment.append(card);
    });
    if (!editing.poses.length) fragment.append(el('div', 'dance-empty', 'Добавьте позы: откройте позу на подиуме (←/→ или галерея G) и нажмите «+ Текущая поза».'));
    cards.replaceChildren(fragment);
    const count = editing.poses.length;
    editorNote.textContent = count < 2 ? 'нужно хотя бы 2 позы' : `${count} ${posesWord(count)}`;
    saveButton.disabled = count < 2;
    tryButton.disabled = count < 2;
    addCurrent.disabled = count >= MAX_POSES || !app.current();
  }

  function move(i, delta) {
    const [entry] = editing.poses.splice(i, 1);
    editing.poses.splice(i + delta, 0, entry);
    renderCards();
  }

  function draft() {
    return { ...editing, name: nameInput.value.trim() || 'Моя связка', loop: loopInput.checked };
  }

  addCurrent.addEventListener('click', () => {
    const current = app.current();
    if (!current || !editing || editing.poses.length >= MAX_POSES) return;
    editing.poses.push({ file: current.file, mirror: false });
    renderCards();
  });
  tryButton.addEventListener('click', () => {
    const phrase = draft();
    playPhrase({ ...phrase, poses: resolve(phrase.poses) });
  });
  saveButton.addEventListener('click', async () => {
    const api = app.api;
    if (!editing || typeof api?.save_sequence !== 'function') return;
    try {
      const saved = await api.save_sequence(draft());
      toast(`Связка «${saved.name}» сохранена`);
      closeEditor();
      await refresh();
      const index = rows.findIndex((entry) => entry.kind === 'own' && entry.phrase.id === saved.id);
      if (index >= 0) select(index);
    } catch (error) {
      report(error, 'Не удалось сохранить связку');
    }
  });
  deleteButton.addEventListener('click', async () => {
    if (!editing?.id) return;
    try {
      await app.api.delete_sequence(editing.id);
      toast('Связка удалена');
      closeEditor();
      await refresh();
    } catch (error) {
      report(error, 'Не удалось удалить связку');
    }
  });
  cancelButton.addEventListener('click', () => closeEditor());
  nameInput.addEventListener('keydown', (event) => {
    if (event.code === 'Enter' && !saveButton.disabled) saveButton.click();
  });

  // --- Panel -------------------------------------------------------------------------
  function isOpen() {
    return !layer.hidden;
  }

  function openList() {
    if (isOpen() || !app.api) return;
    layer.hidden = false;
    document.body.classList.add('dance-list-open');
    app.overlays.open(LIST_ID, closeList);
    render();
    list.focus({ preventScroll: true });
    refresh(); // cheap when nothing changed; picks up edited skeletons
  }

  function closeList() {
    if (!isOpen()) return;
    clearTimeout(pollTimer);
    if (editing) closeEditor();
    app.overlays.close(LIST_ID);
    layer.hidden = true;
    document.body.classList.remove('dance-list-open');
    if (layer.contains(document.activeElement)) document.activeElement.blur();
  }

  function toggleList() {
    if (isOpen()) closeList();
    else openList();
  }

  const typing = (target) => target?.tagName === 'INPUT' && target.type === 'text';

  // Keys inside the panel (capture: before the core moves the pose behind it), and Esc while
  // a dance plays with no panel open (stops the dance instead of quitting).
  window.addEventListener('keydown', (event) => {
    if (event.ctrlKey || event.altKey || event.metaKey) return;
    if (isOpen()) {
      if (event.code === 'Escape' && editing) {
        event.preventDefault();
        event.stopImmediatePropagation();
        closeEditor();
        return;
      }
      if (editing && !typing(event.target) && (event.code === 'ArrowLeft' || event.code === 'ArrowRight')) {
        event.preventDefault();
        event.stopImmediatePropagation();
        app.step(event.code === 'ArrowLeft' ? -1 : 1);
        return;
      }
      if (editing || typing(event.target)) return;
      let handled = true;
      if (event.code === 'ArrowDown') select(Math.min(rows.length - 1, selected + 1));
      else if (event.code === 'ArrowUp') select(selected - 1);
      else if (event.code === 'KeyE') editRow(selected);
      else if (event.code === 'Enter' || event.code === 'NumpadEnter' || event.code === 'Space') {
        if (event.target?.tagName === 'BUTTON') handled = false;
        else if (!event.repeat) play(selected);
      } else handled = false;
      if (handled) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
      return;
    }
    if (event.code === 'Escape' && app.dance.state() && !app.overlays.any()) {
      event.preventDefault();
      event.stopImmediatePropagation();
      app.dance.stop();
    }
  }, true);

  // --- Player controls (remembered in view_prefs.dance) ---------------------------------
  function savePrefs() {
    Promise.resolve(app.api?.save_view_prefs?.({ dance: prefs })).catch((error) => report(error, 'Настройки танца'));
  }

  function togglePause() {
    const state = app.dance.state();
    if (state) app.dance.setPaused(!state.paused);
  }

  function cycleSpeed(direction) {
    const speeds = app.dance.speeds();
    const at = speeds.indexOf(prefs.speed);
    prefs.speed = speeds[Math.min(speeds.length - 1, Math.max(0, (at < 0 ? 0 : at) + direction))];
    app.dance.setSpeed(prefs.speed);
    savePrefs();
  }

  function cycleBpm() {
    const at = BPMS.indexOf(prefs.bpm);
    prefs.bpm = BPMS[(at + 1) % BPMS.length];
    app.dance.setBpm(prefs.bpm);
    savePrefs();
  }

  function toggleStyle() {
    prefs.style = prefs.style === 'soft' ? 'groove' : 'soft';
    app.dance.setStyle(prefs.style);
    savePrefs();
  }

  function renderHud(state) {
    hud.hidden = !state;
    document.body.classList.toggle('dancing', !!state);
    if (!state) return;
    hudName.textContent = state.name;
    hudStep.textContent = `${state.pose + 1} / ${state.count}`;
    pauseButton.textContent = state.paused ? '▶' : '⏸';
    pauseButton.title = state.paused ? 'Продолжить (пробел)' : 'Пауза (пробел)';
    speedButton.textContent = `×${String(state.speed).replace('.', ',')}`;
    bpmButton.textContent = `${state.bpm} BPM`;
    styleButton.textContent = STYLE_LABELS[state.style];
  }

  app.on('dance', (detail) => renderHud(detail.phase === 'end' ? null : detail));
  app.on('poses', () => {
    generation++;
    found = [];
    boards = [];
    own = [];
    status = 'idle';
    if (editing) closeEditor();
    if (isOpen()) refresh();
  });
  app.on('pose', () => {
    if (editing) renderCards(); // "+ Текущая поза" follows the pose on the podium
  });

  // --- Shortcuts ---------------------------------------------------------------------
  const dancing = () => !!app.dance.state();
  app.shortcuts.register({ keys: ['KeyK'], title: 'Танцы: связки (найденные и свои)', group: GROUP, handler: (event) => {
    if (!event.repeat) toggleList();
  } });
  app.shortcuts.register({ keys: ['Space'], title: 'Танец: пауза / продолжить', group: GROUP, enabled: dancing, handler: (event) => {
    if (!event.repeat) togglePause();
  } });
  app.shortcuts.register({ keys: ['Comma'], title: 'Танец: медленнее', group: GROUP, enabled: dancing, handler: () => cycleSpeed(1) });
  app.shortcuts.register({ keys: ['Period'], title: 'Танец: быстрее', group: GROUP, enabled: dancing, handler: () => cycleSpeed(-1) });

  app.ready.then(async () => {
    const saved = await app.api?.get_view_prefs?.();
    const dance = saved?.dance || {};
    prefs = {
      style: STYLE_LABELS[dance.style] ? dance.style : 'soft',
      speed: app.dance.speeds().includes(dance.speed) ? dance.speed : 1,
      bpm: Number.isInteger(dance.bpm) ? dance.bpm : 120,
    };
  }).catch((error) => report(error, 'Настройки танца'));

  // Read-only diagnostics for local checks only.
  window.__poseStageDance = {
    state: () => ({ open: isOpen(), status, found: found.length, own: own.length, selected, editing: editing ? { ...editing } : null, prefs: { ...prefs }, playing: app.dance.state() }),
    play: (index) => play(index),
  };
}
