// PoseStage frontend core: renderer + loop, pose transitions, input, and the App
// controller handed to UI modules (contract: docs/API.md, section 2).
import * as THREE from 'three';
import { createStage, frameCamera, orbitCamera } from './scene/stage.js';
import { createPost } from './scene/post.js';
import { Figure } from './scene/figure.js';
import { complete, inBetween, groove, curve, smooth, createDanceRig, jointPoints, cycle, STYLES } from './scene/dance.js';
import { setSizing, getSizing } from './scene/placement.js';
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
  ['skeleton', './ui/skeleton.js'],
  ['dance', './ui/dance.js'],
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

const events = createEmitter(['pose', 'poses', 'transition', 'dance'], report);
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
  for (const figure of dance?.figures || []) figure.fit(camera);
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
  if (dance) stopDance();
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
  if (dance) stopDance();
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
  stopDance();
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
  if (dance) {
    danceFrame(dt);
    renderer.shadowMap.needsUpdate = true;
  }
  if (idleOn) {
    // Idle animation: breathing, weight shift and head motion; the cast shadow follows.
    for (const figure of [current, transition?.old, transition?.next]) figure?.animate(camera, now / 1000);
    renderer.shadowMap.needsUpdate = true;
  }
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

// --- Idle animation --------------------------------------------------------
let idleOn = false;
let idleFrameId = 0;
let idleLast = 0;

// While the window is unfocused the main loop pauses; the idle animation keeps going at
// ~20 fps (enough for slow breathing) so the pose stays alive next to the drawing program.
function idleTick(now) {
  idleFrameId = 0;
  if (active || !(idleOn || (dance && !dance.paused)) || document.hidden || !framing) return;
  if (now - idleLast >= 48) {
    idleLast = now;
    renderFrame(now);
  }
  idleFrameId = requestAnimationFrame(idleTick);
}

function setIdle(on) {
  idleOn = !!on;
  if (!idleOn) {
    for (const figure of [current, transition?.old, transition?.next]) figure?.animate(camera, null);
    renderer.shadowMap.needsUpdate = true;
    invalidate();
  } else if (!active && !idleFrameId) {
    idleFrameId = requestAnimationFrame(idleTick);
  }
  return idleOn;
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
  if ((idleOn || dance) && !idleFrameId) idleFrameId = requestAnimationFrame(idleTick);
}

// --- Dance: a phrase played on the podium (docs/DANCE.md) -----------------
// One pose per count; between counts both drawings bend by their bones toward a shared
// in-between skeleton (scene/dance.js) and cross-fade. The skeletons are compared in layout
// pixels (y down), where every pose stands as it is placed on the podium.
const DANCE_SPEEDS = [1, 0.5, 0.25];
let dance = null; // {phrase, figures, textures, order, time, frame, step, paused, speed, style, bpm}
let danceToken = 0;
const danceLoader = new THREE.TextureLoader();

function layoutJoints(figure) {
  const p = figure.placement;
  const out = {};
  for (const [id, [x, y]] of Object.entries(figure.danceRig.rest)) {
    out[id] = [p.mapping.centerX + (x - p.cx) * p.scale, -(p.mapping.centerY + (p.cy - y) * p.scale)];
  }
  return out;
}

function toSource(figure, joints) {
  const p = figure.placement;
  const out = {};
  for (const [id, [x, y]] of Object.entries(joints)) {
    out[id] = [p.cx + (x - p.mapping.centerX) / p.scale, p.cy + (y + p.mapping.centerY) / p.scale];
  }
  return out;
}

function showFigure(figure, on) {
  if (!figure) return;
  figure.group.visible = on;
  figure.shadow.visible = on;
}

// phrase: {name, loop, poses: [{file, mirror, pose}]}; options: {bpm, style: "soft"|"groove", speed}.
async function playDance(phrase, options = {}) {
  const entries = (phrase?.poses || []).filter((entry) => entry?.pose?.url && Array.isArray(entry.pose.box));
  if (entries.length < 2 || !studio) throw new Error('В связке меньше двух поз');
  if (entries.some((entry) => !jointPoints(entry.pose).pelvis || !jointPoints(entry.pose).chest)) {
    throw new Error('У позы связки нет скелета');
  }
  stopDance();
  const token = ++danceToken;
  const loads = new Map();
  for (const entry of entries) {
    if (!loads.has(entry.pose.url)) {
      loads.set(entry.pose.url, danceLoader.loadAsync(entry.pose.url).then((texture) => {
        texture.colorSpace = THREE.SRGBColorSpace;
        texture.anisotropy = renderer.capabilities.getMaxAnisotropy();
        return texture;
      }));
    }
  }
  const settled = await Promise.allSettled(loads.values());
  const textures = new Map([...loads.keys()].map((url, i) => [url, settled[i].status === 'fulfilled' ? settled[i].value : null]));
  const failed = settled.find((result) => result.status === 'rejected');
  if (token !== danceToken || failed) {
    for (const texture of textures.values()) texture?.dispose();
    if (failed && token === danceToken) throw failed.reason;
    return null;
  }
  if (transition) finish();
  const figures = entries.map((entry) => {
    const figure = new Figure(entry.pose, textures.get(entry.pose.url), studio.scene, overlay, { mirror: !!entry.mirror, soft: true });
    figure.danceRig = createDanceRig(jointPoints(entry.pose));
    figure.setRig(figure.danceRig);
    figure.fit(camera);
    showFigure(figure, false);
    return figure;
  });
  showFigure(current, false);
  dance = {
    phrase,
    figures,
    textures: [...textures.values()],
    order: cycle(figures.length, !!phrase.loop),
    time: 0,
    frame: 0,
    step: -1,
    paused: false,
    speed: DANCE_SPEEDS.includes(options.speed) ? options.speed : 1,
    style: Object.hasOwn(STYLES, options.style) ? options.style : 'soft',
    bpm: clamp(Number(options.bpm) || 120, 60, 200),
  };
  renderer.shadowMap.needsUpdate = true;
  if (!active && !idleFrameId) idleFrameId = requestAnimationFrame(idleTick);
  invalidate();
  events.emit('dance', { phase: 'start', ...danceState() });
  return danceState();
}

function stopDance() {
  danceToken++;
  if (!dance) return;
  const d = dance;
  dance = null;
  for (const figure of d.figures) figure.dispose();
  for (const texture of d.textures) texture.dispose();
  showFigure(current, true);
  renderer.shadowMap.needsUpdate = true;
  invalidate();
  events.emit('dance', { phase: 'end' });
}

function danceState() {
  if (!dance) return null;
  const { phrase, figures, paused, speed, style, bpm, step, order } = dance;
  return { name: phrase.name, count: figures.length, pose: order[Math.max(0, step)] ?? 0, paused, speed, style, bpm };
}

function danceFrame(dt) {
  const d = dance;
  if (!d.paused) d.time += dt * d.speed;
  const beat = 60 / d.bpm;
  const { share, curve: curveName } = STYLES[d.style];
  const ease = curve(curveName);
  const steps = d.order.length - 1;
  const count = Math.floor(d.time / beat);
  const step = ((count % steps) + steps) % steps;
  const u = (d.time - count * beat) / (beat * share);
  const a = d.figures[d.order[step]];
  const b = d.figures[d.order[step + 1]];
  if (step !== d.step) {
    d.step = step;
    events.emit('dance', { phase: 'step', ...danceState() });
  }
  a.fit(camera);
  b.fit(camera);
  if (!(a.placement?.scale > 0) || !(b.placement?.scale > 0)) return; // 0×0 window (minimised)
  const ja = layoutJoints(a);
  const jb = layoutJoints(b);
  let target = u < 1 ? inBetween(complete(ja, jb), complete(jb, ja), u, ease) : { ...jb };
  if (d.style === 'groove') {
    const ys = Object.values(ja).map(([, y]) => y);
    target = groove(target, d.time, beat, Math.max(...ys) - Math.min(...ys));
  }
  // B fades in over A while they overlap, then what is left of A fades out: no see-through.
  const mix = u < 1 ? smooth(0.2, 0.8, clamp(ease(u))) : 1;
  d.frame++;
  for (const figure of d.figures) {
    if (figure !== a && figure !== b) {
      showFigure(figure, false);
      continue;
    }
    const opacity = figure === b ? clamp(mix * 2) : clamp(2 - mix * 2);
    figure.group.visible = opacity > 0.001;
    figure.mesh.material.opacity = opacity;
    figure.mesh.renderOrder = figure === b ? 2 : 1;
    figure.shadow.visible = figure === (mix < 0.5 ? a : b);
    if (figure.group.visible) {
      figure.danceRig.setTarget(toSource(figure, target));
      figure.animate(camera, d.frame);
    }
  }
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
  // Dance phrases (docs/DANCE.md): played on the podium, one pose per count.
  dance: {
    play: (phrase, options) => playDance(phrase, options),
    stop: () => stopDance(),
    state: () => danceState(),
    setPaused(on) {
      if (dance) dance.paused = !!on;
      if (dance && !on && !active && !idleFrameId) idleFrameId = requestAnimationFrame(idleTick);
      events.emit('dance', { phase: 'state', ...danceState() });
      return danceState();
    },
    setSpeed(speed) {
      if (dance && DANCE_SPEEDS.includes(speed)) dance.speed = speed;
      events.emit('dance', { phase: 'state', ...danceState() });
      return danceState();
    },
    setStyle(style) {
      if (dance && Object.hasOwn(STYLES, style)) dance.style = style;
      events.emit('dance', { phase: 'state', ...danceState() });
      return danceState();
    },
    speeds: () => DANCE_SPEEDS.slice(),
  },
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
    // Pose sizing "file" (default: as drawn, centred only) | "body" (one body size, limits).
    setSizing(mode) {
      const before = getSizing();
      const after = setSizing(mode);
      if (after !== before) {
        for (const figure of [current, transition?.old, transition?.next]) {
          if (figure) figure.placement = null; // refit with the new rule on the next frame
        }
        renderer.shadowMap.needsUpdate = true;
        invalidate();
      }
      return after;
    },
    sizing: () => getSizing(),
    // Idle animation (breathing loop) on/off; returns the state in effect.
    setIdle: (on) => setIdle(on),
    idle: () => idleOn,
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
  } else if ((idleOn || dance) && !idleFrameId) {
    idleFrameId = requestAnimationFrame(idleTick); // restored without focus: keep the pose alive
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
    idle: idleOn,
    dance: danceState(),
    joints: current?.pose.joints_source ?? null,
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
