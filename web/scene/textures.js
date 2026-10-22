import * as THREE from 'three';
import { stepTarget } from '../core/navigation.js';

// At most seven resident OR pending textures, including active transition pins.
const MAX_ENTRIES = 7;

export class TextureCache {
  constructor(poses, renderer, report) {
    this.poses = poses;
    this.renderer = renderer;
    this.report = report;
    this.entries = new Map(); // pose index -> {texture, promise, last}
    this.pins = new Set(); // indices that must not be evicted (shown / transition target)
    this.failed = new Set();
    this.clock = 0;
    this.disposed = false;
    this.loader = new THREE.TextureLoader();
  }

  async get(index) {
    if (this.disposed) {
      throw new Error('Texture cache disposed');
    }
    if (this.failed.has(index)) {
      throw new Error('Texture previously failed');
    }
    let entry = this.entries.get(index);
    if (!entry) {
      this.trim(MAX_ENTRIES - 1);
      if (this.entries.size >= MAX_ENTRIES) {
        throw new Error('Texture cache is busy');
      }
      entry = { texture: null, last: ++this.clock };
      this.entries.set(index, entry);
      entry.promise = this.loader.loadAsync(this.poses[index].url).then((texture) => {
        texture.colorSpace = THREE.SRGBColorSpace;
        texture.anisotropy = this.renderer.capabilities.getMaxAnisotropy();
        texture.generateMipmaps = true;
        texture.minFilter = THREE.LinearMipmapLinearFilter;
        texture.magFilter = THREE.LinearFilter;
        if (this.entries.get(index) !== entry) {
          texture.dispose();
          throw new Error('Evicted preload');
        }
        entry.texture = texture;
        // Decode and upload to the GPU (with mipmaps) now, while the pose waits in the cache —
        // otherwise it happens in the first frame of the transition and the figure jerks.
        try {
          this.renderer.initTexture?.(texture);
        } catch (error) {
          this.report(`initTexture ${this.poses[index].file}: ${error}`);
        }
        return texture;
      }).catch((error) => {
        if (this.entries.get(index) === entry) {
          this.entries.delete(index);
          this.failed.add(index);
          this.report(`Не загрузилась поза ${this.poses[index].file}: ${error}`);
        }
        throw error;
      });
    }
    entry.last = ++this.clock;
    return entry.promise;
  }

  // Evict least-recently-used unpinned entries until at most `limit` remain.
  trim(limit = MAX_ENTRIES) {
    const candidates = [...this.entries]
      .filter(([i]) => !this.pins.has(i))
      .sort((a, b) => a[1].last - b[1].last);
    while (this.entries.size > limit && candidates.length) {
      const [i, entry] = candidates.shift();
      entry.texture?.dispose();
      this.entries.delete(i);
    }
  }

  // Keep the shown pose, the poses ±1/±2 steps away (in `order`, if given) and
  // pins. Outside the `order` subset, the steps reach the nearest subset members
  // in both directions and their neighbours.
  preload(index, order = null) {
    const ring = order && order.length ? order : null;
    const steps = [1, -1, 2, -2].map((d) => stepTarget(index, d, this.poses.length, ring));
    const wanted = new Set([index, ...steps, ...this.pins]);
    for (const [i, entry] of this.entries) {
      if (!wanted.has(i) && !this.pins.has(i)) {
        entry.texture?.dispose();
        this.entries.delete(i);
      }
    }
    for (const i of wanted) {
      if (!this.failed.has(i)) {
        this.get(i).catch(() => {});
      }
    }
    this.trim();
  }

  // Release every texture; pending loads dispose their result when they finish.
  dispose() {
    this.disposed = true;
    for (const entry of this.entries.values()) {
      entry.texture?.dispose();
    }
    this.entries.clear();
    this.pins.clear();
  }
}
