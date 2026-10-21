// Zoom/pan as a camera view offset: the visible sub-rectangle (x, y, width/zoom,
// height/zoom) of the full, un-zoomed layout viewport, in CSS px. Applying the
// same sub-rectangle to every camera keeps studio, shadow and drawing aligned,
// and the composer still renders at full canvas resolution.
export const MIN_ZOOM = 1;
export const MAX_ZOOM = 8;

export class ViewState {
  constructor(width = 1, height = 1) {
    this.width = width;
    this.height = height;
    this.zoom = 1;
    this.x = 0;
    this.y = 0;
  }

  resize(width, height) {
    // Keep the same relative region in view.
    this.x *= width / this.width;
    this.y *= height / this.height;
    this.width = width;
    this.height = height;
    this.clamp();
  }

  // Zoom keeping the layout point under screen point (centerX, centerY) fixed.
  setZoom(zoom, centerX = this.width / 2, centerY = this.height / 2) {
    if (!Number.isFinite(zoom)) {
      return false;
    }
    const next = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom));
    const cx = Number.isFinite(centerX) ? centerX : this.width / 2;
    const cy = Number.isFinite(centerY) ? centerY : this.height / 2;
    const layoutX = this.x + cx / this.zoom;
    const layoutY = this.y + cy / this.zoom;
    const before = [this.zoom, this.x, this.y];
    this.zoom = next;
    this.x = layoutX - cx / next;
    this.y = layoutY - cy / next;
    this.clamp();
    return before[0] !== this.zoom || before[1] !== this.x || before[2] !== this.y;
  }

  // Drag the image by (dx, dy) screen CSS px.
  pan(dx, dy) {
    if (!Number.isFinite(dx) || !Number.isFinite(dy)) {
      return false;
    }
    const before = [this.x, this.y];
    this.x -= dx / this.zoom;
    this.y -= dy / this.zoom;
    this.clamp();
    return before[0] !== this.x || before[1] !== this.y;
  }

  reset() {
    const changed = this.zoom !== 1 || this.x !== 0 || this.y !== 0;
    this.zoom = 1;
    this.x = 0;
    this.y = 0;
    return changed;
  }

  // The visible window never leaves the rendered image.
  clamp() {
    const maxX = this.width - this.width / this.zoom;
    const maxY = this.height - this.height / this.zoom;
    this.x = Math.min(maxX, Math.max(0, this.x));
    this.y = Math.min(maxY, Math.max(0, this.y));
  }

  apply(camera) {
    if (this.zoom === 1) {
      camera.clearViewOffset();
    } else {
      camera.setViewOffset(this.width, this.height, this.x, this.y, this.width / this.zoom, this.height / this.zoom);
    }
  }

  // Layout rect (un-zoomed CSS px) -> on-screen CSS px.
  toScreen(rect) {
    return {
      left: (rect.left - this.x) * this.zoom,
      top: (rect.top - this.y) * this.zoom,
      width: rect.width * this.zoom,
      height: rect.height * this.zoom,
    };
  }
}
