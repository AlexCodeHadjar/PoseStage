// Artist tools (mirror, grayscale, grid, zoom/pan, on-top, compact) — implemented against docs/API.md §2.
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
const SVG_NS = 'http://www.w3.org/2000/svg';

const clampZoom = (z) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));
const onOff = (on) => (on ? 'вкл' : 'выкл');

export function install(app) {
  const canvas = app.view.canvas;
  const state = { flip: false, gray: false, grid: 'off', on_top: false, compact: false };
  const touched = new Set(); // keys changed by the user before the saved prefs arrived
  let windowOnTop = false; // actual window on-top state (compact forces it on)
  let compactSince = 0; // time of the last set_compact call (resize-detection grace period)

  // --- Errors / API -------------------------------------------------------
  function report(error) {
    const text = `Инструменты художника: ${error?.stack || error?.message || error}`;
    console.error(text);
    try {
      Promise.resolve(app.api?.report_error(text)).catch(() => {});
    } catch {
      // Logging must never throw.
    }
  }

  // Calls an API method; resolves to its result, or undefined when the API (or this
  // method, e.g. in a test double) is missing or the call rejects (rejections are reported).
  async function call(name, ...args) {
    const api = app.api;
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

  function save(partial) {
    if (app.api) {
      call('save_view_prefs', partial);
    }
  }

  // --- Mirror-aware view wrappers (screen CSS px in, layout CSS px out) ------
  const raw = {
    setZoom: app.view.setZoom,
    pan: app.view.pan,
    figureRect: app.view.figureRect,
  };
  const width = () => canvas.clientWidth || innerWidth;
  app.view.setZoom = (zoom, centerX, centerY) => {
    const x = state.flip && Number.isFinite(centerX) ? width() - centerX : centerX;
    raw.setZoom(zoom, x, centerY);
  };
  app.view.pan = (dx, dy) => raw.pan(state.flip ? -dx : dx, dy);
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
      app.view.setZoom(next, zoomAnchor[0], zoomAnchor[1]);
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
    app.view.resetView();
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
    app.view.pan(dx, dy);
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
    { keys: ['Equal', 'Shift+Equal', 'NumpadAdd'], title: 'Приблизить (также колесо мыши)', handler: () => zoomBy(KEY_STEP, centerX(), centerY()) },
    { keys: ['Minus', 'NumpadSubtract'], title: 'Отдалить', handler: () => zoomBy(1 / KEY_STEP, centerX(), centerY()) },
    { keys: ['KeyZ'], title: 'Сбросить масштаб и панораму', handler: once(() => resetZoom()) },
    { keys: ['KeyT'], title: 'Поверх всех окон', handler: once(() => toggleOnTop().catch(report)) },
    { keys: ['KeyC'], title: 'Компактное окно (поверх всех)', handler: once(() => toggleCompact().catch(report)) },
  ];
  for (const definition of shortcuts) {
    app.shortcuts.register({ ...definition, group: GROUP });
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
  (async () => {
    await app.ready;
    if (!app.api) {
      return;
    }
    const prefs = (await call('get_view_prefs')) || {};
    if (!touched.has('flip') && typeof prefs.flip === 'boolean') state.flip = prefs.flip;
    if (!touched.has('gray') && typeof prefs.gray === 'boolean') state.gray = prefs.gray;
    if (!touched.has('grid') && GRID_MODES.includes(prefs.grid)) state.grid = prefs.grid;
    if (!touched.has('on_top') && typeof prefs.on_top === 'boolean') state.on_top = prefs.on_top;
    if (!touched.has('compact') && typeof prefs.compact === 'boolean') state.compact = prefs.compact;
    windowOnTop = state.on_top || state.compact;
    render();
    // Re-apply the window state (the window always starts full-screen, not on top).
    if (state.on_top) {
      await setWindowOnTop(true);
    }
    if (state.compact) {
      await setCompact(true);
    }
    render();
  })().catch(report);
}
