// Computes placement numbers through the given scene modules (the refactored ones
// in web/scene, or the frozen pre-refactor copy in ./baseline). tests/web/expected.js
// was produced by this function from the ORIGINAL code (placement.test.html?record=1).
import * as THREE from 'three';
import { FIXTURE_POSES } from './fixtures.js';

export const VIEWPORTS = [
  [1920, 1080],
  [1280, 720],
  [1536, 864],
  [480, 720],
];

// Parallax offsets the render loop can produce (pointer near a corner + breathing).
const CAMERA_SHIFTS = [
  [0, 0],
  [0.07, -0.006],
];

function round(value) {
  return Number(value.toPrecision(10));
}

export function makeCamera(frameCamera, width, height, shift = [0, 0]) {
  const camera = new THREE.PerspectiveCamera(30, 1, 0.05, 250);
  const framing = frameCamera(camera, width, height);
  camera.position.x = framing.base.x + framing.distance * shift[0];
  camera.position.y = framing.base.y + framing.distance * shift[1];
  camera.lookAt(framing.target);
  camera.updateMatrixWorld();
  return { camera, framing };
}

// modules: {frameCamera, groundMapping, fitPlacement, Figure}
export function measure(modules, { exact = false } = {}) {
  const { frameCamera, groundMapping, fitPlacement, Figure } = modules;
  const r = exact ? (value) => value : round;
  const out = [];
  for (const [width, height] of VIEWPORTS) {
    for (const shift of CAMERA_SHIFTS) {
      const { camera } = makeCamera(frameCamera, width, height, shift);
      const mapping = groundMapping(camera);
      const scene = new THREE.Scene();
      const overlay = new THREE.Scene();
      for (const pose of FIXTURE_POSES) {
        const placement = fitPlacement(pose, mapping);
        const figure = new Figure(pose, new THREE.Texture(), scene, overlay);
        figure.fit(camera);
        const positions = figure.shadowGeometry.attributes.position;
        let sum = 0;
        let weighted = 0;
        for (let i = 0; i < positions.array.length; i++) {
          sum += positions.array[i];
          weighted += positions.array[i] * ((i % 7) + 1);
        }
        out.push({
          key: `${width}x${height}/${shift.join(',')}/${pose.file}`,
          mapping: [mapping.centerX, mapping.centerY, mapping.diameter].map(r),
          scale: r(placement.scale),
          x: r(placement.x),
          y: r(placement.y),
          maxContactRadius: r(placement.maxContactRadius),
          support: [placement.cx, placement.cy, placement.threshold].map(r),
          mesh: [figure.mesh.scale.x, figure.mesh.scale.y, figure.group.position.x, figure.group.position.y].map(r),
          shadow: [sum, weighted, positions.getX(0), positions.getY(64), positions.getZ(200)].map(r),
          // Exact mode also compares every shadow vertex.
          vertices: exact ? Array.from(positions.array) : undefined,
        });
        figure.dispose();
      }
    }
  }
  return out;
}
