// «Танцы»: the dance phrases of the collection (docs/DANCE.md) as their own category.
// The list comes from the backend (get_dances, searched in the background); a phrase plays on
// the podium through app.dance (core: bone-driven transitions, one pose per count).
const GROUP = 'Танцы';
const LIST_ID = 'dance-list';
const POLL_MS = 1500;
const STYLE_LABELS = { soft: 'Плавно', groove: 'Ритм' };

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
  let phrases = [];
  let status = 'idle'; // idle | working | ready | error
  let selected = 0;
  let pollTimer = 0;
  let generation = 0; // bumped on a collection switch
  let style = 'soft';
  let speed = 1;
  let playingIndex = -1;

  // --- DOM -------------------------------------------------------------------
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
  const closeButton = button('dance-btn subtle', '✕', 'Закрыть (Esc)');
  closeButton.addEventListener('click', () => closeList());
  header.append(title, statusLine, el('span', 'dance-spacer'), closeButton);
  const list = el('div', 'dance-list');
  list.setAttribute('role', 'listbox');
  list.tabIndex = -1;
  const hint = el('div', 'dance-hint', '↑↓ выбор · Enter — танцевать · Esc закрыть. Во время танца: пробел — пауза, «,» «.» — темп, Esc — стоп');
  panel.append(header, list, hint);
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
  const speedButton = button('dance-btn', '×1', 'Темп: ×1 → ×0,5 → ×0,25 («,» «.»)');
  const styleButton = button('dance-btn', STYLE_LABELS.soft, 'Плавно / ритм: качание, замах и отдача');
  const stopButton = button('dance-btn', '■', 'Стоп (Esc)');
  hud.append(hudName, hudStep, pauseButton, speedButton, styleButton, stopButton);
  pauseButton.addEventListener('click', () => togglePause());
  speedButton.addEventListener('click', () => cycleSpeed(1));
  styleButton.addEventListener('click', () => toggleStyle());
  stopButton.addEventListener('click', () => app.dance.stop());

  document.body.append(openButton, layer, hud);

  // --- Data ------------------------------------------------------------------
  async function refresh() {
    clearTimeout(pollTimer);
    const api = app.api;
    if (!api || typeof api.get_dances !== 'function') return;
    const gen = generation;
    let result;
    try {
      result = await api.get_dances();
    } catch (error) {
      result = { status: 'error', phrases: [], error: error?.message || String(error) };
    }
    if (gen !== generation) return;
    status = result?.status || 'error';
    phrases = Array.isArray(result?.phrases) ? result.phrases : [];
    render(result?.error);
    if (status === 'working' && isOpen()) pollTimer = setTimeout(refresh, POLL_MS);
  }

  function render(error) {
    statusLine.textContent = {
      working: 'Ищу связки… В первый раз около минуты: читаю одежду на всех рисунках.',
      error: `Не удалось найти связки${error ? `: ${error}` : ''}`,
      ready: phrases.length ? `${phrases.length} — по сходству поз, одежды и движения` : '',
      idle: '',
    }[status] || '';
    statusLine.classList.toggle('error', status === 'error');
    const fragment = document.createDocumentFragment();
    phrases.forEach((phrase, index) => {
      const row = el('div', 'dance-row');
      row.setAttribute('role', 'option');
      row.dataset.index = String(index);
      const info = el('div', 'dance-info');
      info.append(
        el('span', 'dance-name', phrase.name),
        el('span', 'dance-meta', `${phrase.poses.length} ${posesWord(phrase.poses.length)}${phrase.loop ? ' · по кругу' : ' · туда и обратно'}`),
      );
      const strip = el('div', 'dance-strip');
      for (const entry of phrase.poses) {
        const img = el('img', `dance-thumb${entry.mirror ? ' mirror' : ''}`);
        img.loading = 'lazy';
        img.decoding = 'async';
        img.alt = '';
        img.draggable = false;
        img.src = entry.pose.thumb;
        img.title = `${entry.file}${entry.mirror ? ' (зеркально)' : ''}`;
        strip.append(img);
      }
      const play = el('span', 'dance-play', '▶');
      row.append(play, info, strip);
      row.classList.toggle('playing', index === playingIndex);
      fragment.append(row);
    });
    if (status === 'ready' && !phrases.length) {
      fragment.append(el('div', 'dance-empty', 'Связок не нашлось: нужны позы со скелетом, близкие по движению и одежде.'));
    }
    list.replaceChildren(fragment);
    select(Math.min(selected, phrases.length - 1), false);
  }

  function select(index, scroll = true) {
    selected = Math.max(0, index);
    list.querySelectorAll('.dance-row').forEach((row, i) => {
      row.classList.toggle('selected', i === selected);
      row.setAttribute('aria-selected', String(i === selected));
      if (i === selected && scroll) row.scrollIntoView({ block: 'nearest' });
    });
  }

  list.addEventListener('click', (event) => {
    const row = event.target.closest('.dance-row');
    if (row) play(Number(row.dataset.index));
  });

  async function play(index) {
    const phrase = phrases[index];
    if (!phrase) return;
    closeList();
    playingIndex = index;
    try {
      await app.dance.play(phrase, { style, speed });
    } catch (error) {
      playingIndex = -1;
      console.error(error);
      Promise.resolve(app.api?.report_error(`dance: ${error?.stack || error}`)).catch(() => {});
    }
  }

  // --- Panel -----------------------------------------------------------------
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
    app.overlays.close(LIST_ID);
    layer.hidden = true;
    document.body.classList.remove('dance-list-open');
    if (layer.contains(document.activeElement)) document.activeElement.blur();
  }

  function toggleList() {
    if (isOpen()) closeList();
    else openList();
  }

  // Keys inside the list (capture: before the core moves the pose behind it), and Esc while
  // a dance plays with no panel open (stops the dance instead of quitting).
  window.addEventListener('keydown', (event) => {
    if (event.ctrlKey || event.altKey || event.metaKey) return;
    if (isOpen()) {
      let handled = true;
      if (event.code === 'ArrowDown') select(Math.min(phrases.length - 1, selected + 1));
      else if (event.code === 'ArrowUp') select(selected - 1);
      else if (event.code === 'Enter' || event.code === 'NumpadEnter' || event.code === 'Space') {
        if (!event.repeat && event.target?.tagName !== 'BUTTON') play(selected);
        else handled = event.target?.tagName !== 'BUTTON';
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

  // --- Player controls ---------------------------------------------------------
  function togglePause() {
    const state = app.dance.state();
    if (state) app.dance.setPaused(!state.paused);
  }

  function cycleSpeed(direction) {
    const speeds = app.dance.speeds();
    const at = speeds.indexOf(speed);
    speed = speeds[Math.min(speeds.length - 1, Math.max(0, at + direction))];
    app.dance.setSpeed(speed);
  }

  function toggleStyle() {
    style = style === 'soft' ? 'groove' : 'soft';
    app.dance.setStyle(style);
    styleButton.textContent = STYLE_LABELS[style];
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
    styleButton.textContent = STYLE_LABELS[state.style];
  }

  app.on('dance', (detail) => {
    if (detail.phase === 'end') {
      playingIndex = -1;
      renderHud(null);
      return;
    }
    renderHud(detail);
  });
  app.on('poses', () => {
    generation++;
    phrases = [];
    status = 'idle';
    if (isOpen()) refresh();
  });

  // --- Shortcuts -----------------------------------------------------------------
  const dancing = () => !!app.dance.state();
  app.shortcuts.register({ keys: ['KeyK'], title: 'Танцы: список связок', group: GROUP, handler: (event) => {
    if (!event.repeat) toggleList();
  } });
  app.shortcuts.register({ keys: ['Space'], title: 'Танец: пауза / продолжить', group: GROUP, enabled: dancing, handler: (event) => {
    if (!event.repeat) togglePause();
  } });
  app.shortcuts.register({ keys: ['Comma'], title: 'Танец: медленнее', group: GROUP, enabled: dancing, handler: () => cycleSpeed(1) });
  app.shortcuts.register({ keys: ['Period'], title: 'Танец: быстрее', group: GROUP, enabled: dancing, handler: () => cycleSpeed(-1) });

  // Read-only diagnostics for local checks only.
  window.__poseStageDance = {
    state: () => ({ open: isOpen(), status, phrases: phrases.length, selected, playing: app.dance.state() }),
    play: (index) => play(index),
  };
}
