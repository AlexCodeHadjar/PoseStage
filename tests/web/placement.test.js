// PoseStage frontend tests. Serve the project root over HTTP and open
// tests/web/placement.test.html (e.g. python -m http.server; no Node needed).
// ?record=1 prints the measure() values instead of testing.
import * as THREE from 'three';
import { test, run, assert, equal, deepEqual, near, waitFor } from './runner.js';
import { measure, makeCamera } from './measure.js';
import { RECORDED } from './expected.js';
import { FIXTURE_POSES } from './fixtures.js';

import { frameCamera } from '../../web/scene/stage.js';
import { groundMapping, fitPlacement } from '../../web/scene/placement.js';
import { Figure } from '../../web/scene/figure.js';
import { TextureCache } from '../../web/scene/textures.js';
import { stepTarget, normalizeOrder, edgeTarget } from '../../web/core/navigation.js';
import { ViewState } from '../../web/core/view.js';
import { normalizeCombo, keyLabel, createOverlays, createShortcuts } from '../../web/core/input.js';

import * as baselineStage from './baseline/scene/stage.js';
import * as baselinePlacement from './baseline/scene/placement.js';
import * as baselineFigure from './baseline/scene/figure.js';

const current = { frameCamera, groundMapping, fitPlacement, Figure };
const baseline = {
  frameCamera: baselineStage.frameCamera,
  groundMapping: baselinePlacement.groundMapping,
  fitPlacement: baselinePlacement.fitPlacement,
  Figure: baselineFigure.Figure,
};

// --- Placement math ------------------------------------------------------

test('placement matches values recorded from the original code', () => {
  const rows = new Map(measure(current).map((row) => [row.key, row]));
  for (const [key, scale, x, y, radius, shadowSum] of RECORDED) {
    const row = rows.get(key);
    assert(row, `missing ${key}`);
    near(row.scale, scale, 1e-9, `${key} scale`);
    near(row.x, x, 1e-9, `${key} x`);
    near(row.y, y, 1e-9, `${key} y`);
    near(row.maxContactRadius, radius, 1e-9, `${key} maxContactRadius`);
    near(row.shadow[0], shadowSum, 1e-8, `${key} shadow sum`);
  }
});

test('placement and every shadow vertex are bit-identical to the frozen original modules', () => {
  const now = measure(current, { exact: true });
  const before = measure(baseline, { exact: true });
  equal(now.length, before.length, 'row count');
  for (let i = 0; i < now.length; i++) {
    deepEqual(now[i], before[i], now[i].key);
  }
});

test('groundMapping ignores the camera view offset (zoom/pan)', () => {
  const plain = makeCamera(frameCamera, 1920, 1080).camera;
  const zoomed = makeCamera(frameCamera, 1920, 1080).camera;
  zoomed.setViewOffset(1920, 1080, 700, 300, 1920 / 4, 1080 / 4);
  const zoomedProjection = zoomed.projectionMatrix.elements.slice();
  const a = groundMapping(plain);
  const b = groundMapping(zoomed);
  deepEqual([b.centerX, b.centerY, b.diameter], [a.centerX, a.centerY, a.diameter], 'mapping');
  deepEqual(Array.from(b.inverse), Array.from(a.inverse), 'inverse');
  deepEqual(zoomed.projectionMatrix.elements, zoomedProjection, 'view offset restored');
  assert(zoomed.view.enabled, 'view stays enabled');
});

test('Figure.fit gives the same placement with a zoomed camera', () => {
  const pose = FIXTURE_POSES[1];
  const plain = makeCamera(frameCamera, 1280, 720, [0.03, 0.001]).camera;
  const zoomed = makeCamera(frameCamera, 1280, 720, [0.03, 0.001]).camera;
  zoomed.setViewOffset(1280, 720, 100, 50, 320, 180);
  const scene = new THREE.Scene();
  const a = new Figure(pose, new THREE.Texture(), scene, scene);
  const b = new Figure(pose, new THREE.Texture(), scene, scene);
  a.fit(plain);
  b.fit(zoomed);
  equal(b.pixelScale, a.pixelScale, 'pixelScale');
  deepEqual(b.group.position.toArray(), a.group.position.toArray(), 'position');
  deepEqual(Array.from(b.shadowGeometry.attributes.position.array), Array.from(a.shadowGeometry.attributes.position.array), 'shadow');
  const rect = a.layoutRect();
  const [x1, y1, x2, y2] = pose.box;
  near(rect.width, (x2 - x1) * a.pixelScale, 1e-12, 'rect width');
  near(rect.left + rect.width / 2, 640 + a.group.position.x, 1e-9, 'rect center x');
  near(rect.top + rect.height / 2, 360 - a.group.position.y, 1e-9, 'rect center y');
});

test('Figure.setPose refreshes the cached placement (support mode change)', () => {
  const camera = makeCamera(frameCamera, 1920, 1080).camera;
  const pose = FIXTURE_POSES[1];
  const scene = new THREE.Scene();
  const figure = new Figure(pose, new THREE.Texture(), scene, scene);
  figure.fit(camera);
  const before = figure.placement.maxContactRadius;
  const changed = { ...pose, support: { ...pose.support, mode: 'ground', cut: 0.55 } };
  figure.setPose(changed);
  figure.fit(camera);
  assert(figure.placement.maxContactRadius !== before, 'placement recomputed');
  near(figure.placement.maxContactRadius, fitPlacement(changed, groundMapping(camera)).maxContactRadius, 1e-12, 'radius');
});

// --- Pure logic ------------------------------------------------------------

test('stepTarget: plain traversal wraps and accumulates', () => {
  equal(stepTarget(0, 1, 5), 1);
  equal(stepTarget(4, 1, 5), 0);
  equal(stepTarget(0, -1, 5), 4);
  equal(stepTarget(0, 13, 12), 1);
  equal(stepTarget(3, 0, 12), 3);
  equal(stepTarget(0, 1, 0), 0);
  // 10 single steps == one step of 10.
  let index = 0;
  for (let i = 0; i < 10; i++) index = stepTarget(index, 1, 12);
  equal(index, stepTarget(0, 10, 12));
});

test('stepTarget: setOrder subset traversal', () => {
  const order = [2, 5, 7];
  equal(stepTarget(5, 1, 12, order), 7);
  equal(stepTarget(7, 1, 12, order), 2, 'wraps');
  equal(stepTarget(2, -1, 12, order), 7, 'wraps back');
  equal(stepTarget(2, 2, 12, order), 7, 'accumulated');
  equal(stepTarget(3, 1, 12, order), 5, 'not in subset -> nearest following');
  equal(stepTarget(3, -1, 12, order), 2, 'not in subset -> nearest preceding');
  equal(stepTarget(8, 1, 12, order), 2, 'nearest following wraps');
  equal(stepTarget(0, -1, 12, order), 7, 'nearest preceding wraps');
  equal(stepTarget(3, 2, 12, order), 7, 'first step lands on 5, second on 7');
  equal(stepTarget(3, 1, 12, []), 3, 'empty subset stays');
  const unsorted = [9, 1, 4];
  equal(stepTarget(1, 1, 12, unsorted), 4, 'custom order is followed');
  equal(stepTarget(4, 1, 12, unsorted), 9);
});

test('normalizeOrder / edgeTarget', () => {
  equal(normalizeOrder(null, 5), null);
  deepEqual(normalizeOrder([3, 3, -1, 9, 1.5, 0, '2'], 5), [3, 0]);
  equal(edgeTarget(-1, 12), 0);
  equal(edgeTarget(1, 12), 11);
  equal(edgeTarget(-1, 12, [4, 2, 9]), 4);
  equal(edgeTarget(1, 12, [4, 2, 9]), 9);
  equal(edgeTarget(1, 12, []), -1);
  equal(edgeTarget(1, 0), -1);
});

test('ViewState: zoom about a point, clamp, toScreen', () => {
  const view = new ViewState(1000, 800);
  view.setZoom(2, 250, 200);
  equal(view.zoom, 2);
  // The layout point under (250, 200) stays there.
  near(view.x + 250 / 2, 250, 1e-12);
  near(view.y + 200 / 2, 200, 1e-12);
  view.setZoom(100);
  equal(view.zoom, 8, 'max zoom');
  view.pan(1e6, 1e6);
  deepEqual([view.x, view.y], [0, 0], 'pan clamps to the top-left edge');
  view.pan(-1e6, -1e6);
  deepEqual([view.x, view.y], [1000 - 1000 / 8, 800 - 800 / 8], 'pan clamps to the bottom-right edge');
  const rect = view.toScreen({ left: view.x + 10, top: view.y + 20, width: 5, height: 6 });
  deepEqual(rect, { left: 80, top: 160, width: 40, height: 48 });
  view.setZoom(0.2);
  equal(view.zoom, 1, 'min zoom');
  deepEqual([view.x, view.y], [0, 0], 'zoom 1 shows everything');
  view.setZoom(4, 1000, 800);
  view.resize(500, 400);
  near(view.x, 375, 1e-12, 'resize keeps the relative region');
  equal(view.reset(), true);
  equal(view.setZoom(NaN), false);
  equal(view.zoom, 1);
});

test('shortcuts: combos, labels, dispatch, typing guard', () => {
  equal(normalizeCombo('shift+Slash'), 'Shift+Slash');
  equal(normalizeCombo('Slash+Shift'), 'Shift+Slash');
  equal(keyLabel('Shift+Slash'), '?');
  equal(keyLabel('KeyH'), 'H');
  equal(keyLabel('Digit1'), '1');
  equal(keyLabel('ArrowLeft'), '←');
  const calls = [];
  const shortcuts = createShortcuts((error) => calls.push(`error ${error}`));
  const off = shortcuts.register({ keys: ['KeyH'], title: 'A', group: 'G', handler: () => calls.push('a') });
  shortcuts.register({ keys: ['KeyH'], title: 'B', group: 'G', handler: () => calls.push('b') });
  const key = (code, extra = {}) => new KeyboardEvent('keydown', { code, cancelable: true, ...extra });
  const event = key('KeyH');
  equal(shortcuts.dispatch(event), true);
  assert(event.defaultPrevented, 'preventDefault');
  equal(shortcuts.dispatch(key('KeyH', { shiftKey: true })), false, 'modifiers must match');
  deepEqual(calls, ['b'], 'latest registration wins');
  const input = document.createElement('input');
  document.body.append(input);
  input.focus();
  equal(shortcuts.dispatch(key('KeyH')), false, 'ignored while typing');
  input.remove();
  deepEqual(shortcuts.list(), [{ keys: ['KeyH'], title: 'A', group: 'G' }, { keys: ['KeyH'], title: 'B', group: 'G' }]);
  off();
  equal(shortcuts.list().length, 1);
});

test('shortcuts: core options anyShift / eventKeys / enabled', () => {
  const calls = [];
  const shortcuts = createShortcuts((error) => calls.push(`error ${error}`));
  let open = false;
  shortcuts.register({ keys: ['ArrowRight'], title: 'next', group: 'G', anyShift: true, enabled: () => !open, handler: () => calls.push('next') });
  shortcuts.register({ keys: ['Shift+Slash'], eventKeys: ['?'], title: 'help', group: 'G', handler: () => calls.push('help') });
  shortcuts.register({ keys: ['Shift+ArrowLeft'], title: 'exact', group: 'G', handler: () => calls.push('exact') });
  shortcuts.register({ keys: ['ArrowLeft'], title: 'prev', group: 'G', anyShift: true, handler: () => calls.push('prev') });
  const key = (code, extra = {}) => new KeyboardEvent('keydown', { code, cancelable: true, ...extra });
  equal(shortcuts.dispatch(key('ArrowRight', { shiftKey: true })), true, 'Shift tolerated');
  equal(shortcuts.dispatch(key('ArrowRight', { ctrlKey: true })), false, 'Ctrl not tolerated');
  equal(shortcuts.dispatch(key('Digit7', { key: '?', shiftKey: true })), true, 'ЙЦУКЕН "?"');
  equal(shortcuts.dispatch(key('ArrowLeft', { shiftKey: true })), true);
  open = true;
  const blocked = key('ArrowRight');
  equal(shortcuts.dispatch(blocked), false, 'disabled while an overlay is open');
  equal(blocked.defaultPrevented, false, 'event left for the overlay');
  deepEqual(calls, ['next', 'help', 'exact'], 'exact match beats the Shift fallback');
  deepEqual(shortcuts.list()[0], { keys: ['ArrowRight'], title: 'next', group: 'G' }, 'list() hides core options');
});

test('shortcuts: description-only rows (mouse gestures)', () => {
  const shortcuts = createShortcuts(() => {});
  shortcuts.register({ keys: ['KeyZ'], title: 'Сброс вида', group: 'Вид', handler: () => {} });
  const off = shortcuts.register({ keys: [], hint: 'Колесо', title: 'Масштаб', group: 'Вид' });
  deepEqual(shortcuts.list(), [
    { keys: ['KeyZ'], title: 'Сброс вида', group: 'Вид' },
    { keys: [], title: 'Масштаб', group: 'Вид', hint: 'Колесо' },
  ]);
  equal(shortcuts.dispatch(new KeyboardEvent('keydown', { code: '', key: '', cancelable: true })), false, 'never matches');
  let threw = false;
  try {
    shortcuts.register({ keys: [], title: 'no hint, no handler' });
  } catch {
    threw = true;
  }
  assert(threw, 'keys: [] without hint still needs a handler');
  off();
  equal(shortcuts.list().length, 1);
});

test('TextureCache.preload: neighbours in order, also from outside the subset', () => {
  const poses = Array.from({ length: 12 }, (_, i) => ({ url: `u${i}`, file: `f${i}` }));
  const cache = new TextureCache(poses, { capabilities: { getMaxAnisotropy: () => 1 } }, () => {});
  const requested = [];
  cache.get = (i) => {
    requested.push(i);
    return Promise.resolve();
  };
  cache.preload(0);
  deepEqual(requested, [0, 1, 11, 2, 10], 'all poses: ±1, ±2 (as before)');
  requested.length = 0;
  cache.preload(5, [2, 5, 7, 9]);
  deepEqual(requested, [5, 7, 2, 9], 'subset neighbours');
  requested.length = 0;
  cache.preload(3, [2, 5, 7]);
  deepEqual([...new Set(requested)].sort((a, b) => a - b), [2, 3, 5, 7], 'outside: nearest members both ways + their neighbours');
});

test('overlays: Esc closes the top one first', () => {
  const closed = [];
  const overlays = createOverlays(() => {});
  overlays.open('a', () => closed.push('a'));
  overlays.open('b', () => closed.push('b'));
  equal(overlays.any(), true);
  equal(overlays.closeTop(), true);
  deepEqual(closed, ['b']);
  overlays.close('a');
  equal(overlays.any(), false);
  equal(overlays.closeTop(), false);
});

test('TextureCache.dispose releases textures and late loads', async () => {
  const disposed = [];
  const fakeTexture = (name) => ({ dispose: () => disposed.push(name) });
  const cache = new TextureCache([{ url: 'a' }, { url: 'b' }], { capabilities: { getMaxAnisotropy: () => 1 } }, () => {});
  cache.entries.set(0, { texture: fakeTexture('t0'), last: 1, promise: Promise.resolve() });
  cache.entries.set(1, { texture: null, last: 2, promise: Promise.resolve() });
  cache.dispose();
  deepEqual(disposed, ['t0']);
  equal(cache.entries.size, 0);
  let rejected = false;
  await cache.get(0).catch(() => {
    rejected = true;
  });
  assert(rejected, 'get() after dispose rejects');
});

// --- Integration: the real web/index.html with a fake pywebview API ----------

function poseImage(pose) {
  const canvas = document.createElement('canvas');
  canvas.width = 64;
  canvas.height = 64;
  const context = canvas.getContext('2d');
  const [x1, y1, x2, y2] = pose.box;
  context.fillStyle = '#c33';
  context.fillRect((x1 / pose.w) * 64, (y1 / pose.h) * 64, ((x2 - x1) / pose.w) * 64, ((y2 - y1) / pose.h) * 64);
  return canvas.toDataURL('image/png');
}

function makePoses(count, prefix = 'fake') {
  return Array.from({ length: count }, (_, i) => {
    const pose = structuredClone(FIXTURE_POSES[i % FIXTURE_POSES.length]);
    pose.file = `${prefix}-${String(i).padStart(2, '0')}.png`;
    pose.url = poseImage(pose);
    return pose;
  });
}

const harness = { calls: [], errors: [], frame: null, app: null, win: null };

function settled() {
  const s = harness.win.__poseStage;
  return !s.transition && !s.loading && s.queued === null;
}

function key(code, extra = {}) {
  harness.win.dispatchEvent(new harness.win.KeyboardEvent('keydown', { code, bubbles: true, cancelable: true, ...extra }));
}

test('integration: app boots with a fake API and shows the first pose', async () => {
  const poses = makePoses(12);
  const api = {
    get_poses: async () => poses,
    get_state: async () => ({ index: 0, file: poses[0].file }),
    save_state: async (index, file) => harness.calls.push(['save_state', index, file]),
    // Library / prefs / window methods used by the UI modules.
    get_library: async () => ({ favorites: [], tags: {} }),
    toggle_favorite: async () => true,
    set_tags: async (file, tags) => tags,
    set_support_mode: async (file) => poses.find((p) => p.file === file),
    get_view_prefs: async () => ({}),
    save_view_prefs: async (prefs) => prefs,
    get_collections: async () => [{ id: 'c1', name: 'fake', folder: 'C:/fake', active: true }],
    set_on_top: async (on) => on,
    set_compact: async (on) => on,
    report_error: async (message) => harness.errors.push(message),
    quit: async () => harness.calls.push(['quit']),
    toggle_fullscreen: async () => harness.calls.push(['toggle_fullscreen']),
    choose_folder: async () => [],
  };
  const frame = document.createElement('iframe');
  frame.src = '../../web/index.html';
  document.body.append(frame);
  await new Promise((resolve) => frame.addEventListener('load', resolve, { once: true }));
  const win = frame.contentWindow;
  win.pywebview = { api };
  win.dispatchEvent(new win.Event('pywebviewready'));
  await waitFor(() => win.__poseStageApp, 5000, 'app');
  Object.assign(harness, { frame, win, app: win.__poseStageApp });
  await harness.app.ready;
  equal(harness.app.index(), 0);
  equal(harness.app.current().file, 'fake-00.png');
  equal(harness.app.poses().length, 12);
  assert(harness.app.poses() !== harness.app.poses(), 'poses() returns a copy');
  equal(win.__poseStage.error, null);
  assert(win.__poseStage.pixelScale > 0, 'figure placed');
  const save = harness.calls.find((c) => c[0] === 'save_state');
  deepEqual(save, ['save_state', 0, 'fake-00.png'], 'save_state(index, file)');
});

test('integration: 10 rapid steps accumulate into at most 2 transitions', async () => {
  const { app } = harness;
  const starts = [];
  const shown = [];
  const offT = app.on('transition', (d) => d.phase === 'start' && starts.push(d.to));
  const offP = app.on('pose', (d) => shown.push(d.index));
  for (let i = 0; i < 10; i++) app.step(1);
  await waitFor(() => app.index() === 10 && settled(), 5000, 'index 10');
  offT();
  offP();
  assert(starts.length <= 2, `transitions: ${starts}`);
  equal(starts[starts.length - 1], 10, 'last transition goes straight to the target');
  equal(shown[shown.length - 1], 10, "'pose' emitted for the final pose");
});

test('integration: a multi-step jump uses ONE transition; goTo; Home/End', async () => {
  const { app } = harness;
  const starts = [];
  const off = app.on('transition', (d) => d.phase === 'start' && starts.push(d.to));
  app.step(3);
  await waitFor(() => app.index() === 1 && settled(), 5000, 'index 1');
  deepEqual(starts, [1], 'step(3) from 10 wraps to 1 in one transition');
  app.goTo(6);
  await waitFor(() => app.index() === 6 && settled(), 5000, 'goTo 6');
  key('End');
  await waitFor(() => app.index() === 11 && settled(), 5000, 'End');
  key('Home');
  await waitFor(() => app.index() === 0 && settled(), 5000, 'Home');
  key('ArrowLeft');
  await waitFor(() => app.index() === 11 && settled(), 5000, 'ArrowLeft wraps');
  key('KeyD');
  await waitFor(() => app.index() === 0 && settled(), 5000, 'KeyD');
  off();
  equal(starts.length, 6, 'one transition per request');
});

test('integration: setOrder restricts ←/→ and Home/End', async () => {
  const { app } = harness;
  app.goTo(3);
  await waitFor(() => app.index() === 3 && settled(), 5000, 'goTo 3');
  app.setOrder([2, 5, 7]);
  app.step(1);
  await waitFor(() => app.index() === 5 && settled(), 5000, 'nearest following');
  key('ArrowRight');
  await waitFor(() => app.index() === 7 && settled(), 5000, '7');
  app.step(1);
  await waitFor(() => app.index() === 2 && settled(), 5000, 'wrap to 2');
  app.step(1);
  app.step(1);
  await waitFor(() => app.index() === 7 && settled(), 5000, 'accumulated within subset');
  key('Home');
  await waitFor(() => app.index() === 2 && settled(), 5000, 'Home = first of subset');
  app.setOrder(null);
  app.step(1);
  await waitFor(() => app.index() === 3 && settled(), 5000, 'all poses again');
});

test('integration: updatePose re-places the current figure live', async () => {
  const { app, win } = harness;
  app.goTo(1);
  await waitFor(() => app.index() === 1 && settled(), 5000, 'goTo 1');
  const before = win.__poseStage.maxContactRadius;
  const events = [];
  const off = app.on('pose', (d) => events.push(d));
  const pose = app.current();
  app.updatePose(1, { ...pose, support: { ...pose.support, mode: 'ground', cut: 0.55 } });
  off();
  equal(win.__poseStage.supportMode, 'ground');
  equal(app.current().support.mode, 'ground');
  assert(win.__poseStage.maxContactRadius !== before, 'placement refreshed');
  equal(events.length, 1, "'pose' emitted");
  app.updatePose(4, { ...app.poses()[4], support: { ...app.poses()[4].support, mode: 'kneel' } });
  equal(app.poses()[4].support.mode, 'kneel', 'non-current pose replaced');
});

test('integration: view zoom/pan keeps placement, figureRect follows', async () => {
  const { app, win } = harness;
  // A pending window resize (e.g. the browser pane opening) must reach the app first,
  // otherwise innerWidth and the app's viewport disagree for a moment.
  await waitFor(() => win.__poseStage.canvas[0] === Math.floor(win.innerWidth * Math.min(win.devicePixelRatio, 2)),
    5000, 'viewport settled');
  const scale = win.__poseStage.pixelScale;
  const r1 = app.view.figureRect();
  assert(r1 && r1.width > 0, 'figureRect');
  app.view.setZoom(2);
  equal(app.view.zoom(), 2);
  const r2 = app.view.figureRect();
  // Tolerances: still frames may move the parallax camera by a hair meanwhile.
  near(r2.width, r1.width * 2, 1e-3, 'width doubles');
  near(win.__poseStage.pixelScale, scale, 1e-3, 'placement not re-fitted');
  const w = win.innerWidth;
  const h = win.innerHeight;
  // figureRect follows the actual view offset (robust to whatever center was used).
  const [ox, oy] = win.__poseStage.viewOffset;
  // setZoom without a center zooms about the viewport center.
  near(ox, w / 4, 1e-6, 'centered offset x');
  near(oy, h / 4, 1e-6, 'centered offset y');
  near(r2.left, (r1.left - ox) * 2, 1e-2, 'zoomed left');
  near(r2.top, (r1.top - oy) * 2, 1e-2, 'zoomed top');
  app.view.pan(1e6, 1e6);
  deepEqual(win.__poseStage.viewOffset, [0, 0], 'pan clamped');
  app.view.pan(-1e6, -1e6);
  deepEqual(win.__poseStage.viewOffset, [w / 2, h / 2], 'pan clamped at the far edge');
  app.view.setZoom(50);
  equal(app.view.zoom(), 8);
  app.view.resetView();
  equal(app.view.zoom(), 1);
  const r3 = app.view.figureRect();
  near(r3.left, r1.left, 1e-2, 'reset restores left');
  near(r3.width, r1.width, 1e-2, 'reset restores width');
  let frames = 0;
  const off = app.view.onFrame(() => frames++);
  equal(typeof off, 'function');
  off();
  assert(app.view.canvas instanceof win.HTMLCanvasElement, 'canvas');
});

test('integration: shortcuts registry, help overlay, Esc order', async () => {
  const { app, win } = harness;
  const hits = [];
  const off = app.shortcuts.register({ keys: ['KeyQ'], title: 'Тест', group: 'Тесты', handler: () => hits.push('q') });
  key('KeyQ');
  deepEqual(hits, ['q']);
  const input = win.document.createElement('input');
  win.document.body.append(input);
  input.focus();
  input.dispatchEvent(new win.KeyboardEvent('keydown', { code: 'KeyQ', bubbles: true, cancelable: true }));
  input.dispatchEvent(new win.KeyboardEvent('keydown', { code: 'ArrowRight', bubbles: true, cancelable: true }));
  input.remove();
  win.dispatchEvent(new win.Event('blur')); // keep the iframe loop paused (background tab)
  deepEqual(hits, ['q'], 'ignored in input');
  assert(settled() && win.__poseStage.queued === null, 'arrow ignored in input');
  const groups = new Set(app.shortcuts.list().map((s) => s.group));
  assert(groups.has('Навигация') && groups.has('Тесты'), 'groups listed');
  off();

  key('Slash', { shiftKey: true });
  equal(app.overlays.any(), true, 'help open');
  const help = win.document.querySelector('#help');
  assert(!help.hidden && help.textContent.includes('Следующая поза'), 'help lists shortcuts');
  key('Escape');
  equal(app.overlays.any(), false, 'Esc closed help');
  assert(help.hidden, 'help hidden');
  assert(!harness.calls.some((c) => c[0] === 'quit'), 'no quit while an overlay was open');
  key('F1');
  equal(app.overlays.any(), true, 'F1 opens help');
  key('F1');
  equal(app.overlays.any(), false, 'F1 toggles help');

  let closed = 0;
  app.overlays.open('custom', () => closed++);
  key('Escape');
  equal(closed, 1, 'custom overlay closed by Esc');
  key('Escape');
  assert(harness.calls.some((c) => c[0] === 'quit'), 'Esc without overlays quits');
  key('KeyF');
  key('KeyF', { repeat: true });
  equal(harness.calls.filter((c) => c[0] === 'toggle_fullscreen').length, 1, 'F toggles once (repeat ignored)');
});

test('integration: reload switches collections; empty list shows the error panel', async () => {
  const { app, win } = harness;
  const seen = [];
  const off = app.on('poses', (d) => seen.push(d));
  await app.reload(makePoses(3, 'other'), 1);
  equal(app.index(), 1);
  equal(app.poses().length, 3);
  equal(app.current().file, 'other-01.png');
  equal(seen.length, 1);
  equal(seen[0].poses.length, 3);
  assert(win.__poseStage.textureCount <= 7, 'texture cache bounded');

  await app.reload([]);
  equal(win.__poseStage.error, 'Позы не найдены');
  assert(!win.document.querySelector('#choose').hidden, '«Выбрать папку…» visible');
  equal(app.current(), null);
  app.step(1); // ignored without poses

  await app.reload(makePoses(5, 'again'));
  equal(win.__poseStage.error, null, 'error cleared');
  equal(app.index(), 0, 'index from get_state');
  app.step(2);
  await waitFor(() => app.index() === 2 && settled(), 5000, 'navigation after reload');
  off();
});

test('integration: Shift+arrow navigates; arrows go to an open overlay; "?" on any layout', async () => {
  const { app } = harness;
  const start = app.index();
  key('ArrowRight', { shiftKey: true });
  await waitFor(() => app.index() === (start + 1) % app.poses().length && settled(), 5000, 'Shift+ArrowRight');
  app.overlays.open('gallery-test', () => {});
  key('ArrowRight');
  key('End');
  equal(harness.win.__poseStage.queued, null, 'no navigation queued');
  assert(settled() && app.index() === (start + 1) % app.poses().length, 'pose unchanged while an overlay is open');
  app.overlays.close('gallery-test');
  const offHint = app.shortcuts.register({ keys: [], hint: 'Колесо', title: 'Масштаб (тест)', group: 'Тесты' });
  harness.win.document.body.classList.add('idle');
  key('Digit7', { key: '?', shiftKey: true });
  equal(app.overlays.any(), true, 'help via key "?"');
  assert(!harness.win.document.body.classList.contains('idle'), 'a handled shortcut wakes the idle chrome');
  const gesture = [...harness.win.document.querySelectorAll('#help kbd.help-gesture')].find((k) => k.textContent === 'Колесо');
  assert(gesture && gesture.closest('dt').nextElementSibling.textContent === 'Масштаб (тест)', 'gesture row in help');
  offHint();
  key('Escape');
  equal(app.overlays.any(), false);
});

test('integration: updatePose with a new bbox during a transition leaves no orphan figure', async () => {
  const { app, win } = harness;
  await waitFor(settled, 5000, 'settled');
  win.dispatchEvent(new win.Event('focus')); // run the real (animated) loop
  const from = app.index();
  app.step(1);
  await waitFor(() => win.__poseStage.transition, 5000, 'transition running');
  const pose = app.current();
  const [x1, y1, x2, y2] = pose.box;
  app.updatePose(from, { ...pose, box: [x1 + 4, y1 + 4, x2 - 4, y2 - 4] });
  equal(win.__poseStage.figures, 2, 'old + next while transitioning');
  win.dispatchEvent(new win.Event('blur')); // finish instantly
  await waitFor(settled, 5000, 'settled');
  equal(win.__poseStage.figures, 1, 'exactly one figure after the transition');
  // A new image URL for the shown pose swaps the texture without an orphan.
  const shown = app.current();
  app.updatePose(app.index(), { ...shown, url: poseImage({ ...shown, box: [0, 0, shown.w, shown.h] }) });
  await waitFor(() => harness.win.__poseStage.figures === 1 && app.current().url !== shown.url, 5000, 'swap');
});

test('integration: no errors were reported', () => {
  deepEqual(harness.errors, []);
});

if (new URLSearchParams(location.search).has('record')) {
  document.querySelector('#log').textContent = JSON.stringify(measure(current));
  document.querySelector('#summary').textContent = 'RECORDED';
} else {
  run();
}
