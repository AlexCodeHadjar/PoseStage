// Skeleton editor (J), skeleton version 2 (docs/SKELETON.md): 16 points from the head down,
// coloured by body part — head white; chest, navel, pelvis pink; arms red; legs green (the
// figure's left side lighter). Points can be visible, hidden (behind the body: dashed) or
// absent (out of the picture: a grey tag by the parent), and pinned as a support (anchor).
// Dragging a point moves the points below it in the chain (Alt: only that point). Saved per
// pose via api.set_joints → %APPDATA%\PoseStage\joint_overrides.json; the idle animation uses it.
import { estimateJoints, templateJoints, JOINT_AXIS, JOINT_IDS, JOINT_PARENT } from '../scene/idle.js';
import { supportContacts } from '../scene/placement.js';

const GROUP = 'Оживление';
const SVG = 'http://www.w3.org/2000/svg';
const COLORS = {
  head: '#f4f4f4',
  pink: '#ff8fc8',
  red_l: '#ff8a8a',
  red_r: '#e83a3a',
  green_l: '#7deba8',
  green_r: '#2fbf6b',
};
const NAMES = {
  head: 'Голова', chest: 'Грудь', navel: 'Пупок', pelvis: 'Таз',
  shoulder: 'Плечо', elbow: 'Локоть', wrist: 'Кисть', hip: 'Бедро', knee: 'Колено', foot: 'Стопа',
};
const STATE_NAMES = { visible: 'видна', hidden: 'скрыта', absent: 'нет в кадре' };
const VIEW_NAMES = { front: 'анфас', three_quarter: 'три четверти', side: 'сбоку', back: 'спиной' };
const SOURCE_NAMES = { user: 'исправлен вами', bundled: 'найден автоматически', estimate: 'шаблон — поправьте точки' };
const TEMPLATES = [['front', 'Анфас'], ['side', 'Сбоку'], ['back', 'Спиной'], ['sit', 'Сидя'], ['lie', 'Лёжа']];

const colorOf = (id) => {
  if (id === 'head') return COLORS.head;
  if (JOINT_AXIS.includes(id)) return COLORS.pink;
  const side = id.endsWith('_l') ? 'l' : 'r';
  return ['shoulder', 'elbow', 'wrist'].includes(id.split('_')[0]) ? COLORS[`red_${side}`] : COLORS[`green_${side}`];
};
const labelOf = (id) => {
  const [name, side] = id.split('_');
  return side ? `${NAMES[name]} (${side === 'l' ? 'Л' : 'П'})` : NAMES[name];
};
const CHILDREN = Object.fromEntries(JOINT_IDS.map((id) => [id, JOINT_IDS.filter((other) => JOINT_PARENT[other] === id)]));
const descendants = (id) => CHILDREN[id].flatMap((child) => [child, ...descendants(child)]);

export function install(app) {
  let editing = null; // {file, joints, history, dirty, wasIdle, source}
  let dragging = null;
  let selected = null;

  // --- DOM ------------------------------------------------------------------------
  const layer = document.createElementNS(SVG, 'svg');
  layer.classList.add('skeleton-layer');
  layer.style.display = 'none'; // SVG elements have no `hidden` property
  const bones = document.createElementNS(SVG, 'g');
  const anchors = document.createElementNS(SVG, 'g');
  const handles = document.createElementNS(SVG, 'g');
  layer.append(bones, anchors, handles);

  const panel = document.createElement('div');
  panel.className = 'skeleton-panel';
  panel.hidden = true;
  const title = document.createElement('div');
  title.className = 'skeleton-title';
  const hint = document.createElement('div');
  hint.className = 'skeleton-hint';
  const legend = document.createElement('div');
  legend.className = 'skeleton-legend';
  for (const [color, text] of [[COLORS.head, 'голова'], [COLORS.pink, 'корпус'], [COLORS.red_r, 'руки'], [COLORS.green_r, 'ноги']]) {
    const item = document.createElement('span');
    const dot = document.createElement('i');
    dot.className = 'skeleton-dot';
    dot.style.background = color; // CSSOM: allowed by the page's CSP
    item.append(dot, text);
    legend.append(item);
  }
  const templates = document.createElement('div');
  templates.className = 'skeleton-buttons skeleton-templates';
  for (const [kind, text] of TEMPLATES) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = text;
    button.title = `Расставить точки по шаблону «${text}»`;
    button.addEventListener('click', () => applyTemplate(kind));
    templates.append(button);
  }
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
  panel.append(title, legend, hint, templates, buttons);

  const menu = document.createElement('div');
  menu.className = 'skeleton-menu';
  menu.hidden = true;
  const menuItems = {};
  for (const [key, text] of [['visible', 'Видна'], ['hidden', 'Скрыта (за телом)'], ['absent', 'Нет в кадре'], ['support', 'Опора на подиуме']]) {
    const item = document.createElement('button');
    item.type = 'button';
    item.textContent = text;
    item.addEventListener('click', () => {
      if (menu.dataset.point) setState(menu.dataset.point, key);
      hideMenu();
    });
    menuItems[key] = item;
    menu.append(item);
  }

  const toastNode = document.createElement('div');
  toastNode.className = 'skeleton-toast';
  toastNode.hidden = true;
  document.body.append(layer, panel, menu, toastNode);
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

  // --- Coordinates: source px <-> screen CSS px (zoom, pan and mirror aware) -----------
  function frame() {
    const pose = app.current();
    const rect = app.view.figureRect();
    if (!pose || !rect) return null;
    const [x1, y1, x2, y2] = pose.box;
    return { rect, x1, y1, x2, y2, flip: app.view.canvas.classList.contains('artist-flip') };
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
  const point = (id) => editing.joints.points[id];
  const round = (value) => Math.round(value * 10) / 10;
  // Where an absent point's grey tag sits: next to its nearest placed ancestor.
  function tagPosition(id) {
    let parent = JOINT_PARENT[id];
    while (parent && !point(parent)?.xy) parent = JOINT_PARENT[parent];
    const base = parent ? point(parent).xy : point('pelvis').xy;
    const side = id.endsWith('_l') ? 1 : -1;
    const pose = app.current();
    const step = 0.03 * (pose.box[3] - pose.box[1]);
    return [base[0] + side * step, base[1] + step];
  }
  // Effective support: explicit flag, else automatic from the lower contour.
  function isSupport(id) {
    const p = point(id);
    if (!p?.xy || p.state === 'absent') return false;
    if (typeof p.support === 'boolean') return p.support;
    if (JOINT_AXIS.includes(id) && id !== 'pelvis') return false;
    const pose = app.current();
    const pin = 0.06 * (pose.box[3] - pose.box[1]);
    return supportContacts(pose).some(([cx, cy]) => Math.hypot(cx - p.xy[0], cy - p.xy[1]) < pin);
  }

  // --- Drawing: persistent SVG nodes, attributes only -------------------------------
  const boneNodes = JOINT_IDS.filter((id) => JOINT_PARENT[id]).map((id) => {
    const line = document.createElementNS(SVG, 'line');
    line.setAttribute('class', 'bone');
    line.setAttribute('stroke', colorOf(id));
    bones.append(line);
    return [id, line];
  });
  const anchorNodes = Object.fromEntries(JOINT_IDS.map((id) => {
    const path = document.createElementNS(SVG, 'path');
    path.setAttribute('class', 'anchor');
    anchors.append(path);
    return [id, path];
  }));
  const handleNodes = Object.fromEntries(JOINT_IDS.map((id) => {
    const circle = document.createElementNS(SVG, 'circle');
    circle.setAttribute('class', 'joint');
    circle.setAttribute('fill', colorOf(id));
    circle.dataset.id = id;
    const label = document.createElementNS(SVG, 'title');
    circle.append(label);
    handles.append(circle);
    return [id, circle];
  }));
  let drawn = '';

  function draw(force = false) {
    if (!editing) return;
    const f = frame();
    if (!f) return;
    const signature = JSON.stringify([f.rect.left, f.rect.top, f.rect.width, f.rect.height, f.flip, selected, editing.joints]);
    if (!force && signature === drawn) return;
    drawn = signature;
    for (const [id, line] of boneNodes) {
      const a = point(JOINT_PARENT[id]);
      const b = point(id);
      const show = a?.xy && b?.xy && a.state !== 'absent' && b.state !== 'absent';
      line.style.display = show ? '' : 'none';
      if (!show) continue;
      const [ax, ay] = toScreen(f, a.xy);
      const [bx, by] = toScreen(f, b.xy);
      line.setAttribute('x1', ax);
      line.setAttribute('y1', ay);
      line.setAttribute('x2', bx);
      line.setAttribute('y2', by);
      line.classList.toggle('hidden-bone', a.state === 'hidden' || b.state === 'hidden');
    }
    for (const id of JOINT_IDS) {
      const p = point(id);
      const circle = handleNodes[id];
      const absent = !p || p.state === 'absent' || !p.xy;
      const [x, y] = toScreen(f, absent ? tagPosition(id) : p.xy);
      circle.setAttribute('cx', x);
      circle.setAttribute('cy', y);
      circle.setAttribute('r', absent ? 5 : id === 'head' ? 11 : JOINT_AXIS.includes(id) ? 9 : 8);
      circle.setAttribute('fill', absent ? '#8b8f96' : colorOf(id));
      circle.classList.toggle('hidden-joint', p?.state === 'hidden');
      circle.classList.toggle('absent-joint', absent);
      circle.classList.toggle('selected', selected === id);
      circle.firstChild.textContent = `${labelOf(id)}: ${STATE_NAMES[absent ? 'absent' : p.state]}${isSupport(id) ? ', опора' : ''}`;
      const anchor = anchorNodes[id];
      if (!absent && isSupport(id)) {
        anchor.setAttribute('d', `M${x - 6} ${y + 15}h12l-6 -6z`);
        anchor.style.display = '';
      } else {
        anchor.style.display = 'none';
      }
    }
    // Side views: the shoulders nearly coincide.
    const [sl, sr, chest, pelvis] = ['shoulder_l', 'shoulder_r', 'chest', 'pelvis'].map((id) => point(id)?.xy);
    const torso = chest && pelvis ? Math.hypot(chest[0] - pelvis[0], chest[1] - pelvis[1]) : 0;
    const side = sl && sr && torso && Math.hypot(sl[0] - sr[0], sl[1] - sr[1]) < 0.25 * torso;
    hint.textContent = side && editing.joints.view !== 'side'
      ? 'Похоже на позу сбоку — попробуйте шаблон «Сбоку» и скройте дальние руку и ногу (H).'
      : 'Тяните точки (Alt — только одну). Правая кнопка — состояние. H — скрыть, Delete — нет в кадре, Ctrl+Z — шаг назад.';
  }

  // --- Editing operations -------------------------------------------------------------
  function remember() {
    editing.history.push(structuredClone(editing.joints));
    editing.dirty = true;
  }
  function setState(id, state) {
    const p = point(id);
    if (!p) return;
    remember();
    if (state === 'support') {
      p.support = !isSupport(id);
    } else if (state === 'absent') {
      if (JOINT_AXIS.includes(id)) {
        toast('Голова, грудь, пупок и таз нужны всегда');
        editing.history.pop();
        return;
      }
      p.state = 'absent';
      delete p.xy;
      delete p.support;
    } else {
      if (!p.xy) p.xy = tagPosition(id).map(round);
      p.state = state;
    }
    draw(true);
  }
  function applyTemplate(kind) {
    remember();
    editing.joints = templateJoints(app.current(), kind);
    draw(true);
    toast(`Шаблон «${TEMPLATES.find(([k]) => k === kind)[1]}»: подвиньте точки по рисунку`);
  }

  // --- Dragging ---------------------------------------------------------------------
  handles.addEventListener('pointerdown', (event) => {
    const id = event.target.dataset?.id;
    if (!editing || !id) return;
    event.preventDefault();
    event.stopPropagation();
    hideMenu();
    selected = id;
    if (event.button === 2) {
      showMenu(id, event.clientX, event.clientY);
      draw(true);
      return;
    }
    if (event.button !== 0) return;
    const f = frame();
    if (!f) return;
    const p = point(id);
    const start = toSource(f, event.clientX, event.clientY);
    const moving = event.altKey ? [id] : [id, ...descendants(id)];
    remember();
    if (p.state === 'absent' || !p.xy) {
      p.state = 'visible'; // dragging a grey tag brings the point back
      p.xy = tagPosition(id).map(round);
    }
    dragging = {
      pointer: event.pointerId,
      node: event.target,
      start,
      origins: moving.filter((key) => point(key)?.xy).map((key) => [key, [...point(key).xy]]),
    };
    try {
      event.target.setPointerCapture(event.pointerId);
    } catch {
      // no live pointer (synthetic event): window-level listeners still end the drag
    }
    draw(true);
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
    const dx = x - dragging.start[0];
    const dy = y - dragging.start[1];
    for (const [key, [ox, oy]] of dragging.origins) point(key).xy = [round(ox + dx), round(oy + dy)];
    draw();
  }, true);
  function endDrag() {
    if (dragging?.node?.hasPointerCapture?.(dragging.pointer)) dragging.node.releasePointerCapture(dragging.pointer);
    dragging = null;
  }
  window.addEventListener('pointerup', () => dragging && endDrag(), true);
  window.addEventListener('pointercancel', () => dragging && endDrag(), true);
  handles.addEventListener('lostpointercapture', () => dragging && endDrag());
  handles.addEventListener('contextmenu', (event) => event.preventDefault());

  // --- Context menu -------------------------------------------------------------------
  function showMenu(id, x, y) {
    const p = point(id);
    const state = p?.xy ? p.state : 'absent';
    menu.dataset.point = id;
    for (const key of ['visible', 'hidden', 'absent']) menuItems[key].classList.toggle('on', state === key);
    menuItems.absent.disabled = JOINT_AXIS.includes(id);
    menuItems.support.classList.toggle('on', isSupport(id));
    menuItems.support.disabled = state === 'absent';
    menu.hidden = false;
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    menu.style.left = `${Math.max(8, Math.min(x, innerWidth - width - 8))}px`;
    menu.style.top = `${Math.max(8, Math.min(y, innerHeight - height - 8))}px`;
  }
  function hideMenu() {
    menu.hidden = true;
    delete menu.dataset.point;
  }
  window.addEventListener('pointerdown', (event) => {
    if (!menu.hidden && !menu.contains(event.target)) hideMenu();
  }, true);

  // --- Session ----------------------------------------------------------------------
  function open() {
    const pose = app.current();
    if (!pose) return;
    if (!api()) {
      toast('Правка скелета недоступна');
      return;
    }
    const source = pose.joints?.version === 2 ? pose.joints_source : 'estimate';
    editing = {
      file: pose.file,
      joints: structuredClone(pose.joints?.version === 2 ? pose.joints : estimateJoints(pose)),
      history: [],
      dirty: source === 'estimate', // a template is worth saving as is
      wasIdle: app.view.idle?.() ?? false,
      source,
    };
    selected = null;
    if (editing.wasIdle) app.view.setIdle(false); // still drawing while the points are moved
    title.textContent = `Скелет: ${SOURCE_NAMES[source] || source}, вид — ${VIEW_NAMES[editing.joints.view] || editing.joints.view}`;
    resetButton.disabled = source !== 'user';
    layer.style.display = '';
    panel.hidden = false;
    document.body.classList.add('skeleton-editing');
    app.overlays.open('skeleton', () => close());
    draw(true);
  }

  function close() {
    if (!editing) return;
    const wasIdle = editing.wasIdle;
    endDrag();
    hideMenu();
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
      draw(true);
    }
  }

  // Keys while editing come first (capture) so H hides a point instead of mirroring.
  window.addEventListener('keydown', (event) => {
    if (!editing) return;
    // Typing in a field (e.g. the gallery's search) keeps its keys.
    const target = event.target;
    if (target?.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target?.tagName)) return;
    const take = () => {
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    if (event.code === 'Enter' || event.code === 'NumpadEnter') {
      take();
      save();
    } else if (event.code === 'KeyZ' && (event.ctrlKey || event.metaKey)) {
      take();
      undo();
    } else if (event.code === 'KeyH' && !event.ctrlKey && !event.altKey) {
      take();
      if (selected) setState(selected, point(selected)?.state === 'hidden' ? 'visible' : 'hidden');
      else toast('Сначала выберите точку');
    } else if (event.code === 'Delete' || event.code === 'Backspace') {
      take();
      if (selected) setState(selected, 'absent');
    }
  }, true);

  app.view.onFrame(() => {
    if (editing) draw();
  });
  window.addEventListener('resize', () => editing && draw(true));
  app.on('pose', () => {
    if (editing && app.current()?.file !== editing.file) close();
  });

  app.shortcuts.register({
    keys: ['KeyJ'],
    title: 'Скелет позы: поправить точки для оживления',
    group: GROUP,
    handler: (event) => {
      if (event?.repeat) return;
      if (editing) close();
      else open();
    },
  });
}
