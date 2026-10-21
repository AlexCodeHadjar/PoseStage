import * as THREE from 'three';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

export const RADIUS = 2;
export const TOP = 0.4;
export const ANCHOR_Z = -0.12;

// A horizontal plane with a radial black gradient: cheap fake contact/ambient shadow.
function softDisc(opacity, width, depth, y) {
  const canvas = document.createElement('canvas');
  canvas.width = 128;
  canvas.height = 128;
  const context = canvas.getContext('2d');
  const gradient = context.createRadialGradient(64, 64, 4, 64, 64, 64);
  gradient.addColorStop(0, `rgba(0,0,0,${opacity})`);
  gradient.addColorStop(0.4, `rgba(0,0,0,${opacity * 0.6})`);
  gradient.addColorStop(1, 'rgba(0,0,0,0)');
  context.fillStyle = gradient;
  context.fillRect(0, 0, 128, 128);

  const map = new THREE.CanvasTexture(canvas);
  const material = new THREE.MeshBasicMaterial({
    map,
    transparent: true,
    depthWrite: false,
    toneMapped: false,
  });
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(width, depth), material);
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = y;
  return mesh;
}

// Lathe profile of the podium: flat bottom, rounded lower and upper bevels, flat top.
function podiumProfile() {
  const bevel = 0.055;
  const profile = [new THREE.Vector2(0, 0.006), new THREE.Vector2(1.94, 0.006)];
  for (let i = 0; i <= 8; i++) {
    const a = -Math.PI / 2 + (i * Math.PI) / 16;
    profile.push(new THREE.Vector2(
      RADIUS - bevel + bevel * Math.cos(a),
      0.006 + bevel + bevel * Math.sin(a),
    ));
  }
  profile.push(new THREE.Vector2(RADIUS, TOP - bevel));
  for (let i = 0; i <= 8; i++) {
    const a = (i * Math.PI) / 16;
    profile.push(new THREE.Vector2(
      RADIUS - bevel + bevel * Math.cos(a),
      TOP - bevel + bevel * Math.sin(a),
    ));
  }
  profile.push(new THREE.Vector2(0, TOP));
  return profile;
}

export function createStage(renderer) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color('#292c31');
  // Fog shares the background color, avoiding a visible horizon line.
  scene.fog = new THREE.Fog('#292c31', 11, 28);

  const pmrem = new THREE.PMREMGenerator(renderer);
  const room = new RoomEnvironment();
  const env = pmrem.fromScene(room, 0.04);
  scene.environment = env.texture;
  room.dispose();
  pmrem.dispose();
  scene.environmentIntensity = 0.5;

  const floorMaterial = new THREE.MeshStandardMaterial({
    color: '#030405',
    roughness: 0.94,
    metalness: 0,
    envMapIntensity: 0.06,
  });
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(200, 200), floorMaterial);
  floor.rotation.x = -Math.PI / 2;
  floor.receiveShadow = true;
  scene.add(floor);
  const groundShadow = softDisc(0.5, 6.3, 5.5, 0.006);
  scene.add(groundShadow);

  const podium = new THREE.Group();
  scene.add(podium);
  const ceramic = new THREE.MeshPhysicalMaterial({
    color: '#f4f4f2',
    roughness: 0.29,
    metalness: 0,
    clearcoat: 0.85,
    clearcoatRoughness: 0.14,
    envMapIntensity: 0.8,
  });
  const body = new THREE.Mesh(new THREE.LatheGeometry(podiumProfile(), 160), ceramic);
  body.castShadow = true;
  body.receiveShadow = true;
  podium.add(body);

  // Eight faint vertical seams make the podium rotation readable.
  const seamMaterial = new THREE.LineBasicMaterial({ color: '#a8a9aa', transparent: true, opacity: 0.22 });
  for (let i = 0; i < 8; i++) {
    const a = (i * Math.PI) / 4;
    const points = [0.055, 0.34, 0.378].map((y) => new THREE.Vector3(
      Math.sin(a) * (RADIUS + 0.001),
      y,
      Math.cos(a) * (RADIUS + 0.001),
    ));
    podium.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(points), seamMaterial));
  }

  const contact = softDisc(0.29, 0.55, 0.32, TOP + 0.002);
  contact.position.z = ANCHOR_Z;
  scene.add(contact);

  const key = new THREE.DirectionalLight('#fff7ec', 2.8);
  key.position.set(-3.5, 8, 4);
  key.castShadow = true;
  Object.assign(key.shadow.camera, { left: -5, right: 5, top: 6, bottom: -3, near: 0.1, far: 25 });
  key.shadow.mapSize.set(2048, 2048);
  key.shadow.bias = -0.00012;
  key.shadow.normalBias = 0.012;
  key.shadow.radius = 4;
  key.target.position.set(0, 0.7, 0);
  scene.add(key, key.target);

  const fill = new THREE.DirectionalLight('#e4ecff', 0.9);
  fill.position.set(5, 3, 5);
  scene.add(fill);

  const rim = new THREE.DirectionalLight('#ffffff', 2.3);
  rim.position.set(1, 6, -4);
  scene.add(rim);

  scene.add(new THREE.AmbientLight('#cbd3e0', 0.15));
  const backdrop = createBackdrop(scene, renderer, { floor, groundShadow });
  return { scene, podium, contact, env, backdrop };
}

// Places the camera so the podium spans 48% of the viewport width, seen slightly from above.
export function frameCamera(camera, width, height) {
  camera.userData.viewport = [width, height];
  camera.aspect = width / height;
  const tangent = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
  const distance = (RADIUS * 2) / (0.48 * 2 * tangent * camera.aspect);
  const targetY = TOP + 2 * distance * tangent * 0.265;
  const target = new THREE.Vector3(0, targetY, 0);
  camera.position.set(0, targetY + distance * Math.sin(0.112), distance * Math.cos(0.112));
  camera.lookAt(target);
  camera.updateProjectionMatrix();
  camera.updateMatrixWorld();
  return { distance, target, base: camera.position.clone() };
}

// --- Camera orbit (parallax) ------------------------------------------------
// Orbits the camera around framing.target: `yaw` around the vertical axis, `pitch` added to
// the base elevation (radians). Zero angles give exactly the frameCamera() pose.
export function orbitCamera(camera, framing, yaw, pitch) {
  const { target, base, distance } = framing;
  const elevation = Math.atan2(base.y - target.y, base.z - target.z) + pitch;
  const flat = Math.cos(elevation) * distance;
  camera.position.set(
    target.x + Math.sin(yaw) * flat,
    target.y + Math.sin(elevation) * distance,
    target.z + Math.cos(yaw) * flat,
  );
  camera.lookAt(target);
}

// --- Background image ("backdrop") ------------------------------------------
// A large textured plane far behind the podium: it gets real parallax from the moving camera.
// It writes alpha 0, which the output pass (post.js) uses to skip tone mapping, and the finish
// pass to skip the studio gradient, so the picture keeps its own colors.
export const BACKDROP_Z = -20;
const BACKDROP_TEXTURE_SIDE = 4096;
const BLUR_CANVAS_SIDE = 1024; // a blurred picture needs no more detail than this
const STUDIO_LINEAR = new THREE.Color('#292c31'); // under transparent pixels of a PNG

const backdropShader = {
  vertexShader: /* glsl */ `
    varying vec2 vUv;
    void main() {
      vUv = uv;
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }
  `,
  fragmentShader: /* glsl */ `
    uniform sampler2D map;
    uniform float brightness;
    uniform vec3 under;
    varying vec2 vUv;
    void main() {
      vec4 t = texture2D(map, vUv);
      gl_FragColor = vec4(mix(under, t.rgb, t.a) * brightness, 0.0);
    }
  `,
};

// Blurred copy of `image` on a canvas (CSS filter blur), edges padded so they do not fade.
function blurredCanvas(image, blur) {
  const w = image.naturalWidth || image.width;
  const h = image.naturalHeight || image.height;
  const scale = Math.min(1, BLUR_CANVAS_SIDE / Math.max(w, h));
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(w * scale));
  canvas.height = Math.max(1, Math.round(h * scale));
  const radius = (blur * Math.max(canvas.width, canvas.height)) / 1280;
  const pad = radius * 2.5;
  const context = canvas.getContext('2d');
  context.filter = `blur(${radius.toFixed(2)}px)`;
  context.drawImage(image, -pad, -pad, canvas.width + 2 * pad, canvas.height + 2 * pad);
  return canvas;
}

function createBackdrop(scene, renderer, { floor, groundShadow }) {
  const material = new THREE.ShaderMaterial({
    uniforms: {
      map: { value: null },
      brightness: { value: 1 },
      under: { value: STUDIO_LINEAR.clone() },
    },
    vertexShader: backdropShader.vertexShader,
    fragmentShader: backdropShader.fragmentShader,
    blending: THREE.NoBlending,
    depthWrite: false,
    fog: false,
  });
  const plane = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), material);
  plane.visible = false;
  plane.renderOrder = -1;
  plane.frustumCulled = false;
  scene.add(plane);

  // Without the dark floor the podium's key-light shadow lands on this invisible catcher.
  const catcher = new THREE.Mesh(
    new THREE.PlaneGeometry(24, 24),
    new THREE.ShadowMaterial({ opacity: 0.42, fog: false, depthWrite: false }),
  );
  catcher.rotation.x = -Math.PI / 2;
  catcher.position.y = 0.001;
  catcher.receiveShadow = true;
  catcher.visible = false;
  scene.add(catcher);

  const studioFog = scene.fog;
  const groundOpacity = groundShadow.material.opacity;
  const anisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
  const maxSide = Math.min(BACKDROP_TEXTURE_SIDE, renderer.capabilities.maxTextureSize);
  let image = null;
  let blur = 0;
  let texture = null;
  let aspect = 1;
  let fitArgs = null;

  function makeTexture() {
    texture?.dispose();
    texture = null;
    if (!image) return;
    const w = image.naturalWidth || image.width;
    const h = image.naturalHeight || image.height;
    let source = image;
    if (blur > 0.01) {
      source = blurredCanvas(image, blur);
    } else if (Math.max(w, h) > maxSide) {
      source = blurredCanvas(image, 0); // only downscales (GPU limit)
    }
    texture = source instanceof HTMLCanvasElement ? new THREE.CanvasTexture(source) : new THREE.Texture(source);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.anisotropy = anisotropy;
    texture.generateMipmaps = true;
    texture.minFilter = THREE.LinearMipmapLinearFilter;
    texture.needsUpdate = true;
    material.uniforms.map.value = texture;
    aspect = w / h;
  }

  // Plane size: covers every view the camera can take (all parallax extremes), with the
  // picture "cover"-fitted at its own aspect.
  const probe = new THREE.PerspectiveCamera();
  const ray = new THREE.Vector3();
  function fit(camera, framing, maxYaw, maxPitch) {
    fitArgs = [camera, framing, maxYaw, maxPitch];
    if (!image) return;
    probe.fov = camera.fov;
    probe.aspect = camera.aspect;
    probe.near = camera.near;
    probe.far = camera.far;
    probe.updateProjectionMatrix();
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const yaw of [-maxYaw, 0, maxYaw]) {
      for (const pitch of [-maxPitch, 0, maxPitch]) {
        orbitCamera(probe, framing, yaw, pitch);
        probe.updateMatrixWorld();
        for (const [nx, ny] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) {
          ray.set(nx, ny, 0.5).unproject(probe).sub(probe.position);
          if (ray.z >= -1e-6) continue;
          const t = (BACKDROP_Z - probe.position.z) / ray.z;
          const x = probe.position.x + ray.x * t;
          const y = probe.position.y + ray.y * t;
          minX = Math.min(minX, x); maxX = Math.max(maxX, x);
          minY = Math.min(minY, y); maxY = Math.max(maxY, y);
        }
      }
    }
    const width = (maxX - minX) * 1.04;
    const height = (maxY - minY) * 1.04;
    const cover = aspect > width / height ? [height * aspect, height] : [width, width / aspect];
    plane.scale.set(cover[0], cover[1], 1);
    plane.position.set((minX + maxX) / 2, (minY + maxY) / 2, BACKDROP_Z);
  }

  function apply() {
    const on = !!image;
    plane.visible = on;
    catcher.visible = on;
    floor.visible = !on;
    scene.fog = on ? null : studioFog;
    groundShadow.material.opacity = on ? groundOpacity * 0.8 : groundOpacity;
    if (on && fitArgs) fit(...fitArgs);
  }

  return {
    get active() {
      return !!image;
    },
    // `img` (a decoded HTMLImageElement) or null for the studio.
    setImage(img) {
      if (img === image) return;
      image = img || null;
      makeTexture();
      apply();
    },
    setBlur(value) {
      const next = Math.min(24, Math.max(0, Number(value) || 0));
      if (Math.abs(next - blur) < 0.01) return;
      blur = next;
      if (image) makeTexture();
    },
    setDim(value) {
      material.uniforms.brightness.value = 1 - Math.min(0.85, Math.max(0, Number(value) || 0));
    },
    // All at once: the texture is rebuilt at most once.
    configure(img, blurValue, dimValue) {
      this.setDim(dimValue);
      const next = Math.min(24, Math.max(0, Number(blurValue) || 0));
      if ((img || null) === image) {
        this.setBlur(next);
        return;
      }
      blur = next;
      image = img || null;
      makeTexture();
      apply();
    },
    fit,
  };
}
