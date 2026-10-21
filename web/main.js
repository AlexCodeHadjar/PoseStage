// PoseStage frontend core: renderer + loop, pose transitions, input, and the App
// controller handed to UI modules (contract: docs/API.md, section 2).
import * as THREE from 'three';
import { createStage, frameCamera, orbitCamera } from './scene/stage.js';
import { createPost } from './scene/post.js';
import { Figure } from './scene/figure.js';
import { TextureCache } from './scene/textures.js';
import { createEmitter } from './core/events.js';
import { normalizeOrder, stepTarget, edgeTarget } from './core/navigation.js';
import { ViewState } from './core/view.js';
import { createShortcuts, createOverlays } from './core/input.js';
import { createHelp } from './core/help.js';
import { createChrome } from './core/chrome.js';

const TRANSITION_SECONDS = 0.66;
const FAST_SPEED = 8; // transition speed-up while more steps are queued
const UI_MODULES = [
  ['artist', './ui/artist.js'],
  ['navigator', './ui/navigator.js'],
];

const canvas = document.querySelector('#stage');
const errorPanel = document.querySelector('#error');
const errorMessage = document.querySelector('#message');
const chooseButton = document.querySelector('#choose');

// --- Scene ---------------------------------------------------------------
let api = null;
let renderer = null;
let studio = null;
let post = null;
let camera = null; // LAYOUT camera: parallax motion, never zoomed; placement uses it
let viewCamera = null; // render camera = layout camera + zoom/pan view offset
let figureCamera = null; // orthographic camera of the screen-space drawing overlay
let overlay = null; // scene holding the visible drawings
let framing = null;
let cache = null;

// --- Pose state ----------------------------------------------------------
let poses = [];
let current = null; // Figure on screen (the erased one during a transition)
let index = 0;
let order = null; // ←/→ traversal subset (setOrder), null = all
let transition = null;
let loading = false; // a texture for the next transition is loading
let heading = null; // index being loaded for the next transition
let queued = null; // accumulated target that has not started yet
let pendingSteps = 0; // signed step sum behind `queued` (direction + diagnostics)
let generation = 0; // bumped by reload(); stale async work compares it
let angle = 0; // podium rotation

// --- Loop / diagnostics --------------------------------------------------
let active = document.hasFocus();
let frameId = 0;
let stillFrameId = 0;
let lastFrame = 0;
let mouse = { x: 0, y: 0, inside: false }; // last pointer position (−1…1), kept while inside the window
let frameMs = 0; // smoothed CPU time of renderFrame (diagnostics)
let frameCount = 0;
let sampleStart = 0;
let fps = 60;
let quality = 0;
let firstPoseMs = null;
let renderedFrames = 0;
const started = performance.now();
const lastShadowCamera = new THREE.Vector3(Infinity, Infinity, Infinity);
const view = new ViewState(innerWidth, innerHeight);
const frameCallbacks = new Set();
const queuedReports = [];

const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));

// --- Parallax ------------------------------------------------------------
// The camera orbits the podium following the LAST pointer position (no snap-back while the
// pointer rests); it eases back to the centre only when the pointer leaves the window or the
// window loses focus. yaw/pitch: radians at the window edge; soft ≈ the original ±4° sway.
const PARALLAX_MODES = {
  off: { yaw: 0, pitch: 0 },
  soft: { yaw: THREE.MathUtils.degToRad(4), pitch: THREE.MathUtils.degToRad(0.35) },
  strong: { yaw: THREE.MathUtils.degToRad(9.5), pitch: THREE.MathUtils.degToRad(3.5) },
};
const PARALLAX_SMOOTH_TIME = 0.32; // s
let parallaxMode = 'soft';
const sway = { yaw: 0, pitch: 0, yawSpeed: 0, pitchSpeed: 0 };
const backdropFit = { yaw: -1, pitch: -1 }; // angles the background plane currently covers

// The background picture must cover every view the camera can take: the current mode's
// extremes and wherever the camera still is while easing from a stronger mode. Growing is
// immediate; shrinking (a visible re-crop) waits until the camera has settled.
function fitBackdrop(force = false) {
  if (!studio || !framing) return;
  const amplitude = PARALLAX_MODES[parallaxMode];
  const yaw = Math.max(amplitude.yaw, Math.abs(sway.yaw)) + 0.004;
  const pitch = Math.max(amplitude.pitch, Math.abs(sway.pitch)) + 0.002;
  const grow = yaw > backdropFit.yaw || pitch > backdropFit.pitch;
  const settled = Math.abs(sway.yawSpeed) + Math.abs(sway.pitchSpeed) < 1e-4;
  const shrink = settled && (yaw < backdropFit.yaw - 0.002 || pitch < backdropFit.pitch - 0.002);
  if (force || grow || shrink) {
    backdropFit.yaw = yaw;
    backdropFit.pitch = pitch;
    studio.backdrop.fit(camera, framing, yaw, pitch);
  }
}

// Critically damped spring towards `target` (no overshoot); returns [value, velocity].
function smoothDamp(value, velocity, target, smoothTime, dt) {
  const omega = 2 / smoothTime;
  const x = omega * dt;
  const decay = 1 / (1 + x + 0.48 * x * x + 0.235 * x * x * x);
  const change = value - target;
  const temp = (velocity + omega * change) * dt;
  return [target + (change + temp) * decay, (velocity - omega * temp) * decay];
}
// Cubic ease-in-out.
const ease = (x) => {
  x = clamp(x);
  return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
};

function sendReport(text) {
  try {
    Promise.resolve(api.report_error(text)).catch(() => {});
  } catch {
    // Logging must never throw.
  }
}

// Errors go to the console and the PoseStage log (queued until the API is ready).
function report(message) {
  console.error(message);
  if (api) {
    sendReport(String(message));
  } else {
    queuedReports.push(String(message));
  }
}

function saveState() {
  try {
    // The file name pins the pose even if the save lands after a collection switch.
    Promise.resolve(api?.save_state(index, poses[index]?.file ?? null)).catch(report);
  } catch (error) {
    report(error);
  }
}

const events = createEmitter(['pose', 'poses', 'transition'], report);
const shortcuts = createShortcuts(report);
const overlays = createOverlays(report);
const help = createHelp(shortcuts, overlays);
const chrome = createChrome();
let resolveReady;
const ready = new Promise((resolve) => {
  resolveReady = resolve;
});

// --- Error panel ---------------------------------------------------------
function showError(message, choose = false) {
  errorMessage.textContent = message;
  chooseButton.hidden = !choose;
  errorPanel.hidden = false;
  document.body.classList.add('error');
  resolveReady();
}

function clearError() {
  errorPanel.hidden = true;
  document.body.classList.remove('error');
}

// --- Viewport ------------------------------------------------------------
function syncViewCamera() {
  viewCamera.position.copy(camera.position);
  viewCamera.quaternion.copy(camera.quaternion);
  viewCamera.updateMatrixWorld();
}

// Same zoom/pan sub-rectangle on the studio and the drawing cameras.
function applyView() {
  if (!viewCamera) {
    return;
  }
  viewCamera.aspect = camera.aspect;
  view.apply(viewCamera);
  view.apply(figureCamera);
  syncViewCamera();
}

function resize() {
  if (!renderer) {
    return;
  }
  const w = innerWidth;
  const h = innerHeight;
  // Keep the final figure at native DPI even when the studio adapts its resolution.
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setSize(w, h, false);
  const studioRatio = Math.min(renderer.getPixelRatio(), Math.sqrt((1920 * 1080) / (w * h))) * Math.pow(0.8, quality);
  post.composer.setPixelRatio(studioRatio);
  post.composer.setSize(w, h);
  framing = frameCamera(camera, w, h);
  fitBackdrop(true);
  figureCamera.left = -w / 2;
  figureCamera.right = w / 2;
  figureCamera.top = h / 2;
  figureCamera.bottom = -h / 2;
  figureCamera.updateProjectionMatrix();
  view.resize(w, h);
  applyView();
  current?.fit(camera);
  transition?.next.fit(camera);
  renderer.shadowMap.needsUpdate = true;
  chrome.updateWindowed();
  invalidate();
}

// --- Navigation ----------------------------------------------------------
// Where the next relative step starts from: the furthest already-requested target.
function baseTarget() {
  if (queued !== null) return queued;
  if (transition) return transition.target;
  if (heading !== null) return heading;
  return index;
}

function requestStep(delta) {
  chrome.wake();
  const steps = Math.trunc(Number(delta));
  if (!steps || !current || !poses.length) {
    return;
  }
  // Rapid presses accumulate into ONE target (one transition, not N).
  pendingSteps += steps;
  enqueue(stepTarget(baseTarget(), steps, poses.length, order));
}

function requestGoTo(target) {
  chrome.wake();
  if (!Number.isInteger(target) || target < 0 || target >= poses.length || !current) {
    return;
  }
  pendingSteps = 0;
  enqueue(target);
}

function enqueue(target) {
  queued = target;
  if (transition) {
    transition.speed = FAST_SPEED;
    return;
  }
  if (!loading) {
    startQueued();
  }
}

function startQueued() {
  if (queued === null) {
    return;
  }
  const target = queued;
  const direction = Math.sign(pendingSteps) || Math.sign(target - index) || 1;
  queued = null;
  pendingSteps = 0;
  if (target !== index) {
    begin(target, direction).catch(report);
  }
}

async function begin(target, direction) {
  if (!current || !poses.length) {
    return;
  }
  const gen = generation;
  loading = true;
  let texture = null;
  // A broken image is skipped: try the next pose in the same direction.
  const tried = new Set();
  while (target !== index && !tried.has(target)) {
    tried.add(target);
    heading = target;
    cache.pins = new Set([index, target]);
    try {
      texture = await cache.get(target);
      break;
    } catch (error) {
      if (gen !== generation) return;
      report(error);
      target = stepTarget(target, direction, poses.length, order);
    }
  }
  if (gen !== generation) {
    return; // reload() replaced everything meanwhile
  }
  loading = false;
  heading = null;
  if (!texture || target === index) {
    queued = null;
    pendingSteps = 0;
    cache.pins = new Set([index]);
    return;
  }

  const next = new Figure(poses[target], texture, studio.scene, overlay);
  next.fit(camera);
  next.face(camera);
  next.uniforms.uReveal.value = 0;
  transition = {
    old: current,
    next,
    target,
    time: 0,
    speed: queued !== null ? FAST_SPEED : 1,
    fromAngle: angle,
    toAngle: angle + (direction * Math.PI) / 4,
  };
  renderer.shadowMap.needsUpdate = true;
  events.emit('transition', { phase: 'start', from: index, to: target });
  if (!active) {
    finish();
  }
}

function finish() {
  if (!transition) {
    return;
  }
  const t = transition;
  angle = t.toAngle;
  studio.podium.rotation.y = angle;
  t.old.dispose();
  current = t.next;
  current.uniforms.uReveal.value = 1;
  current.uniforms.uErase.value = 0;
  index = t.target;
  transition = null;
  studio.contact.material.opacity = 1;
  cache.pins = new Set([index]);
  cache.preload(index, order);
  renderer.shadowMap.needsUpdate = true;
  saveState();
  invalidate();
  events.emit('transition', { phase: 'end', index });
  events.emit('pose', { index, pose: poses[index] });
  // pause() clears the queue before finishing, so blurring the window still drops
  // pending key presses; programmatic goTo()/step() while unfocused still complete.
  startQueued();
}

// Drop queued steps and an unfinished transition (used by reload).
function abortNavigation() {
  queued = null;
  pendingSteps = 0;
  loading = false;
  heading = null;
  if (transition) {
    const t = transition;
    transition = null;
    t.next.dispose();
    angle = t.toAngle;
    studio.podium.rotation.y = angle;
    studio.contact.material.opacity = 1;
    events.emit('transition', { phase: 'end', index, aborted: true });
  }
}

// Replace the pose list (collection switch, «Выбрать папку…»). Without `startIndex`
// the backend's saved state picks the pose (restored by file name).
async function reload(list, startIndex) {
  const gen = ++generation;
  abortNavigation();
  current?.dispose();
  current = null;
  cache?.dispose();
  cache = null;
  order = null;
  poses = Array.isArray(list) ? list.slice() : [];
  invalidate();
  if (!poses.length) {
    index = 0;
    showError('Позы не найдены', true);
    events.emit('poses', { poses: [], index: -1 });
    return;
  }
  clearError();

  let start = startIndex;
  if (!Number.isInteger(start)) {
    const state = await api.get_state();
    if (gen !== generation) return;
    start = state?.index;
  }
  index = clamp(Number.isInteger(start) ? start : 0, 0, poses.length - 1);
  const ownCache = new TextureCache(poses, renderer, report);
  cache = ownCache;

  for (let tries = 0; tries < poses.length; tries++) {
    try {
      cache.pins = new Set([index]);
      const texture = await cache.get(index);
      if (gen !== generation) return;
      current = new Figure(poses[index], texture, studio.scene, overlay);
      current.fit(camera);
      current.face(camera);
      renderer.shadowMap.needsUpdate = true;
      cache.preload(index, order);
      if (firstPoseMs === null) {
        firstPoseMs = performance.now() - started;
      }
      saveState();
      invalidate();
      events.emit('poses', { poses: poses.slice(), index });
      events.emit('pose', { index, pose: poses[index] });
      resolveReady();
      return;
    } catch (error) {
      if (gen !== generation || cache !== ownCache) return;
      report(error);
      index = (index + 1) % poses.length;
    }
  }
  showError('Позы не найдены', true);
  events.emit('poses', { poses: poses.slice(), index: -1 });
}

function sameGeometry(a, b) {
  return a.w === b.w && a.h === b.h && a.box.every((value, i) => value === b.box[i]);
}

// Replace pose data; re-place live when it is on screen.
function updatePose(i, pose) {
  if (!Number.isInteger(i) || i < 0 || i >= poses.length || !pose || !Array.isArray(pose.box)) {
    return;
  }
  const previous = poses[i];
  poses[i] = pose; // TextureCache shares this array
  if (previous.url !== pose.url && cache) {
    cache.failed.delete(i);
    if (!cache.pins.has(i)) {
      cache.entries.get(i)?.texture?.dispose();
      cache.entries.delete(i);
    }
  }
  if (!current) {
    return;
  }
  if (transition && transition.target === i) {
    transition.next = refreshFigure(transition.next, pose);
  }
  if (i === index) {
    if (previous.url !== pose.url && !transition) {
      swapTexture(generation).catch(report);
      return;
    }
    const refreshed = refreshFigure(current, pose);
    if (transition && transition.old === current) {
      transition.old = refreshed; // finish() must dispose the figure actually on screen
    }
    current = refreshed;
    events.emit('pose', { index, pose });
  }
  renderer.shadowMap.needsUpdate = true;
  invalidate();
}

// Same image: re-place in place, or rebuild the meshes when the bbox changed.
function refreshFigure(figure, pose) {
  if (figure.pose.url !== pose.url) {
    return figure; // swapTexture / next transition handles a new image
  }
  if (sameGeometry(figure.pose, pose)) {
    figure.setPose(pose);
    figure.fit(camera);
    return figure;
  }
  const rebuilt = new Figure(pose, figure.texture, studio.scene, overlay);
  rebuilt.uniforms.uReveal.value = figure.uniforms.uReveal.value;
  rebuilt.uniforms.uErase.value = figure.uniforms.uErase.value;
  rebuilt.fit(camera);
  figure.dispose();
  return rebuilt;
}

// The current pose got a different image URL: load it and swap without animation.
async function swapTexture(gen) {
  const shown = index;
  const stale = cache.entries.get(shown);
  cache.entries.delete(shown); // the stale texture is now owned here, not by the cache
  try {
    let texture = null;
    try {
      texture = await cache.get(shown);
    } catch {
      return; // TextureCache already reported the failed load
    }
    if (gen !== generation || shown !== index || transition || !current) {
      return;
    }
    const figure = new Figure(poses[shown], texture, studio.scene, overlay);
    figure.fit(camera);
    current.dispose();
    current = figure;
    renderer.shadowMap.needsUpdate = true;
    invalidate();
    events.emit('pose', { index, pose: poses[index] });
  } finally {
    if (stale?.texture) {
      disposeWhenUnused(stale.texture);
    }
  }
}

// Dispose a texture no longer tracked by the cache as soon as no figure shows it.
function disposeWhenUnused(texture) {
  const used = [current, transition?.old, transition?.next].some((figure) => figure?.texture === texture);
  if (!used) {
    texture.dispose();
    return;
  }
  const offTransition = events.on('transition', (detail) => {
    if (detail.phase === 'end') retry();
  });
  const offPoses = events.on('poses', retry);
  function retry() {
    offTransition();
    offPoses();
    disposeWhenUnused(texture);
  }
}

function setOrder(indices) {
  order = normalizeOrder(indices, poses.length);
  if (cache && current && !transition && !loading) {
    cache.preload(index, order);
  }
}

function goEdge(edge) {
  const target = edgeTarget(edge, poses.length, order);
  if (target >= 0) {
    requestGoTo(target);
  }
}

// --- Render loop ---------------------------------------------------------
function renderFrame(now) {
  const dt = lastFrame ? clamp((now - lastFrame) / 1000, 0, 0.06) : 1 / 60;
  lastFrame = now;

  // Parallax orbit from the pointer plus a slow "breath" (also in mode off); damped while
  // zoomed so the on-screen motion keeps its size.
  const amplitude = PARALLAX_MODES[parallaxMode];
  const damp = 1 / view.zoom;
  const x = mouse.inside ? mouse.x : 0;
  const y = mouse.inside ? mouse.y : 0;
  [sway.yaw, sway.yawSpeed] = smoothDamp(sway.yaw, sway.yawSpeed, x * amplitude.yaw * damp, PARALLAX_SMOOTH_TIME, dt);
  [sway.pitch, sway.pitchSpeed] = smoothDamp(sway.pitch, sway.pitchSpeed, y * amplitude.pitch * damp, PARALLAX_SMOOTH_TIME, dt);
  const breathYaw = Math.sin(now / 1450) * 0.002 * damp;
  const breathPitch = Math.cos(now / 1600) * 0.001 * damp;
  if (studio.backdrop.active) fitBackdrop();
  orbitCamera(camera, framing, sway.yaw + breathYaw, sway.pitch + breathPitch);
  camera.updateMatrixWorld();
  syncViewCamera();

  current?.face(camera);
  transition?.next.face(camera);
  if (camera.position.distanceToSquared(lastShadowCamera) > Math.pow(framing.distance * 0.0003, 2)) {
    renderer.shadowMap.needsUpdate = true;
    lastShadowCamera.copy(camera.position);
  }

  if (transition) {
    const t = transition;
    t.time += dt * t.speed;
    const progress = clamp(t.time / TRANSITION_SECONDS);
    // Old drawing erases first; the new one starts revealing a quarter in.
    t.old.uniforms.uErase.value = ease(progress / 0.65);
    t.next.uniforms.uReveal.value = ease((progress - 0.25) / 0.75);
    studio.contact.material.opacity = Math.max(1 - t.old.uniforms.uErase.value, t.next.uniforms.uReveal.value);
    studio.podium.rotation.y = THREE.MathUtils.lerp(t.fromAngle, t.toAngle, ease(progress));
    renderer.shadowMap.needsUpdate = true;
    if (progress >= 1) {
      finish();
    }
  }

  post.finish.uniforms.time.value = now / 1000;
  renderer.info.autoReset = false;
  renderer.info.reset();
  post.composer.render(dt);
  // AgX, bloom, film grain, vignette and SMAA have ALREADY run. This final unlit
  // sRGB pass never sees those filters. clearDepth leaves the studio color intact.
  renderer.setRenderTarget(null);
  renderer.clearDepth();
  renderer.render(overlay, figureCamera);
  renderedFrames++;

  for (const callback of [...frameCallbacks]) {
    try {
      callback();
    } catch (error) {
      frameCallbacks.delete(callback); // a broken callback would otherwise spam every frame
      report(error);
    }
  }
}

function draw(now) {
  frameId = 0;
  if (!active || document.hidden) {
    return;
  }
  const frameStart = performance.now();
  renderFrame(now);
  frameMs += (performance.now() - frameStart - frameMs) * 0.05;
  frameCount++;
  if (!sampleStart) {
    sampleStart = now;
  }
  if (now - sampleStart >= 2500) {
    fps = (frameCount * 1000) / (now - sampleStart);
    frameCount = 0;
    sampleStart = now;
    // Adaptive quality: drop bloom, then studio resolution, on slow GPUs.
    if (firstPoseMs !== null && now - started > 6000 && fps < 53 && quality < 3) {
      post.bloom.enabled = false;
      quality++;
      resize();
    }
  }
  frameId = requestAnimationFrame(draw);
}

// While the loop is paused (window blurred), still show view/pose changes once.
function invalidate() {
  if (active || stillFrameId || !renderer || !framing) {
    return;
  }
  stillFrameId = requestAnimationFrame((now) => {
    stillFrameId = 0;
    if (!active && !document.hidden) {
      renderFrame(now);
    }
  });
}

function resume() {
  active = true;
  lastFrame = 0;
  sampleStart = 0;
  frameCount = 0;
  if (!frameId) {
    frameId = requestAnimationFrame(draw);
  }
}

function pause() {
  active = false;
  cancelAnimationFrame(frameId);
  frameId = 0;
  queued = null;
  pendingSteps = 0;
  finish();
  lastFrame = 0;
}

// --- Background: the studio or a picture behind the podium ----------------
let background = { mode: 'studio', url: null, blur: 0, dim: 0 };
let backgroundToken = 0;
const backgroundImages = new Map(); // url -> Promise<HTMLImageElement> (a few recent ones)

function loadBackgroundImage(url) {
  let promise = backgroundImages.get(url);
  if (!promise) {
    promise = new Promise((resolve, reject) => {
      const image = new Image();
      image.decoding = 'async';
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error(`Не удалось загрузить фон: ${url}`));
      image.src = url;
    });
    promise.catch(() => backgroundImages.delete(url));
    backgroundImages.set(url, promise);
    for (const key of backgroundImages.keys()) {
      if (backgroundImages.size <= 3) break;
      if (key !== url && key !== background.url) backgroundImages.delete(key);
    }
  }
  return promise;
}

// {mode: "studio"|"image", url, blur: 0…24, dim: 0…0.85}; resolves with the applied state.
// A later call wins over an earlier one whose image is still loading. Rejects if the image
// cannot be loaded (the previous background stays).
async function setBackground(options = {}) {
  const token = ++backgroundToken;
  const blur = clamp(Number(options.blur) || 0, 0, 24);
  const dim = clamp(Number(options.dim) || 0, 0, 0.85);
  const url = options.mode === 'image' && typeof options.url === 'string' && options.url ? options.url : null;
  const image = url ? await loadBackgroundImage(url) : null;
  if (token !== backgroundToken || !studio) {
    return { ...background };
  }
  studio.backdrop.configure(image, blur, dim);
  post.setBackdrop(!!image);
  background = { mode: image ? 'image' : 'studio', url, blur, dim };
  renderer.shadowMap.needsUpdate = true;
  invalidate();
  return { ...background };
}

// --- App controller (docs/API.md §2) --------------------------------------
const app = {
  get api() {
    return api;
  },
  ready,
  poses: () => poses.slice(),
  index: () => index,
  current: () => (current ? poses[index] ?? null : null),
  goTo: (target) => requestGoTo(target),
  step: (delta) => requestStep(delta),
  reload: (list, startIndex) => reload(list, startIndex),
  updatePose,
  setOrder,
  on: events.on,
  shortcuts: {
    register: shortcuts.register,
    list: shortcuts.list,
  },
  overlays: {
    open: overlays.open,
    close: overlays.close,
    any: overlays.any,
  },
  view: {
    canvas,
    setZoom(zoom, centerX, centerY) {
      if (view.setZoom(Number(zoom), centerX, centerY)) viewChanged();
    },
    zoom: () => view.zoom,
    pan(dx, dy) {
      if (view.pan(Number(dx), Number(dy))) viewChanged();
    },
    resetView() {
      if (view.reset()) viewChanged();
    },
    figureRect() {
      const figure = transition ? transition.next : current;
      const rect = figure?.layoutRect();
      return rect ? view.toScreen(rect) : null;
    },
    // Parallax mode "off" | "soft" | "strong"; returns the mode in effect.
    setParallax(mode) {
      if (Object.hasOwn(PARALLAX_MODES, mode)) parallaxMode = mode;
      return parallaxMode;
    },
    parallax: () => parallaxMode,
    setBackground: (options) => setBackground(options),
    background: () => ({ ...background }),
    onFrame(callback) {
      if (typeof callback !== 'function') {
        throw new TypeError('onFrame(callback)');
      }
      frameCallbacks.add(callback);
      return () => frameCallbacks.delete(callback);
    },
  },
};

function viewChanged() {
  applyView();
  invalidate();
}

// --- Built-in shortcuts (core) -------------------------------------------
function quit() {
  queued = null;
  pendingSteps = 0;
  finish();
  api?.quit();
}

// Pose-changing keys belong to an open overlay (e.g. the gallery) while one is open.
const noOverlay = () => !overlays.any();
shortcuts.register({ keys: ['ArrowLeft', 'KeyA'], title: 'Предыдущая поза', group: 'Навигация', anyShift: true, enabled: noOverlay, handler: () => requestStep(-1) });
shortcuts.register({ keys: ['ArrowRight', 'KeyD'], title: 'Следующая поза', group: 'Навигация', anyShift: true, enabled: noOverlay, handler: () => requestStep(1) });
shortcuts.register({ keys: ['Home'], title: 'Первая поза', group: 'Навигация', anyShift: true, enabled: noOverlay, handler: () => goEdge(-1) });
shortcuts.register({ keys: ['End'], title: 'Последняя поза', group: 'Навигация', anyShift: true, enabled: noOverlay, handler: () => goEdge(1) });
shortcuts.register({
  keys: ['KeyF', 'F11'],
  title: 'Полный экран',
  group: 'Окно',
  anyShift: true,
  handler: (event) => {
    if (!event.repeat) api?.toggle_fullscreen();
  },
});
shortcuts.register({
  keys: ['Escape'],
  title: 'Закрыть панель, иначе выход',
  group: 'Окно',
  anyShift: true,
  handler: (event) => {
    if (overlays.closeTop() || event.repeat) return;
    quit();
  },
});
shortcuts.register({
  keys: ['Shift+Slash', 'F1'],
  eventKeys: ['?'], // any layout, e.g. Shift+Digit7 on ЙЦУКЕН
  title: 'Справка по клавишам',
  group: 'Окно',
  handler: (event) => {
    if (!event.repeat) help.toggle();
  },
});

// --- UI modules ----------------------------------------------------------
// Imported dynamically so a broken module (even a syntax error) cannot stop the viewer.
// install() is not awaited: modules await app.ready themselves.
function installModules() {
  return Promise.all(UI_MODULES.map(async ([name, path]) => {
    try {
      const module = await import(path);
      Promise.resolve(module.install(app)).catch((error) => report(`UI module ${name}: ${error?.stack || error}`));
    } catch (error) {
      report(`UI module ${name}: ${error?.stack || error}`);
    }
  }));
}

// --- Startup -------------------------------------------------------------
// pywebview may create window.pywebview.api before its methods are filled in:
// the API counts as ready only once get_poses exists. Resolves with a wrapper,
// because a Proxy-based API (dev harness) would otherwise look like a thenable.
function waitForApi() {
  return new Promise((resolve) => {
    let timer = 0;
    const check = () => {
      if (typeof window.pywebview?.api?.get_poses !== 'function') {
        return;
      }
      clearInterval(timer);
      window.removeEventListener('pywebviewready', check);
      resolve({ api: window.pywebview.api });
    };
    timer = setInterval(check, 50);
    window.addEventListener('pywebviewready', check);
    check();
  });
}

async function start() {
  const apiReady = waitForApi();
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false, powerPreference: 'high-performance' });
  } catch (error) {
    showError('WebGL недоступен. Включите аппаратное ускорение и обновите драйвер видеокарты.');
    throw error;
  }
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.AgXToneMapping;
  renderer.toneMappingExposure = 1;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.shadowMap.autoUpdate = false;
  renderer.autoClear = false;
  renderer.setClearColor('#16181b', 1);

  camera = new THREE.PerspectiveCamera(30, 1, 0.05, 250);
  viewCamera = new THREE.PerspectiveCamera(30, 1, 0.05, 250);
  overlay = new THREE.Scene();
  figureCamera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 20);
  figureCamera.position.z = 10;
  studio = createStage(renderer);
  post = createPost(renderer, studio.scene, viewCamera);
  resize();
  if (active) {
    resume();
  }
  canvas.addEventListener('webglcontextlost', (event) => {
    event.preventDefault();
    pause();
    showError('Графический контекст потерян. Перезапустите PoseStage.');
    report('WebGL context lost');
  });

  // Not awaited: module loading stays off the critical path to the first pose.
  installModules();

  ({ api } = await apiReady);
  for (const text of queuedReports.splice(0)) {
    sendReport(text);
  }

  let list = await api.get_poses();
  if (!list.length) {
    showError('Позы не найдены', true);
    list = await api.choose_folder();
  }
  await reload(list);
}

document.querySelector('#prev').addEventListener('click', () => requestStep(-1));
document.querySelector('#next').addEventListener('click', () => requestStep(1));
chooseButton.addEventListener('click', async () => {
  chooseButton.disabled = true;
  try {
    await reload(await api.choose_folder());
  } catch (error) {
    report(error);
  } finally {
    chooseButton.disabled = false;
  }
});
document.querySelector('#titlebar').addEventListener('dblclick', () => api?.toggle_fullscreen());

// A handled shortcut wakes the idle chrome so tool-state changes are visible.
window.addEventListener('keydown', (event) => {
  if (shortcuts.dispatch(event)) {
    chrome.wake();
  }
});
window.addEventListener('pointermove', (event) => {
  chrome.wake();
  mouse = { x: clamp((event.clientX / innerWidth) * 2 - 1, -1, 1), y: clamp(1 - (event.clientY / innerHeight) * 2, -1, 1), inside: true };
});
// Parallax eases back to the centre when the pointer leaves the window or focus is lost.
document.addEventListener('mouseout', (event) => {
  if (!event.relatedTarget) mouse.inside = false;
});
window.addEventListener('blur', () => {
  mouse.inside = false;
});
window.addEventListener('pointerdown', chrome.wake);
window.addEventListener('resize', resize);
// Moving to a monitor with another Windows scale changes devicePixelRatio without a resize.
(function watchPixelRatio() {
  matchMedia(`(resolution: ${devicePixelRatio}dppx)`).addEventListener('change', () => {
    resize();
    watchPixelRatio();
  }, { once: true });
})();
window.addEventListener('focus', resume);
window.addEventListener('blur', pause);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    pause();
  } else if (document.hasFocus()) {
    resume();
  }
});
window.addEventListener('contextmenu', (event) => event.preventDefault());
window.addEventListener('error', (event) => report(event.message));
window.addEventListener('unhandledrejection', (event) => report(event.reason));
chrome.wake();
chrome.updateWindowed();

// Read-only diagnostics for local acceptance tests; no UI or external telemetry.
Object.defineProperty(window, '__poseStage', {
  get: () => ({
    index,
    file: poses[index]?.file,
    count: poses.length,
    transition: !!transition,
    pending: pendingSteps,
    queued,
    loading,
    active,
    fps,
    quality,
    firstPoseMs,
    renderedFrames,
    textureCount: cache?.entries.size,
    gpuTextures: renderer?.info.memory.textures,
    gpuGeometries: renderer?.info.memory.geometries,
    programs: renderer?.info.programs.length,
    box: current?.pose.box,
    pixelScale: current?.pixelScale,
    supportMode: current?.pose.support?.mode,
    maxContactRadius: current?.placement?.maxContactRadius,
    stature: current?.pose.stature ?? null,
    scaleAdjust: current?.pose.scale_adjust ?? 1,
    sizeLimited: current?.placement?.limited ?? false,
    figures: overlay ? overlay.children.length : 0,
    supportCenter: current?.placement ? [current.placement.mapping.centerX, current.placement.mapping.centerY] : null,
    podiumTop: 0.4,
    canvas: [canvas.width, canvas.height],
    zoom: view.zoom,
    parallax: parallaxMode,
    sway: [sway.yaw, sway.pitch],
    frameMs,
    background: background.mode,
    viewOffset: [view.x, view.y],
    order: order ? order.length : null,
    overlays: overlays.top(),
    windowed: document.body.classList.contains('windowed'),
    error: errorPanel.hidden ? null : errorMessage.textContent,
  }),
});
// Diagnostics handle for the browser dev harness / acceptance checks.
window.__poseStageApp = app;

start().catch((error) => {
  report(error);
  if (errorPanel.hidden) {
    showError('Не удалось запустить сцену. Подробности сохранены в журнале PoseStage.');
  }
  resolveReady();
});

export { app };
