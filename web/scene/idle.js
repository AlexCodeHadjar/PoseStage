// Idle animation ("breathing" loop) for flat pose drawings, driven by the pose skeleton
// version 2 (docs/SKELETON.md): a displacement field in source pixels (y down).
//   breathing   the chest rises along the torso (pelvis → chest); in side view also forward
//   weight      everything above the pelvis turns around it (less when sitting / side, none lying)
//   head        tilts around the neck (35 % of the way from the chest to the head)
//   hands       a visible, free, unsupported wrist swings around its elbow
// Supports never move: the lower contour's contacts plus points flagged "support", minus
// contour contacts next to a point explicitly flagged support: false.
// desktop.py IdleRig is a line-by-line port (tests check both against the same values).
import { supportContacts } from './placement.js';

export const IDLE_LOOP = 6; // seconds; every motion's period divides it, so the loop is seamless
const SWAY = (1.25 * Math.PI) / 180;
const HEAD = (2.4 * Math.PI) / 180;
const HAND = (2.2 * Math.PI) / 180;
const LIFT = 0.028; // chest rise, share of the torso length

export const JOINT_AXIS = ['head', 'chest', 'navel', 'pelvis'];
export const JOINT_LIMBS = ['shoulder', 'elbow', 'wrist', 'hip', 'knee', 'foot'].flatMap((name) => [`${name}_l`, `${name}_r`]);
export const JOINT_IDS = [...JOINT_AXIS, ...JOINT_LIMBS];
// Each point's parent (the editor drags descendants along).
export const JOINT_PARENT = {
  chest: 'head', navel: 'chest', pelvis: 'navel',
  shoulder_l: 'chest', shoulder_r: 'chest', elbow_l: 'shoulder_l', elbow_r: 'shoulder_r',
  wrist_l: 'elbow_l', wrist_r: 'elbow_r', hip_l: 'pelvis', hip_r: 'pelvis',
  knee_l: 'hip_l', knee_r: 'hip_r', foot_l: 'knee_l', foot_r: 'knee_r',
};

const smooth = (edge0, edge1, x) => {
  const t = Math.min(1, Math.max(0, (x - edge0) / (edge1 - edge0)));
  return t * t * (3 - 2 * t);
};

// Template skeletons in box coordinates [u, v] (0..1), the figure's left first. Unlisted
// state = visible. "front": the figure's left is on the picture's right.
const TEMPLATES = {
  front: {
    head: [0.5, 0.07], chest: [0.5, 0.24], navel: [0.5, 0.4], pelvis: [0.5, 0.5],
    shoulder: [[0.64, 0.2], [0.36, 0.2]], elbow: [[0.7, 0.35], [0.3, 0.35]], wrist: [[0.72, 0.48], [0.28, 0.48]],
    hip: [[0.57, 0.52], [0.43, 0.52]], knee: [[0.57, 0.74], [0.43, 0.74]], foot: [[0.57, 0.96], [0.43, 0.96]],
  },
  side: {
    head: [0.5, 0.07], chest: [0.53, 0.24], navel: [0.51, 0.4], pelvis: [0.48, 0.5],
    shoulder: [[0.5, 0.2], [0.47, 0.2]], elbow: [[0.5, 0.36], [0.47, 0.36]], wrist: [[0.5, 0.5], [0.47, 0.5]],
    hip: [[0.49, 0.52], [0.47, 0.52]], knee: [[0.5, 0.74], [0.47, 0.74]], foot: [[0.5, 0.96], [0.47, 0.96]],
    hidden: ['shoulder_r', 'elbow_r', 'wrist_r', 'hip_r', 'knee_r', 'foot_r'],
  },
  sit: {
    head: [0.5, 0.1], chest: [0.5, 0.3], navel: [0.5, 0.45], pelvis: [0.5, 0.6],
    shoulder: [[0.6, 0.27], [0.4, 0.27]], elbow: [[0.68, 0.42], [0.32, 0.42]], wrist: [[0.72, 0.6], [0.28, 0.6]],
    hip: [[0.56, 0.62], [0.44, 0.62]], knee: [[0.75, 0.75], [0.25, 0.75]], foot: [[0.85, 0.95], [0.15, 0.95]],
    support: ['pelvis'],
  },
  lie: {
    head: [0.08, 0.5], chest: [0.25, 0.5], navel: [0.4, 0.52], pelvis: [0.52, 0.55],
    shoulder: [[0.23, 0.42], [0.23, 0.58]], elbow: [[0.35, 0.4], [0.35, 0.6]], wrist: [[0.45, 0.4], [0.45, 0.6]],
    hip: [[0.53, 0.5], [0.53, 0.6]], knee: [[0.72, 0.5], [0.72, 0.6]], foot: [[0.92, 0.55], [0.92, 0.62]],
    hidden: ['shoulder_r', 'elbow_r', 'wrist_r', 'hip_r', 'knee_r', 'foot_r'],
    support: ['pelvis'],
  },
};
TEMPLATES.back = Object.fromEntries(Object.entries(TEMPLATES.front).map(([key, value]) => [
  key, Array.isArray(value[0]) ? [value[1], value[0]] : value, // the figure's left is now on the left
]));
const TEMPLATE_VIEW = { front: 'front', side: 'side', back: 'back', sit: 'front', lie: 'side' };

// A skeleton of the given kind ("front" | "side" | "back" | "sit" | "lie") fitted to the box.
export function templateJoints(pose, kind) {
  const template = TEMPLATES[kind] || TEMPLATES.front;
  const [x1, y1, x2, y2] = pose.box;
  const at = ([u, v]) => [Math.round((x1 + u * (x2 - x1)) * 10) / 10, Math.round((y1 + v * (y2 - y1)) * 10) / 10];
  const points = {};
  for (const name of JOINT_AXIS) points[name] = { xy: at(template[name]), state: 'visible' };
  for (const name of ['shoulder', 'elbow', 'wrist', 'hip', 'knee', 'foot']) {
    ['l', 'r'].forEach((side, i) => {
      const id = `${name}_${side}`;
      points[id] = { xy: at(template[name][i]), state: template.hidden?.includes(id) ? 'hidden' : 'visible' };
    });
  }
  for (const id of template.support || []) points[id].support = true;
  return { version: 2, view: TEMPLATE_VIEW[kind] || 'front', points };
}

// The skeleton to use when a pose has none: a template chosen from the support mode / box.
export function estimateJoints(pose) {
  const [x1, y1, x2, y2] = pose.box;
  const mode = pose.support?.mode;
  if ((x2 - x1) / Math.max(1, y2 - y1) > 1.4) return templateJoints(pose, 'lie');
  return templateJoints(pose, mode === 'kneel' || mode === 'ground' ? 'sit' : 'front');
}

export function createRig(pose) {
  const joints = pose.joints?.version === 2 ? pose.joints : estimateJoints(pose);
  const points = joints.points;
  const xy = (id) => points[id]?.xy;
  const visible = (id) => points[id]?.state === 'visible' && !!points[id].xy;
  const head = xy('head');
  const chest = xy('chest');
  const pelvis = xy('pelvis');
  const torso = Math.max(1, Math.hypot(chest[0] - pelvis[0], chest[1] - pelvis[1]));
  const axis = [(chest[0] - pelvis[0]) / torso, (chest[1] - pelvis[1]) / torso];
  const lying = Math.abs(axis[1]) < 0.5;
  const sitting = pose.support?.mode === 'kneel' || pose.support?.mode === 'ground';
  const view = joints.view || 'front';
  const sway = lying ? 0 : SWAY * (view === 'side' ? 0.6 : 1) * (sitting ? 0.8 : 1);
  // Breathing direction: along the torso; lying, the chest rises up the picture; in side view
  // it also moves forward, toward the side of the head → pelvis line the chest is on.
  let lift = lying ? [0, -1] : [...axis];
  if (view === 'side' && !lying) {
    // Forward = from the pelvis → head line toward the chest (the chest sits on the front).
    const lineLength = Math.max(1, Math.hypot(head[0] - pelvis[0], head[1] - pelvis[1]));
    const line = [(head[0] - pelvis[0]) / lineLength, (head[1] - pelvis[1]) / lineLength];
    const offset = [chest[0] - pelvis[0], chest[1] - pelvis[1]];
    const along_ = offset[0] * line[0] + offset[1] * line[1];
    const perp = [offset[0] - line[0] * along_, offset[1] - line[1] * along_];
    const size = Math.hypot(perp[0], perp[1]);
    const forward = size > 1e-3 ? [perp[0] / size, perp[1] / size] : [-axis[1], axis[0]];
    const raw = [axis[0] + forward[0] * 0.5, axis[1] + forward[1] * 0.5];
    const norm = Math.hypot(raw[0], raw[1]);
    lift = [raw[0] / norm, raw[1] / norm]; // as strong as in front view, just tilted forward
  }
  const neck = [chest[0] + (head[0] - chest[0]) * 0.35, chest[1] + (head[1] - chest[1]) * 0.35];
  const headRadius = Math.max(Math.hypot(head[0] - chest[0], head[1] - chest[1]) * 1.25, torso * 0.45);
  const along = (x, y) => ((x - pelvis[0]) * axis[0] + (y - pelvis[1]) * axis[1]) / torso;
  const neckAlong = along(...neck);
  const shoulderWidth = visible('shoulder_l') && visible('shoulder_r')
    ? Math.max(1, Math.hypot(xy('shoulder_l')[0] - xy('shoulder_r')[0], xy('shoulder_l')[1] - xy('shoulder_r')[1]))
    : torso * 0.6;
  // Hands swing only when visible, hanging free (away from the torso axis) and not a support.
  const hands = ['l', 'r']
    .filter((side) => visible(`elbow_${side}`) && visible(`wrist_${side}`) && points[`wrist_${side}`].support !== true)
    .map((side) => [xy(`elbow_${side}`), xy(`wrist_${side}`)])
    .filter(([elbow, wrist]) => {
      const reach = Math.hypot(wrist[0] - elbow[0], wrist[1] - elbow[1]);
      const fromAxis = Math.abs((wrist[0] - pelvis[0]) * axis[1] - (wrist[1] - pelvis[1]) * axis[0]);
      return reach > shoulderWidth * 0.25 && fromAxis > shoulderWidth * 0.9;
    });
  const pin = 0.06 * (pose.box[3] - pose.box[1]);
  // Pins: contour contacts not next to a point marked "not a support", plus marked supports.
  const released = JOINT_IDS.filter((id) => points[id]?.support === false && points[id].xy).map(xy);
  const pins = supportContacts(pose).filter(([cx, cy]) => released.every(([rx, ry]) => Math.hypot(cx - rx, cy - ry) > pin * 1.5));
  for (const id of JOINT_IDS) if (points[id]?.support === true && points[id].xy) pins.push(xy(id));

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
      for (const [cx, cy] of pins) nearest = Math.min(nearest, Math.hypot(x - cx, y - cy));
      return 1 - smooth(pin * 0.5, pin * 1.6, nearest);
    },
    // Displacement [dx, dy] (source px) of source point (x, y) at time t (seconds).
    displace(x, y, t, lock = this.lockAt(x, y)) {
      if (lock >= 1) return [0, 0];
      const w = (2 * Math.PI) / IDLE_LOOP;
      const breath = Math.sin(2 * w * t);
      const swing = Math.sin(w * t);
      const nod = Math.sin(w * t + 1.3) + 0.35 * Math.sin(3 * w * t + 0.4);
      const s = along(x, y);
      let nx = x;
      let ny = y;
      let rx;
      let ry;
      if (sway) {
        const weight = smooth(-0.12, 0.25, s);
        [rx, ry] = rotate(nx, ny, pelvis[0], pelvis[1], sway * swing);
        nx += (rx - nx) * weight;
        ny += (ry - ny) * weight;
      }
      const rise = LIFT * torso * (breath * 0.5 + 0.5) * smooth(0.15, 0.85, s);
      nx += lift[0] * rise;
      ny += lift[1] * rise;
      const distance = Math.hypot(x - head[0], y - head[1]);
      const headWeight = smooth(-0.05, 0.35, (s - neckAlong) / 0.5) * (1 - smooth(headRadius * 0.8, headRadius * 1.4, distance));
      if (headWeight > 0) {
        [rx, ry] = rotate(nx, ny, neck[0], neck[1], HEAD * nod);
        nx += (rx - nx) * headWeight;
        ny += (ry - ny) * headWeight;
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
