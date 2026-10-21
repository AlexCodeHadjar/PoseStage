// Idle animation ("breathing" loop) for flat pose drawings. A displacement field in source
// pixels (y down) built from the pose's skeleton: the upper body sways around the hips, the
// chest rises along the torso, the head tilts at the neck and free hands swing at the elbows.
// Supports (feet, knees, hands on the podium) stay pinned. desktop.py IdleRig is a port.
import { supportContacts } from './placement.js';

export const IDLE_LOOP = 6; // seconds; every motion's period divides it, so the loop is seamless
const SWAY = (1.25 * Math.PI) / 180;
const HEAD = (2.4 * Math.PI) / 180;
const HAND = (2.2 * Math.PI) / 180;
const LIFT = 0.028; // chest rise, share of the torso length

const smooth = (edge0, edge1, x) => {
  const t = Math.min(1, Math.max(0, (x - edge0) / (edge1 - edge0)));
  return t * t * (3 - 2 * t);
};
const mid = ([a, b]) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];

// A skeleton from the box alone, for poses without detected or corrected joints.
export function estimateJoints(pose) {
  const [x1, y1, x2, y2] = pose.box;
  const h = y2 - y1;
  const cx = (x1 + x2) / 2;
  const [nose, shoulder, hip] = {
    feet: [0.07, 0.2, 0.5],
    kneel: [0.09, 0.26, 0.62],
    ground: [0.12, 0.32, 0.75],
  }[pose.support?.mode] || [0.07, 0.2, 0.5];
  const side = (y, half) => [[cx + half, y1 + y * h], [cx - half, y1 + y * h]];
  const shoulders = side(shoulder, 0.11 * h);
  return { nose: [cx, y1 + nose * h], shoulders, elbows: shoulders, wrists: shoulders, hips: side(hip, 0.07 * h) };
}

export function createRig(pose) {
  const joints = pose.joints || estimateJoints(pose);
  const shoulders = mid(joints.shoulders);
  const hips = mid(joints.hips);
  const nose = joints.nose;
  const torso = Math.max(1, Math.hypot(shoulders[0] - hips[0], shoulders[1] - hips[1]));
  const axis = [(shoulders[0] - hips[0]) / torso, (shoulders[1] - hips[1]) / torso];
  const neck = [shoulders[0] + (nose[0] - shoulders[0]) * 0.35, shoulders[1] + (nose[1] - shoulders[1]) * 0.35];
  const headLength = Math.hypot(nose[0] - shoulders[0], nose[1] - shoulders[1]);
  const headRadius = Math.max(headLength * 1.25, torso * 0.45);
  const along = (x, y) => ((x - hips[0]) * axis[0] + (y - hips[1]) * axis[1]) / torso;
  const neckAlong = along(...neck);
  // Hands swing only when they hang free, away from the torso axis.
  const width = Math.max(1, Math.hypot(joints.shoulders[0][0] - joints.shoulders[1][0], joints.shoulders[0][1] - joints.shoulders[1][1]));
  const hands = [0, 1]
    .map((i) => [joints.elbows[i], joints.wrists[i]])
    .filter(([elbow, wrist]) => {
      const reach = Math.hypot(wrist[0] - elbow[0], wrist[1] - elbow[1]);
      const fromAxis = Math.abs((wrist[0] - hips[0]) * axis[1] - (wrist[1] - hips[1]) * axis[0]);
      return reach > width * 0.25 && fromAxis > width * 0.9;
    });
  const contacts = supportContacts(pose);
  const pin = 0.06 * (pose.box[3] - pose.box[1]);

  const rotate = (x, y, cx, cy, angle) => {
    const c = Math.cos(angle);
    const s = Math.sin(angle);
    const dx = x - cx;
    const dy = y - cy;
    return [cx + c * dx - s * dy, cy + s * dx + c * dy];
  };

  return {
    joints,
    // 1 where the drawing rests on the podium (never moves), 0 elsewhere.
    lockAt(x, y) {
      let nearest = Infinity;
      for (const [cx, cy] of contacts) nearest = Math.min(nearest, Math.hypot(x - cx, y - cy));
      return 1 - smooth(pin * 0.5, pin * 1.6, nearest);
    },
    // Displacement [dx, dy] (source px) of source point (x, y) at time t (seconds).
    displace(x, y, t, lock = this.lockAt(x, y)) {
      if (lock >= 1) return [0, 0];
      const w = (2 * Math.PI) / IDLE_LOOP;
      const breath = Math.sin(2 * w * t);
      const sway = Math.sin(w * t);
      const nod = Math.sin(w * t + 1.3) + 0.35 * Math.sin(3 * w * t + 0.4);
      const s = along(x, y);
      let nx = x;
      let ny = y;
      const weight = smooth(-0.12, 0.25, s);
      let [rx, ry] = rotate(nx, ny, hips[0], hips[1], SWAY * sway);
      nx += (rx - nx) * weight;
      ny += (ry - ny) * weight;
      const lift = LIFT * torso * (breath * 0.5 + 0.5) * smooth(0.15, 0.85, s);
      nx += axis[0] * lift;
      ny += axis[1] * lift;
      const distance = Math.hypot(x - nose[0], y - nose[1]);
      const head = smooth(-0.05, 0.35, (s - neckAlong) / 0.5) * (1 - smooth(headRadius * 0.8, headRadius * 1.4, distance));
      if (head > 0) {
        [rx, ry] = rotate(nx, ny, neck[0], neck[1], HEAD * nod);
        nx += (rx - nx) * head;
        ny += (ry - ny) * head;
      }
      for (const [elbow, wrist] of hands) {
        const reach = Math.hypot(wrist[0] - elbow[0], wrist[1] - elbow[1]);
        const hand = 1 - smooth(reach * 0.5, reach * 1.1, Math.hypot(x - wrist[0], y - wrist[1]));
        if (hand <= 0) continue;
        [rx, ry] = rotate(nx, ny, elbow[0], elbow[1], HAND * Math.sin(2 * w * t + elbow[0] * 0.01));
        nx += (rx - nx) * hand;
        ny += (ry - ny) * hand;
      }
      const free = 1 - lock;
      return [(nx - x) * free, (ny - y) * free];
    },
  };
}
