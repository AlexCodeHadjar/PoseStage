// Shared test geometry: viewports, parallax camera shifts and a framed studio camera.
import * as THREE from 'three';

export const VIEWPORTS = [
  [1920, 1080],
  [1280, 720],
  [1536, 864],
  [480, 720],
];

// Parallax offsets the render loop can produce (pointer near a corner + breathing).
export const CAMERA_SHIFTS = [
  [0, 0],
  [0.07, -0.006],
];

export function makeCamera(frameCamera, width, height, shift = [0, 0]) {
  const camera = new THREE.PerspectiveCamera(30, 1, 0.05, 250);
  const framing = frameCamera(camera, width, height);
  camera.position.x = framing.base.x + framing.distance * shift[0];
  camera.position.y = framing.base.y + framing.distance * shift[1];
  camera.lookAt(framing.target);
  camera.updateMatrixWorld();
  return { camera, framing };
}
