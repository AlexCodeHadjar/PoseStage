// Navigation UI (gallery, favorites, tags, filters, support mode, collections, counter, toasts)
// — implemented against docs/API.md §2. DOM is built with createElement/textContent only:
// file names and tags are user data. Styles: web/ui/navigator.css (classes + CSSOM only, CSP).

const GROUP = 'Коллекция';
const OVERLAY_ID = 'gallery';
const MODES = ['feet', 'kneel', 'ground'];
const MODE_NAMES = { feet: 'стопы', kneel: 'колени', ground: 'сидя/лёжа' };
const MODE_SHORT = { feet: 'стоя', kneel: 'колени', ground: 'сидя' };
const FILTER_MODES = [
  ['all', 'Все'],
  ['feet', 'Стоя'],
  ['kneel', 'На коленях'],
  ['ground', 'Сидя-лёжа'],
];
const SVG_NS = 'http://www.w3.org/2000/svg';

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function button(className, text, title) {
  const node = el('button', className, text);
  node.type = 'button';
  if (title) {
    node.title = title;
    node.setAttribute('aria-label', title);
  }
  // Buttons never keep keyboard focus: arrows/Enter keep driving the grid selection.
  node.addEventListener('mousedown', (event) => event.preventDefault());
  return node;
}

function gridIcon() {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  for (const [x, y] of [[4, 4], [13.5, 4], [4, 13.5], [13.5, 13.5]]) {
    const rect = document.createElementNS(SVG_NS, 'rect');
    rect.setAttribute('x', x);
    rect.setAttribute('y', y);
    rect.setAttribute('width', 6.5);
    rect.setAttribute('height', 6.5);
    rect.setAttribute('rx', 1.6);
    svg.append(rect);
  }
  return svg;
}

function isTyping(element) {
  if (!element || element.nodeType !== 1) return false;
  if (element.isContentEditable) return true;
  const tag = element.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag === 'INPUT') {
    const type = (element.getAttribute('type') || 'text').toLowerCase();
    return !['button', 'checkbox', 'radio', 'range', 'color', 'submit', 'reset', 'image', 'file'].includes(type);
  }
  return false;
}

const fold = (text) => String(text).toLocaleLowerCase('ru');

export function install(app) {
  // --- State ---------------------------------------------------------------
  let poses = [];
  let library = { favorites: new Set(), tags: {} };
  let collections = [];
  let filter = { mode: 'all', favorites: false, tag: null };
  let search = '';
  let matches = null; // filtered indices (ascending) or null when no filter is active
  let libraryGen = 0;
  let libraryReady = false; // the active collection's library has been loaded
  let posesGen = 0; // bumped on every 'poses' event (collection switch / reload)
  let prefsRestored = false;
  let busy = false; // a collection operation is running

  // --- Toast -----------------------------------------------------------------
  const toastBox = el('div', 'nav-toast');
  toastBox.setAttribute('role', 'status');
  toastBox.setAttribute('aria-live', 'polite');
  let toastTimer = 0;
  function toast(text, kind = '') {
    toastBox.textContent = text;
    toastBox.classList.toggle('error', kind === 'error');
    toastBox.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastBox.classList.remove('show'), kind === 'error' ? 3600 : 1900);
  }

  function report(error, text) {
    const message = error?.message || String(error);
    console.error(error);
    try {
      Promise.resolve(app.api?.report_error(`navigator: ${text}: ${error?.stack || message}`)).catch(() => {});
    } catch {
      // logging never throws
    }
    toast(`${text}: ${message.length > 120 ? `${message.slice(0, 117)}…` : message}`, 'error');
  }

  // Backend call; resolves to undefined (and reports) on failure. Never resolves with app.api.
  async function call(name, text, ...args) {
    const api = app.api;
    if (!api) {
      toast('Бэкенд ещё не готов', 'error');
      return undefined;
    }
    // An API without this method (older backend, test fakes): degrade silently.
    if (typeof api[name] !== 'function') {
      return undefined;
    }
    try {
      return await api[name](...args);
    } catch (error) {
      report(error, text);
      return undefined;
    }
  }

  // --- Counter -------------------------------------------------------------
  const counter = el('div', 'nav-counter');
  counter.setAttribute('aria-live', 'off');
  const counterPos = el('span', 'nav-counter-pos');
  const counterName = el('span', 'nav-counter-name');
  const counterStar = el('span', 'nav-counter-star', '★');
  counterStar.title = 'В избранном';
  counter.append(counterPos, counterName, counterStar);
  counter.hidden = true;

  function updateCounter() {
    const index = app.index();
    const pose = poses[index];
    if (!pose || !poses.length || !app.current()) {
      counter.hidden = true;
      return;
    }
    counter.hidden = false;
    let text = `${index + 1} / ${poses.length}`;
    if (matches) {
      if (matches.length) {
        const position = matches.indexOf(index);
        text = `${position >= 0 ? position + 1 : '—'} / ${matches.length} из ${poses.length}`;
      } else {
        text = `${index + 1} / ${poses.length} · фильтр: 0`;
      }
    }
    counterPos.textContent = text;
    counterName.textContent = pose.file;
    counter.title = pose.file;
    counterStar.hidden = !library.favorites.has(pose.file);
  }

  // --- Grid button (top-right) -----------------------------------------------
  const openButton = button('nav-open', '', 'Галерея (G)');
  openButton.append(gridIcon());
  openButton.addEventListener('click', () => toggleGallery());

  // --- Gallery DOM ---------------------------------------------------------
  const gallery = el('div', 'nav-gallery');
  gallery.hidden = true;
  gallery.setAttribute('role', 'dialog');
  gallery.setAttribute('aria-modal', 'true');
  gallery.setAttribute('aria-label', 'Галерея поз');
  const panel = el('div', 'nav-panel');
  gallery.append(panel);

  // Header row 1: title, collections.
  const header = el('div', 'nav-header');
  const titleRow = el('div', 'nav-row');
  const title = el('h2', 'nav-title', 'Галерея');
  const collectionSelect = el('select', 'nav-select nav-collection');
  collectionSelect.title = 'Коллекция';
  collectionSelect.setAttribute('aria-label', 'Коллекция');
  const addButton = button('nav-btn', 'Добавить папку…', 'Добавить папку-коллекцию (O)');
  const removeButton = button('nav-btn subtle', 'Удалить', 'Убрать коллекцию из списка (файлы не удаляются)');
  const confirmBox = el('span', 'nav-confirm');
  confirmBox.hidden = true;
  const confirmText = el('span', 'nav-confirm-text');
  const confirmYes = button('nav-btn danger', 'Убрать');
  const confirmNo = button('nav-btn subtle', 'Отмена');
  confirmBox.append(confirmText, confirmYes, confirmNo);
  const closeButton = button('nav-btn nav-close', '✕', 'Закрыть (Esc)');
  const spacer = el('span', 'nav-spacer');
  titleRow.append(title, collectionSelect, addButton, removeButton, confirmBox, spacer, closeButton);

  // Header row 2: filters.
  const filterRow = el('div', 'nav-row nav-filters');
  const modeGroup = el('div', 'nav-segmented');
  modeGroup.setAttribute('role', 'group');
  modeGroup.setAttribute('aria-label', 'Опора');
  const modeButtons = new Map();
  for (const [mode, label] of FILTER_MODES) {
    const b = button('nav-seg', label);
    b.addEventListener('click', () => setFilter({ mode }));
    modeButtons.set(mode, b);
    modeGroup.append(b);
  }
  const favButton = button('nav-btn nav-toggle', '★ Только избранное', 'Только избранное');
  favButton.addEventListener('click', () => setFilter({ favorites: !filter.favorites }));
  const tagSelect = el('select', 'nav-select nav-tag');
  tagSelect.title = 'Тег';
  tagSelect.setAttribute('aria-label', 'Фильтр по тегу');
  tagSelect.addEventListener('change', () => {
    setFilter({ tag: tagSelect.value || null });
    tagSelect.blur();
    focusGrid();
  });
  const searchInput = el('input', 'nav-search');
  searchInput.type = 'search';
  searchInput.placeholder = 'Поиск по имени…';
  searchInput.setAttribute('aria-label', 'Поиск по имени файла');
  searchInput.spellcheck = false;
  searchInput.maxLength = 100;
  let searchTimer = 0;
  let searchSaveTimer = 0;
  searchInput.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      search = searchInput.value.trim().slice(0, 100);
      applyFilter();
      clearTimeout(searchSaveTimer);
      searchSaveTimer = setTimeout(saveFilter, 600);
    }, 120);
  });
  const resetButton = button('nav-btn subtle', 'Сбросить', 'Сбросить фильтр');
  resetButton.addEventListener('click', () => resetFilter());
  const countLabel = el('span', 'nav-count');
  filterRow.append(modeGroup, favButton, tagSelect, searchInput, resetButton, el('span', 'nav-spacer'), countLabel);
  header.append(titleRow, filterRow);

  // Grid.
  const scroller = el('div', 'nav-scroller');
  scroller.tabIndex = -1;
  const grid = el('div', 'nav-grid');
  grid.setAttribute('role', 'listbox');
  grid.setAttribute('aria-label', 'Позы');
  const empty = el('div', 'nav-empty');
  empty.hidden = true;
  scroller.append(grid, empty);

  // Details of the selected pose.
  const details = el('div', 'nav-details');
  const detailName = el('span', 'nav-detail-name');
  const detailFav = button('nav-btn nav-star', '☆', 'Избранное (S)');
  const supportGroup = el('div', 'nav-segmented');
  supportGroup.setAttribute('role', 'group');
  supportGroup.setAttribute('aria-label', 'Опора позы');
  const supportButtons = new Map();
  for (const [mode, label, key] of [['auto', 'Авто', '0'], ['feet', 'Стопы', '1'], ['kneel', 'Колени', '2'], ['ground', 'Сидя-лёжа', '3']]) {
    const b = button('nav-seg', label, `Опора: ${label.toLowerCase()} (${key})`);
    b.addEventListener('click', () => {
      const pose = poses[selected];
      if (pose) setSupport(pose.file, mode);
    });
    supportButtons.set(mode, b);
    supportGroup.append(b);
  }
  const tagsInput = el('input', 'nav-tags');
  tagsInput.type = 'text';
  tagsInput.placeholder = 'теги через запятую, Enter — сохранить';
  tagsInput.setAttribute('aria-label', 'Теги выбранной позы');
  tagsInput.spellcheck = false;
  tagsInput.maxLength = 700;
  const hint = el('span', 'nav-hint', '←↑→↓ выбор · Enter открыть · ПКМ выбрать · Esc закрыть');
  details.append(detailName, detailFav, supportGroup, tagsInput, hint);

  panel.append(header, scroller, details);
  document.body.append(counter, toastBox, openButton, gallery);

  // --- Gallery cells -------------------------------------------------------
  let cells = []; // index -> cell element (rebuilt per pose list)
  let cellsFor = null; // the pose list the cells were built for
  let visible = []; // indices currently shown (filtered)
  let selected = -1;

  function badgeFor(cell, pose) {
    const support = cell.querySelector('.nav-cell-mode');
    const mode = pose.support?.mode;
    const user = pose.support_source === 'user';
    support.hidden = !(user || (mode && mode !== 'feet'));
    support.textContent = `${MODE_SHORT[mode] || mode || ''}${user ? ' ✎' : ''}`;
    support.title = user ? 'Опора задана вручную' : 'Опора';
    cell.querySelector('.nav-cell-star').hidden = !library.favorites.has(pose.file);
    const tags = library.tags[pose.file];
    cell.title = tags?.length ? `${pose.file}\n${tags.join(', ')}` : pose.file;
  }

  function buildCells() {
    cellsFor = poses;
    const fragment = document.createDocumentFragment();
    cells = poses.map((pose, index) => {
      const cell = el('div', 'nav-cell');
      cell.setAttribute('role', 'option');
      cell.dataset.index = String(index);
      const img = el('img', 'nav-thumb');
      img.loading = 'lazy';
      img.decoding = 'async';
      img.alt = '';
      img.draggable = false;
      img.addEventListener('load', () => cell.classList.add('loaded'), { once: true });
      img.addEventListener('error', () => cell.classList.add('broken'), { once: true });
      img.src = pose.thumb;
      const name = el('span', 'nav-cell-name', pose.file.split('/').pop());
      const star = el('span', 'nav-cell-star', '★');
      const mode = el('span', 'nav-cell-mode');
      const number = el('span', 'nav-cell-num', String(index + 1));
      cell.append(img, number, star, mode, name);
      badgeFor(cell, pose);
      fragment.append(cell);
      return cell;
    });
    grid.replaceChildren(fragment);
  }

  grid.addEventListener('click', (event) => {
    const cell = event.target.closest('.nav-cell');
    if (!cell) return;
    openPose(Number(cell.dataset.index));
  });
  grid.addEventListener('contextmenu', (event) => {
    const cell = event.target.closest('.nav-cell');
    if (!cell) return;
    event.preventDefault();
    select(Number(cell.dataset.index), false);
  });
  // The view behind must not zoom while the gallery scrolls.
  gallery.addEventListener('wheel', (event) => event.stopPropagation(), { passive: true });
  gallery.addEventListener('pointerdown', (event) => {
    if (event.target === gallery) closeGallery();
  });

  function renderVisibility() {
    if (!isOpen()) return;
    if (cellsFor !== poses) buildCells();
    const show = matches ? new Set(matches) : null;
    visible = [];
    cells.forEach((cell, index) => {
      const on = !show || show.has(index);
      cell.hidden = !on;
      if (on) visible.push(index);
    });
    const noResults = matches !== null && !matches.length;
    empty.hidden = !(noResults || !poses.length);
    empty.textContent = emptyMessage();
    grid.hidden = !visible.length;
    countLabel.textContent = matches ? `${matches.length} из ${poses.length}` : `${poses.length}`;
    markCurrent();
    if (!visible.includes(selected)) {
      const current = app.index();
      select(visible.includes(current) ? current : (visible[0] ?? -1), true);
    }
  }

  function emptyMessage() {
    if (!poses.length) return 'В коллекции нет поз.';
    if (filter.favorites && libraryReady && !library.favorites.size) {
      return 'В этой коллекции пока нет избранных поз. Отметьте позу клавишей S или снимите «Только избранное».';
    }
    if (filter.favorites && !poses.some((pose) => library.favorites.has(pose.file))) {
      return 'Нет избранных поз в этой коллекции. Снимите «Только избранное» или нажмите «Сбросить».';
    }
    return 'Нет поз, подходящих под фильтр. Нажмите «Сбросить».';
  }

  let currentCell = null;
  function markCurrent() {
    currentCell?.classList.remove('current');
    currentCell = cellsFor === poses ? cells[app.index()] || null : null;
    currentCell?.classList.add('current');
  }

  function select(index, scroll = true, block = 'nearest') {
    cells[selected]?.classList.remove('selected');
    cells[selected]?.setAttribute('aria-selected', 'false');
    selected = index;
    const cell = cells[index];
    if (cell) {
      cell.classList.add('selected');
      cell.setAttribute('aria-selected', 'true');
      if (scroll) cell.scrollIntoView({ block, inline: 'nearest' });
    }
    renderDetails();
  }

  function renderDetails() {
    const pose = poses[selected];
    details.classList.toggle('none', !pose);
    if (!pose) {
      detailName.textContent = '';
      tagsInput.value = '';
      tagsInput.disabled = true;
      return;
    }
    tagsInput.disabled = false;
    detailName.textContent = `${selected + 1}. ${pose.file}`;
    detailName.title = pose.file;
    const fav = library.favorites.has(pose.file);
    detailFav.textContent = fav ? '★' : '☆';
    detailFav.classList.toggle('on', fav);
    const source = pose.support_source;
    const mode = pose.support?.mode;
    for (const [key, b] of supportButtons) {
      const on = key === 'auto' ? source !== 'user' : source === 'user' && mode === key;
      b.classList.toggle('on', on);
      b.classList.toggle('effective', key === mode && source !== 'user');
    }
    if (document.activeElement !== tagsInput || tagsInput.dataset.file !== pose.file) {
      tagsInput.value = (library.tags[pose.file] || []).join(', ');
      tagsInput.dataset.file = pose.file;
    }
  }

  // Columns of the responsive grid (for ↑/↓).
  function columns() {
    const template = getComputedStyle(grid).gridTemplateColumns;
    return Math.max(1, template.split(' ').filter(Boolean).length);
  }

  function moveSelection(delta) {
    if (!visible.length) return;
    const position = visible.indexOf(selected);
    const next = position < 0 ? 0 : Math.min(visible.length - 1, Math.max(0, position + delta));
    select(visible[next]);
  }

  function focusGrid() {
    scroller.focus({ preventScroll: true });
  }

  // --- Open / close --------------------------------------------------------
  function isOpen() {
    return !gallery.hidden;
  }

  function openGallery() {
    if (isOpen() || !app.api) return;
    gallery.hidden = false;
    document.body.classList.add('nav-gallery-open');
    app.overlays.open(OVERLAY_ID, closeGallery);
    selected = -1;
    renderVisibility();
    const current = app.index();
    select(visible.includes(current) ? current : (visible[0] ?? -1), true, 'center');
    focusGrid();
    refreshCollections();
  }

  function closeGallery() {
    if (!isOpen()) return;
    commitTags();
    hideConfirm();
    app.overlays.close(OVERLAY_ID);
    gallery.hidden = true;
    document.body.classList.remove('nav-gallery-open');
    if (gallery.contains(document.activeElement)) document.activeElement.blur();
  }

  function toggleGallery() {
    if (isOpen()) closeGallery();
    else openGallery();
  }

  function openPose(index) {
    if (!Number.isInteger(index) || !poses[index]) return;
    closeGallery();
    if (index !== app.index()) app.goTo(index);
  }

  // Keys inside the gallery. Capture phase on window runs before the core's
  // bubbling window listener, so ←/→/Home/End never move the pose behind it.
  window.addEventListener('keydown', (event) => {
    if (!isOpen()) return;
    const typing = isTyping(event.target) || isTyping(document.activeElement);
    const plain = !event.ctrlKey && !event.altKey && !event.metaKey;
    if (event.code === 'Escape' && typing) {
      // The core ignores shortcuts while typing: close from here.
      event.preventDefault();
      event.stopImmediatePropagation();
      if (event.target === tagsInput) commitTags();
      closeGallery();
      return;
    }
    if (event.ctrlKey && event.code === 'KeyF') {
      event.preventDefault();
      event.stopImmediatePropagation();
      searchInput.focus();
      searchInput.select();
      return;
    }
    if (typing) {
      if (event.target === searchInput && (event.code === 'ArrowDown' || event.code === 'Enter')) {
        event.preventDefault();
        focusGrid();
      }
      return; // the core ignores shortcuts while typing; the field's own handlers run
    }
    if (!plain) return;
    let handled = true;
    switch (event.code) {
      case 'ArrowLeft':
      case 'KeyA':
        moveSelection(-1);
        break;
      case 'ArrowRight':
      case 'KeyD':
        moveSelection(1);
        break;
      case 'ArrowUp':
        moveSelection(-columns());
        break;
      case 'ArrowDown':
        moveSelection(columns());
        break;
      case 'PageUp':
        moveSelection(-columns() * Math.max(1, Math.floor(scroller.clientHeight / (cells[selected]?.offsetHeight || 200))));
        break;
      case 'PageDown':
        moveSelection(columns() * Math.max(1, Math.floor(scroller.clientHeight / (cells[selected]?.offsetHeight || 200))));
        break;
      case 'Home':
        if (visible.length) select(visible[0]);
        break;
      case 'End':
        if (visible.length) select(visible[visible.length - 1]);
        break;
      case 'Enter':
      case 'NumpadEnter':
        if (event.target?.tagName === 'BUTTON' && event.target !== openButton) {
          handled = false;
        } else if (!event.repeat) {
          openPose(selected);
        }
        break;
      default:
        handled = false;
    }
    if (handled) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, true);

  // --- Library (favorites, tags) ------------------------------------------
  async function loadLibrary() {
    const gen = ++libraryGen;
    const data = await call('get_library', 'Не удалось загрузить избранное и теги');
    if (gen !== libraryGen || !data) return;
    library = {
      favorites: new Set(Array.isArray(data.favorites) ? data.favorites : []),
      tags: data.tags && typeof data.tags === 'object' ? data.tags : {},
    };
    libraryReady = true;
    refreshTagOptions();
    refreshBadges();
    if (!dropMissingTag()) applyFilter();
  }

  // The saved tag filter is global; a collection without that tag would show nothing.
  // Drop just the tag (mode/favorites stay) once both the library and the prefs are in.
  function dropMissingTag() {
    if (!filter.tag || !libraryReady || !prefsRestored) return false;
    const wanted = fold(filter.tag);
    if (allTags().some((tag) => fold(tag) === wanted)) return false;
    const missing = filter.tag;
    filter = { ...filter, tag: null };
    refreshTagOptions();
    applyFilter();
    saveFilter();
    toast(`Тег «${missing}» не найден в этой коллекции — фильтр по тегу снят`);
    return true;
  }

  function allTags() {
    const seen = new Map();
    for (const list of Object.values(library.tags)) {
      for (const tag of list || []) {
        if (!seen.has(fold(tag))) seen.set(fold(tag), tag);
      }
    }
    return [...seen.values()].sort((a, b) => a.localeCompare(b, 'ru'));
  }

  function refreshTagOptions() {
    const tags = allTags();
    const options = [el('option', '', tags.length ? 'Все теги' : 'Тегов нет')];
    options[0].value = '';
    for (const tag of tags) {
      const option = el('option', '', tag);
      option.value = tag;
      options.push(option);
    }
    // A saved tag that does not exist (yet) in this collection stays selectable.
    if (filter.tag && !tags.some((tag) => fold(tag) === fold(filter.tag))) {
      const option = el('option', '', `${filter.tag} (нет)`);
      option.value = filter.tag;
      options.push(option);
    }
    tagSelect.replaceChildren(...options);
    tagSelect.value = filter.tag || '';
  }

  function refreshBadges() {
    if (cellsFor === poses) {
      cells.forEach((cell, index) => badgeFor(cell, poses[index]));
    }
    renderDetails();
    updateCounter();
  }

  async function toggleFavorite(index) {
    const pose = poses[index];
    if (!pose) return;
    const on = await call('toggle_favorite', 'Не удалось изменить избранное', pose.file);
    if (typeof on !== 'boolean') return;
    if (on) library.favorites.add(pose.file);
    else library.favorites.delete(pose.file);
    toast(on ? 'Добавлено в избранное' : 'Убрано из избранного');
    if (cells[index] && cellsFor === poses) badgeFor(cells[index], pose);
    renderDetails();
    if (filter.favorites) applyFilter();
    else updateCounter();
  }

  let tagsInFlight = null;
  function parseTags(text) {
    return text.split(',').map((tag) => tag.trim()).filter(Boolean);
  }

  async function commitTags() {
    const file = tagsInput.dataset.file;
    if (!file || tagsInput.disabled) return;
    const before = (library.tags[file] || []).join(', ');
    const wanted = parseTags(tagsInput.value);
    const key = JSON.stringify([file, wanted]);
    if (wanted.join(', ') === before || key === tagsInFlight) return;
    tagsInFlight = key;
    let result;
    try {
      result = await call('set_tags', 'Не удалось сохранить теги', file, wanted);
    } finally {
      tagsInFlight = null;
    }
    if (!Array.isArray(result)) return;
    if (result.length) library.tags[file] = result;
    else delete library.tags[file];
    if (tagsInput.dataset.file === file) tagsInput.value = result.join(', ');
    toast(result.length ? `Теги: ${result.join(', ')}` : 'Теги очищены');
    const index = poses.findIndex((pose) => pose.file === file);
    if (cells[index] && cellsFor === poses) badgeFor(cells[index], poses[index]);
    refreshTagOptions();
    if (filter.tag) applyFilter();
  }

  tagsInput.addEventListener('keydown', (event) => {
    if (event.code === 'Enter' || event.code === 'NumpadEnter') {
      event.preventDefault();
      commitTags();
      tagsInput.blur();
      focusGrid();
    }
  });
  tagsInput.addEventListener('change', () => commitTags());
  detailFav.addEventListener('click', () => toggleFavorite(selected));

  // --- Support mode --------------------------------------------------------
  async function setSupport(file, mode) {
    const gen = posesGen;
    // A size change still waiting for its debounced save must reach the backend first,
    // otherwise this reply would carry the old scale_adjust.
    if (pendingScale.has(file)) {
      clearTimeout(scaleTimer);
      saveScales();
    }
    await savingScales;
    let pose = await call('set_support_mode', 'Не удалось изменить опору', file, mode);
    // The collection was switched/reloaded meanwhile: this result belongs to the old list.
    if (gen !== posesGen) return;
    if (!pose || typeof pose !== 'object' || !Array.isArray(pose.box)) return;
    const index = app.poses().findIndex((item) => item.file === file);
    if (index < 0) return;
    // [ / ] pressed while this request was in flight: keep the size shown on screen.
    if (pendingScale.has(file)) pose = { ...pose, scale_adjust: pendingScale.get(file) };
    app.updatePose(index, pose);
    poses[index] = pose;
    const name = MODE_NAMES[pose.support?.mode] || pose.support?.mode || '?';
    if (pose.support_source === 'user') toast(`Опора: ${name} (вручную)`);
    else if (pose.support_source === 'bundled') toast(`Опора: авто — ${name} (из набора)`);
    else toast(`Опора: авто (${name})`);
    if (cells[index] && cellsFor === poses) badgeFor(cells[index], pose);
    renderDetails();
    if (filter.mode !== 'all') applyFilter();
  }

  // --- Size correction ([ / ]) ----------------------------------------------
  // Applied at once (optimistic), saved debounced: holding a key must stay smooth.
  const pendingScale = new Map(); // file -> factor waiting to be saved
  let scaleTimer = 0;
  let savingScales = Promise.resolve(); // the save currently in flight

  function nudgeScale(index, ratio) {
    const pose = poses[index];
    if (!pose) return;
    if (app.view.sizing?.() === 'file') {  // the picture keeps its file scale in this mode
      toast('Поправка размера работает в режиме «единый размер тела» — клавиша M');
      return;
    }
    const before = pose.scale_adjust > 0 ? pose.scale_adjust : 1;
    const next = ratio === null ? 1 : Math.min(2, Math.max(0.5, Math.round(before * ratio * 1000) / 1000));
    if (next === before) {
      toast(ratio === null ? 'Размер позы: 100 %' : `Размер позы: предел ${Math.round(next * 100)} %`);
      return;
    }
    const updated = { ...pose, scale_adjust: next };
    poses[index] = updated;
    app.updatePose(index, updated);
    const limited = index === app.index() && window.__poseStage?.sizeLimited;
    toast(`Размер позы: ${Math.round(next * 100)} %${limited ? ' — больше не помещается на подиум' : ''}`);
    pendingScale.set(pose.file, next);
    clearTimeout(scaleTimer);
    scaleTimer = setTimeout(saveScales, 350);
  }

  function saveScales() {
    savingScales = savingScales.then(sendScales, sendScales);
    return savingScales;
  }

  async function sendScales() {
    const gen = posesGen;
    const batch = [...pendingScale];
    pendingScale.clear();
    for (const [file, factor] of batch) {
      const pose = await call('set_pose_scale', 'Не удалось сохранить размер позы', file, factor);
      if (gen !== posesGen || pendingScale.has(file)) continue; // superseded meanwhile
      if (!pose || typeof pose !== 'object' || !Array.isArray(pose.box)) continue;
      const index = poses.findIndex((item) => item.file === file);
      if (index >= 0 && poses[index].scale_adjust === pose.scale_adjust) {
        poses[index] = pose;
        app.updatePose(index, pose);
      }
    }
  }

  // Target of S / 1–3 / 0: the gallery selection while it is open, else the pose on screen.
  function targetIndex() {
    if (isOpen()) return selected;
    return app.current() ? app.index() : -1;
  }

  // --- Filter --------------------------------------------------------------
  function filterActive() {
    return filter.mode !== 'all' || filter.favorites || !!filter.tag || !!search;
  }

  function computeMatches() {
    if (!filterActive()) return null;
    const tag = filter.tag ? fold(filter.tag) : null;
    const query = search ? fold(search) : null;
    const result = [];
    poses.forEach((pose, index) => {
      if (filter.mode !== 'all' && pose.support?.mode !== filter.mode) return;
      if (filter.favorites && !library.favorites.has(pose.file)) return;
      if (tag && !(library.tags[pose.file] || []).some((item) => fold(item) === tag)) return;
      if (query && !fold(pose.file).includes(query)) return;
      result.push(index);
    });
    return result;
  }

  function applyFilter() {
    matches = computeMatches();
    // Never setOrder([]): an empty result keeps ←/→ over the whole collection.
    app.setOrder(matches && matches.length ? matches : null);
    for (const [mode, b] of modeButtons) b.classList.toggle('on', filter.mode === mode);
    favButton.classList.toggle('on', filter.favorites);
    tagSelect.classList.toggle('on', !!filter.tag);
    if (tagSelect.value !== (filter.tag || '')) refreshTagOptions();
    resetButton.disabled = !filterActive();
    counter.classList.toggle('filtered', !!matches);
    renderVisibility();
    updateCounter();
  }

  function saveFilter() {
    clearTimeout(searchSaveTimer);
    call('save_view_prefs', 'Не удалось сохранить фильтр', { filter: { ...filter, search } });
  }

  function setFilter(change) {
    filter = { ...filter, ...change };
    applyFilter();
    saveFilter();
  }

  function resetFilter() {
    filter = { mode: 'all', favorites: false, tag: null };
    search = '';
    searchInput.value = '';
    clearTimeout(searchTimer);
    refreshTagOptions();
    applyFilter();
    saveFilter();
  }

  async function restorePrefs() {
    const prefs = await call('get_view_prefs', 'Не удалось загрузить настройки');
    prefsRestored = true;
    const saved = prefs?.filter;
    if (!saved || typeof saved !== 'object') {
      dropMissingTag();
      return;
    }
    if (typeof saved.search === 'string' && !searchInput.value) {
      search = saved.search.trim().slice(0, 100);
      searchInput.value = search;
    }
    filter = {
      mode: FILTER_MODES.some(([mode]) => mode === saved.mode) ? saved.mode : 'all',
      favorites: saved.favorites === true,
      tag: typeof saved.tag === 'string' && saved.tag ? saved.tag : null,
    };
    refreshTagOptions();
    if (!dropMissingTag()) applyFilter();
  }

  // --- Collections ---------------------------------------------------------
  function renderCollections() {
    const options = collections.map((entry) => {
      const option = el('option', '', entry.name);
      option.value = entry.id;
      option.title = entry.folder;
      return option;
    });
    collectionSelect.replaceChildren(...options);
    const active = collections.find((entry) => entry.active);
    collectionSelect.value = active?.id || '';
    collectionSelect.title = active ? `Коллекция: ${active.folder}` : 'Коллекция';
    collectionSelect.disabled = busy || collections.length < 2;
    removeButton.disabled = busy || collections.length < 2;
    removeButton.title = collections.length < 2
      ? 'Последнюю коллекцию убрать нельзя'
      : 'Убрать коллекцию из списка (файлы не удаляются)';
    addButton.disabled = busy;
  }

  async function refreshCollections() {
    const list = await call('get_collections', 'Не удалось получить список коллекций');
    if (Array.isArray(list)) {
      collections = list;
      renderCollections();
    }
  }

  function activeId() {
    return collections.find((entry) => entry.active)?.id ?? null;
  }

  // add/switch/remove: apply {collections, poses} returned by the backend.
  async function collectionOp(name, text, ...args) {
    if (busy) return;
    busy = true;
    hideConfirm();
    renderCollections();
    gallery.classList.add('busy');
    const before = activeId();
    try {
      const data = await call(name, text, ...args);
      if (!data || !Array.isArray(data.collections) || !Array.isArray(data.poses)) return;
      collections = data.collections;
      const after = activeId();
      const changed = after !== before || data.poses.length !== poses.length
        || data.poses.some((pose, i) => pose.file !== poses[i]?.file);
      if (changed) {
        commitTagsSilently();
        selected = -1;
        await app.reload(data.poses); // 'poses' event reloads library + filter
        const active = collections.find((entry) => entry.active);
        toast(active ? `Коллекция: ${active.name} (${data.poses.length})` : 'Коллекция обновлена');
      } else if (name === 'add_collection') {
        toast('Папка не добавлена');
      }
    } finally {
      busy = false;
      gallery.classList.remove('busy');
      renderCollections();
      if (isOpen()) {
        renderVisibility();
        select(visible.includes(app.index()) ? app.index() : (visible[0] ?? -1), true, 'center');
      }
    }
  }

  function commitTagsSilently() {
    tagsInput.dataset.file = '';
  }

  collectionSelect.addEventListener('change', () => {
    const id = collectionSelect.value;
    collectionSelect.blur();
    focusGrid();
    if (id && id !== activeId()) collectionOp('switch_collection', 'Не удалось переключить коллекцию', id);
  });
  addButton.addEventListener('click', () => addCollection());
  closeButton.addEventListener('click', () => closeGallery());

  function addCollection() {
    collectionOp('add_collection', 'Не удалось добавить папку');
  }

  let confirmId = null;
  function hideConfirm() {
    confirmId = null;
    confirmBox.hidden = true;
    removeButton.hidden = false;
  }
  removeButton.addEventListener('click', () => {
    const active = collections.find((entry) => entry.id === collectionSelect.value) || collections.find((entry) => entry.active);
    if (!active || collections.length < 2) return;
    confirmId = active.id;
    confirmText.textContent = `Убрать «${active.name}» из списка? Файлы останутся на диске.`;
    confirmBox.hidden = false;
    removeButton.hidden = true;
  });
  confirmNo.addEventListener('click', () => hideConfirm());
  confirmYes.addEventListener('click', () => {
    const id = confirmId;
    hideConfirm();
    if (id) collectionOp('remove_collection', 'Не удалось убрать коллекцию', id);
  });

  // --- Events --------------------------------------------------------------
  app.on('poses', (detail) => {
    poses = Array.isArray(detail?.poses) ? detail.poses : app.poses();
    posesGen++;
    library = { favorites: new Set(), tags: {} };
    libraryReady = false;
    cellsFor = null;
    selected = -1;
    // reload() resets the core's order; re-apply (library-dependent parts refine after load).
    applyFilter();
    if (app.api) loadLibrary();
  });

  app.on('pose', (detail) => {
    const index = detail?.index;
    if (Number.isInteger(index) && detail.pose && poses[index] && poses[index] !== detail.pose) {
      poses[index] = detail.pose; // updatePose() by someone else
      if (cells[index] && cellsFor === poses) badgeFor(cells[index], detail.pose);
    }
    if (isOpen()) markCurrent();
    updateCounter();
  });

  // --- Shortcuts -----------------------------------------------------------
  const register = (keys, title, handler) => app.shortcuts.register({ keys, title, group: GROUP, handler });
  register(['KeyG'], 'Галерея миниатюр', (event) => {
    if (!event.repeat) toggleGallery();
  });
  register(['KeyS'], 'Избранное: добавить / убрать', (event) => {
    if (!event.repeat) toggleFavorite(targetIndex());
  });
  const supportKeys = [
    ['Digit1', 'Numpad1', 'feet', 'Опора: стопы'],
    ['Digit2', 'Numpad2', 'kneel', 'Опора: колени'],
    ['Digit3', 'Numpad3', 'ground', 'Опора: сидя / лёжа'],
    ['Digit0', 'Numpad0', 'auto', 'Опора: авто'],
  ];
  for (const [digit, numpad, mode, title] of supportKeys) {
    register([digit, numpad], title, (event) => {
      if (event.repeat) return;
      const pose = poses[targetIndex()];
      if (pose) setSupport(pose.file, mode);
    });
  }
  const scaleKeys = [
    [['BracketLeft'], 'Размер позы: меньше на 3 %', 1 / 1.03],
    [['BracketRight'], 'Размер позы: больше на 3 %', 1.03],
    [['Shift+BracketLeft'], 'Размер позы: меньше на 10 %', 1 / 1.1],
    [['Shift+BracketRight'], 'Размер позы: больше на 10 %', 1.1],
    [['Backslash', 'IntlBackslash'], 'Размер позы: сбросить к 100 %', null],
  ];
  for (const [keys, title, ratio] of scaleKeys) {
    register(keys, title, () => nudgeScale(targetIndex(), ratio));
  }
  register(['KeyO'], 'Добавить папку-коллекцию', (event) => {
    if (!event.repeat) addCollection();
  });

  // --- Startup -------------------------------------------------------------
  renderCollections();
  applyFilter();
  app.ready.then(async () => {
    if (!app.api) return;
    if (!poses.length) poses = app.poses();
    await Promise.all([restorePrefs(), refreshCollections()]);
    updateCounter();
  }).catch((error) => report(error, 'Навигация'));

  // Read-only diagnostics for local checks only.
  window.__poseStageNav = {
    state: () => ({
      open: isOpen(),
      selected,
      filter: { ...filter, search },
      matches: matches ? matches.length : null,
      favorites: library.favorites.size,
      tags: allTags(),
      collections: collections.map((entry) => entry.name),
      prefsRestored,
    }),
  };
}
