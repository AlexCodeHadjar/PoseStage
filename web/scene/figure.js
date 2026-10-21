import * as THREE from 'three';
import { dissolveMaterial } from '../shaders/dissolve.js';
import { groundMapping, fitPlacement, toGround, baselineAt, forgetPose } from './placement.js';
import { createRig } from './idle.js';

// Mesh density: the drawing bends smoothly when the idle animation moves its vertices.
const SEGMENTS_X = 24;
const SEGMENTS_Y = 36;
// Shadow strip rows: one while still (cheap: it is re-laid out whenever the camera sways),
// more while animating so the shadow can follow the bending body.
const SHADOW_ROWS_STILL = 1;
const SHADOW_ROWS_ANIMATED = 16;

function shadowPlane(pose, rows) {
  const geometry = new THREE.PlaneGeometry(1, 1, 128, rows);
  cropUV(geometry, pose);
  return geometry;
}

// Restrict a unit plane's UVs to the pose bbox (texture v grows upwards).
function cropUV(geometry, pose) {
  const [x1, y1, x2, y2] = pose.box;
  const uv = geometry.attributes.uv;
  for (let i = 0; i < uv.count; i++) {
    uv.setXY(
      i,
      (x1 + uv.getX(i) * (x2 - x1)) / pose.w,
      1 - y2 / pose.h + (uv.getY(i) * (y2 - y1)) / pose.h,
    );
  }
}

export class Figure {
  constructor(pose, texture, stageScene, overlayScene) {
    this.pose = pose;
    this.texture = texture;
    this.uniforms = {
      uReveal: { value: 1 },
      uErase: { value: 0 },
      uBounds: { value: new THREE.Vector2(1 - pose.box[3] / pose.h, 1 - pose.box[1] / pose.h) },
    };

    // Visible drawing: an unlit plane in the screen-space overlay scene.
    this.geometry = new THREE.PlaneGeometry(1, 1, SEGMENTS_X, SEGMENTS_Y);
    cropUV(this.geometry, pose);
    this.basePositions = this.geometry.attributes.position.array.slice();
    const material = new THREE.MeshBasicMaterial({
      map: texture,
      alphaTest: 0.5,
      side: THREE.DoubleSide,
      toneMapped: false,
      alphaToCoverage: true,
    });
    dissolveMaterial(material, this.uniforms, true);
    this.mesh = new THREE.Mesh(this.geometry, material);
    this.group = new THREE.Group();
    this.group.add(this.mesh);
    overlayScene.add(this.group);

    // Only the invisible shadow geometry follows the ground depths. The visible
    // PNG retains its original proportions, colors, and straight image plane.
    this.shadowGeometry = shadowPlane(pose, SHADOW_ROWS_STILL);
    const ghost = new THREE.MeshBasicMaterial({
      map: texture,
      alphaTest: 0.5,
      colorWrite: false,
      depthWrite: false,
      side: THREE.DoubleSide,
    });
    ghost.onBeforeCompile = (shader) => {
      shader.fragmentShader = 'void main(){gl_FragColor=vec4(0.0);}';
    };
    ghost.customProgramCacheKey = () => 'pose-shadow-only-caster-v1';
    this.shadow = new THREE.Mesh(this.shadowGeometry, ghost);
    this.shadow.castShadow = true;
    this.shadow.frustumCulled = false;
    this.shadow.customDepthMaterial = new THREE.MeshDepthMaterial({
      map: texture,
      alphaTest: 0.5,
      depthPacking: THREE.RGBADepthPacking,
      side: THREE.DoubleSide,
    });
    dissolveMaterial(this.shadow.customDepthMaterial, this.uniforms, false);
    stageScene.add(this.shadow);

    this.ground = new THREE.Vector3();
    this.time = null; // idle animation time in seconds, null when still
  }

  get rig() {
    if (!this._rig) {
      this._rig = createRig(this.pose);
      this._locks = new WeakMap();
    }
    return this._rig;
  }

  // Per-vertex source points (px) and pin weights of a geometry, computed once.
  vertices(geometry, toSource) {
    let cached = this._locks.get(geometry);
    if (!cached) {
      const count = geometry.attributes.position.count;
      const sources = new Float32Array(count * 2);
      const locks = new Float32Array(count);
      for (let i = 0; i < count; i++) {
        const [x, y] = toSource(i);
        sources[i * 2] = x;
        sources[i * 2 + 1] = y;
        locks[i] = this.rig.lockAt(x, y);
      }
      cached = { sources, locks };
      this._locks.set(geometry, cached);
    }
    return cached;
  }

  // Swap the shadow strip between the still (1 row) and animated (16 rows) density.
  shadowRows(rows) {
    if (this.shadowGeometry.parameters.heightSegments === rows) return;
    const geometry = shadowPlane(this.pose, rows);
    this.shadow.geometry = geometry;
    this.shadowGeometry.dispose();
    this.shadowGeometry = geometry;
  }

  // Idle animation frame at `time` seconds (null: back to the still drawing).
  animate(camera, time) {
    if (time === this.time) return;
    this.time = time;
    const [x1, y1, x2, y2] = this.pose.box;
    const width = x2 - x1;
    const height = y2 - y1;
    const base = this.basePositions;
    const positions = this.geometry.attributes.position;
    if (time === null) {
      positions.array.set(base);
    } else {
      const rig = this.rig;
      const { sources, locks } = this.vertices(this.geometry, (i) => [
        x1 + (base[i * 3] + 0.5) * width,
        y2 - (base[i * 3 + 1] + 0.5) * height,
      ]);
      for (let i = 0; i < positions.count; i++) {
        const [dx, dy] = rig.displace(sources[i * 2], sources[i * 2 + 1], time, locks[i]);
        positions.setXY(i, base[i * 3] + dx / width, base[i * 3 + 1] - dy / height);
      }
    }
    positions.needsUpdate = true;
    this.shadowRows(time === null ? SHADOW_ROWS_STILL : SHADOW_ROWS_ANIMATED);
    if (this.placement && camera) this.layoutShadow(camera);
  }

  // `camera` is the LAYOUT camera: its view offset (zoom/pan), if any, is ignored.
  fit(camera) {
    const mappingForCamera = groundMapping(camera);
    if (this.placement?.mapping === mappingForCamera) {
      return;
    }
    const placement = fitPlacement(this.pose, mappingForCamera);
    this.placement = placement;
    const { scale, x, y, threshold, mapping, cx, cy } = placement;
    const [x1, y1, x2, y2] = this.pose.box;
    this.pixelScale = scale;
    this.mesh.scale.set((x2 - x1) * scale, (y2 - y1) * scale, 1);
    this.group.position.set(x, y, 0);
    // While animating, animate() lays the shadow out right after this (once per frame).
    if (this.time == null) this.layoutShadow(camera);
  }

  // Each shadow vertex column lies on the camera ray through its screen pixel,
  // at the depth where that column's support baseline touches the podium top.
  // While animating, vertices follow the drawing's displacement; depths stay per column.
  layoutShadow(camera) {
    const { scale, threshold, mapping, cx, cy } = this.placement;
    const positions = this.shadowGeometry.attributes.position;
    const uv = this.shadowGeometry.attributes.uv;
    const projection = mapping.projection;
    const world = camera.matrixWorld.elements;
    const time = this.time ?? null;
    const animated = time !== null;
    const { sources, locks } = animated
      ? this.vertices(this.shadowGeometry, (i) => [uv.getX(i) * this.pose.w, (1 - uv.getY(i)) * this.pose.h])
      : { sources: null, locks: null };
    let dx = 0;
    let dy = 0;
    for (let i = 0; i < positions.count; i++) {
      const sourceX = animated ? sources[i * 2] : uv.getX(i) * this.pose.w;
      const sourceY = animated ? sources[i * 2 + 1] : (1 - uv.getY(i)) * this.pose.h;
      if (animated) [dx, dy] = this.rig.displace(sourceX, sourceY, time, locks[i]);
      const px = mapping.centerX + (sourceX + dx - cx) * scale;
      const py = mapping.centerY + (cy - sourceY - dy) * scale;
      const gy = mapping.centerY + (cy - baselineAt(this.pose, sourceX, threshold)) * scale;
      toGround(mapping, px, gy, this.ground).applyMatrix4(camera.matrixWorldInverse);
      const depth = -this.ground.z;
      // Un-project layout pixel (px, py) at view-space depth `depth`.
      const vx = (((px * 2) / mapping.width + projection[8]) * depth) / projection[0];
      const vy = (((py * 2) / mapping.height + projection[9]) * depth) / projection[5];
      positions.setXYZ(
        i,
        world[0] * vx + world[4] * vy - world[8] * depth + world[12],
        world[1] * vx + world[5] * vy - world[9] * depth + world[13],
        world[2] * vx + world[6] * vy - world[10] * depth + world[14],
      );
    }
    positions.needsUpdate = true;
  }

  face(camera) {
    this.fit(camera);
  }

  // Replace pose data (e.g. a new support mode) and re-place on the next fit().
  // The bbox must be unchanged; otherwise build a new Figure.
  setPose(pose) {
    forgetPose(this.pose);
    forgetPose(pose);
    this.pose = pose;
    this.placement = null;
    this._rig = null; // joints or supports may have changed
    const time = this.time;
    this.time = undefined; // force the next animate() to rebuild
    if (time === null) this.animate(null, null);
  }

  sourceToScreen(x, y, target = new THREE.Vector3()) {
    const p = this.placement;
    return target.set(p.mapping.centerX + (x - p.cx) * p.scale, p.mapping.centerY + (p.cy - y) * p.scale, 0);
  }

  // Drawing bbox in un-zoomed CSS px relative to the viewport's top-left corner.
  layoutRect() {
    const p = this.placement;
    if (!p) {
      return null;
    }
    const [x1, y1, x2, y2] = this.pose.box;
    const width = (x2 - x1) * p.scale;
    const height = (y2 - y1) * p.scale;
    return {
      left: p.mapping.width / 2 + p.x - width / 2,
      top: p.mapping.height / 2 - p.y - height / 2,
      width,
      height,
    };
  }

  dispose() {
    this.group.removeFromParent();
    this.shadow.removeFromParent();
    this.geometry.dispose();
    this.shadowGeometry.dispose();
    this.mesh.material.dispose();
    this.shadow.material.dispose();
    this.shadow.customDepthMaterial.dispose();
  }
}
