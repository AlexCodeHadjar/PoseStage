import * as THREE from 'three';
import { TOP, RADIUS } from './stage.js';

export const SUPPORT_RADIUS = RADIUS - 0.18;

const mappings = new WeakMap(); // camera -> last ground mapping
const supports = new WeakMap(); // pose object -> support contacts
const point = new THREE.Vector3();
const unzoomed = new THREE.Matrix4();

// Layout must never see the zoom/pan view offset: otherwise a zoomed view would
// re-fit the figure into the visible sub-rectangle. Returns the projection the
// camera would have without camera.view (identical matrix when no offset is set).
function layoutProjection(camera) {
  if (!camera.view || !camera.view.enabled) {
    return camera.projectionMatrix;
  }
  camera.view.enabled = false;
  camera.updateProjectionMatrix();
  unzoomed.copy(camera.projectionMatrix);
  camera.view.enabled = true;
  camera.updateProjectionMatrix();
  return unzoomed;
}

// Invert the projection of the actual horizontal podium top. Source drawings
// already contain perspective, so their support pixels have different depths.
//
// Screen coordinates used throughout placement are "layout pixels": origin at
// the viewport center, +y up, full un-zoomed viewport of camera.userData.viewport.
export function groundMapping(camera) {
  const [width, height] = camera.userData.viewport || [1920, 1080];
  const projection = layoutProjection(camera);
  const signature = [width, height, ...camera.matrixWorld.elements, ...projection.elements];
  const previous = mappings.get(camera);
  if (previous && signature.every((value, i) => value === previous.signature[i])) {
    return previous;
  }

  const viewProjection = new THREE.Matrix4().multiplyMatrices(projection, camera.matrixWorldInverse);
  const e = viewProjection.elements;
  // For points on the plane y = TOP the projection reduces to a 3x3 homography
  // of (x, z, 1); its inverse maps NDC back onto the podium top.
  const inverse = new THREE.Matrix3().set(
    e[0], e[8], e[4] * TOP + e[12],
    e[1], e[9], e[5] * TOP + e[13],
    e[3], e[11], e[7] * TOP + e[15],
  ).invert().elements;

  // Screen-space bounding box of the podium's top rim.
  let left = Infinity;
  let right = -Infinity;
  let bottom = Infinity;
  let top = -Infinity;
  for (let i = 0; i < 128; i++) {
    const a = (i * Math.PI) / 64;
    point.set(Math.cos(a) * RADIUS, TOP, Math.sin(a) * RADIUS);
    // Same operations as Vector3.project(camera), with the un-zoomed projection.
    point.applyMatrix4(camera.matrixWorldInverse).applyMatrix4(projection);
    left = Math.min(left, (point.x * width) / 2);
    right = Math.max(right, (point.x * width) / 2);
    bottom = Math.min(bottom, (point.y * height) / 2);
    top = Math.max(top, (point.y * height) / 2);
  }

  const mapping = {
    signature,
    width,
    height,
    inverse,
    projection: projection.elements.slice(),
    centerX: (left + right) / 2,
    centerY: (bottom + top) / 2,
    diameter: right - left,
  };
  mappings.set(camera, mapping);
  return mapping;
}

// Layout pixel (x, y) -> world point on the podium top.
export function toGround(mapping, x, y, target = new THREE.Vector3()) {
  const u = (x * 2) / mapping.width;
  const v = (y * 2) / mapping.height;
  const e = mapping.inverse;
  const w = e[2] * u + e[5] * v + e[8];
  return target.set((e[0] * u + e[3] * v + e[6]) / w, TOP, (e[1] * u + e[4] * v + e[7]) / w);
}

// Support contacts: profile points in the bottom part of the bbox (below `cut`),
// and the center of their bounding box, which is anchored to the podium center.
function supportData(pose) {
  if (supports.has(pose)) {
    return supports.get(pose);
  }
  const [x1, y1, x2, y2] = pose.box;
  const bh = y2 - y1;
  const threshold = y1 + (pose.support?.cut ?? 0.93) * bh;
  let contacts = (pose.support?.profile || [[(x1 + x2) / 2, y2]]).filter((p) => p[1] >= threshold);
  if (!contacts.length) {
    contacts = [[(x1 + x2) / 2, y2]];
  }
  const xs = contacts.map((p) => p[0]);
  const ys = contacts.map((p) => p[1]);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
  const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
  const data = { threshold, contacts, cx, cy };
  supports.set(pose, data);
  return data;
}

// Source points where the drawing rests on the podium (lower silhouette below the cut).
export function supportContacts(pose) {
  return supportData(pose).contacts;
}

// Drops cached support data of a pose object (use after mutating pose.support in place).
export function forgetPose(pose) {
  supports.delete(pose);
}

// A standing body (the pose's estimated stature) takes this share of the viewport height.
export const STATURE_SHARE = 0.61;

// Screen px per source px that gives every pose of a collection the same body size:
// pose.stature is the estimated standing height in source px (poses.py: reference·√area),
// pose.scale_adjust the user's correction ([ / ] keys). Without stature (old data) the
// bounding box stands in for the body.
export function bodyScale(pose, mapping) {
  const [, y1, , y2] = pose.box;
  const stature = pose.stature > 0 ? pose.stature : y2 - y1;
  const adjust = pose.scale_adjust > 0 ? pose.scale_adjust : 1;
  return ((STATURE_SHARE * mapping.height) / stature) * adjust;
}

// Pose sizing. "file" (default): every picture gets the same scale, exactly as drawn in its
// file, with only its support centred on the podium (no body estimate, no correction, no
// limits). "body": one estimated body size per collection, shrunk only to fit the podium.
// A standing figure fills about this share of its PNG's height in the bundled set, so it has
// the same size in both modes.
export const FILE_FIGURE_SHARE = 0.86;
let sizing = 'file';

export function setSizing(mode) {
  if (mode === 'body' || mode === 'file') sizing = mode;
  return sizing;
}

export function getSizing() {
  return sizing;
}

export function fitPlacement(pose, mapping) {
  const [x1, y1, x2, y2] = pose.box;
  const { threshold, contacts, cx, cy } = supportData(pose);

  if (sizing === 'file') {
    const scale = (STATURE_SHARE * mapping.height) / (FILE_FIGURE_SHARE * pose.h);
    const radius = Math.max(...contacts.map(([x, y]) => {
      const point = toGround(mapping, mapping.centerX + (x - cx) * scale, mapping.centerY + (cy - y) * scale);
      return Math.hypot(point.x, point.z);
    }));
    return {
      scale,
      desired: scale,
      limited: false,
      cx,
      cy,
      threshold,
      contacts,
      maxContactRadius: radius,
      mapping,
      x: mapping.centerX + ((x1 + x2) / 2 - cx) * scale,
      y: mapping.centerY + (cy - (y1 + y2) / 2) * scale,
    };
  }

  // The same body size for all poses; shrink a pose only if it cannot fit otherwise:
  // keep it inside 42% of the viewport width either side of the podium axis (arms may
  // reach past the podium, not past the arrows) and between 43% above / 44.5% below
  // the podium center line.
  const desired = bodyScale(pose, mapping);
  const halfWidth = mapping.width * 0.42;
  let high = Math.min(
    desired,
    (halfWidth + mapping.centerX) / Math.max(1e-9, cx - x1),
    (halfWidth - mapping.centerX) / Math.max(1e-9, x2 - cx),
    (mapping.height * 0.43 - mapping.centerY) / Math.max(1e-9, cy - y1),
    (mapping.centerY + mapping.height * 0.445) / Math.max(1e-9, y2 - cy),
  );

  const temp = new THREE.Vector3();
  // Largest distance from the podium axis of any contact at scale s.
  const radiusAt = (s) => {
    let max = 0;
    for (const [x, y] of contacts) {
      toGround(mapping, mapping.centerX + (x - cx) * s, mapping.centerY + (cy - y) * s, temp);
      max = Math.max(max, Math.hypot(temp.x, temp.z));
    }
    return max;
  };

  // Binary search for the largest scale whose contacts still stand on the podium.
  let lo = 0;
  if (radiusAt(high) <= SUPPORT_RADIUS) {
    lo = high;
  } else {
    for (let i = 0; i < 22; i++) {
      const mid = (lo + high) / 2;
      if (radiusAt(mid) <= SUPPORT_RADIUS) {
        lo = mid;
      } else {
        high = mid;
      }
    }
  }

  return {
    scale: lo,
    desired,
    // The pose had to be made smaller than the common body size (podium or screen edge).
    limited: lo < desired * 0.999,
    cx,
    cy,
    threshold,
    contacts,
    maxContactRadius: radiusAt(lo),
    mapping,
    x: mapping.centerX + ((x1 + x2) / 2 - cx) * lo,
    y: mapping.centerY + (cy - (y1 + y2) / 2) * lo,
  };
}

// Interpolated support profile height (source px) at source x, never above `threshold`.
export function baselineAt(pose, x, threshold) {
  const profile = pose.support?.profile;
  if (!profile?.length) {
    return pose.box[3];
  }
  let lo = 0;
  let hi = profile.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (profile[mid][0] < x) {
      lo = mid;
    } else {
      hi = mid;
    }
  }
  const t = THREE.MathUtils.clamp((x - profile[lo][0]) / Math.max(0.001, profile[hi][0] - profile[lo][0]), 0, 1);
  return Math.max(threshold, THREE.MathUtils.lerp(profile[lo][1], profile[hi][1], t));
}
