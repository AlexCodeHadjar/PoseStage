// Artist tools (mirror, grayscale, grid, background, parallax, zoom/pan, on-top, compact) —
// implemented against docs/API.md §2.
//
// Mirror and grayscale are CSS classes on the WebGL canvas (GPU-composited, no re-render).
// Because the mirror is a pure screen-space flip, this module wraps app.view.setZoom/pan/
// figureRect so every caller keeps working in ON-SCREEN CSS px while the image is mirrored.

const GROUP = 'Инструменты художника';
const MIN_ZOOM = 1;
const MAX_ZOOM = 8;
const WHEEL_STEP = 1.15; // zoom factor per mouse-wheel notch (deltaY = 100)
const KEY_STEP = 1.25; // zoom factor per +/− press
const DOUBLE_ZOOM = 2.5;
const DRAG_THRESHOLD = 4; // px before a press on the canvas becomes a pan
const TOAST_MS = 1400;
const GRID_MODES = ['off', 'thirds', 'proportions'];
const GRID_NAMES = { off: 'выкл', thirds: 'трети', proportions: 'пропорции' };
const HEADS = 8;
const PARALLAX_MODES = ['off', 'soft', 'strong'];
const PARALLAX_NAMES = { off: 'выкл', soft: 'мягкий', strong: 'сильный' };
const BACKGROUND_ID = /^[0-9a-f]{16}$/;
const BLUR_MAX = 24;
const DIM_MAX = 0.85;
const PANEL_ID = 'artist-background';
const SVG_NS = 'http://www.w3.org/2000/svg';

const clampZoom = (z) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));
const onOff = (on) => (on ? 'вкл' : 'выкл');

export function install(app) {
  const canvas = app.view.canvas;
  const state = {
    flip: false,
    gray: false,
    grid: 'off',
    on_top: false,
    compact: false,
    parallax: 'soft',
    sizing: 'file',
    background: { mode: 'studio', id: null, blur: 0, dim: 0 },
  };
  let backgrounds = []; // [{id, url, w, h}]: copies in %APPDATA%\PoseStage\backgrounds
  let panelOpen = false;
  const touched = new Set(); // keys changed by the user before the saved prefs arrived
  let windowOnTop = false; // actual window on-top state (compact forces it on)
  let compactSince = 0; // time of the last set_compact call (resize-detection grace period)

  // --- Errors / API -------------------------------------------------------
  function report(error) {
    const text = `Инструменты художника: ${error?.stack || error?.message || error}`;
    console.error(text);
    try {
      Promise.resolve(currentApi()?.report_error(text)).catch(() => {});
    } catch {
      // Logging must never throw.
    }
  }

  // Calls an API method; resolves to its result, or undefined when the API (or this
  // method, e.g. in a test double) is missing or the call rejects (rejections are reported).
  async function call(name, ...args) {
    const api = currentApi();
    if (!api || typeof api[name] !== 'function') {
      return undefined;
    }
    try {
      const result = await api[name](...args);
      return result;
    } catch (error) {
      report(error);
      return undefined;
    }
  }

  // The core sets app.api only after the UI modules are installed; pywebview's object is
  // available earlier (read it directly; never resolve a promise with it — the dev
  // harness API is a thenable-looking Proxy).
  function currentApi() {
    return app.api || window.pywebview?.api || null;
  }

  function save(partial) {
    call('save_view_prefs', partial);
  }

  // --- Mirror-aware view wrappers (screen CSS px in, layout CSS px out) ------
  // Calls from outside this module also cancel a running zoom glide, so an external
  // setZoom/pan/resetView is never overridden by the animation's remaining frames.
  const raw = {
    setZoom: app.view.setZoom,
    pan: app.view.pan,
    resetView: app.view.resetView,
    figureRect: app.view.figureRect,
  };
  const width = () => canvas.clientWidth || innerWidth;
  function applyZoom(zoom, centerX, centerY) {
    const x = state.flip && Number.isFinite(centerX) ? width() - centerX : centerX;
    raw.setZoom(zoom, x, centerY);
  }
  function applyPan(dx, dy) {
    raw.pan(state.flip ? -dx : dx, dy);
  }
  app.view.setZoom = (zoom, centerX, centerY) => {
    stopZoomAnimation();
    applyZoom(zoom, centerX, centerY);
  };
  app.view.pan = (dx, dy) => {
    stopZoomAnimation();
    applyPan(dx, dy);
  };
  app.view.resetView = () => {
    stopZoomAnimation();
    raw.resetView();
  };
  app.view.figureRect = () => {
    const rect = raw.figureRect();
    if (!rect || !state.flip) {
      return rect;
    }
    return { ...rect, left: width() - rect.left - rect.width };
  };

  // --- DOM ----------------------------------------------------------------
  const grid = document.createElement('canvas');
  grid.className = 'artist-grid';
  grid.setAttribute('aria-hidden', 'true');
  grid.hidden = true;
  canvas.after(grid); // below the arrows and other UI in stacking order
  const gridContext = grid.getContext('2d');

  const toast = document.createElement('div');
  toast.className = 'artist-toast';
  toast.setAttribute('role', 'status');
  toast.setAttribute('aria-live', 'polite');

  const strip = document.createElement('div');
  strip.className = 'artist-tools';
  strip.setAttribute('role', 'toolbar');
  strip.setAttribute('aria-label', 'Инструменты художника');
  strip.setAttribute('aria-orientation', 'vertical');

  function icon(paths) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    for (const [d, cls] of paths) {
      const path = document.createElementNS(SVG_NS, 'path');
      path.setAttribute('d', d);
      if (cls) path.setAttribute('class', cls);
      svg.append(path);
    }
    return svg;
  }

  function button(label, key, paths, onClick, toggle = true) {
    const element = document.createElement('button');
    element.type = 'button';
    element.className = 'artist-tool';
    element.setAttribute('aria-label', label);
    element.title = `${label} (${key})`;
    if (toggle) element.setAttribute('aria-pressed', 'false');
    element.append(icon(paths));
    element.addEventListener('click', (event) => {
      event.stopPropagation();
      try {
        onClick();
      } catch (error) {
        report(error);
      }
    });
    strip.append(element);
    return element;
  }

  const buttons = {
    flip: button('Зеркально по горизонтали', 'H', [
      ['M12 3.5v17', 'dash'],
      ['M9 7 4 17h5z'],
      ['M15 7l5 10h-5z', 'fill'],
    ], () => toggleFlip()),
    gray: button('Оттенки серого', 'V', [
      ['M12 4a8 8 0 1 0 0 16a8 8 0 1 0 0-16z'],
      ['M12 4a8 8 0 0 1 0 16z', 'fill'],
    ], () => toggleGray()),
    grid: button('Сетка', 'L', [
      ['M4.5 4.5h15v15h-15z'],
      ['M9.5 4.5v15M14.5 4.5v15M4.5 9.5h15M4.5 14.5h15'],
    ], () => cycleGrid()),
    background: button('Фон', 'B', [
      ['M4 5.5h16v13H4z'],
      ['M4 15.5l4.5-4.5 4 4 2.5-2.5 5 5'],
      ['M15.6 7.6a1.4 1.4 0 1 0 0 2.8a1.4 1.4 0 1 0 0-2.8z', 'fill'],
    ], () => togglePanel(), false),
    parallax: button('Параллакс', 'P', [
      ['M12 4 3.5 8.5 12 13l8.5-4.5z'],
      ['M3.5 12.5 12 17l8.5-4.5'],
      ['M3.5 16.2 12 20.7l8.5-4.5', 'dash'],
    ], () => cycleParallax()),
    zoom: button('Сбросить масштаб', 'Z', [
      ['M10.5 4.5a6 6 0 1 0 0 12a6 6 0 1 0 0-12z'],
      ['M15 15l5 5M8 10.5h5'],
    ], () => resetZoom(), false),
    onTop: button('Поверх всех окон', 'T', [
      ['M8.5 4h7M10 4v6l-3 4h10l-3-4V4'],
      ['M12 14v6.5'],
    ], () => toggleOnTop()),
    compact: button('Компактное окно', 'C', [
      ['M3.5 5h17v14h-17z'],
      ['M12.5 11h5.5v5.5h-5.5z', 'fill'],
    ], () => toggleCompact()),
  };
  buttons.zoom.title = 'Сбросить масштаб (Z) · колесо — масштаб, перетаскивание — панорама, двойной щелчок — 2,5×';

  document.body.append(strip, toast);

  // --- Toast / zoom badge -------------------------------------------------
  let toastTimer = 0;
  function showToast(text, kind = '') {
    toast.textContent = text;
    toast.classList.toggle('is-zoom', kind === 'zoom');
    toast.classList.add('is-visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('is-visible'), TOAST_MS);
  }

  // --- Render state ------------------------------------------------------
  function setPressed(element, on) {
    element.setAttribute('aria-pressed', String(on));
    element.classList.toggle('is-active', on);
  }

  function render() {
    canvas.classList.toggle('artist-flip', state.flip);
    canvas.classList.toggle('artist-gray', state.gray);
    setPressed(buttons.flip, state.flip);
    setPressed(buttons.gray, state.gray);
    setPressed(buttons.grid, state.grid !== 'off');
    buttons.grid.dataset.mode = state.grid === 'thirds' ? '⅓' : state.grid === 'proportions' ? '8' : '';
    buttons.grid.title = `Сетка: ${GRID_NAMES[state.grid]} (L)`;
    setPressed(buttons.onTop, windowOnTop);
    buttons.onTop.title = state.compact && !state.on_top && windowOnTop
      ? 'Поверх всех окон (T) — включено компактным режимом'
      : 'Поверх всех окон (T)';
    setPressed(buttons.compact, state.compact);
    setPressed(buttons.parallax, state.parallax !== 'off');
    buttons.parallax.dataset.mode = state.parallax === 'strong' ? '2' : '';
    buttons.parallax.title = `Параллакс: ${PARALLAX_NAMES[state.parallax]} (P)`;
    buttons.background.classList.toggle('is-active', state.background.mode === 'image' || panelOpen);
    buttons.background.setAttribute('aria-expanded', String(panelOpen));
    buttons.background.title = state.background.mode === 'image' ? 'Фон: изображение (B)' : 'Фон: студия (B)';
    grid.hidden = state.grid === 'off';
    gridDirty = true;
    drawGrid();
  }

  // --- 1. Mirror / grayscale ---------------------------------------------
  function toggleFlip() {
    state.flip = !state.flip;
    touched.add('flip');
    render();
    save({ flip: state.flip });
    showToast(`Зеркально: ${onOff(state.flip)}`);
  }

  function toggleGray() {
    state.gray = !state.gray;
    touched.add('gray');
    render();
    save({ gray: state.gray });
    showToast(`Оттенки серого: ${onOff(state.gray)}`);
  }

  // --- 2. Grid overlay ---------------------------------------------------
  let gridDirty = true;
  let gridKey = '';
  let gridFaded = false;

  function cycleGrid() {
    state.grid = GRID_MODES[(GRID_MODES.indexOf(state.grid) + 1) % GRID_MODES.length];
    touched.add('grid');
    render();
    save({ grid: state.grid });
    showToast(`Сетка: ${GRID_NAMES[state.grid]}`);
  }

  function drawGrid() {
    if (state.grid === 'off') {
      if (gridKey !== 'off') {
        gridContext.clearRect(0, 0, grid.width, grid.height);
        gridKey = 'off';
      }
      return;
    }
    const dpr = Math.min(devicePixelRatio || 1, 3);
    const w = grid.clientWidth || innerWidth;
    const h = grid.clientHeight || innerHeight;
    const rect = state.grid === 'proportions' ? app.view.figureRect() : null;
    const key = [state.grid, w, h, dpr, rect && [rect.left, rect.top, rect.width, rect.height].map((v) => v.toFixed(1))].join('|');
    if (!gridDirty && key === gridKey) {
      return;
    }
    gridDirty = false;
    gridKey = key;
    const pw = Math.round(w * dpr);
    const ph = Math.round(h * dpr);
    if (grid.width !== pw || grid.height !== ph) {
      grid.width = pw;
      grid.height = ph;
    }
    const g = gridContext;
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, pw, ph);
    // Work in device px so 1-px lines stay crisp at any DPR.
    const snap = (v) => Math.round(v * dpr) + 0.5;
    const stroke = (x1, y1, x2, y2, style) => {
      g.beginPath();
      g.moveTo(x1, y1);
      g.lineTo(x2, y2);
      g.strokeStyle = style;
      g.stroke();
    };
    const line = (x1, y1, x2, y2, alpha = 0.42) => {
      g.lineWidth = Math.max(1, Math.round(dpr));
      const o = Math.max(1, Math.round(dpr)); // soft dark under-stroke: visible on light paper too
      stroke(x1 + o, y1 + o, x2 + o, y2 + o, 'rgba(0, 0, 0, 0.28)');
      stroke(x1, y1, x2, y2, `rgba(255, 255, 255, ${alpha})`);
    };

    if (state.grid === 'thirds') {
      for (const f of [1 / 3, 2 / 3]) {
        line(snap(w * f), 0, snap(w * f), ph);
        line(0, snap(h * f), pw, snap(h * f));
      }
      return;
    }

    if (!rect || rect.width <= 0 || rect.height <= 0) {
      return;
    }
    const overhang = Math.max(14, rect.width * 0.12);
    const left = rect.left - overhang;
    const right = rect.left + rect.width + overhang;
    const cx = snap(rect.left + rect.width / 2);
    line(cx, snap(rect.top), cx, snap(rect.top + rect.height), 0.38);
    const unit = rect.height / HEADS;
    for (let i = 0; i <= HEADS; i++) {
      const y = snap(rect.top + unit * i);
      const strong = i === 0 || i === HEADS || i === HEADS / 2;
      line(snap(left), y, snap(right), y, strong ? 0.5 : 0.3);
    }
    // Head numbers 1–8, subtle, left of the figure in the middle of each band.
    const fontPx = Math.max(9, Math.min(13, unit * 0.32));
    if (unit >= 10) {
      g.font = `${Math.round(fontPx * dpr)}px Segoe UI, sans-serif`;
      g.textAlign = 'right';
      g.textBaseline = 'middle';
      const tx = Math.round((left - 5) * dpr);
      for (let i = 0; i < HEADS; i++) {
        const ty = Math.round((rect.top + unit * (i + 0.5)) * dpr);
        g.fillStyle = 'rgba(0, 0, 0, 0.35)';
        g.fillText(String(i + 1), tx + 1, ty + 1);
        g.fillStyle = 'rgba(255, 255, 255, 0.55)';
        g.fillText(String(i + 1), tx, ty);
      }
    }
  }

  function setGridFaded(faded) {
    gridFaded = faded;
    grid.classList.toggle('is-faded', faded);
  }

  // --- 3. Zoom & pan -----------------------------------------------------
  let zoomTarget = null;
  let zoomAnchor = [undefined, undefined];
  let zoomFrame = 0;
  let zoomLast = 0;
  let lastZoom = 1;

  function zoomTo(target, x, y, badge = true) {
    zoomTarget = clampZoom(target);
    zoomAnchor = [x, y];
    if (badge) {
      showToast(`${Math.round(zoomTarget * 100)} %`, 'zoom');
    }
    if (!zoomFrame) {
      zoomLast = 0;
      zoomFrame = requestAnimationFrame(zoomStep);
    }
  }

  // Exponential approach in log space: equal-feeling speed in and out.
  function zoomStep(now) {
    zoomFrame = 0;
    if (zoomTarget === null) {
      return;
    }
    const dt = zoomLast ? Math.min(0.05, (now - zoomLast) / 1000) : 1 / 60;
    zoomLast = now;
    const current = app.view.zoom();
    const k = 1 - Math.exp(-dt * 16);
    let next = Math.exp(Math.log(current) + (Math.log(zoomTarget) - Math.log(current)) * k);
    if (Math.abs(Math.log(next / zoomTarget)) < 0.003) {
      next = zoomTarget;
    }
    try {
      applyZoom(next, zoomAnchor[0], zoomAnchor[1]);
    } catch (error) {
      report(error);
      zoomTarget = null;
      return;
    }
    if (next === zoomTarget) {
      zoomTarget = null;
    } else {
      zoomFrame = requestAnimationFrame(zoomStep);
    }
    syncZoomState();
  }

  function stopZoomAnimation() {
    zoomTarget = null;
    cancelAnimationFrame(zoomFrame);
    zoomFrame = 0;
  }

  function zoomBy(factor, x, y) {
    zoomTo((zoomTarget ?? app.view.zoom()) * factor, x, y);
  }

  function resetZoom(badge = true) {
    if (app.view.zoom() === 1 && zoomTarget === null) {
      if (badge) showToast('100 %', 'zoom');
      return;
    }
    zoomTo(1, undefined, undefined, badge);
  }

  function resetZoomNow() {
    stopZoomAnimation();
    raw.resetView();
    syncZoomState();
  }

  function syncZoomState() {
    const zoom = app.view.zoom();
    if (zoom === lastZoom) {
      return;
    }
    lastZoom = zoom;
    const zoomed = zoom > 1.0001;
    document.body.classList.toggle('artist-zoomed', zoomed);
    buttons.zoom.classList.toggle('is-active', zoomed); // a command, highlighted while zoomed
    gridDirty = true;
  }

  // Wheel over the stage (or the bare body) zooms around the cursor.
  window.addEventListener('wheel', (event) => {
    const target = event.target;
    if (!(target === canvas || target === grid || target === document.body || target === document.documentElement)) {
      return;
    }
    if (app.overlays.any() || document.body.classList.contains('error')) {
      return;
    }
    event.preventDefault(); // also blocks Ctrl+wheel page zoom
    let delta = event.deltaY;
    if (event.deltaMode === 1) delta *= 33;
    else if (event.deltaMode === 2) delta *= innerHeight;
    delta = Math.max(-300, Math.min(300, delta));
    if (!delta) return;
    zoomBy(Math.pow(WHEEL_STEP, -delta / 100), event.clientX, event.clientY);
  }, { passive: false });

  // Left-drag pans while zoomed; a press that does not move stays a plain click.
  let drag = null;
  let suppressClick = false;

  canvas.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 || app.view.zoom() <= 1.0001 || event.ctrlKey || event.altKey || event.metaKey) {
      return;
    }
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY, panning: false };
  });

  canvas.addEventListener('pointermove', (event) => {
    if (!drag || event.pointerId !== drag.id) {
      return;
    }
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (!drag.panning) {
      if (Math.hypot(dx, dy) < DRAG_THRESHOLD) {
        return;
      }
      drag.panning = true;
      try {
        canvas.setPointerCapture(event.pointerId);
      } catch {
        // The pointer may already be gone.
      }
      document.body.classList.add('artist-panning');
    }
    drag.x = event.clientX;
    drag.y = event.clientY;
    applyPan(dx, dy); // a pan during a zoom glide keeps the glide going
  });

  function endDrag(event) {
    if (!drag || (event && event.pointerId !== drag.id)) {
      return;
    }
    if (drag.panning) {
      suppressClick = true;
      setTimeout(() => {
        suppressClick = false;
      }, 0);
      document.body.classList.remove('artist-panning');
    }
    drag = null;
  }
  canvas.addEventListener('pointerup', endDrag);
  canvas.addEventListener('pointercancel', endDrag);
  canvas.addEventListener('lostpointercapture', endDrag);
  // A click that ended a pan is not a click for anyone else.
  canvas.addEventListener('click', (event) => {
    if (suppressClick) {
      event.stopImmediatePropagation();
      event.preventDefault();
    }
  }, true);

  canvas.addEventListener('dblclick', (event) => {
    if (app.overlays.any()) {
      return;
    }
    const zoomed = (zoomTarget ?? app.view.zoom()) > 1.0001;
    if (zoomed) {
      zoomTo(1, event.clientX, event.clientY);
    } else {
      zoomTo(DOUBLE_ZOOM, event.clientX, event.clientY);
    }
  });

  // --- 4a. Parallax -------------------------------------------------------
  function setParallax(mode) {
    const applied = typeof app.view.setParallax === 'function' ? app.view.setParallax(mode) : mode;
    state.parallax = PARALLAX_MODES.includes(applied) ? applied : mode;
  }

  function cycleParallax() {
    touched.add('parallax');
    setParallax(PARALLAX_MODES[(PARALLAX_MODES.indexOf(state.parallax) + 1) % PARALLAX_MODES.length]);
    render();
    save({ parallax: state.parallax });
    showToast(`Параллакс: ${PARALLAX_NAMES[state.parallax]}`);
  }

  // --- 4a. Pose sizing: exactly as drawn in the files (default), or one body size ---
  function applySizing() {
    if (typeof app.view.setSizing === 'function') state.sizing = app.view.setSizing(state.sizing);
  }

  function toggleSizing() {
    touched.add('sizing');
    state.sizing = state.sizing === 'file' ? 'body' : 'file';
    applySizing();
    save({ sizing: state.sizing });
    showToast(state.sizing === 'file'
      ? 'Масштаб: как в файле — рисунок без изменений, по центру подиума'
      : 'Масштаб: единый размер тела ([ / ] — поправка)');
  }

  // --- 4b. Background: studio or a stored picture (glass popover) ------------
  const panel = document.createElement('div');
  panel.className = 'artist-bg';
  panel.hidden = true;
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', 'Фон');
  const panelTitle = document.createElement('div');
  panelTitle.className = 'artist-bg-title';
  panelTitle.textContent = 'Фон';
  const tiles = document.createElement('div');
  tiles.className = 'artist-bg-tiles';
  const actions = document.createElement('div');
  actions.className = 'artist-bg-actions';
  const addButton = textButton('Добавить изображение…', () => addBackground().catch(report));
  const removeButton = textButton('Удалить', () => removeSelected().catch(report));
  removeButton.classList.add('is-danger');
  actions.append(addButton, removeButton);
  const blurSlider = slider('Размытие', 0, BLUR_MAX, 1, (v) => `${Math.round(v)}`, 'blur');
  const dimSlider = slider('Затемнение', 0, DIM_MAX, 0.01, (v) => `${Math.round(v * 100)} %`, 'dim');
  panel.append(panelTitle, tiles, actions, blurSlider.row, dimSlider.row);
  document.body.append(panel);

  function textButton(label, onClick) {
    const element = document.createElement('button');
    element.type = 'button';
    element.className = 'artist-bg-button';
    element.textContent = label;
    element.addEventListener('click', (event) => {
      event.stopPropagation();
      onClick();
    });
    return element;
  }

  function slider(label, min, max, step, format, key) {
    const row = document.createElement('label');
    row.className = 'artist-bg-slider';
    const name = document.createElement('span');
    name.textContent = label;
    const input = document.createElement('input');
    input.type = 'range';
    input.min = String(min);
    input.max = String(max);
    input.step = String(step);
    input.setAttribute('aria-label', label);
    const output = document.createElement('output');
    row.append(name, input, output);
    input.addEventListener('input', () => {
      touched.add('background');
      state.background[key] = Math.min(max, Math.max(min, Number(input.value) || 0));
      output.textContent = format(state.background[key]);
      scheduleBackground();
    });
    input.addEventListener('change', saveBackground);
    return {
      row,
      sync() {
        input.value = String(state.background[key]);
        output.textContent = format(state.background[key]);
        input.disabled = state.background.mode !== 'image';
      },
    };
  }

  function selectedEntry() {
    const bg = state.background;
    return bg.mode === 'image' ? backgrounds.find((entry) => entry.id === bg.id) || null : null;
  }

  function tile(label, selected, onClick, imageUrl = null) {
    const element = document.createElement('button');
    element.type = 'button';
    element.className = 'artist-bg-tile';
    element.setAttribute('aria-label', label);
    element.setAttribute('aria-pressed', String(selected));
    element.title = label;
    element.classList.toggle('is-selected', selected);
    if (imageUrl) {
      const img = document.createElement('img');
      img.alt = '';
      img.decoding = 'async';
      img.loading = 'lazy';
      img.src = imageUrl;
      element.append(img);
    } else {
      element.classList.add('is-studio');
      element.textContent = label;
    }
    element.addEventListener('click', (event) => {
      event.stopPropagation();
      onClick();
    });
    return element;
  }

  function renderPanel() {
    const selected = selectedEntry();
    const focused = panel.contains(document.activeElement) ? document.activeElement.getAttribute('aria-label') : null;
    tiles.replaceChildren(
      tile('Студия', !selected, () => selectBackground(null)),
      ...backgrounds.map((entry, i) => tile(
        `Изображение ${i + 1} · ${entry.w}×${entry.h}`,
        selected?.id === entry.id,
        () => selectBackground(entry.id),
        entry.url,
      )),
    );
    if (focused) {
      [...tiles.children].find((element) => element.getAttribute('aria-label') === focused)?.focus();
    }
    removeButton.disabled = !selected;
    addButton.disabled = typeof currentApi()?.choose_background !== 'function' || addButton.dataset.busy === '1';
    blurSlider.sync();
    dimSlider.sync();
    placePanel(); // the content height may have changed
  }

  // The popover never covers the core's ← → arrows, the navigator's counter or the tool strip.
  // Tried in order: left of the strip; left of the strip but above the counter; above the
  // strip; the column between the arrows (narrow layout, scrolls if it must); finally above
  // the strip with scrolling.
  const PANEL_GAP = 8;
  const PANEL_WIDTH = 292;
  function panelObstacles() {
    const rect = (selector) => {
      const element = document.querySelector(selector);
      const r = element && element.getClientRects().length ? element.getBoundingClientRect() : null;
      return r && r.width > 0 && r.height > 0 ? r : null;
    };
    return { prev: rect('#prev'), next: rect('#next'), counter: rect('.nav-counter'), strip: strip.getBoundingClientRect() };
  }

  function placePanel() {
    if (!panelOpen) return;
    const { prev, next, counter, strip: tools } = panelObstacles();
    const blocks = [prev, next, counter, tools].filter(Boolean);
    const vw = innerWidth;
    const vh = innerHeight;
    const hits = (a, b) => a.left < b.right + PANEL_GAP && a.right > b.left - PANEL_GAP
      && a.top < b.bottom + PANEL_GAP && a.bottom > b.top - PANEL_GAP;
    const tryPlace = (width, right, bottom, scroll = false) => {
      width = Math.floor(Math.min(PANEL_WIDTH, width));
      right = Math.round(right);
      bottom = Math.round(bottom);
      if (width < 150) return false;
      panel.classList.toggle('is-narrow', width < 260);
      panel.style.width = `${width}px`;
      panel.style.right = `${right}px`;
      panel.style.bottom = `${bottom}px`;
      panel.style.maxHeight = 'none';
      const room = vh - bottom - PANEL_GAP;
      const natural = panel.offsetHeight;
      if (room < 120 || (!scroll && natural > room)) return false;
      const height = Math.min(natural, room);
      const box = { left: vw - right - width, right: vw - right, top: vh - bottom - height, bottom: vh - bottom };
      if (box.left < PANEL_GAP || blocks.some((b) => hits(box, b))) return false;
      panel.style.maxHeight = `${room}px`;
      return true;
    };
    const full = vw - 2 * PANEL_GAP;
    const besideStrip = vw - tools.left + 10;
    // The column between the arrows, above whatever sits at the bottom inside it.
    const left = Math.ceil(prev ? prev.right + PANEL_GAP : PANEL_GAP);
    const right = Math.floor(next ? next.left - PANEL_GAP : vw - PANEL_GAP);
    const floor = Math.min(vh, ...[tools, counter].filter((b) => b && b.left < right && b.right > left).map((b) => b.top));
    const placed = tryPlace(Math.min(full, tools.left - 10 - PANEL_GAP), besideStrip, vh - tools.bottom)
      || (counter && tryPlace(Math.min(full, tools.left - 10 - PANEL_GAP), besideStrip, vh - counter.top + PANEL_GAP))
      || tryPlace(Math.min(full, tools.right - PANEL_GAP), vw - tools.right, vh - tools.top + 10)
      || tryPlace(right - left, vw - right, vh - floor + PANEL_GAP, true);
    if (!placed) {
      tryPlace(Math.min(full, tools.right - PANEL_GAP), vw - tools.right, vh - tools.top + 10, true);
      const room = vh - (vh - tools.top + 10) - PANEL_GAP;
      panel.style.maxHeight = `${Math.max(120, room)}px`;
    }
  }

  function openPanel() {
    if (panelOpen) return;
    panelOpen = true;
    panel.hidden = false;
    renderPanel();
    placePanel();
    app.overlays.open(PANEL_ID, closePanel);
    render();
    refreshBackgrounds().catch(report);
    (tiles.querySelector('.is-selected') || tiles.firstElementChild)?.focus({ preventScroll: true });
  }

  function closePanel() {
    if (!panelOpen) return;
    panelOpen = false;
    if (panel.contains(document.activeElement)) document.activeElement.blur();
    panel.hidden = true;
    app.overlays.close(PANEL_ID);
    render();
  }

  function togglePanel() {
    if (panelOpen) closePanel();
    else openPanel();
  }

  // A press anywhere else closes the popover (the B button toggles it itself).
  window.addEventListener('pointerdown', (event) => {
    if (panelOpen && !panel.contains(event.target) && !buttons.background.contains(event.target)) {
      closePanel();
    }
  }, true);
  window.addEventListener('resize', placePanel);

  async function refreshBackgrounds() {
    const list = await call('list_backgrounds');
    if (Array.isArray(list)) {
      backgrounds = list.filter((entry) => entry && BACKGROUND_ID.test(entry.id) && typeof entry.url === 'string');
      if (panelOpen) renderPanel();
    }
  }

  function saveBackground() {
    save({ background: { ...state.background } });
  }

  let failedBackground = null; // url whose load error was already shown
  async function applyBackground() {
    if (typeof app.view.setBackground !== 'function') return;
    const entry = selectedEntry();
    const bg = state.background;
    try {
      await app.view.setBackground({ mode: entry ? 'image' : 'studio', url: entry?.url ?? null, blur: bg.blur, dim: bg.dim });
      failedBackground = null;
    } catch (error) {
      if (failedBackground !== entry?.url) {
        failedBackground = entry?.url ?? null;
        report(error);
        showToast('Не удалось загрузить фон');
      }
    }
  }

  let backgroundFrame = 0;
  function scheduleBackground() {
    if (!backgroundFrame) {
      backgroundFrame = requestAnimationFrame(() => {
        backgroundFrame = 0;
        applyBackground();
      });
    }
  }

  function selectBackground(id) {
    touched.add('background');
    state.background.mode = id ? 'image' : 'studio';
    state.background.id = id || null;
    if (panelOpen) renderPanel();
    render();
    applyBackground();
    saveBackground();
  }

  async function addBackground() {
    const api = currentApi();
    if (typeof api?.choose_background !== 'function' || addButton.dataset.busy === '1') return;
    addButton.dataset.busy = '1';
    addButton.disabled = true;
    try {
      const entry = await api.choose_background();
      if (!entry || !BACKGROUND_ID.test(entry.id)) return;
      await refreshBackgrounds();
      if (!backgrounds.some((b) => b.id === entry.id)) backgrounds.push(entry);
      selectBackground(entry.id);
      showToast('Фон: изображение');
    } catch (error) {
      report(error);
      showToast('Не удалось открыть изображение');
    } finally {
      addButton.dataset.busy = '';
      if (panelOpen) renderPanel();
    }
  }

  async function removeSelected() {
    const entry = selectedEntry();
    if (!entry) return;
    const list = await call('remove_background', entry.id);
    backgrounds = Array.isArray(list) ? list : backgrounds.filter((b) => b.id !== entry.id);
    selectBackground(null);
    showToast('Фон удалён, включена студия');
  }

  // --- 4. Window: on top / compact ---------------------------------------
  async function setWindowOnTop(on) {
    const result = await call('set_on_top', on);
    windowOnTop = typeof result === 'boolean' ? result : on;
  }

  async function toggleOnTop() {
    const on = !windowOnTop;
    state.on_top = on;
    windowOnTop = on;
    touched.add('on_top');
    render();
    showToast(`Поверх всех окон: ${onOff(on)}`);
    save({ on_top: on });
    await setWindowOnTop(on);
    render();
  }

  async function setCompact(on) {
    compactSince = performance.now();
    const result = await call('set_compact', on);
    const value = typeof result === 'boolean' ? result : on;
    state.compact = value;
    windowOnTop = value || state.on_top; // backend: compact forces on top, leaving restores the pref
  }

  async function toggleCompact() {
    const on = !state.compact;
    state.compact = on;
    windowOnTop = on || state.on_top;
    touched.add('compact');
    render();
    showToast(on ? 'Компактное окно: вкл (поверх всех)' : 'Компактное окно: выкл');
    save({ compact: on });
    await setCompact(on);
    render();
  }

  // The core's F / title-bar double-click (toggle_fullscreen) leaves compact mode in the
  // backend without telling the UI: notice the window becoming full-screen again.
  window.addEventListener('resize', () => {
    gridDirty = true;
    drawGrid();
    if (!state.compact || performance.now() - compactSince < 1500) {
      return;
    }
    const fullscreen = document.fullscreenElement
      || (Math.abs(innerWidth - screen.width) <= 2 && Math.abs(innerHeight - screen.height) <= 2);
    if (fullscreen) {
      state.compact = false;
      windowOnTop = state.on_top;
      save({ compact: false });
      render();
    }
  });

  // --- Shortcuts ---------------------------------------------------------
  const once = (fn) => (event) => {
    if (!event?.repeat) fn();
  };
  const centerX = () => (canvas.clientWidth || innerWidth) / 2;
  const centerY = () => (canvas.clientHeight || innerHeight) / 2;
  const shortcuts = [
    { keys: ['KeyH'], title: 'Зеркально по горизонтали', handler: once(toggleFlip) },
    { keys: ['KeyV'], title: 'Оттенки серого (тональный этюд)', handler: once(toggleGray) },
    { keys: ['KeyL'], title: 'Сетка: выкл → трети → пропорции (8 голов)', handler: once(cycleGrid) },
    { keys: ['KeyB'], title: 'Фон: студия или изображение', handler: once(togglePanel) },
    { keys: ['KeyP'], title: 'Параллакс: выкл → мягкий → сильный', handler: once(cycleParallax) },
    { keys: ['KeyM'], title: 'Масштаб поз: как в файле ↔ единый размер тела', handler: once(toggleSizing) },
    { keys: ['Equal', 'Shift+Equal', 'NumpadAdd'], title: 'Приблизить', handler: () => zoomBy(KEY_STEP, centerX(), centerY()) },
    { keys: ['Minus', 'NumpadSubtract'], title: 'Отдалить', handler: () => zoomBy(1 / KEY_STEP, centerX(), centerY()) },
    { keys: ['KeyZ'], title: 'Сбросить масштаб и панораму', handler: once(() => resetZoom()) },
    { keys: ['KeyT'], title: 'Поверх всех окон', handler: once(() => toggleOnTop().catch(report)) },
    { keys: ['KeyC'], title: 'Компактное окно (поверх всех)', handler: once(() => toggleCompact().catch(report)) },
    {
      keys: ['KeyW'],
      title: 'Подиум на рабочем столе, без окна',
      handler: once(() => {
        showToast('Подиум на рабочем столе…');
        call('open_desktop').catch(report);
      }),
    },
  ];
  for (const definition of shortcuts) {
    app.shortcuts.register({ ...definition, group: GROUP });
  }
  // Mouse gestures as description-only help rows (core: keys: [] + hint, no handler).
  // An older core rejects such rows with a TypeError: then they are simply skipped.
  const gestures = [
    { hint: 'Колесо мыши', title: 'Масштаб у курсора' },
    { hint: 'Перетаскивание', title: 'Панорама (при масштабе больше 100 %)' },
    { hint: 'Двойной щелчок', title: 'Масштаб 1× ↔ 2,5×' },
  ];
  try {
    for (const gesture of gestures) {
      app.shortcuts.register({ keys: [], ...gesture, group: GROUP });
    }
  } catch {
    // Description-only entries are not supported by this core.
  }

  // --- Events / frame hook -----------------------------------------------
  app.view.onFrame(() => {
    syncZoomState();
    if (!gridFaded) {
      drawGrid();
    }
  });

  app.on('transition', (detail) => {
    if (detail?.phase === 'start') {
      setGridFaded(true);
      if (app.view.zoom() > 1.0001 || zoomTarget !== null) {
        resetZoom(false); // glide back to the full view while the pose dissolves
      }
    } else if (detail?.phase === 'end') {
      gridDirty = true;
      drawGrid();
      setGridFaded(false);
    }
  });

  let shownPose = null;
  app.on('pose', (detail) => {
    const id = `${detail?.index}|${detail?.pose?.url}`;
    if (shownPose !== null && id !== shownPose) {
      resetZoomNow(); // updatePose() of the same image keeps the view
    }
    shownPose = id;
    gridDirty = true;
    drawGrid();
  });

  render();

  // --- 6. Restore saved prefs ----------------------------------------------
  // Fetched as soon as pywebview's API exists (before the first pose is shown) so
  // flip/gray/grid never flash off; app.ready is the fallback.
  let restoring = false;
  function restoreOnce() {
    // pywebview may create window.pywebview.api before its methods are injected.
    if (restoring || typeof currentApi()?.get_view_prefs !== 'function') {
      return;
    }
    restoring = true;
    restore().catch(report);
  }

  async function restore() {
    const [saved, list] = await Promise.all([call('get_view_prefs'), call('list_backgrounds')]);
    const prefs = saved || {};
    if (Array.isArray(list)) {
      backgrounds = list.filter((entry) => entry && BACKGROUND_ID.test(entry.id) && typeof entry.url === 'string');
    }
    if (!touched.has('parallax') && PARALLAX_MODES.includes(prefs.parallax)) state.parallax = prefs.parallax;
    setParallax(state.parallax);
    if (!touched.has('sizing') && (prefs.sizing === 'body' || prefs.sizing === 'file')) state.sizing = prefs.sizing;
    applySizing();
    const bg = prefs.background;
    if (!touched.has('background') && bg && typeof bg === 'object') {
      if (bg.mode === 'studio' || bg.mode === 'image') state.background.mode = bg.mode;
      if (bg.id === null || BACKGROUND_ID.test(bg.id)) state.background.id = bg.id;
      if (Number.isFinite(bg.blur)) state.background.blur = Math.min(BLUR_MAX, Math.max(0, bg.blur));
      if (Number.isFinite(bg.dim)) state.background.dim = Math.min(DIM_MAX, Math.max(0, bg.dim));
      applyBackground();
    }
    if (panelOpen) renderPanel();
    if (!touched.has('flip') && typeof prefs.flip === 'boolean') state.flip = prefs.flip;
    if (!touched.has('gray') && typeof prefs.gray === 'boolean') state.gray = prefs.gray;
    if (!touched.has('grid') && GRID_MODES.includes(prefs.grid)) state.grid = prefs.grid;
    if (!touched.has('on_top') && typeof prefs.on_top === 'boolean') state.on_top = prefs.on_top;
    // The compact window is a working mode for one session: every launch starts full-screen.
    windowOnTop = state.on_top || state.compact;
    render();
    // Re-apply on-top (the window always starts full-screen, not on top).
    if (state.on_top) {
      await setWindowOnTop(true);
    }
    render();
  }

  window.addEventListener('pywebviewready', restoreOnce, { once: true });
  restoreOnce();
  app.ready.then(restoreOnce, () => {});
}
