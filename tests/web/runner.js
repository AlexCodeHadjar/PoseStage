// Tiny browser test runner: test(name, fn) registers, run() executes in order and
// writes "PASS n / FAIL n" plus details into the page (readable as page text).
const tests = [];

export function test(name, fn) {
  tests.push({ name, fn });
}

export class AssertionError extends Error {}

export function assert(condition, message = 'assertion failed') {
  if (!condition) {
    throw new AssertionError(message);
  }
}

export function equal(actual, expected, message = '') {
  if (!Object.is(actual, expected)) {
    throw new AssertionError(`${message} expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

export function deepEqual(actual, expected, message = '') {
  const a = JSON.stringify(actual);
  const b = JSON.stringify(expected);
  if (a !== b) {
    throw new AssertionError(`${message} expected ${b.slice(0, 300)}, got ${a.slice(0, 300)}`);
  }
}

export function near(actual, expected, tolerance, message = '') {
  const scale = Math.max(1, Math.abs(expected));
  if (!(Math.abs(actual - expected) <= tolerance * scale)) {
    throw new AssertionError(`${message} expected ${expected} ±${tolerance}, got ${actual}`);
  }
}

export async function waitFor(predicate, timeout = 5000, message = 'condition') {
  const start = performance.now();
  while (performance.now() - start < timeout) {
    if (predicate()) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new AssertionError(`timed out waiting for ${message}`);
}

export async function run() {
  const summary = document.querySelector('#summary');
  const log = document.querySelector('#log');
  const lines = [];
  let pass = 0;
  let fail = 0;
  for (const { name, fn } of tests) {
    try {
      await fn();
      pass++;
      lines.push(`PASS  ${name}`);
    } catch (error) {
      fail++;
      lines.push(`FAIL  ${name}\n      ${error?.stack || error}`);
    }
    log.textContent = lines.join('\n');
  }
  summary.textContent = `PASS ${pass} / FAIL ${fail}`;
  summary.className = fail ? 'fail' : 'pass';
  window.__testResults = { pass, fail, lines };
  return { pass, fail };
}
