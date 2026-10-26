// «Поза»: posing a marked-up figure (docs/RIG.md) inside the «Разметка» editor.
// The pose is solved and the mesh deformed by the backend (rig.py through pose_eval / commands):
// this module only draws the vertex positions it gets (WebGL: every part a layer, in the
// layer order) and turns pointer gestures into pose commands, one undo step per gesture.

const TOOLS = [
  ['rotate', 'Поворот', 'Тянуть сустав: кость, которая к нему ведёт, поворачивается вокруг своего начала', 'R'],
  ['ik', 'Дотянуться', 'Тянуть кисть, стопу, кончик пальца, голову: цепочка сгибается сама (IK)', 'I'],
  ['root', 'Сдвиг', 'Сдвинуть всю фигуру; опоры удерживают свои суставы', 'M'],
  ['contact', 'Опора', 'Клик по суставу: закрепить его на месте / снять', 'C'],
  ['weights', 'Веса', 'Выбрать кость кликом, кистью добавить ей влияние (Alt — убрать)', 'W'],
  ['surface', 'Дорисовка', 'Рисовать на выбранной дорисовке (Alt — стирать)', 'P'],
  ['view', 'Просмотр', 'Двигать и приближать', 'V'],
];
const END_CHAINS = { head: 'head' };
for (const [s, side] of [['l', 'left'], ['r', 'right']]) {
  END_CHAINS[`wrist_${s}`] = `${side}_arm`;
  END_CHAINS[`foot_${s}`] = `${side}_leg`;
  END_CHAINS[`elbow_${s}`] = `${side}_upper_arm`;
  END_CHAINS[`knee_${s}`] = `${side}_thigh`;
  for (const f of ['thumb', 'index', 'middle', 'ring', 'pinky']) END_CHAINS[`${side}_hand.${f}.tip`] = `${side}_hand.${f}`;
}

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

function numberInput(value, step = 1) {
  const input = el('input', 'parts-number');
  input.type = 'number';
  input.step = String(step);
  input.value = value === null || value === undefined ? '' : String(value);
  return input;
}

// --- WebGL: the posed figure, one draw per part ---------------------------------------------------
const VS = `#version 300 es
in vec2 aPos; in vec2 aUv;
uniform vec4 uView; uniform vec2 uCanvas;
out vec2 vUv;
void main() {
  vec2 p = (aPos * uView.x + uView.yz) * uView.w;
  vec2 c = p / uCanvas * 2.0 - 1.0;
  gl_Position = vec4(c.x, -c.y, 0.0, 1.0);
  vUv = aUv;
}`;
const FS = `#version 300 es
precision highp float;
in vec2 vUv;
uniform sampler2D uSource; uniform sampler2D uLabels; uniform vec2 uSize; uniform float uPart; uniform vec4 uSolid;
out vec4 color;
void main() {
  if (uSolid.a > 0.0) { color = vec4(uSolid.rgb * uSolid.a, uSolid.a); return; }
  if (uPart >= 0.0) {
    ivec2 t = ivec2(clamp(floor(vUv), vec2(0.0), uSize - 1.0));
    float l = floor(texelFetch(uLabels, t, 0).r * 255.0 + 0.5);
    if (abs(l - uPart) > 0.5) discard;
  }
  vec4 c = texture(uSource, vUv / uSize);
  color = vec4(c.rgb * c.a, c.a);
}`;

class MeshView {
  constructor(canvas) {
    this.canvas = canvas;
    const gl = canvas.getContext('webgl2', { premultipliedAlpha: true, alpha: true, antialias: true });
    this.gl = gl;
    if (!gl) return;
    const shader = (type, text) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, text);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const program = gl.createProgram();
    gl.attachShader(program, shader(gl.VERTEX_SHADER, VS));
    gl.attachShader(program, shader(gl.FRAGMENT_SHADER, FS));
    gl.linkProgram(program);
    this.program = program;
    this.loc = Object.fromEntries(['uView', 'uCanvas', 'uSource', 'uLabels', 'uSize', 'uPart', 'uSolid']
      .map((n) => [n, gl.getUniformLocation(program, n)]));
    this.aPos = gl.getAttribLocation(program, 'aPos');
    this.aUv = gl.getAttribLocation(program, 'aUv');
    this.parts = [];
    this.surfaces = new Map();
    this.quad = this.buffers(new Float32Array(8), new Float32Array(8), new Uint16Array([0, 1, 2, 0, 2, 3]));
  }

  texture(image, smooth) {
    const gl = this.gl;
    const t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, false);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, smooth ? gl.LINEAR : gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, smooth ? gl.LINEAR : gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  }

  setImages(source, labels) {
    if (!this.gl) return;
    const gl = this.gl;
    if (this.source) gl.deleteTexture(this.source);
    if (this.labels) gl.deleteTexture(this.labels);
    this.source = this.texture(source, true);
    this.labels = this.texture(labels, false);
    this.size = [source.naturalWidth, source.naturalHeight];
  }

  setSurface(id, image) {
    if (!this.gl) return;
    const old = this.surfaces.get(id);
    if (old) this.gl.deleteTexture(old);
    this.surfaces.set(id, this.texture(image, true));
  }

  buffers(pos, uv, index) {
    const gl = this.gl;
    const vao = gl.createVertexArray();
    gl.bindVertexArray(vao);
    const posBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, posBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, pos, gl.DYNAMIC_DRAW);
    gl.enableVertexAttribArray(this.aPos);
    gl.vertexAttribPointer(this.aPos, 2, gl.FLOAT, false, 0, 0);
    const uvBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, uvBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, uv, gl.STATIC_DRAW);
    gl.enableVertexAttribArray(this.aUv);
    gl.vertexAttribPointer(this.aUv, 2, gl.FLOAT, false, 0, 0);
    const indexBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, indexBuffer);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, index, gl.STATIC_DRAW);
    gl.bindVertexArray(null);
    return { vao, posBuffer, uvBuffer, indexBuffer, count: index.length };
  }

  setMesh(mesh) {
    if (!this.gl) return;
    for (const part of this.parts) {
      this.gl.deleteVertexArray(part.vao);
      this.gl.deleteBuffer(part.posBuffer);
      this.gl.deleteBuffer(part.uvBuffer);
      this.gl.deleteBuffer(part.indexBuffer);
    }
    this.parts = mesh.parts.map((part) => {
      const uv = new Float32Array(part.verts.length * 2);
      part.verts.forEach((v, k) => {
        uv[2 * k] = mesh.vertices[2 * v];
        uv[2 * k + 1] = mesh.vertices[2 * v + 1];
      });
      const index = new Uint32Array(part.cells.length * 6);
      part.cells.forEach(([a, b, c, d], k) => index.set([a, b, c, a, c, d], 6 * k));
      return { value: part.value, part: part.part, ...this.buffers(uv.slice(), uv, index) };
    });
  }

  setPositions(parts) {
    if (!this.gl) return;
    const gl = this.gl;
    for (const part of this.parts) {
      const flat = parts[String(part.value)];
      if (!flat) continue;
      gl.bindBuffer(gl.ARRAY_BUFFER, part.posBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(flat), gl.DYNAMIC_DRAW);
    }
  }

  draw(view, surfaces, transforms) {
    const gl = this.gl;
    if (!gl || !this.source) return;
    const dpr = window.devicePixelRatio || 1;
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.clearColor(0, 0, 0, 0);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(this.program);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.uniform4f(this.loc.uView, view.s, view.ox, view.oy, dpr);
    gl.uniform2f(this.loc.uCanvas, this.canvas.width, this.canvas.height);
    gl.uniform2f(this.loc.uSize, this.size[0], this.size[1]);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.source);
    gl.uniform1i(this.loc.uSource, 0);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.labels);
    gl.uniform1i(this.loc.uLabels, 1);
    const [w, h] = this.size;
    // the picture's area, as in the markup view
    this.quadAt([[0, 0], [w, 0], [w, h], [0, h]], [0, 0, w, 0, w, h, 0, h]);
    gl.uniform4f(this.loc.uSolid, 0.914, 0.914, 0.914, 1);
    gl.bindVertexArray(this.quad.vao);
    gl.drawElements(gl.TRIANGLES, 6, gl.UNSIGNED_SHORT, 0);
    gl.uniform4f(this.loc.uSolid, 0, 0, 0, 0);
    const byPart = new Map();
    for (const s of surfaces) {
      if (!byPart.has(s.part)) byPart.set(s.part, []);
      byPart.get(s.part).push(s);
    }
    for (const part of this.parts) {
      for (const s of (byPart.get(part.part) || []).filter((x) => !x.above)) this.drawSurface(s, transforms);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, this.source);
      gl.uniform1f(this.loc.uPart, part.value);
      gl.bindVertexArray(part.vao);
      gl.drawElements(gl.TRIANGLES, part.count, gl.UNSIGNED_INT, 0);
      for (const s of (byPart.get(part.part) || []).filter((x) => x.above)) this.drawSurface(s, transforms);
    }
    gl.bindVertexArray(null);
  }

  quadAt(points, uv) {
    const gl = this.gl;
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quad.posBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(points.flat()), gl.DYNAMIC_DRAW);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quad.uvBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(uv), gl.DYNAMIC_DRAW);
  }

  drawSurface(surface, transforms) {
    const gl = this.gl;
    const texture = this.surfaces.get(surface.id);
    const t = transforms[surface.bone] || [1, 0, 0, 1, 0, 0];
    if (!texture) return;
    const [w, h] = this.size;
    const map = ([x, y]) => [t[0] * x + t[1] * y + t[4], t[2] * x + t[3] * y + t[5]];
    this.quadAt([[0, 0], [w, 0], [w, h], [0, h]].map(map), [0, 0, w, 0, w, h, 0, h]);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, texture);
    gl.uniform1f(this.loc.uPart, -1);
    gl.bindVertexArray(this.quad.vao);
    gl.drawElements(gl.TRIANGLES, 6, gl.UNSIGNED_SHORT, 0);
  }
}

// --- The posing mode ---------------------------------------------------------------------------------
export function createPose(env) {
  let rig = null; // get_rig
  let mesh = null;
  let state = null; // pose_eval of the document's pose
  let live = null; // pose_eval of a gesture in progress
  let meshKey = null;
  let tool = 'rotate';
  let selectedBone = null;
  let selectedSurface = null;
  let exact = false; // show the backend's reference render instead of WebGL
  let showSkeleton = true;
  let showRest = false;
  let issues = null;
  let gesture = null;
  let pending = null; // pose waiting to be evaluated
  let evaluating = false;
  let radius = 14;
  let color = '#d9c4b3';
  const images = { labels: null, posed: null, weights: null, surfaces: new Map() };
  // Timeline (timeline.py): the pose at `now`; gestures write keys there while `record` is on.
  let anim = null;
  let now = 0;
  let playing = false;
  let record = false;
  let playStart = 0;
  let exportJob = null;
  let checked = null;

  const glCanvas = el('canvas', 'parts-gl');
  const view3d = new MeshView(glCanvas);
  const toolbar = el('div', 'parts-pose-toolbar');
  const toolBar = el('div', 'parts-tools');
  const toolButtons = TOOLS.map(([id, label, hint, key]) => {
    const node = button('parts-chip', label, `${hint} (${key})`);
    node.addEventListener('click', () => setTool(id));
    toolBar.append(node);
    return [id, node];
  });
  const exactLabel = el('label', 'parts-check');
  const exactInput = el('input');
  exactInput.type = 'checkbox';
  exactLabel.append(exactInput, ' Точная отрисовка');
  exactLabel.title = 'Картинка так, как её сохранит экспорт (рисует Python)';
  const skeletonLabel = el('label', 'parts-check');
  const skeletonInput = el('input');
  skeletonInput.type = 'checkbox';
  skeletonInput.checked = true;
  skeletonLabel.append(skeletonInput, ' Скелет');
  const restLabel = el('label', 'parts-check');
  const restInput = el('input');
  restInput.type = 'checkbox';
  restLabel.append(restInput, ' Исходник');
  restLabel.title = 'Исходный рисунок бледно под позой';
  const sizeLabel = el('label', 'parts-range');
  const sizeInput = el('input');
  sizeInput.type = 'range';
  sizeInput.min = '2';
  sizeInput.max = '80';
  sizeInput.value = String(radius);
  sizeLabel.append('Кисть ', sizeInput);
  const colorInput = el('input', 'parts-color');
  colorInput.type = 'color';
  colorInput.value = color;
  const resetButton = button('parts-btn', 'Сбросить позу', 'Вернуть позу покоя (одно действие, отменяется)');
  const validateButton = button('parts-btn', 'Проверить риг', 'Кости, цели, опоры, ограничения, открывшиеся места');
  toolbar.append(toolBar, exactLabel, skeletonLabel, restLabel, sizeLabel, colorInput, resetButton, validateButton);

  // --- Timeline bar ---
  const timelineBar = el('div', 'parts-timeline');
  const playButton = button('parts-btn', '▶', 'Проиграть / пауза (пробел)');
  const timeInput = el('input', 'parts-time');
  timeInput.type = 'range';
  timeInput.min = '0';
  timeInput.step = '0.001';
  const timeLabel = el('span', 'parts-time-label', '0,00 с');
  const keyStrip = el('div', 'parts-keys');
  const recordLabel = el('label', 'parts-check');
  const recordInput = el('input');
  recordInput.type = 'checkbox';
  recordLabel.append(recordInput, ' Запись ключей');
  recordLabel.title = 'Повороты, цели и сдвиг ставят ключи в текущий момент, а не меняют статическую позу';
  const keyButton = button('parts-btn', 'Ключ позы', 'Запомнить всю текущую позу ключами в этот момент (K)');
  const deleteKeyButton = button('parts-btn', 'Удалить ключ', 'Ключ выбранной кости в этот момент');
  const durationInput = numberInput(2, 0.1);
  durationInput.title = 'Длительность, с';
  const fpsSelect = el('select', 'parts-select');
  for (const v of [12, 15, 24, 25, 30]) {
    const option = el('option', '', `${v} к/с`);
    option.value = String(v);
    fpsSelect.append(option);
  }
  const loopLabel = el('label', 'parts-check');
  const loopInput = el('input');
  loopInput.type = 'checkbox';
  loopLabel.append(loopInput, ' Цикл');
  const checkButton = button('parts-btn', 'Проверить кадры', 'Где цели не достигнуты, опоры скользят, повороты упёрлись в ограничения');
  const exportButton = button('parts-btn', 'Кадры PNG…', 'Экспорт кадров анимации в папку (тот же расчёт, что превью)');
  const exportProgress = el('span', 'parts-muted');
  const timeWrap = el('div', 'parts-time-wrap');
  timeWrap.append(timeInput, keyStrip);
  timelineBar.append(playButton, timeWrap, timeLabel, recordLabel, keyButton, deleteKeyButton, durationInput, fpsSelect, loopLabel,
    checkButton, exportButton, exportProgress);

  const animated = () => !!anim && Object.keys(anim.tracks || {}).length > 0;
  const frameStep = () => 1 / (anim?.fps || 24);

  function renderTimeline() {
    if (!anim) return;
    timeInput.max = String(anim.duration);
    timeInput.value = String(now);
    timeLabel.textContent = `${now.toFixed(2).replace('.', ',')} / ${Number(anim.duration).toFixed(2).replace('.', ',')} с`;
    durationInput.value = String(anim.duration);
    fpsSelect.value = String(anim.fps);
    loopInput.checked = !!anim.loop;
    playButton.textContent = playing ? '⏸' : '▶';
    keyStrip.replaceChildren();
    const tracks = anim.tracks || {};
    const own = selectedBone ? tracks[`rot:${selectedBone}`] || [] : [];
    const times = new Map();
    for (const keys of Object.values(tracks)) for (const k of keys) times.set(k.t, (times.get(k.t) || 0) + 1);
    for (const [t] of times) {
      const mark = el('span', `parts-key${own.some((k) => Math.abs(k.t - t) < 1e-6) ? ' own' : ''}`);
      mark.style.left = `${(100 * t) / anim.duration}%`;
      mark.title = `${t} с`;
      mark.addEventListener('click', () => seek(t));
      keyStrip.append(mark);
    }
    deleteKeyButton.disabled = !own.some((k) => Math.abs(k.t - now) < 1e-3);
  }

  async function seek(t) {
    now = Math.max(0, Math.min(anim?.duration || 0, t));
    renderTimeline();
    if (animated()) await evaluateNow();
  }

  async function evaluateNow() {
    state = await env.call('pose_eval', { instance_id: env.iid(), time: animated() ? now : null });
    view3d.setPositions(state.parts);
    if (exact) await loadPosed();
    env.draw();
  }

  timeInput.addEventListener('input', () => seek(Number(timeInput.value)));
  recordInput.addEventListener('change', () => { record = recordInput.checked; });
  playButton.addEventListener('click', () => togglePlay());
  keyButton.addEventListener('click', () => env.run('key_pose', { time: round3(now), instance_id: env.iid() }));
  deleteKeyButton.addEventListener('click', () => {
    if (selectedBone) env.run('delete_keyframe', { track_id: `rot:${selectedBone}`, time: round3(now), instance_id: env.iid() });
  });
  durationInput.addEventListener('change', () => env.run('set_animation', { duration: Number(durationInput.value), instance_id: env.iid() }));
  fpsSelect.addEventListener('change', () => env.run('set_animation', { fps: Number(fpsSelect.value), instance_id: env.iid() }));
  loopInput.addEventListener('change', () => env.run('set_animation', { loop: loopInput.checked, instance_id: env.iid() }));
  checkButton.addEventListener('click', async () => {
    try {
      checked = await env.call('check_animation', { instance_id: env.iid() });
      env.renderPanels();
    } catch (error) {
      env.toast(String(error.message || error), true);
    }
  });
  exportButton.addEventListener('click', async () => {
    const reply = await env.api().parts_export_frames(env.pid(), {});
    if (!reply) return;
    if (reply.error) {
      env.toast(reply.error, true);
      return;
    }
    exportJob = reply;
    watchExport();
  });

  async function watchExport() {
    if (!exportJob) return;
    exportJob = await env.api().parts_job(exportJob.job_id);
    if (['queued', 'running'].includes(exportJob.status)) {
      exportProgress.textContent = `${Math.round(exportJob.progress * 100)}% · ${exportJob.stage || ''}`;
      setTimeout(watchExport, 300);
      return;
    }
    exportProgress.textContent = '';
    if (exportJob.status === 'done') env.toast(`Кадров: ${exportJob.result.frames} → ${exportJob.result.folder}`);
    else if (exportJob.status === 'error') env.toast(exportJob.error || 'Экспорт не удался', true);
    exportJob = null;
  }

  const round3 = (v) => Math.round(v * 1000) / 1000;

  function togglePlay() {
    if (!animated()) {
      env.toast('Нет ключей: включите «Запись ключей» и поставьте позы в разные моменты, или «Ключ позы»');
      return;
    }
    playing = !playing;
    renderTimeline();
    if (playing) {
      playStart = performance.now() - now * 1000;
      playTick();
    }
  }

  // A timer at the animation's fps (not requestAnimationFrame: it keeps time in a background window too).
  async function playTick() {
    if (!playing) return;
    const stamp = performance.now();
    let t = (stamp - playStart) / 1000;
    if (t > anim.duration) {
      if (anim.loop) {
        t %= anim.duration;
        playStart = stamp - t * 1000;
      } else {
        playing = false;
        t = anim.duration;
      }
    }
    now = t;
    renderTimeline();
    try {
      await evaluateNow(); // the backend evaluates; a slow frame is skipped, time keeps going
    } catch (error) {
      playing = false;
      env.toast(String(error.message || error), true);
    }
    if (playing) setTimeout(playTick, Math.max(0, frameStep() * 1000 - (performance.now() - stamp)));
  }

  exactInput.addEventListener('change', async () => {
    exact = exactInput.checked;
    if (exact) await loadPosed();
    env.draw();
  });
  skeletonInput.addEventListener('change', () => { showSkeleton = skeletonInput.checked; env.draw(); });
  restInput.addEventListener('change', () => { showRest = restInput.checked; env.draw(); });
  sizeInput.addEventListener('input', () => { radius = Number(sizeInput.value); });
  colorInput.addEventListener('input', () => { color = colorInput.value; });
  resetButton.addEventListener('click', () => env.run('reset_pose', { instance_id: env.iid() }));
  validateButton.addEventListener('click', async () => {
    try {
      issues = (await env.call('validate_rig', { instance_id: env.iid() })).issues;
      env.renderPanels();
    } catch (error) {
      env.toast(String(error.message || error), true);
    }
  });

  function setTool(id, redraw = true) {
    tool = id;
    for (const [t, node] of toolButtons) node.classList.toggle('on', t === tool);
    sizeLabel.hidden = !['weights', 'surface'].includes(tool);
    colorInput.hidden = tool !== 'surface';
    env.canvas.dataset.tool = { rotate: 'joints', ik: 'joints', root: 'view', contact: 'joints', weights: 'brush', surface: 'brush', view: 'view' }[tool];
    if (!redraw) return;
    if (tool === 'weights' && selectedBone) loadWeights();
    env.draw();
  }

  const restView = () => tool === 'weights' || tool === 'surface';

  // --- Data -------------------------------------------------------------------------------------
  async function refresh() {
    if (!env.pid()) return;
    const iid = env.iid();
    rig = await env.call('get_rig', { instance_id: iid });
    const key = JSON.stringify([rig.mesh_key, rig.layers]);
    if (key !== meshKey || !mesh) {
      mesh = await env.call('pose_mesh', { instance_id: iid });
      images.labels = await env.loadImage(env.layerUrl('rig_labels'));
      view3d.setImages(env.sourceImage(), images.labels);
      view3d.setMesh(mesh);
      meshKey = key;
    }
    for (const s of rig.surfaces) {
      const image = await env.loadImage(env.layerUrl(`s_${s.id}`));
      images.surfaces.set(s.id, image);
      view3d.setSurface(s.id, image);
    }
    if (selectedSurface && !rig.surfaces.some((s) => s.id === selectedSurface)) selectedSurface = null;
    anim = await env.call('get_animation', { instance_id: iid });
    if (now > anim.duration) now = anim.duration;
    state = await env.call('pose_eval', { instance_id: iid, time: animated() ? now : null });
    view3d.setPositions(state.parts);
    renderTimeline();
    if (exact) await loadPosed();
    if (tool === 'weights' && selectedBone) await loadWeights();
    env.renderPanels();
    env.draw();
  }

  async function loadPosed() {
    images.posed = await env.loadImage(env.layerUrl('posed'));
  }

  async function loadWeights() {
    images.weights = selectedBone ? await env.loadImage(env.layerUrl(`w_${selectedBone}`)) : null;
    env.draw();
  }

  function poseOf() {
    // the pose on screen: at `now` when animated, else the static pose
    const used = animated() && state?.pose ? state.pose : rig?.pose;
    return JSON.parse(JSON.stringify(used || { root: [0, 0], rotations: {}, ik: {} }));
  }

  // Evaluate a gesture's pose without changing the document; one request in flight.
  function preview(pose) {
    pending = pose;
    if (evaluating) return;
    evaluating = true;
    (async () => {
      while (pending) {
        const next = pending;
        pending = null;
        try {
          live = await env.call('pose_eval', { instance_id: env.iid(), pose: next, time: animated() ? now : null });
          view3d.setPositions(live.parts);
          env.draw();
        } catch (error) {
          env.toast(String(error.message || error), true);
        }
      }
      evaluating = false;
    })();
  }

  const current = () => live || state;

  // --- Drawing (2D canvas over the WebGL one) ----------------------------------------------------
  function surfacesForGl() {
    return (rig?.surfaces || []).filter((s) => s.enabled).map((s) => ({
      id: s.id, part: s.part_id, above: s.above, bone: s.bone_id || (rig.bindings[s.part_id] || [])[0],
    }));
  }

  function transforms() {
    return current()?.transforms || {};
  }

  function draw2d(ctx, view) {
    const dpr = window.devicePixelRatio || 1;
    const source = env.sourceImage();
    if (!source || !rig) return;
    const restMode = restView();
    glCanvas.style.visibility = exact || restMode ? 'hidden' : 'visible';
    if (!exact && !restMode) view3d.draw(view, surfacesForGl(), transforms());
    ctx.setTransform(dpr * view.s, 0, 0, dpr * view.s, dpr * view.ox, dpr * view.oy);
    const [w, h] = [source.naturalWidth, source.naturalHeight];
    if (exact || restMode) {
      ctx.fillStyle = '#e9e9e9';
      ctx.fillRect(0, 0, w, h);
    }
    if (restMode) {
      ctx.drawImage(source, 0, 0);
      if (tool === 'weights' && images.weights) ctx.drawImage(images.weights, 0, 0);
      if (tool === 'surface') {
        ctx.globalAlpha = 0.8;
        for (const s of rig.surfaces) {
          if (s.enabled && images.surfaces.get(s.id)) ctx.drawImage(images.surfaces.get(s.id), 0, 0);
        }
        ctx.globalAlpha = 1;
      }
    } else {
      if (exact && images.posed) ctx.drawImage(images.posed, 0, 0);
      if (showRest) {
        ctx.globalAlpha = 0.3;
        ctx.drawImage(source, 0, 0);
        ctx.globalAlpha = 1;
      }
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (showSkeleton) drawSkeleton(ctx, restMode);
    drawContacts(ctx);
    drawGesture(ctx);
  }

  function boneSegments(restMode) {
    if (restMode || !current()) {
      return Object.entries(rig.bones).map(([id, b]) => [id, [...b.head, ...b.tail]]);
    }
    return Object.entries(current().bones);
  }

  function drawSkeleton(ctx, restMode) {
    const toScreen = env.toScreen;
    ctx.lineCap = 'round';
    for (const [id, seg] of boneSegments(restMode)) {
      const a = toScreen(seg[0], seg[1]);
      const b = toScreen(seg[2], seg[3]);
      const fine = id.includes('_hand.');
      ctx.strokeStyle = id === selectedBone ? '#ffb000' : fine ? 'rgba(255,255,255,0.95)' : 'rgba(25,25,25,0.85)';
      ctx.lineWidth = fine ? 1.6 : 3;
      ctx.beginPath();
      ctx.moveTo(...a);
      ctx.lineTo(...b);
      ctx.stroke();
    }
    if (restMode) return;
    const report = current()?.report || {};
    const missed = new Set(Object.entries({ ...report.ik, ...report.contacts }).filter(([, r]) => r.miss_px > 2).map(([k]) => k));
    for (const [id, p] of Object.entries(current()?.joints || {})) {
      const [x, y] = toScreen(...p);
      const r = id.includes('_hand.') ? 3 : 5;
      const isEnd = END_CHAINS[id] !== undefined;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = missed.has(END_CHAINS[id]) ? '#ff5050' : tool === 'ik' && isEnd ? '#7fb2ff' : '#ffffff';
      ctx.fill();
      ctx.lineWidth = 1.2;
      ctx.strokeStyle = '#151515';
      ctx.stroke();
    }
    for (const [chain, target] of Object.entries((live ? gesture?.pose?.ik : rig.pose?.ik) || {})) {
      const [x, y] = toScreen(...target);
      ctx.strokeStyle = missed.has(chain) ? '#ff5050' : '#4f8cff';
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(x - 7, y);
      ctx.lineTo(x + 7, y);
      ctx.moveTo(x, y - 7);
      ctx.lineTo(x, y + 7);
      ctx.stroke();
    }
  }

  function drawContacts(ctx) {
    for (const c of rig.contacts || []) {
      if (!c.enabled) continue;
      const [x, y] = env.toScreen(...c.point);
      ctx.strokeStyle = '#2f7dff';
      ctx.lineWidth = 2;
      ctx.strokeRect(x - 6, y - 6, 12, 12);
      ctx.beginPath();
      ctx.moveTo(x - 9, y + 9);
      ctx.lineTo(x + 9, y + 9);
      ctx.stroke();
    }
  }

  function drawGesture(ctx) {
    if (!gesture || gesture.kind !== 'stroke' || !gesture.points.length) return;
    const view = env.view;
    ctx.strokeStyle = tool === 'surface' ? (gesture.erase ? '#ffffff' : color) : gesture.erase ? '#3a7bff' : '#ff4b1e';
    ctx.globalAlpha = 0.5;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.lineWidth = radius * 2 * view.s;
    ctx.beginPath();
    const first = env.toScreen(...gesture.points[0]);
    ctx.moveTo(...first);
    for (const p of gesture.points) ctx.lineTo(...env.toScreen(...p));
    if (gesture.points.length === 1) ctx.lineTo(first[0] + 0.01, first[1]);
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // --- Hit testing --------------------------------------------------------------------------------
  function jointAt(sx, sy, filter = () => true) {
    let best = null;
    let bestD = 12;
    for (const [id, p] of Object.entries(current()?.joints || {})) {
      if (!filter(id)) continue;
      const [x, y] = env.toScreen(...p);
      const d = Math.hypot(x - sx, y - sy) - (id.includes('_hand.') ? 0 : 3);
      if (d < bestD) {
        best = id;
        bestD = d;
      }
    }
    return best;
  }

  function boneAt(sx, sy, restMode) {
    let best = null;
    let bestD = 10;
    for (const [id, seg] of boneSegments(restMode)) {
      const [ax, ay] = env.toScreen(seg[0], seg[1]);
      const [bx, by] = env.toScreen(seg[2], seg[3]);
      const dx = bx - ax;
      const dy = by - ay;
      const t = Math.max(0, Math.min(1, ((sx - ax) * dx + (sy - ay) * dy) / (dx * dx + dy * dy || 1)));
      const d = Math.hypot(sx - ax - t * dx, sy - ay - t * dy);
      if (d < bestD) {
        best = id;
        bestD = d;
      }
    }
    return best;
  }

  function boneEndingAt(joint) {
    return Object.entries(rig.bones).find(([, b]) => b.tail_joint === joint)?.[0] || null;
  }

  // --- Pointer ------------------------------------------------------------------------------------
  function pointerDown(event, sx, sy, at) {
    if (!rig || tool === 'view') return false;
    const pose = poseOf();
    if (tool === 'rotate') {
      const joint = jointAt(sx, sy);
      const bone = joint && (boneEndingAt(joint) || (joint === 'head' ? 'head' : null));
      if (!bone) {
        const picked = boneAt(sx, sy, false);
        if (picked) {
          selectedBone = picked;
          env.renderPanels();
          env.draw();
        }
        return true;
      }
      selectedBone = bone;
      const seg = current().bones[bone];
      gesture = { kind: 'rotate', bone, pivot: [seg[0], seg[1]], start: at, base: pose.rotations[bone] || 0, pose };
    } else if (tool === 'ik') {
      const joint = jointAt(sx, sy, (id) => END_CHAINS[id] !== undefined);
      if (!joint) return true;
      gesture = { kind: 'ik', chain: END_CHAINS[joint], pose };
      gesture.pose.ik = { ...(gesture.pose.ik || {}), [gesture.chain]: at };
    } else if (tool === 'root') {
      gesture = { kind: 'root', start: at, base: pose.root || [0, 0], pose };
    } else if (tool === 'contact') {
      const joint = jointAt(sx, sy, (id) => rig.contact_joints.includes(id));
      if (joint) {
        const has = (rig.contacts || []).some((c) => c.joint === joint);
        env.run('set_contact', { entity_id: joint, options: has ? { remove: true } : {}, instance_id: env.iid() });
      }
      return true;
    } else if (tool === 'weights') {
      if (event.altKey === false && !selectedBone) {
        const picked = boneAt(sx, sy, true);
        if (picked) {
          selectedBone = picked;
          loadWeights();
          env.renderPanels();
        }
        return true;
      }
      if (event.detail === 2 || event.ctrlKey) { // pick another bone
        const picked = boneAt(sx, sy, true);
        if (picked) {
          selectedBone = picked;
          loadWeights();
          env.renderPanels();
        }
        return true;
      }
      gesture = { kind: 'stroke', points: [at], erase: event.altKey };
    } else if (tool === 'surface') {
      if (!selectedSurface) {
        env.toast('Выберите или создайте дорисовку в списке слева', true);
        return true;
      }
      gesture = { kind: 'stroke', points: [at], erase: event.altKey };
    }
    env.renderPanels();
    env.draw();
    return true;
  }

  function pointerMove(event, sx, sy, at) {
    if (!gesture) {
      if (rig) {
        const joint = jointAt(sx, sy);
        const bone = joint ? boneEndingAt(joint) : boneAt(sx, sy, restView());
        env.status(`${Math.round(at[0])}, ${Math.round(at[1])}${joint ? ` · сустав ${joint}` : ''}${bone ? ` · кость ${bone}` : ''}`);
      }
      return false;
    }
    if (gesture.kind === 'rotate') {
      const [px, py] = gesture.pivot;
      const a0 = Math.atan2(gesture.start[1] - py, gesture.start[0] - px);
      const a1 = Math.atan2(at[1] - py, at[0] - px);
      let delta = (a1 - a0) * 180 / Math.PI;
      delta = ((delta + 540) % 360) - 180;
      gesture.angle = Math.round((gesture.base + delta) * 10) / 10;
      gesture.pose.rotations = { ...gesture.pose.rotations, [gesture.bone]: gesture.angle };
      preview(gesture.pose);
    } else if (gesture.kind === 'ik') {
      gesture.pose.ik[gesture.chain] = [Math.round(at[0] * 10) / 10, Math.round(at[1] * 10) / 10];
      preview(gesture.pose);
    } else if (gesture.kind === 'root') {
      gesture.pose.root = [Math.round(gesture.base[0] + at[0] - gesture.start[0]), Math.round(gesture.base[1] + at[1] - gesture.start[1])];
      preview(gesture.pose);
    } else if (gesture.kind === 'stroke') {
      const last = gesture.points[gesture.points.length - 1];
      if (Math.hypot(at[0] - last[0], at[1] - last[1]) * env.view.s > 2) gesture.points.push(at);
      env.draw();
    }
    return true;
  }

  async function pointerUp() {
    const g = gesture;
    gesture = null;
    if (!g) return false;
    const iid = env.iid();
    const round = (p) => [Math.round(p[0] * 10) / 10, Math.round(p[1] * 10) / 10];
    const at = record ? { time: round3(now) } : {};
    if (g.kind === 'rotate' && g.angle !== undefined) {
      await env.run('set_joint_rotation', { bone_id: g.bone, angle: g.angle, instance_id: iid, ...at });
    } else if (g.kind === 'ik') {
      await env.run('set_ik_target', { chain_id: g.chain, position: g.pose.ik[g.chain], instance_id: iid, ...at });
    } else if (g.kind === 'root') {
      await env.run('set_root', { offset: g.pose.root, instance_id: iid, ...at });
    } else if (g.kind === 'stroke' && tool === 'weights') {
      await env.run('edit_weights', { bone_id: selectedBone, strokes: [{ points: g.points.map(round), radius }],
        operation: g.erase ? 'subtract' : 'add', amount: 0.35, instance_id: iid });
    } else if (g.kind === 'stroke' && tool === 'surface') {
      const rgbValue = [1, 3, 5].map((k) => parseInt(color.slice(k, k + 2), 16));
      await env.run('paint_surface', { surface_id: selectedSurface, strokes: [{ points: g.points.map(round), radius }],
        color: rgbValue, mode: g.erase ? 'erase' : 'paint', instance_id: iid });
    }
    live = null;
    if (state) view3d.setPositions(state.parts);
    env.draw();
    return true;
  }

  function cancel() {
    if (!gesture) return false;
    gesture = null;
    live = null;
    if (state) view3d.setPositions(state.parts);
    env.draw();
    return true;
  }

  // --- Panels ---------------------------------------------------------------------------------------
  function renderSide(container) {
    container.replaceChildren();
    if (!rig) return;
    const caps = env.caps();
    container.append(el('div', 'parts-group', 'Слои (сверху — ближе)'));
    const order = [...(rig.layers || [])].reverse();
    for (const pid of order) {
      const part = caps.parts[pid];
      if (!part || !(mesh?.parts || []).some((p) => p.part === pid)) continue;
      const row = el('div', 'parts-row');
      const swatch = el('span', 'parts-swatch');
      swatch.style.background = `rgb(${part.color.join(',')})`;
      const up = button('parts-lock on', '▲', 'Выше (ближе)');
      const down = button('parts-lock on', '▼', 'Ниже (дальше)');
      const move = (delta) => {
        const layers = [...rig.layers];
        const i = layers.indexOf(pid);
        const j = i + delta;
        if (j < 0 || j >= layers.length) return;
        [layers[i], layers[j]] = [layers[j], layers[i]];
        env.run('set_layer_order', { part_ids: layers, instance_id: env.iid() });
      };
      up.addEventListener('click', () => move(1));
      down.addEventListener('click', () => move(-1));
      row.append(swatch, el('span', 'parts-name', part.name), up, down);
      container.append(row);
    }
    container.append(el('div', 'parts-group', 'Дорисовки'));
    container.append(el('p', 'parts-muted', 'Не наблюдаемый рисунок: отдельный слой, его можно выключить.'));
    for (const s of rig.surfaces || []) {
      const row = el('div', `parts-row${s.id === selectedSurface ? ' active' : ''}`);
      const on = el('input');
      on.type = 'checkbox';
      on.checked = s.enabled;
      on.addEventListener('click', (event) => event.stopPropagation());
      on.addEventListener('change', () => env.run('set_surface', { surface_id: s.id, enabled: on.checked, instance_id: env.iid() }));
      const remove = button('parts-lock on', '✕', 'Удалить');
      remove.addEventListener('click', (event) => {
        event.stopPropagation();
        env.run('set_surface', { surface_id: s.id, remove: true, instance_id: env.iid() });
      });
      row.append(on, el('span', 'parts-name', `${s.name} · ${caps.parts[s.part_id]?.name || s.part_id}${s.above ? ' · над' : ''}`), remove);
      row.addEventListener('click', () => {
        selectedSurface = s.id;
        setTool('surface');
        renderSide(container);
      });
      container.append(row);
    }
    const partSelect = el('select', 'parts-select');
    for (const [id, p] of Object.entries(caps.parts)) {
      const option = el('option', '', p.name);
      option.value = id;
      partSelect.append(option);
    }
    partSelect.value = 'chest';
    const add = button('parts-btn', '+ Нарисовать', 'Новая пустая дорисовка под выбранной частью');
    add.addEventListener('click', async () => {
      const reply = await env.run('add_surface', { part_id: partSelect.value, instance_id: env.iid() });
      if (reply?.surface) {
        selectedSurface = reply.surface.id;
        setTool('surface');
      }
    });
    const importButton = button('parts-btn', 'Импорт…', 'Картинка того же размера, что исходник, — слой под частью');
    importButton.addEventListener('click', async () => {
      const reply = await env.api().parts_import_surface(env.pid(), partSelect.value, false);
      if (reply?.error) env.toast(reply.error, true);
      else if (reply) await env.refresh();
    });
    const addRow = el('div', 'parts-field');
    addRow.append(partSelect);
    container.append(addRow, add, importButton);
  }

  function renderDetails(container) {
    container.replaceChildren();
    if (!rig) return;
    const pose = rig.pose || {};
    const report = state?.report || {};
    if (selectedBone && rig.bones[selectedBone]) {
      const bone = rig.bones[selectedBone];
      container.append(el('h3', '', `Кость ${selectedBone}`));
      container.append(el('p', 'parts-muted', `${bone.head_joint} → ${bone.tail_joint || 'продолжение'} · ${bone.length} px`
        + ` · родитель ${bone.parent || '—'}`));
      const angle = numberInput(pose.rotations?.[selectedBone] ?? 0, 1);
      angle.addEventListener('change', () => env.run('set_joint_rotation', { bone_id: selectedBone, angle: Number(angle.value), instance_id: env.iid() }));
      container.append(field('Поворот, °', angle));
      const solved = state?.rotations?.[selectedBone];
      if (solved !== undefined && Math.abs(solved - (pose.rotations?.[selectedBone] ?? 0)) > 0.05) {
        container.append(el('p', 'parts-muted', `После IK, опор и ограничений: ${solved}°`));
      }
      const limits = rig.constraints?.[selectedBone] || {};
      const lo = numberInput(limits.min ?? '', 1);
      const hi = numberInput(limits.max ?? '', 1);
      const bend = el('select', 'parts-select');
      for (const [v, label] of [['', 'авто'], ['1', 'в одну сторону'], ['-1', 'в другую']]) {
        const option = el('option', '', label);
        option.value = v;
        bend.append(option);
      }
      bend.value = limits.bend ? String(limits.bend) : '';
      const apply = () => env.run('set_constraint', {
        bone_id: selectedBone, min: lo.value === '' ? null : Number(lo.value), max: hi.value === '' ? null : Number(hi.value),
        bend: bend.value ? Number(bend.value) : null, instance_id: env.iid(),
      });
      lo.addEventListener('change', apply);
      hi.addEventListener('change', apply);
      bend.addEventListener('change', apply);
      container.append(field('Мин., °', lo), field('Макс., °', hi), field('Сгиб (IK)', bend));
      const reset = button('parts-btn', 'Сбросить кость', 'Поворот этой кости — как в покое');
      reset.addEventListener('click', () => env.run('reset_pose', { bones: [selectedBone], instance_id: env.iid() }));
      container.append(reset);
      const parts = Object.entries(rig.bindings).filter(([, bs]) => bs.includes(selectedBone)).map(([p]) => env.caps().parts[p]?.name || p);
      if (parts.length) container.append(el('p', 'parts-muted', `Двигает: ${parts.join(', ')}`));
    } else {
      container.append(el('p', 'parts-muted', 'Кликните по кости или суставу. Поворот — тянуть сустав; «Дотянуться» — тянуть кисть, стопу или кончик пальца.'));
    }
    const lock = el('input');
    lock.type = 'checkbox';
    lock.checked = !!rig.pose_locked;
    lock.addEventListener('change', () => env.run('set_pose_locked', { locked: lock.checked, instance_id: env.iid() }));
    container.append(field('Поза зафиксирована от ИИ', lock));
    const ikEntries = Object.entries(pose.ik || {});
    if (ikEntries.length) {
      container.append(el('h3', '', 'Цели'));
      for (const [chain] of ikEntries) {
        const row = el('div', 'parts-field');
        const miss = report.ik?.[chain]?.miss_px;
        row.append(el('span', '', `${chain}${miss > 2 ? ` — промах ${miss} px` : ''}`));
        const clear = button('parts-btn subtle', 'Убрать');
        clear.addEventListener('click', () => env.run('set_ik_target', { chain_id: chain, position: null, instance_id: env.iid() }));
        row.append(clear);
        container.append(row);
      }
    }
    if ((rig.contacts || []).length) {
      container.append(el('h3', '', 'Опоры'));
      for (const c of rig.contacts) {
        const row = el('div', 'parts-field');
        const miss = report.contacts?.[c.id]?.miss_px;
        row.append(el('span', '', `${c.joint}${miss > 2 ? ` — соскальзывает на ${miss} px` : ''}`));
        const clear = button('parts-btn subtle', 'Снять');
        clear.addEventListener('click', () => env.run('set_contact', { entity_id: c.joint, options: { remove: true }, instance_id: env.iid() }));
        row.append(clear);
        container.append(row);
      }
    }
    if (issues) {
      container.append(el('h3', '', `Проверка рига: ${issues.length || 'замечаний нет'}`));
      for (const issue of issues) container.append(el('p', `parts-issue ${issue.severity}`, issue.message));
    }
    if (checked) {
      const bad = checked.frames_with_problems;
      container.append(el('h3', '', `Кадры: ${bad.length ? `${bad.length} из ${checked.checked} с замечаниями` : `все ${checked.checked} без замечаний`}`));
      for (const f of bad.slice(0, 40)) {
        const row = el('p', 'parts-issue warning link', `${f.time.toFixed(2)} с — ${f.problems.join('; ')}`);
        row.addEventListener('click', () => seek(f.time));
        container.append(row);
      }
    }
  }

  function field(label, control) {
    const row = el('label', 'parts-field');
    row.append(el('span', '', label), control);
    return row;
  }

  function key(event) {
    if (event.ctrlKey || event.metaKey || event.altKey) return false;
    if (event.code === 'Space') {
      event.preventDefault();
      togglePlay();
      return true;
    }
    if (event.code === 'KeyK') {
      keyButton.click();
      return true;
    }
    if ((event.code === 'Comma' || event.code === 'Period') && anim) {
      seek(now + (event.code === 'Period' ? frameStep() : -frameStep()));
      return true;
    }
    const found = TOOLS.find(([, , , k]) => `Key${k}` === event.code);
    if (found) {
      setTool(found[0]);
      return true;
    }
    if (event.code === 'BracketLeft' || event.code === 'BracketRight') {
      radius = Math.max(2, Math.min(80, radius + (event.code === 'BracketLeft' ? -2 : 2)));
      sizeInput.value = String(radius);
      return true;
    }
    return false;
  }

  function resize() {
    const rect = glCanvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    glCanvas.width = Math.max(1, Math.round(rect.width * dpr));
    glCanvas.height = Math.max(1, Math.round(rect.height * dpr));
  }

  setTool('rotate', false);
  return {
    glCanvas, toolbar, timeline: timelineBar, stop: () => { playing = false; renderTimeline(); }, refresh, draw2d, pointerDown, pointerMove, pointerUp, cancel, renderSide, renderDetails, key, resize,
    debug: () => ({ tool, selectedBone, gesture, joints: current()?.joints, rig }),
    available: () => !!view3d.gl, reset: () => { rig = null; mesh = null; meshKey = null; state = null; selectedBone = null; issues = null; },
  };
}
