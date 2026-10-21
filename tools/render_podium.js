// Renders the studio podium on a transparent background for the desktop (layered window)
// mode, plus the projection data desktop.py needs to place poses exactly like placement.js.
import * as THREE from 'three';
import { createStage, RADIUS, TOP } from '../web/scene/stage.js';
import { groundMapping, SUPPORT_RADIUS, STATURE_SHARE } from '../web/scene/placement.js';

const WIDTH = 1200;
const HEIGHT = 1400;
const DIAMETER_SHARE = 0.86; // podium diameter / canvas width
const ELEVATION = 0.112; // same camera elevation as frameCamera()
const BOTTOM_MARGIN = 0.035; // podium bottom edge above the canvas bottom (share of height)

const canvas = document.querySelector('#c');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(1);
renderer.setSize(WIDTH, HEIGHT, false);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.AgXToneMapping;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.setClearColor(0x000000, 0);

const studio = createStage(renderer);
const { scene } = studio;
scene.background = null;
scene.fog = null;
studio.contact.visible = false; // the desktop renderer draws each pose's own shadow
let key = null;
scene.traverse((object) => {
  if (object.isDirectionalLight && object.castShadow) key = object;
  if (!object.isMesh) return;
  if (object.material.isShadowMaterial) object.visible = true; // podium shadow catcher
  else if (object.material.isMeshStandardMaterial && !object.material.isMeshPhysicalMaterial) object.visible = false; // dark floor
  else if (object.material.isMeshBasicMaterial && object.material.map && object.position.y < 0.01) {
    object.scale.setScalar(0.62); // soft ground disc: must fade out inside the canvas
  }
});

// Frame like frameCamera(), but portrait and with the podium filling most of the width.
const camera = new THREE.PerspectiveCamera(30, WIDTH / HEIGHT, 0.05, 250);
camera.userData.viewport = [WIDTH, HEIGHT];
const tangent = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
const distance = (RADIUS * 2) / (DIAMETER_SHARE * 2 * tangent * camera.aspect);

function place(targetY) {
  const target = new THREE.Vector3(0, targetY, 0);
  camera.position.set(0, targetY + distance * Math.sin(ELEVATION), distance * Math.cos(ELEVATION));
  camera.lookAt(target);
  camera.updateProjectionMatrix();
  camera.updateMatrixWorld();
}

// Screen y (px, y up from the canvas centre) of the podium's lowest visible point.
function podiumBottom() {
  const point = new THREE.Vector3();
  let lowest = Infinity;
  for (let i = 0; i < 64; i++) {
    const a = (i / 64) * Math.PI * 2;
    point.set(Math.cos(a) * RADIUS, 0, Math.sin(a) * RADIUS).project(camera);
    lowest = Math.min(lowest, (point.y * HEIGHT) / 2);
  }
  return lowest;
}

// Raise the camera target until the podium bottom sits BOTTOM_MARGIN above the canvas edge.
let lo = TOP;
let hi = TOP + 20;
const wanted = -HEIGHT / 2 + BOTTOM_MARGIN * HEIGHT;
for (let i = 0; i < 40; i++) {
  const mid = (lo + hi) / 2;
  place(mid);
  if (podiumBottom() < wanted) hi = mid;
  else lo = mid;
}
place(lo);

renderer.render(scene, camera);

const mapping = groundMapping(camera);
const viewProjection = new THREE.Matrix4().multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse);
const light = key.position.clone().sub(key.target.position);
window.__podium = {
  png: canvas.toDataURL('image/png'),
  meta: {
    width: WIDTH,
    height: HEIGHT,
    radius: RADIUS,
    top: TOP,
    supportRadius: SUPPORT_RADIUS,
    statureShare: STATURE_SHARE,
    centerX: mapping.centerX,
    centerY: mapping.centerY,
    diameter: mapping.diameter,
    inverse: Array.from(mapping.inverse),
    viewProjection: viewProjection.elements.slice(),
    // Ground offset per unit of height for the key light's shadow (world x, z).
    shadowDirection: [-light.x / light.y, -light.z / light.y],
  },
};
if (new URLSearchParams(location.search).has('save')) {
  const response = await fetch('/save', { method: 'POST', body: JSON.stringify(window.__podium) });
  document.title = response.ok ? 'saved' : `save failed ${response.status}`;
} else {
  document.title = 'done';
}
