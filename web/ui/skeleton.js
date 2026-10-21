// Skeleton editor (J): drag the pose's joints to correct the automatic detection. The idle
// animation bends the drawing around these joints; corrections are saved per pose
// (api.set_joints → %APPDATA%\PoseStage\joint_overrides.json) and win over the detection.
import { estimateJoints } from '../scene/idle.js';

const GROUP = 'Оживление';
const SVG = 'http://www.w3.org/2000/svg';
const POINTS = [
  ['nose', null, 'Голова'],
  ['shoulders', 0, 'Плечо (левое на рисунке)'],
  ['shoulders', 1, 'Плечо (правое на рисунке)'],
  ['elbows', 0, 'Локоть'],
  ['elbows', 1, 'Локоть'],
  ['wrists', 0, 'Кисть'],
  ['wrists', 1, 'Кисть'],
  ['hips', 0, 'Таз'],
  ['hips', 1, 'Таз'],
];
const SOURCE_NAMES = {
  user: 'исправлен вами',
  bundled: 'найден автоматически',
  estimate: 'оценён по силуэту — поправьте точки',
};

export function install(app) {
  let editing = null; // {index, file, joints, history, wasIdle, source}
  let dragging = null;

  const layer = document.createElementNS(SVG, 'svg');
  layer.classList.add('skeleton-layer');
  layer.style.display = 'none'; // SVG elements have no `hidden` property
  const bones = document.createElementNS(SVG, 'g');
  const handles = document.createElementNS(SVG, 'g');
  layer.append(bones, handles);

  const panel = document.createElement('div');
  panel.className = 'skeleton-panel';
  panel.hidden = true;
  const title = document.createElement('div');
  title.className = 'skeleton-title';
  const hint = document.createElement('div');
  hint.className = 'skeleton-hint';
  hint.textContent = 'Перетаскивайте точки суставов. Ctrl+Z — шаг назад.';
  const buttons = document.createElement('div');
  buttons.className = 'skeleton-buttons';
  const button = (text, label, handler, primary = false) => {
    const element = document.createElement('button');
    element.type = 'button';
    element.textContent = text;
    element.title = label;
    if (primary) element.classList.add('primary');
    element.addEventListener('click', handler);
    buttons.append(element);
    return element;
  };
  button('Сохранить', 'Сохранить скелет (Enter)', () => save(), true);
  const resetButton = button('Сбросить к авто', 'Вернуть автоматически найденный скелет', () => reset());
  button('Отмена', 'Выйти без сохранения (Esc)', () => close());
  panel.append(title, hint, buttons);
  document.body.append(layer, panel);

  const toastNode = document.createElement('div');
  toastNode.className = 'skeleton-toast';
  toastNode.hidden = true;
  document.body.append(toastNode);
  let toastTimer = 0;
  function toast(text) {
    toastNode.textContent = text;
    toastNode.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toastNode.hidden = true), 2600);
  }

  const api = () => (typeof app.api?.set_joints === 'function' ? app.api : null);
  const report = (error) => {
    console.error(error);
    try {
      app.api?.report_error?.(`Скелет: ${error?.stack || error}`);
    } catch {
      // the bridge itself failed: nothing more to do
    }
  };

  // --- Coordinates: source px <-> screen CSS px (zoom, pan and mirror aware) ---------
  function frame() {
    const pose = app.current();
    const rect = app.view.figureRect();
    if (!pose || !rect) return null;
    const [x1, y1, x2, y2] = pose.box;
    const flip = app.view.canvas.classList.contains('artist-flip');
    return { rect, x1, y1, x2, y2, flip };
  }
  function toScreen(f, [x, y]) {
    const u = (x - f.x1) / (f.x2 - f.x1);
    return [f.rect.left + (f.flip ? 1 - u : u) * f.rect.width, f.rect.top + ((y - f.y1) / (f.y2 - f.y1)) * f.rect.height];
  }
  function toSource(f, sx, sy) {
    let u = (sx - f.rect.left) / f.rect.width;
    if (f.flip) u = 1 - u;
    return [f.x1 + u * (f.x2 - f.x1), f.y1 + ((sy - f.rect.top) / f.rect.height) * (f.y2 - f.y1)];
  }
  const get = (joints, [name, side]) => (side === null ? joints[name] : joints[name][side]);
  function set(joints, [name, side], value) {
    if (side === null) joints[name] = value;
    else joints[name][side] = value;
  }

  // --- Drawing: persistent SVG nodes, only their coordinates change -------------------
  const BONES = [
    [['nose', null], 'neck', 'spine'],
    [['shoulders', 0], ['shoulders', 1], 'spine'],
    [['hips', 0], ['hips', 1], 'spine'],
    [['shoulders', 0], ['hips', 0], 'side-a'],
    [['shoulders', 1], ['hips', 1], 'side-b'],
    [['shoulders', 0], ['elbows', 0], 'side-a'],
    [['elbows', 0], ['wrists', 0], 'side-a'],
    [['shoulders', 1], ['elbows', 1], 'side-b'],
    [['elbows', 1], ['wrists', 1], 'side-b'],
  ];
  const boneNodes = BONES.map(([, , kind]) => {
    const line = document.createElementNS(SVG, 'line');
    line.setAttribute('class', `bone ${kind}`);
    bones.append(line);
    return line;
  });
  const handleNodes = POINTS.map((key, i) => {
    const circle = document.createElementNS(SVG, 'circle');
    const kind = key[1] === null ? 'spine' : key[1] === 0 ? 'side-a' : 'side-b';
    circle.setAttribute('class', `joint ${kind}`);
    circle.setAttribute('r', key[0] === 'nose' ? 11 : 9);
    circle.dataset.index = String(i);
    const label = document.createElementNS(SVG, 'title');
    label.textContent = key[2];
    circle.append(label);
    handles.append(circle);
    return circle;
  });
  let drawn = '';

  function draw() {
    if (!editing) return;
    const f = frame();
    if (!f) return;
    const j = editing.joints;
    // Redraw only when the figure moved on screen or a joint changed.
    const signature = JSON.stringify([f.rect.left, f.rect.top, f.rect.width, f.rect.height, f.flip, j]);
    if (signature === drawn) return;
    drawn = signature;
    const neck = [(j.shoulders[0][0] + j.shoulders[1][0]) / 2, (j.shoulders[0][1] + j.shoulders[1][1]) / 2];
    const at = (key) => toScreen(f, key === 'neck' ? neck : get(j, key));
    BONES.forEach(([a, b], i) => {
      const [ax, ay] = at(a);
      const [bx, by] = at(b);
      boneNodes[i].setAttribute('x1', ax);
      boneNodes[i].setAttribute('y1', ay);
      boneNodes[i].setAttribute('x2', bx);
      boneNodes[i].setAttribute('y2', by);
    });
    POINTS.forEach((key, i) => {
      const [x, y] = at(key);
      handleNodes[i].setAttribute('cx', x);
      handleNodes[i].setAttribute('cy', y);
    });
  }

  // Dragging: the circle keeps the pointer capture (it is never replaced); any release,
  // cancel or lost capture anywhere ends the drag, so a joint can never stick to the cursor.
  handles.addEventListener('pointerdown', (event) => {
    const index = Number(event.target.dataset?.index);
    if (!editing || !Number.isInteger(index) || event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    dragging = { index, pointer: event.pointerId, node: event.target };
    editing.history.push(structuredClone(editing.joints));
    try {
      event.target.setPointerCapture(event.pointerId);
    } catch {
      // no live pointer (synthetic event): window-level listeners still end the drag
    }
  });
  window.addEventListener('pointermove', (event) => {
    if (!dragging || event.pointerId !== dragging.pointer) return;
    if (!(event.buttons & 1)) {
      endDrag();
      return;
    }
    const f = frame();
    if (!f) return;
    const [x, y] = toSource(f, event.clientX, event.clientY);
    set(editing.joints, POINTS[dragging.index], [Math.round(x * 10) / 10, Math.round(y * 10) / 10]);
    editing.dirty = true;
    draw();
  }, true);
  function endDrag() {
    if (dragging?.node?.hasPointerCapture?.(dragging.pointer)) dragging.node.releasePointerCapture(dragging.pointer);
    dragging = null;
  }
  window.addEventListener('pointerup', () => dragging && endDrag(), true);
  window.addEventListener('pointercancel', () => dragging && endDrag(), true);
  handles.addEventListener('lostpointercapture', () => dragging && endDrag());

  // --- Edit session -----------------------------------------------------------------
  function open() {
    const pose = app.current();
    if (!pose) return;
    if (!api()) {
      toast('Правка скелета недоступна');
      return;
    }
    const source = pose.joints ? pose.joints_source : 'estimate';
    editing = {
      index: app.index(),
      file: pose.file,
      joints: structuredClone(pose.joints || estimateJoints(pose)),
      history: [],
      dirty: !pose.joints, // an estimated skeleton is worth saving as is
      wasIdle: app.view.idle?.() ?? false,
      source,
    };
    if (editing.wasIdle) app.view.setIdle(false); // still drawing while the points are moved
    title.textContent = `Скелет: ${SOURCE_NAMES[source] || source}`;
    resetButton.disabled = source !== 'user';
    layer.style.display = '';
    panel.hidden = false;
    document.body.classList.add('skeleton-editing');
    app.overlays.open('skeleton', () => close());
    drawn = '';
    draw();
  }

  function close() {
    if (!editing) return;
    const wasIdle = editing.wasIdle;
    endDrag();
    editing = null;
    layer.style.display = 'none';
    panel.hidden = true;
    document.body.classList.remove('skeleton-editing');
    app.overlays.close('skeleton');
    if (wasIdle) app.view.setIdle(true);
  }

  async function apply(joints, message) {
    const session = editing;
    try {
      const pose = await api().set_joints(session.file, joints);
      if (pose && Array.isArray(pose.box)) {
        const index = app.poses().findIndex((item) => item.file === session.file);
        if (index >= 0) app.updatePose(index, pose);
      }
      if (editing === session) close();
      toast(message);
    } catch (error) {
      report(error);
      toast('Не удалось сохранить скелет');
    }
  }

  function save() {
    if (!editing) return;
    if (!editing.dirty) {
      close();
      return;
    }
    apply(editing.joints, 'Скелет сохранён — оживление использует ваши точки');
  }

  function reset() {
    if (editing?.source === 'user') apply(null, 'Скелет: вернулся автоматический');
  }

  function undo() {
    const previous = editing?.history.pop();
    if (previous) {
      editing.joints = previous;
      draw();
    }
  }

  // Keys while editing: the core ignores its own shortcuts under an overlay; Esc closes
  // through app.overlays.
  window.addEventListener('keydown', (event) => {
    if (!editing) return;
    if (event.code === 'Enter' || event.code === 'NumpadEnter') {
      event.preventDefault();
      save();
    } else if (event.code === 'KeyZ' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      undo();
    }
  }, true);

  app.view.onFrame(() => {
    if (editing) draw();
  });
  window.addEventListener('resize', () => editing && draw());
  app.on('pose', () => {
    if (editing && app.current()?.file !== editing.file) close();
  });

  app.shortcuts.register({
    keys: ['KeyJ'],
    title: 'Скелет позы: поправить суставы для оживления',
    group: GROUP,
    handler: (event) => {
      if (event?.repeat) return;
      if (editing) close();
      else open();
    },
  });
}
