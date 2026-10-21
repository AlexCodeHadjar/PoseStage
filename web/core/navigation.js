// Pure index arithmetic for ←/→ traversal (no DOM, no Three.js; unit-tested).

function mod(value, count) {
  return ((value % count) + count) % count;
}

// Valid, de-duplicated traversal order, or null for "all poses in natural order".
export function normalizeOrder(indices, count) {
  if (indices === null || indices === undefined) {
    return null;
  }
  const seen = new Set();
  const order = [];
  for (const value of indices) {
    if (Number.isInteger(value) && value >= 0 && value < count && !seen.has(value)) {
      seen.add(value);
      order.push(value);
    }
  }
  return order;
}

// Index reached from `index` after `delta` steps (wrapping). With an `order`
// subset, steps walk that subset; if `index` is not in it, the first step lands
// on the nearest member following (delta > 0) or preceding (delta < 0) `index`.
// Returns `index` when there is nowhere to go (empty list/subset or delta 0).
export function stepTarget(index, delta, count, order = null) {
  if (!count || !delta) {
    return index;
  }
  if (!order) {
    return mod(index + delta, count);
  }
  if (!order.length) {
    return index;
  }
  const direction = Math.sign(delta);
  let steps = Math.abs(delta);
  let position = order.indexOf(index);
  if (position < 0) {
    let best = Infinity;
    order.forEach((value, k) => {
      const distance = direction > 0 ? mod(value - index, count) : mod(index - value, count);
      if (distance > 0 && distance < best) {
        best = distance;
        position = k;
      }
    });
    steps -= 1;
  }
  return order[mod(position + direction * steps, order.length)];
}

// First (edge < 0) or last (edge > 0) index of the traversal.
export function edgeTarget(edge, count, order = null) {
  if (order) {
    if (!order.length) {
      return -1;
    }
    return edge < 0 ? order[0] : order[order.length - 1];
  }
  if (!count) {
    return -1;
  }
  return edge < 0 ? 0 : count - 1;
}
