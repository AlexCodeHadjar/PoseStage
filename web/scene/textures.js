import * as THREE from 'three';

export class TextureCache {
  constructor(poses,renderer,report) {
    this.poses=poses;this.renderer=renderer;this.report=report;this.entries=new Map();this.pins=new Set();this.failed=new Set();this.clock=0;
    this.loader=new THREE.TextureLoader();
  }
  async get(index) {
    if(this.failed.has(index))throw new Error('Texture previously failed');
    let entry=this.entries.get(index);
    if(!entry) {
      // At most seven resident OR pending textures, including active transition pins.
      this.trim(6);
      if(this.entries.size>=7)throw new Error('Texture cache is busy');
      entry={texture:null,last:++this.clock};this.entries.set(index,entry);
      entry.promise=this.loader.loadAsync(this.poses[index].url).then(texture=>{
        texture.colorSpace=THREE.SRGBColorSpace;texture.anisotropy=this.renderer.capabilities.getMaxAnisotropy();
        texture.generateMipmaps=true;texture.minFilter=THREE.LinearMipmapLinearFilter;texture.magFilter=THREE.LinearFilter;
        if(this.entries.get(index)!==entry){texture.dispose();throw new Error('Evicted preload');}
        entry.texture=texture;return texture;
      }).catch(error=>{
        if(this.entries.get(index)===entry){this.entries.delete(index);this.failed.add(index);this.report(`Не загрузилась поза ${this.poses[index].file}: ${error}`);}
        throw error;
      });
    }
    entry.last=++this.clock;return entry.promise;
  }
  trim(limit=7) {
    const entries=[...this.entries].filter(([i])=>!this.pins.has(i)).sort((a,b)=>a[1].last-b[1].last);
    while(this.entries.size>limit && entries.length){const [i,e]=entries.shift();e.texture?.dispose();this.entries.delete(i);}
  }
  preload(index) {
    const n=this.poses.length, wanted=new Set([index,...[1,-1,2,-2].map(d=>(index+d+n)%n),...this.pins]);
    for(const [i,e] of this.entries)if(!wanted.has(i)&&!this.pins.has(i)){e.texture?.dispose();this.entries.delete(i);}
    for(const i of wanted)if(!this.failed.has(i))this.get(i).catch(()=>{});
    this.trim();
  }
}
