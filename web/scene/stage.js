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
  scene.add(softDisc(0.5, 6.3, 5.5, 0.006));

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
  return { scene, podium, contact, env };
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
