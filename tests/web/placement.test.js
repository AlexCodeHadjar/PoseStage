// PoseStage frontend tests. Serve the project root over HTTP and open
// tests/web/placement.test.html (e.g. python -m http.server; no Node needed).
import * as THREE from 'three';
import { test, run, assert, equal, deepEqual, near, waitFor } from './runner.js';
import { makeCamera, VIEWPORTS, CAMERA_SHIFTS } from './measure.js';
import { FIXTURE_POSES } from './fixtures.js';

import { frameCamera } from '../../web/scene/stage.js';
import { groundMapping, fitPlacement, bodyScale, STATURE_SHARE, SUPPORT_RADIUS, setSizing, FILE_FIGURE_SHARE } from '../../web/scene/placement.js';
import { Figure } from '../../web/scene/figure.js';
import { createRig, estimateJoints, templateJoints, IDLE_LOOP, JOINT_IDS } from '../../web/scene/idle.js';
import { complete, inBetween, groove, curve, createDanceRig, jointPoints, cycle, mirrorPose } from '../../web/scene/dance.js';
import { DANCE_REFERENCE } from './dance_reference.js';
import { TextureCache } from '../../web/scene/textures.js';
import { stepTarget, normalizeOrder, edgeTarget } from '../../web/core/navigation.js';
import { ViewState } from '../../web/core/view.js';
import { normalizeCombo, keyLabel, createOverlays, createShortcuts } from '../../web/core/input.js';

// --- Placement math ------------------------------------------------------

// Fixture poses as the backend now sends them: the same body drawn at different sizes.
const BODY_POSES = FIXTURE_POSES.map((pose, i) => ({ ...pose, stature: (pose.box[3] - pose.box[1]) * (0.8 + 0.1 * i) }));

test('one body size: every pose that fits gets stature × scale = share of the viewport', () => {
  setSizing('body');
  try {
    let free = 0;
    for (const [width, height] of VIEWPORTS) {
      const mapping = groundMapping(makeCamera(frameCamera, width, height).camera);
      for (const pose of BODY_POSES) {
        const placement = fitPlacement(pose, mapping);
        near(placement.desired, (STATURE_SHARE * height) / pose.stature, 1e-12, `${pose.file} desired`);
        assert(placement.scale <= placement.desired * (1 + 1e-12), `${pose.file} never larger than the body size`);
        if (!placement.limited) {
          free++;
          near(placement.scale * pose.stature, STATURE_SHARE * height, 1e-9, `${width}x${height} ${pose.file} body`);
        }
      }
    }
    // Fixtures include deliberately oversized bodies and a narrow 480×720 window.
    assert(free >= VIEWPORTS.length * 2, `fixtures that fit unshrunk (${free})`);
  } finally {
    setSizing('file');
  }
});

test('scale_adjust multiplies the size; no stature falls back to the box height', () => {
  setSizing('body');
  try {
    const mapping = groundMapping(makeCamera(frameCamera, 1920, 1080).camera);
    const pose = BODY_POSES[3];
    near(bodyScale({ ...pose, scale_adjust: 1.1 }, mapping), bodyScale(pose, mapping) * 1.1, 1e-12, 'adjust');
    const bare = { ...FIXTURE_POSES[3] };
    near(bodyScale(bare, mapping), (STATURE_SHARE * 1080) / (bare.box[3] - bare.box[1]), 1e-12, 'fallback');
    const smaller = fitPlacement({ ...pose, scale_adjust: 0.8 }, mapping);
    near(smaller.scale, fitPlacement(pose, mapping).scale * 0.8, 1e-9, 'smaller pose keeps the ratio');
  } finally {
    setSizing('file');
  }
});

test('file sizing (default): one scale for every picture, support centred, no limits', () => {
  equal(setSizing('nope'), 'file', 'default mode');
  const mapping = groundMapping(makeCamera(frameCamera, 1920, 1080).camera);
  try {
    setSizing('file');
    for (const pose of BODY_POSES) {
      const p = fitPlacement({ ...pose, scale_adjust: 1.4 }, mapping);
      near(p.scale, (STATURE_SHARE * 1080) / (FILE_FIGURE_SHARE * pose.h), 1e-12, `${pose.file} scale`);
      equal(p.limited, false);
      near(p.x, mapping.centerX + ((pose.box[0] + pose.box[2]) / 2 - p.cx) * p.scale, 1e-9, 'support centred');
    }
  } finally {
    setSizing('file');
  }
});

// --- Idle animation ------------------------------------------------------------

// desktop.py IdleRig values for FIXTURE_POSES[1]: [x, y, t, dx, dy], front = estimated
// (template) skeleton, side = templateJoints(pose, 'side').
const PYTHON_IDLE = {
  front: [
    [512.0, 160.0, 0.7, 8.610277719, -6.041531923],
    [512.0, 160.0, 2.9, -2.148770816, -2.509225198],
    [512.0, 380.0, 0.7, 1.926889374, -4.216512848],
    [512.0, 380.0, 2.9, 0.301020104, -1.679754181],
    [436.4, 336.0, 0.7, 2.577241112, -7.238134463],
    [436.4, 336.0, 2.9, 0.401556717, -2.615618767],
  ],
  side: [
    [512.0, 160.0, 0.7, 9.645747127, -5.392094288],
    [512.0, 160.0, 2.9, -1.212802473, -2.021913067],
    [512.0, 380.0, 0.7, 3.221255597, -3.642547617],
    [512.0, 380.0, 2.9, 1.000799066, -1.459009792],
    [436.4, 336.0, 0.7, 4.495234472, -5.886071997],
    [436.4, 336.0, 2.9, 1.412810574, -2.191088953],
  ],
};

test('idle rig (skeleton v2) matches desktop.py, loops seamlessly and pins the supports', () => {
  const base = FIXTURE_POSES[1];
  for (const [kind, rows] of Object.entries(PYTHON_IDLE)) {
    const pose = { ...base, joints: kind === 'side' ? templateJoints(base, 'side') : null };
    const rig = createRig(pose);
    for (const [x, y, t, dx, dy] of rows) {
      const [jx, jy] = rig.displace(x, y, t);
      near(jx, dx, 1e-6, `${kind} dx at ${x},${y},${t}`);
      near(jy, dy, 1e-6, `${kind} dy at ${x},${y},${t}`);
      const [lx, ly] = rig.displace(x, y, t + IDLE_LOOP);
      near(lx, jx, 1e-9, 'loop x');
      near(ly, jy, 1e-9, 'loop y');
    }
  }
  const rig = createRig(base);
  for (const [x, y] of base.support.profile.filter((point) => point[1] >= base.box[1] + 0.93 * (base.box[3] - base.box[1]))) {
    deepEqual(rig.displace(x, y, 1.3), [0, 0], `support ${x},${y} pinned`);
  }
});

test('skeleton v2 templates: 16 points, sides, hidden far limbs, support flags', () => {
  const pose = FIXTURE_POSES[1];
  equal(JOINT_IDS.length, 16);
  const front = templateJoints(pose, 'front');
  const back = templateJoints(pose, 'back');
  assert(front.points.shoulder_l.xy[0] > front.points.shoulder_r.xy[0], 'front: figure left on the picture right');
  assert(back.points.shoulder_l.xy[0] < back.points.shoulder_r.xy[0], 'back: sides swap');
  equal(templateJoints(pose, 'side').points.wrist_r.state, 'hidden');
  equal(templateJoints(pose, 'sit').points.pelvis.support, true);
  equal(estimateJoints(pose).view, 'front');
  // A hidden or supported wrist never swings.
  const joints = templateJoints(pose, 'front');
  const free = createRig({ ...pose, joints });
  joints.points.wrist_l = { ...joints.points.wrist_l, state: 'hidden' };
  joints.points.wrist_r = { ...joints.points.wrist_r, support: true };
  const still = createRig({ ...pose, joints });
  const wrist = joints.points.wrist_r.xy;
  deepEqual(still.displace(wrist[0], wrist[1], 0.9), [0, 0], 'supported wrist pinned');
  assert(Math.hypot(...free.displace(wrist[0], wrist[1], 0.9)) >= 0, 'free rig runs');
});

test('dance transitions (scene/dance.js) match dance_motion.py', () => {
  const R = DANCE_REFERENCE;
  const ja = complete(R.JA, R.JB);
  const jb = complete(R.JB, R.JA);
  const same = (actual, expected, label) => {
    deepEqual(Object.keys(actual).sort(), Object.keys(expected).sort(), `${label} joints`);
    for (const [id, [x, y]] of Object.entries(expected)) {
      near(actual[id][0], x, 1e-6, `${label} ${id} x`);
      near(actual[id][1], y, 1e-6, `${label} ${id} y`);
    }
  };
  for (const [u, name, expected] of R.between) same(inBetween(ja, jb, u, curve(name)), expected, `in-between ${name} ${u}`);
  const mid = inBetween(ja, jb, 0.37, curve('hit'));
  same(groove(mid, 0.83, 0.5, 750), R.groove, 'groove');
  const rig = createDanceRig(ja);
  rig.setTarget(mid);
  for (const [x, y, fx, fy] of R.forward) {
    const [dx, dy] = rig.displace(x, y);
    near(x + dx, fx, 1e-6, `forward x at ${x},${y}`);
    near(y + dy, fy, 1e-6, `forward y at ${x},${y}`);
  }
  equal(rig.lockAt(0, 0), 0, 'nothing pinned in a dance');
  deepEqual(cycle(4, true), [0, 1, 2, 3, 0]);
  deepEqual(cycle(4, false), [0, 1, 2, 3, 2, 1, 0]);
  const pose = FIXTURE_POSES[1];
  deepEqual(Object.keys(jointPoints({ ...pose, joints: templateJoints(pose, 'front') })).length, 16);
  // mirrorPose = dance.mirror_pose: box and profile flip, sides swap, twice gives the pose back.
  const original = { ...pose, joints: templateJoints(pose, 'side') };
  const flipped = mirrorPose(original);
  deepEqual(flipped.box, [pose.w - pose.box[2], pose.box[1], pose.w - pose.box[0], pose.box[3]], 'box');
  equal(flipped.joints.points.wrist_l.state, 'hidden', 'the hidden far side becomes the left');
  equal(flipped.joints.points.shoulder_l.xy[0], pose.w - original.joints.points.shoulder_r.xy[0]);
  const twice = mirrorPose(flipped);
  deepEqual(twice.joints, original.joints, 'joints back');
  deepEqual(twice.box, original.box, 'box back');
  deepEqual(twice.support.profile, [...original.support.profile].sort((a, b) => a[0] - b[0] || a[1] - b[1]), 'profile back');
});

test('TextureCache uploads a loaded texture right away (no hitch when the transition starts)', async () => {
  const uploaded = [];
  const renderer = { capabilities: { getMaxAnisotropy: () => 1 }, initTexture: (texture) => uploaded.push(texture) };
  const cache = new TextureCache([{ url: 'a.png', file: 'a.png' }], renderer, () => {});
  const texture = new THREE.Texture();
  cache.loader = { loadAsync: async () => texture };
  equal(await cache.get(0), texture);
  deepEqual(uploaded, [texture]);
  cache.dispose();
});

test('Figure.animate bends the drawing and restores it', () => {
  const camera = makeCamera(frameCamera, 1280, 720).camera;
  const scene = new THREE.Scene();
  const figure = new Figure(FIXTURE_POSES[1], new THREE.Texture(), scene, scene);
  figure.fit(camera);
  const still = Array.from(figure.geometry.attributes.position.array);
  const stillShadow = Array.from(figure.shadowGeometry.attributes.position.array);
  figure.animate(camera, 0.7);
  const moved = Array.from(figure.geometry.attributes.position.array);
  assert(moved.some((value, i) => Math.abs(value - still[i]) > 1e-4), 'drawing moved');
  assert(Array.from(figure.shadowGeometry.attributes.position.array).some((value, i) => Math.abs(value - stillShadow[i]) > 1e-6), 'shadow moved');
  figure.animate(camera, null);
  deepEqual(Array.from(figure.geometry.attributes.position.array), still, 'restored');
  figure.dispose();
});

test('limits: contacts on the podium, figure inside the screen margins', () => {
  setSizing('body');
  try {
    for (const [width, height] of VIEWPORTS) {
      for (const shift of CAMERA_SHIFTS) {
        const mapping = groundMapping(makeCamera(frameCamera, width, height, shift).camera);
        for (const base of BODY_POSES) {
          for (const adjust of [1, 2]) {
            const pose = { ...base, scale_adjust: adjust };
            const p = fitPlacement(pose, mapping);
            const [x1, y1, x2, y2] = pose.box;
            const key = `${width}x${height}/${shift}/${pose.file}×${adjust}`;
            assert(p.scale > 0, `${key} scale`);
            assert(p.maxContactRadius <= SUPPORT_RADIUS + 1e-9, `${key} contacts on the podium`);
            assert(mapping.centerX + (x1 - p.cx) * p.scale >= -width * 0.42 - 1e-6, `${key} left edge`);
            assert(mapping.centerX + (x2 - p.cx) * p.scale <= width * 0.42 + 1e-6, `${key} right edge`);
            assert(mapping.centerY + (p.cy - y1) * p.scale <= height * 0.43 + 1e-6, `${key} top edge`);
            if (adjust === 2) assert(p.limited, `${key} doubled size must be limited`);
          }
        }
      }
    }
  } finally {
    setSizing('file');
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
  // A hidden browser pane may never deliver it: send one ourselves.
  win.dispatchEvent(new win.Event('resize'));
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

run();
