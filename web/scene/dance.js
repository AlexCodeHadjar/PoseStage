// Bone-driven transitions between the poses of a dance phrase (docs/DANCE.md § 5).
// dance_motion.py is the reference: in_between, groove, the skin weights and the forward map
// are line-by-line ports (tests check both against the same values).
//
// The in-between skeleton comes from forward kinematics from the pelvis: every bone turns from
// its angle in pose A to its angle in pose B (the short way) on its own timing — the torso
// leads, the limbs follow — and bone lengths blend. A drawing follows a skeleton by linear
// blend skinning: each point moves with its three nearest bones. Here the drawing is a mesh,
// so the map runs forward (rest vertex → posed vertex) and needs no inverse.

export const TREE = [
  ['navel', 'pelvis'], ['chest', 'navel'], ['head', 'chest'],
  ['shoulder_l', 'chest'], ['shoulder_r', 'chest'], ['elbow_l', 'shoulder_l'], ['elbow_r', 'shoulder_r'],
  ['wrist_l', 'elbow_l'], ['wrist_r', 'elbow_r'],
  ['hip_l', 'pelvis'], ['hip_r', 'pelvis'], ['knee_l', 'hip_l'], ['knee_r', 'hip_r'],
  ['foot_l', 'knee_l'], ['foot_r', 'knee_r'],
];
// Share of the move a bone waits before it starts.
const DELAY = {
  navel: 0, chest: 0.02, head: 0.1, shoulder_l: 0.05, shoulder_r: 0.05, elbow_l: 0.12, elbow_r: 0.12,
  wrist_l: 0.2, wrist_r: 0.2, hip_l: 0, hip_r: 0, knee_l: 0.04, knee_r: 0.04, foot_l: 0.08, foot_r: 0.08,
};
const EXTEND = { head: 0.7, wrist_l: 0.35, wrist_r: 0.35, foot_l: 0.2, foot_r: 0.2 }; // skull, hands, feet
// Progress curves: keys are turning points, smoothstep between them.
export const CURVES = {
  soft: [[0, 0], [1, 1]],
  hit: [[0, 0], [0.16, -0.07], [0.55, 1.09], [0.74, 0.97], [0.88, 1.01], [1, 1]], // wind-up, snap, overshoot
  fast: [[0, 0], [0.12, -0.04], [0.6, 1.06], [0.82, 0.985], [1, 1]],
};
const GROOVE = 0.02; // bounce depth, share of the figure's height
const SWAY = 0.012; // side sway over two beats
// Per style: (share of each count spent moving, curve). Matches dance_motion.dance_frames.
export const STYLES = { soft: { share: 0.78, curve: 'soft' }, groove: { share: 0.6, curve: 'hit' } };

export function smooth(a, b, x) {
  const t = Math.min(1, Math.max(0, (x - a) / (b - a)));
  return t * t * (3 - 2 * t);
}

export function curve(name) {
  const keys = CURVES[name];
  return (x) => {
    x = Math.min(1, Math.max(0, x));
    for (let i = 1; i < keys.length; i++) {
      const [x0, y0] = keys[i - 1];
      const [x1, y1] = keys[i];
      if (x <= x1) return y0 + (y1 - y0) * smooth(x0, x1, x);
    }
    return keys[keys.length - 1][1];
  };
}

// Python's (a + π) % 2π − π (the result takes the divisor's sign).
const wrap = (a) => {
  const r = (a + Math.PI) % (2 * Math.PI);
  return (r < 0 ? r + 2 * Math.PI : r) - Math.PI;
};

// Fill a joint missing in one pose from the other pose's bone, relative to its parent.
export function complete(joints, other) {
  const out = { ...joints };
  for (const [child, parent] of TREE) {
    if (!(child in out) && parent in out && child in other && parent in other) {
      out[child] = [out[parent][0] + other[child][0] - other[parent][0], out[parent][1] + other[child][1] - other[parent][1]];
    }
  }
  return out;
}

// Skeleton at progress u (0..1): the pelvis moves, every bone rotates on its own timing.
export function inBetween(ja, jb, u, ease) {
  const pa = ja.pelvis;
  const pb = jb.pelvis;
  const k = ease(u);
  const out = { pelvis: [pa[0] + (pb[0] - pa[0]) * k, pa[1] + (pb[1] - pa[1]) * k] };
  for (const [child, parent] of TREE) {
    if (!(child in ja) || !(child in jb) || !(parent in out)) continue;
    const va = [ja[child][0] - ja[parent][0], ja[child][1] - ja[parent][1]];
    const vb = [jb[child][0] - jb[parent][0], jb[child][1] - jb[parent][1]];
    const t = ease((u - DELAY[child]) / (1 - DELAY[child]));
    const angleA = Math.atan2(va[1], va[0]);
    const angle = angleA + wrap(Math.atan2(vb[1], vb[0]) - angleA) * t;
    const length = Math.hypot(...va) + (Math.hypot(...vb) - Math.hypot(...va)) * t;
    out[child] = [out[parent][0] + Math.cos(angle) * length, out[parent][1] + Math.sin(angle) * length];
  }
  return out;
}

// Bounce down on every beat (the head a little later), knees open, the chest sways over two.
export function groove(joints, time, beat, height) {
  const dip = (delay) => {
    let phase = (time / beat - delay) % 1;
    if (phase < 0) phase += 1;
    return GROOVE * height * ((1 + Math.cos(2 * Math.PI * phase)) / 2) ** 2;
  };
  const body = dip(0);
  const head = dip(0.07) * 1.2;
  const sway = SWAY * height * Math.sin((Math.PI * time) / beat);
  const pelvisX = joints.pelvis[0];
  const out = {};
  for (const [key, [x, y]] of Object.entries(joints)) {
    if (key.startsWith('foot')) out[key] = [x, y];
    else if (key.startsWith('knee')) out[key] = [x + (x - pelvisX >= 0 ? 0.35 : -0.35) * body, y + 0.5 * body];
    else if (key === 'pelvis' || key === 'hip_l' || key === 'hip_r') out[key] = [x, y + body];
    else if (key === 'navel') out[key] = [x + 0.5 * sway, y + body];
    else if (key === 'head') out[key] = [x + 1.2 * sway, y + head];
    else out[key] = [x + sway, y + body]; // chest, shoulders, arms
  }
  return out;
}

// Linear blend skinning of one drawing: points follow their three nearest bones.
export class Skin {
  constructor(joints, torso) {
    this.bones = [];
    for (const [child, parent] of TREE) {
      if (!(child in joints) || !(parent in joints)) continue;
      const a = joints[parent];
      const b = joints[child];
      const ext = EXTEND[child] || 0;
      this.bones.push([child, [a[0], a[1]], [b[0] + (b[0] - a[0]) * ext, b[1] + (b[1] - a[1]) * ext]]);
    }
    this.soft = 0.08 * torso;
  }

  weights(x, y) {
    const out = [];
    for (const [child, a, b] of this.bones) {
      const dx = b[0] - a[0];
      const dy = b[1] - a[1];
      const t = Math.max(0, Math.min(1, ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy || 1)));
      out.push([1 / (Math.hypot(x - a[0] - t * dx, y - a[1] - t * dy) + this.soft) ** 4, child]);
    }
    out.sort((p, q) => q[0] - p[0] || (q[1] > p[1] ? 1 : q[1] < p[1] ? -1 : 0));
    const top = out.slice(0, 3);
    const total = top.reduce((sum, [w]) => sum + w, 0);
    return top.map(([w, child]) => [w / total, child]);
  }
}

// Per bone: [cos·scale, sin·scale, rest start, posed start].
export function transforms(rest, pose) {
  const out = {};
  for (const [child, parent] of TREE) {
    if (!(child in rest) || !(parent in rest) || !(child in pose) || !(parent in pose)) continue;
    const ra = [rest[child][0] - rest[parent][0], rest[child][1] - rest[parent][1]];
    const pa = [pose[child][0] - pose[parent][0], pose[child][1] - pose[parent][1]];
    const rotation = wrap(Math.atan2(pa[1], pa[0]) - Math.atan2(ra[1], ra[0]));
    const scale = Math.max(0.7, Math.min(1.4, (Math.hypot(...pa) || 1) / (Math.hypot(...ra) || 1)));
    out[child] = [Math.cos(rotation) * scale, Math.sin(rotation) * scale, rest[parent], pose[parent]];
  }
  return out;
}

// Rest point → posed point.
export function forward(weights, moves, x, y) {
  let nx = 0;
  let ny = 0;
  for (const [w, child] of weights) {
    const move = moves[child];
    if (!move) {
      nx += w * x;
      ny += w * y;
      continue;
    }
    const [c, s, r0, p0] = move;
    const dx = x - r0[0];
    const dy = y - r0[1];
    nx += w * (p0[0] + c * dx - s * dy);
    ny += w * (p0[1] + s * dx + c * dy);
  }
  return [nx, ny];
}

// Skeleton points {id: [x, y]} of a pose (source px); absent points are left out.
export function jointPoints(pose) {
  const points = pose.joints?.version === 2 ? pose.joints.points : null;
  const out = {};
  for (const [id, point] of Object.entries(points || {})) {
    if (point?.xy && point.state !== 'absent') out[id] = [point.xy[0], point.xy[1]];
  }
  return out;
}

// A Figure rig (same interface as idle.js createRig) that bends the drawing from its own
// skeleton `rest` to `setTarget(skeleton)` (both in the drawing's source px). Nothing is pinned:
// in a dance the feet move too.
export function createDanceRig(rest) {
  const torso = Math.max(1, Math.hypot(rest.chest[0] - rest.pelvis[0], rest.chest[1] - rest.pelvis[1]));
  const skin = new Skin(rest, torso);
  const cache = new Map(); // mesh vertices are fixed: weights once per vertex
  let moves = {};
  return {
    rest,
    setTarget(target) {
      moves = transforms(rest, target);
    },
    lockAt() {
      return 0;
    },
    displace(x, y) {
      const key = `${x},${y}`;
      let weights = cache.get(key);
      if (!weights) {
        weights = skin.weights(x, y);
        cache.set(key, weights);
      }
      const [nx, ny] = forward(weights, moves, x, y);
      return [nx - x, ny - y];
    },
  };
}

// The pose flipped left-right: box, support profile and skeleton (sides swapped), as
// dance.mirror_pose does on the backend. The texture is flipped by Figure({mirror: true}).
export function mirrorPose(pose) {
  const w = pose.w;
  const [x1, y1, x2, y2] = pose.box;
  const swap = (id) => (id.endsWith('_l') ? `${id.slice(0, -2)}_r` : id.endsWith('_r') ? `${id.slice(0, -2)}_l` : id);
  const out = { ...pose, box: [w - x2, y1, w - x1, y2] };
  if (Array.isArray(pose.support?.profile)) {
    out.support = { ...pose.support, profile: pose.support.profile.map(([x, y]) => [w - x, y]).sort((a, b) => a[0] - b[0] || a[1] - b[1]) };
  }
  if (pose.joints?.points) {
    out.joints = {
      ...pose.joints,
      points: Object.fromEntries(Object.entries(pose.joints.points).map(([id, point]) => [
        swap(id), point?.xy ? { ...point, xy: [w - point.xy[0], point.xy[1]] } : { ...point },
      ])),
    };
  }
  return out;
}

// Order of the poses in one cycle: a loop goes round; an open phrase dances there and back.
export function cycle(count, loop) {
  const there = [...Array(count).keys()];
  if (count < 2) return there;
  return loop ? [...there, 0] : [...there, ...there.slice(0, -1).reverse()];
}
