// Window chrome: idle-cursor hiding and the windowed-mode drag bar state.

const IDLE_MS = 2500;

export function createChrome() {
  let idleTimer = 0;

  // Any pointer/keyboard activity shows the cursor, arrows and title bar again.
  function wake() {
    document.body.classList.remove('idle');
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => document.body.classList.add('idle'), IDLE_MS);
  }

  // Fullscreen = the page covers the whole screen. Otherwise (normal or compact
  // window) body.windowed shows the thin `pywebview-drag-region` bar.
  function isFullscreen() {
    if (document.fullscreenElement) {
      return true;
    }
    return Math.abs(innerWidth - screen.width) <= 2 && Math.abs(innerHeight - screen.height) <= 2;
  }

  function updateWindowed() {
    document.body.classList.toggle('windowed', !isFullscreen());
  }

  return { wake, updateWindowed, isFullscreen };
}
